"""HTTP API. Local-only by default; every /api route except /api/health needs the workspace token."""

from __future__ import annotations

import os
import platform
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from jobfinder import __version__
from jobfinder.config import PRESETS, default_connections_text, parse_connections
from jobfinder.core import App
from jobfinder.models import (
    SCHEMA_EXPORTS,
    Company,
    CompanyLink,
    ContactLink,
    Criteria,
    Profile,
    RoleLink,
    RunRequest,
    SkillSet,
    WorkplanSubmission,
)
from jobfinder.services import discovery, excel, websearch
from jobfinder.services import profiles as profile_svc
from jobfinder.services import workplan as wp_svc
from jobfinder.services.skills import catalog, occupations

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
ENV_KEYS = ["OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_SEARCH_MODEL", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
            "GEMINI_API_KEY", "GEMINI_MODEL", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "PERPLEXITY_API_KEY",
            "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT", "OLLAMA_MODEL",
            "OLLAMA_BASE_URL", "LOCAL_GEMMA_MODEL", "LOCAL_QWEN_CODER_MODEL", "LOCAL_QWEN_FLASH_MODEL",
            "LMSTUDIO_MODEL", "LMSTUDIO_BASE_URL",
            "TAVILY_API_KEY", "BRAVE_API_KEY", "SERPAPI_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY",
            "RAPIDAPI_KEY", "JOOBLE_API_KEY"]
SECRET_HINT = ("KEY", "TOKEN", "SECRET")


def create_app(app_ctx: App | None = None, *, watch_config: bool = True) -> FastAPI:
    state: dict[str, Any] = {"app": app_ctx}

    @asynccontextmanager
    async def lifespan(_: FastAPI):  # type: ignore[no-untyped-def]
        if state["app"] is None:
            state["app"] = App(watch_config=watch_config)
        a: App = state["app"]
        a.store.fail_stale_runs()
        a.store.cache_prune()
        yield
        a.close()

    api = FastAPI(title="JobFinder API", version=__version__, lifespan=lifespan,
                  description="Profiles, criteria, skills, companies, roles, contacts and the AI workplan loop.")

    def A() -> App:  # noqa: N802
        if state["app"] is None:
            state["app"] = App(watch_config=watch_config)
        return state["app"]

    # ------------------------------------------------------------------ security
    @api.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        host = (request.headers.get("host") or "").split(":")[0].lower()
        allowed_hosts = {"127.0.0.1", "localhost", "testserver", "::1", "[::1]"}
        extra = os.environ.get("JOBFINDER_ALLOWED_HOSTS", "")
        allowed_hosts |= {h.strip().lower() for h in extra.split(",") if h.strip()}
        if host not in allowed_hosts:
            return JSONResponse({"detail": "host not allowed"}, status_code=403)
        path = request.url.path
        if path.startswith("/api/") and path != "/api/health":
            token = A().ws.api_token()
            got = request.headers.get("x-jobfinder-token") or ""
            auth = request.headers.get("authorization") or ""
            if auth.lower().startswith("bearer "):
                got = got or auth[7:]
            if not got and request.method == "GET":
                got = request.query_params.get("token", "")
            if not secrets.compare_digest(got, token):
                return JSONResponse({"detail": "missing or invalid token (see workspace state/api_token)"}, status_code=401)
        return await call_next(request)

    @api.exception_handler(KeyError)
    async def not_found(_: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": f"not found: {exc.args[0] if exc.args else ''}"}, status_code=404)

    @api.exception_handler(ValueError)
    async def bad_value(_: Request, exc: ValueError) -> JSONResponse:
        if isinstance(exc, ValidationError):
            return JSONResponse({"detail": exc.errors(include_url=False)}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=400)

    # ------------------------------------------------------------------ ui
    @api.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> HTMLResponse:
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__JF_TOKEN__", A().ws.api_token()).replace("__JF_VERSION__", __version__),
                            headers={"Cache-Control": "no-store"})

    api.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    # ------------------------------------------------------------------ meta
    @api.get("/api/health")
    def health() -> dict[str, Any]:
        a = A()
        return {"status": "ok", "app": "jobfinder", "version": __version__, "schema_version": a.store.schema_version(),
                "config_error": a.config.last_error}

    @api.get("/api/schemas")
    def schemas() -> dict[str, Any]:
        return {"schemas": sorted(SCHEMA_EXPORTS)}

    @api.get("/api/schemas/{name}")
    def schema(name: str) -> dict[str, Any]:
        if name not in SCHEMA_EXPORTS:
            raise KeyError(name)
        return SCHEMA_EXPORTS[name].model_json_schema(by_alias=True)

    @api.get("/api/catalog/skills")
    def skills_catalog(q: str | None = None) -> list[dict[str, Any]]:
        from jobfinder.models import fold

        out = [{"id": s.id, "name": s.name, "category": s.category} for s in catalog().by_id.values()]
        if q:
            out = [s for s in out if fold(q) in fold(s["name"]) or fold(q) in s["id"]]
        return out

    @api.get("/api/catalog/occupations")
    def occupations_catalog() -> list[dict[str, Any]]:
        return [{"key": o.key, "codes": o.codes, "titles": o.titles, "skills": o.skills} for o in occupations()]

    # ------------------------------------------------------------------ profiles
    @api.get("/api/profiles")
    def list_profiles() -> list[dict[str, Any]]:
        a = A()
        return [{**p.model_dump(mode="json", exclude_none=True), "stats": a.store.stats(p.id or "")}
                for p in a.store.list_profiles()]

    @api.post("/api/profiles", status_code=201)
    def create_profile(p: Profile) -> Profile:
        if p.id and A().store.get_profile(p.id):
            raise HTTPException(409, f"profile {p.id} exists")
        return A().store.save_profile(p)

    @api.get("/api/profiles/{pid}")
    def get_profile(pid: str) -> Profile:
        p = A().store.get_profile(pid)
        if not p:
            raise KeyError(pid)
        return p

    @api.put("/api/profiles/{pid}")
    def update_profile(pid: str, p: Profile) -> Profile:
        if not A().store.get_profile(pid):
            raise KeyError(pid)
        p.id = pid
        return A().store.save_profile(p)

    @api.delete("/api/profiles/{pid}")
    def delete_profile(pid: str) -> dict[str, Any]:
        if not A().store.delete_profile(pid):
            raise KeyError(pid)
        return {"deleted": pid}

    @api.get("/api/profiles/{pid}/stats")
    def profile_stats(pid: str) -> dict[str, Any]:
        _need_profile(A(), pid)
        return A().store.stats(pid)

    @api.get("/api/profiles/{pid}/bundle")
    def profile_bundle(pid: str) -> dict[str, Any]:
        return profile_svc.export_bundle(A(), pid)

    @api.post("/api/profiles/import", status_code=201)
    def profile_import(bundle: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if bundle.get("format") != profile_svc.BUNDLE_FORMAT or "profile" not in bundle:
            raise HTTPException(400, f"not a {profile_svc.BUNDLE_FORMAT} file")
        return profile_svc.import_bundle(A(), bundle)

    # ------------------------------------------------------------------ criteria
    @api.get("/api/profiles/{pid}/criteria")
    def list_criteria(pid: str) -> list[Criteria]:
        _need_profile(A(), pid)
        return A().store.list_criteria(pid)

    @api.post("/api/profiles/{pid}/criteria", status_code=201)
    def create_criteria(pid: str, cr: Criteria) -> Criteria:
        _need_profile(A(), pid)
        cr.profile_id = pid
        if cr.id and A().store.get_criteria(cr.id):
            raise HTTPException(409, f"criteria {cr.id} exists")
        return A().store.save_criteria(cr)

    @api.post("/api/criteria/validate")
    def validate_criteria(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            cr = Criteria.model_validate(body)
        except ValidationError as e:
            return {"valid": False, "errors": e.errors(include_url=False, include_context=False)}
        return {"valid": True, "criteria": cr.model_dump(mode="json", exclude_none=True),
                "role_queries": [q.model_dump(exclude_none=True) for q in discovery.role_queries(cr)],
                "company_queries": [q for q, _ in discovery.company_queries(cr)]}

    @api.get("/api/criteria/{cid}")
    def get_criteria(cid: str) -> Criteria:
        cr = A().store.get_criteria(cid)
        if not cr:
            raise KeyError(cid)
        return cr

    @api.put("/api/criteria/{cid}")
    def update_criteria(cid: str, cr: Criteria, actor: str = Query("user", pattern="^(user|ai)$")) -> Criteria:
        existing = A().store.get_criteria(cid)
        if not existing:
            raise KeyError(cid)
        cr.id, cr.profile_id = cid, existing.profile_id
        if cr.search_strategy.version < existing.search_strategy.version:
            cr.search_strategy = existing.search_strategy  # editor loaded an older strategy; don't revert the AI's newer one
        if actor == "ai":
            changed = profile_svc.guard_user_fields(existing, cr)
            if changed:
                raise HTTPException(403, f"AI may only change search_strategy; tried to change {changed}")
        return A().store.save_criteria(cr)

    @api.patch("/api/criteria/{cid}/strategy")
    def patch_strategy(cid: str, patch: dict[str, Any] = Body(...), actor: str = Query("ai")) -> Criteria:
        return profile_svc.update_strategy(A(), cid, patch, actor)

    @api.get("/api/criteria/{cid}/strategy/history")
    def strategy_history(cid: str) -> list[dict[str, Any]]:
        return A().store.strategy_history(cid)

    @api.delete("/api/criteria/{cid}")
    def delete_criteria(cid: str) -> dict[str, Any]:
        if not A().store.delete_criteria(cid):
            raise KeyError(cid)
        return {"deleted": cid}

    # ------------------------------------------------------------------ skills
    @api.get("/api/profiles/{pid}/skills")
    def get_skills(pid: str) -> SkillSet:
        _need_profile(A(), pid)
        return A().store.get_skillset(pid)

    @api.put("/api/profiles/{pid}/skills")
    def put_skills(pid: str, s: SkillSet) -> SkillSet:
        _need_profile(A(), pid)
        s.profile_id = pid
        cat = catalog()
        for sk in s.skills:
            if not sk.id or sk.id == sk.name:
                sk.id = cat.resolve(sk.name)
        return A().store.save_skillset(s)

    @api.post("/api/profiles/{pid}/skills/upload")
    async def upload_skills(pid: str, file: UploadFile = File(...), mode: str = Form("merge"),
                            use_llm: bool = Form(True)) -> dict[str, Any]:
        _need_profile(A(), pid)
        data = await file.read()
        if len(data) > 15_000_000:
            raise HTTPException(413, "file too large (max 15 MB)")
        return profile_svc.ingest_skills_file(A(), pid, file.filename or "upload.txt", data, mode=mode, use_llm=use_llm)

    @api.get("/api/profiles/{pid}/resumes")
    def list_resumes(pid: str) -> list[dict[str, Any]]:
        return A().store.list_resumes(pid)

    # ------------------------------------------------------------------ pipeline: companies / roles / contacts
    @api.get("/api/profiles/{pid}/companies")
    def list_companies(pid: str, include_excluded: bool = True) -> list[dict[str, Any]]:
        _need_profile(A(), pid)
        return [{**c.model_dump(mode="json", exclude_none=True), "pipeline": link.model_dump(mode="json", exclude_none=True)}
                for c, link in A().store.pipeline_companies(pid, include_excluded)]

    @api.post("/api/profiles/{pid}/companies", status_code=201)
    def add_company(pid: str, comp: Company, criteria_id: str | None = None) -> dict[str, Any]:
        a = A()
        _need_profile(a, pid)
        saved, _ = a.store.upsert_company(comp)
        link, _ = a.store.link_company(pid, saved.id, {"criteria_ids": [criteria_id] if criteria_id else []})  # type: ignore[arg-type]
        return {**saved.model_dump(mode="json", exclude_none=True), "pipeline": link.model_dump(mode="json")}

    @api.patch("/api/profiles/{pid}/companies/{cid}")
    def patch_company_link(pid: str, cid: str, patch: CompanyLink) -> CompanyLink:
        if A().store.get_company_link(pid, cid) is None:
            raise KeyError(cid)
        return A().store.link_company(pid, cid, patch.model_dump(exclude_unset=True))[0]

    @api.delete("/api/profiles/{pid}/companies/{cid}")
    def unlink_company(pid: str, cid: str) -> dict[str, Any]:
        return {"removed": A().store.unlink(pid, "company", cid)}

    @api.get("/api/profiles/{pid}/roles")
    def list_roles(pid: str, company_id: str | None = None, min_score: float | None = None,
                   passed: bool | None = None, include_closed: bool = False) -> list[dict[str, Any]]:
        _need_profile(A(), pid)
        out = []
        for r, link in A().store.pipeline_roles(pid, company_id):
            if min_score is not None and (link.score or 0) < min_score:
                continue
            if passed is not None and bool(link.match and link.match.passed) != passed:
                continue
            if not include_closed and (link.status == "closed" or r.status == "closed"):
                continue
            d = r.model_dump(mode="json", exclude_none=True, exclude={"description"})
            d["pipeline"] = link.model_dump(mode="json", exclude_none=True)
            out.append(d)
        return out

    @api.patch("/api/profiles/{pid}/roles/{rid}")
    def patch_role_link(pid: str, rid: str, patch: RoleLink) -> RoleLink:
        if A().store.get_role_link(pid, rid) is None:
            raise KeyError(rid)
        return A().store.link_role(pid, rid, patch.model_dump(exclude_unset=True))[0]

    @api.delete("/api/profiles/{pid}/roles/{rid}")
    def unlink_role(pid: str, rid: str) -> dict[str, Any]:
        return {"removed": A().store.unlink(pid, "role", rid)}

    @api.get("/api/profiles/{pid}/roles/{rid}/brief")
    def role_brief(pid: str, rid: str) -> dict[str, Any]:
        return resume_brief(A(), pid, rid)

    @api.get("/api/profiles/{pid}/contacts")
    def list_contacts(pid: str, role_id: str | None = None) -> list[dict[str, Any]]:
        _need_profile(A(), pid)
        return [{**c.model_dump(mode="json", exclude_none=True), "pipeline": link.model_dump(mode="json", exclude_none=True)}
                for c, link in A().store.pipeline_contacts(pid, role_id)]

    @api.patch("/api/profiles/{pid}/contacts/{cid}")
    def patch_contact_link(pid: str, cid: str, patch: ContactLink) -> ContactLink:
        if A().store.get_contact(cid) is None:
            raise KeyError(cid)
        return A().store.link_contact(pid, cid, patch.role_id, patch.model_dump(exclude_unset=True))[0]

    @api.delete("/api/profiles/{pid}/contacts/{cid}")
    def unlink_contact(pid: str, cid: str) -> dict[str, Any]:
        return {"removed": A().store.unlink(pid, "contact", cid)}

    @api.get("/api/companies/{cid}")
    def get_company(cid: str) -> Company:
        c = A().store.get_company(cid)
        if not c:
            raise KeyError(cid)
        return c

    @api.get("/api/roles/{rid}")
    def get_role(rid: str) -> dict[str, Any]:
        r = A().store.get_role(rid)
        if not r:
            raise KeyError(rid)
        return r.model_dump(mode="json", exclude_none=True)

    # ------------------------------------------------------------------ workplan
    @api.get("/api/profiles/{pid}/workplan")
    def get_workplan(pid: str, include_excluded: bool = False, include_closed: bool = False,
                     company_id: list[str] | None = Query(None), full: bool = False,
                     max_roles_per_company: int = 50) -> dict[str, Any]:
        wp = wp_svc.build(A(), pid, include_excluded=include_excluded, include_closed=include_closed, company_ids=company_id,
                          max_roles_per_company=max_roles_per_company, max_description=None if full else 800)
        return wp.model_dump(mode="json", by_alias=True, exclude_none=True)

    @api.post("/api/profiles/{pid}/workplan")
    @api.put("/api/profiles/{pid}/workplan")
    def submit_workplan(pid: str, sub: WorkplanSubmission) -> dict[str, Any]:
        return wp_svc.merge(A(), pid, sub).model_dump()

    # ------------------------------------------------------------------ guided mode (small AIs, copy-paste, humans)
    @api.get("/api/guided/next")
    def guided_next(profile_id: str | None = None) -> dict[str, Any]:
        from jobfinder.services.guided import Guide

        return Guide(A()).next(profile_id).model_dump()

    @api.post("/api/guided/{task_id}/answer")
    def guided_answer(task_id: str, answer: str = Body(..., embed=True)) -> dict[str, Any]:
        from jobfinder.services.guided import Guide

        return Guide(A()).answer(task_id, answer)

    # ------------------------------------------------------------------ runs
    @api.post("/api/profiles/{pid}/runs", status_code=202)
    def start_run(pid: str, req: RunRequest) -> dict[str, Any]:
        return A().runner.submit(pid, req).model_dump(mode="json")

    @api.get("/api/runs")
    def list_runs(profile_id: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        return [r.model_dump(mode="json", exclude={"log"}) for r in A().store.list_runs(profile_id, limit)]

    @api.get("/api/runs/{rid}")
    def get_run(rid: str) -> dict[str, Any]:
        r = A().store.get_run(rid)
        if not r:
            raise KeyError(rid)
        return r.model_dump(mode="json")

    @api.post("/api/runs/{rid}/cancel")
    def cancel_run(rid: str) -> dict[str, Any]:
        return {"cancelled": A().runner.cancel(rid)}

    @api.get("/api/runs-resumable")
    def resumable_runs(profile_id: str | None = None) -> list[dict[str, Any]]:
        return [wp_svc.run_summary(r) for r in A().runner.resumable(profile_id)]

    @api.post("/api/runs/{rid}/resume", status_code=202)
    def resume_run(rid: str) -> dict[str, Any]:
        return A().runner.resume(rid).model_dump(mode="json", exclude={"log"})

    @api.post("/api/runs/{rid}/dismiss")
    def dismiss_run(rid: str) -> dict[str, Any]:
        return {"dismissed": A().runner.dismiss(rid)}

    # ------------------------------------------------------------------ excel
    @api.get("/api/export/excel")
    def export_excel(profile_id: list[str] | None = Query(None)) -> Response:
        data = excel.export_bytes(A(), profile_id)
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": 'attachment; filename="JobFinder.xlsx"'})

    @api.post("/api/export/excel/save")
    def save_excel(profile_id: list[str] | None = Body(None, embed=True)) -> dict[str, Any]:
        return {"path": str(excel.export(A(), profile_id))}

    @api.post("/api/import/excel")
    async def import_excel(file: UploadFile = File(...)) -> dict[str, Any]:
        return excel.import_workbook(A(), await file.read())

    # ------------------------------------------------------------------ tools for agents
    @api.post("/api/tools/web-search")
    def tool_web_search(query: str = Body(..., embed=True), n: int = Body(10, embed=True),
                        country: str | None = Body(None, embed=True)) -> list[dict[str, Any]]:
        a = A()
        http = a.http()
        try:
            return [h.dict() for h in websearch.web_search(a.conn, http, query, n, country)]
        finally:
            http.close()

    @api.post("/api/tools/fetch")
    def tool_fetch(url: str = Body(..., embed=True), max_chars: int = Body(20000, embed=True)) -> dict[str, Any]:
        return fetch_page(A(), url, max_chars)

    # ------------------------------------------------------------------ settings
    @api.get("/api/admin/connections")
    def connections() -> dict[str, Any]:
        a = A()
        from jobfinder.llm import is_configured

        conn = a.conn
        return {
            "file": str(a.ws.connections_file), "env_file": str(a.ws.env_file),
            "loaded_at": a.config.loaded_at, "error": a.config.last_error,
            "models": {n: {"provider": m.provider, "model": m.model, "configured": is_configured(m)}
                       for n, m in conn.llm.models.items()},
            "routes": conn.llm.routes,
            "web_search": websearch.available(conn),
            "sources": {n: {"enabled": bool(conn.source(n)),
                            "configured": discovery.SOURCES[n].configured(conn.sources[n].model_dump()) if n in conn.sources and n in discovery.SOURCES else False,
                            "keyless": discovery.SOURCES[n].keyless if n in discovery.SOURCES else None}
                        for n in discovery.SOURCES},
            "presets": list(PRESETS),
            "config": a.config.redacted(),
        }

    @api.get("/api/admin/connections/raw")
    def connections_raw() -> dict[str, Any]:
        f = A().ws.connections_file
        return {"path": str(f), "text": f.read_text(encoding="utf-8") if f.exists() else ""}

    @api.put("/api/admin/connections/raw")
    def put_connections_raw(text: str = Body(..., embed=True)) -> dict[str, Any]:
        a = A()
        try:
            parse_connections(text, a.config.env)
        except (ValidationError, ValueError, yaml.YAMLError) as e:
            raise HTTPException(422, f"invalid connections.yaml: {e}") from e
        a.ws.connections_file.write_text(text, encoding="utf-8")
        a.config.reload()
        return {"saved": True, "error": a.config.last_error}

    @api.post("/api/admin/preset")
    def apply_preset(preset: str = Body(..., embed=True)) -> dict[str, Any]:
        """Applies a steps.yaml preset (which models do which step). Old connections presets still accepted."""
        from jobfinder import steps as steps_mod

        a = A()
        if preset in steps_mod.PRESETS:
            return save_steps_text(a, steps_mod.render(_steps_models_raw(a), steps_mod.preset_steps(preset), preset))
        if preset not in PRESETS:
            raise HTTPException(400, f"unknown preset; choose {list(steps_mod.PRESETS)}")
        backup = a.ws.connections_file.with_suffix(".yaml.bak")
        if a.ws.connections_file.exists():
            backup.write_text(a.ws.connections_file.read_text(encoding="utf-8"), encoding="utf-8")
        a.ws.connections_file.write_text(default_connections_text(preset), encoding="utf-8")
        a.config.reload()
        return {"preset": preset, "backup": str(backup)}

    # ------------------------------------------------------------------ steps.yaml (model per step)
    @api.get("/api/admin/steps")
    def steps_info() -> dict[str, Any]:
        from jobfinder import steps as steps_mod
        from jobfinder.llm import is_configured

        a = A()
        llm_cfg = a.conn.llm
        steps_out = {}
        for step in steps_mod.STEPS:
            opt = llm_cfg.steps.get(step)
            chain = llm_cfg.route(step)
            ready = [n for n, _ in a.llm.models_for(step)]
            steps_out[step] = {**steps_mod.STEP_INFO[step], "models": chain, "ready": ready,
                               "active": ready[0] if ready else None, "enabled": opt.enabled if opt else True,
                               "temperature": opt.temperature if opt else None,
                               "max_tokens": opt.max_tokens if opt else None}
        models = {n: {"provider": m.provider, "model": m.model, "ready": is_configured(m), "web_search": m.web_search,
                      "local": m.provider in ("ollama", "lmstudio")} for n, m in llm_cfg.models.items()}
        f = a.ws.steps_file
        return {"file": str(f), "text": f.read_text(encoding="utf-8") if f.exists() else "",
                "preset": a.config.steps_preset, "presets": steps_mod.PRESETS, "steps": steps_out, "models": models,
                "detected_local": detect_local_models(a), "error": a.config.last_error}

    @api.put("/api/admin/steps/raw")
    def put_steps_raw(text: str = Body(..., embed=True)) -> dict[str, Any]:
        return save_steps_text(A(), text)

    @api.put("/api/admin/steps")
    def put_steps(steps: dict[str, dict[str, Any]] = Body(..., embed=True),
                  add_models: dict[str, dict[str, Any]] | None = Body(None, embed=True)) -> dict[str, Any]:
        """Structured edit from the dashboard: per-step model lists/options, optionally new local models."""
        from jobfinder import steps as steps_mod

        a = A()
        models = _steps_models_raw(a)
        for name, spec in (add_models or {}).items():
            if not re.fullmatch(r"[a-z0-9_]{1,40}", name):
                raise HTTPException(400, f"model name '{name}' must be lowercase letters, digits or _")
            models[name] = {k: v for k, v in spec.items() if k in ("provider", "model", "api_base", "api_key",
                                                                    "timeout_s", "temperature", "max_tokens", "web_search")}
        cur = _steps_raw(a)
        merged = {**cur, **{k: {kk: vv for kk, vv in v.items() if vv is not None and vv != ""} for k, v in steps.items()}}
        return save_steps_text(a, steps_mod.render(models, merged, None))

    @api.post("/api/admin/steps/test")
    def test_step(step: str = Body(..., embed=True)) -> dict[str, Any]:
        import time as _time

        a = A()
        t0 = _time.monotonic()
        try:
            if step == "web_search":
                hits = websearch._ai_search(a.conn, "data engineer jobs Monterrey", 3, "MX")
                return {"ok": True, "ms": int((_time.monotonic() - t0) * 1000), "answer": [h.url for h in hits]}
            text, model = a.llm.chat(step, [{"role": "user", "content": "Reply with exactly: OK"}])
            return {"ok": True, "model": model, "ms": int((_time.monotonic() - t0) * 1000), "answer": str(text)[:200]}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}

    @api.get("/api/admin/env")
    def env_status() -> list[dict[str, Any]]:
        env = A().config.env
        out = []
        for k in ENV_KEYS:
            v = env.get(k) or ""
            secret = any(s in k for s in SECRET_HINT)
            out.append({"key": k, "set": bool(v), "value": (v[:3] + "…" if v else "") if secret else v, "secret": secret})
        return out

    @api.put("/api/admin/env")
    def env_update(values: dict[str, str] = Body(...)) -> dict[str, Any]:
        bad = [k for k in values if k not in ENV_KEYS]
        if bad:
            raise HTTPException(400, f"unknown keys {bad}")
        A().config.set_env_values({k: v.strip() for k, v in values.items()})
        return {"updated": sorted(values)}

    @api.post("/api/admin/connections/test")
    def test_connections() -> dict[str, Any]:
        a = A()
        models = [a.llm.ping(n) for n in a.conn.llm.models]
        http = a.http()
        search: dict[str, Any] = {}
        try:
            for name in websearch.available(a.conn):
                try:
                    hits = websearch.PROVIDERS[name](http, a.conn.search_provider(name)["api_key"], "jobs", 1, None)  # type: ignore[index]
                    search[name] = {"ok": True, "results": len(hits)}
                except Exception as e:
                    search[name] = {"ok": False, "error": str(e)[:200]}
            sources: dict[str, Any] = {}
            try:
                r = http.get("https://boards-api.greenhouse.io/v1/boards/gitlab", use_cache=False)
                sources["greenhouse"] = {"ok": r.status_code == 200}
            except Exception as e:
                sources["greenhouse"] = {"ok": False, "error": str(e)[:200]}
        finally:
            http.close()
        return {"models": models, "search": search, "sources": sources}

    @api.get("/api/admin/system")
    def system_info() -> dict[str, Any]:
        a = A()
        return {"os": platform.platform(), "python": platform.python_version(), "ram_gb": ram_gb(),
                "ollama": ollama_status(a), "workspace": str(a.ws.root), "db": str(a.ws.db_path),
                "exports": str(a.exports_dir()), "version": __version__,
                "local_model_advice": local_model_advice(ram_gb())}

    return api


def _need_profile(a: App, pid: str) -> None:
    if a.store.get_profile(pid) is None:
        raise KeyError(pid)


def _steps_yaml(a: App) -> dict[str, Any]:
    f = a.ws.steps_file
    raw = yaml.safe_load(f.read_text(encoding="utf-8")) if f.exists() else {}
    return raw if isinstance(raw, dict) else {}


def _steps_models_raw(a: App) -> dict[str, dict[str, Any]]:
    """Models from steps.yaml with ${ENV} placeholders kept (never write expanded secrets back)."""
    from jobfinder import steps as steps_mod

    models = _steps_yaml(a).get("models")
    return dict(models) if isinstance(models, dict) else dict(steps_mod.DEFAULT_LOCAL_MODELS)


def _steps_raw(a: App) -> dict[str, dict[str, Any]]:
    steps = _steps_yaml(a).get("steps")
    return {k: dict(v or {}) for k, v in steps.items()} if isinstance(steps, dict) else {}


def save_steps_text(a: App, text: str) -> dict[str, Any]:
    """Validate against the current connections + .env before writing; keep a .bak of the previous file."""
    from jobfinder.config import apply_steps, parse_steps

    try:
        conn_text = a.ws.connections_file.read_text(encoding="utf-8") if a.ws.connections_file.exists() else ""
        apply_steps(parse_connections(conn_text, a.config.env), parse_steps(text, a.config.env))
    except (ValidationError, ValueError, yaml.YAMLError) as e:
        raise HTTPException(422, f"invalid steps.yaml: {e}") from e
    f = a.ws.steps_file
    if f.exists():
        f.with_suffix(".yaml.bak").write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    f.write_text(text, encoding="utf-8")
    a.config.reload()
    return {"saved": True, "error": a.config.last_error, "path": str(f)}


def detect_local_models(a: App) -> dict[str, list[str]]:
    """Models currently served by Ollama / LM Studio, to offer one-click wiring in the dashboard."""
    from jobfinder.llm.router import LOCAL_DEFAULT_BASE, _local_models

    out: dict[str, list[str]] = {}
    for provider, env_key in (("ollama", "OLLAMA_BASE_URL"), ("lmstudio", "LMSTUDIO_BASE_URL")):
        base = a.config.env.get(env_key) or LOCAL_DEFAULT_BASE[provider]
        served = _local_models(base)
        out[provider] = sorted(served) if served else []
    return out


def resume_brief(a: App, pid: str, rid: str) -> dict[str, Any]:
    """Everything an external resume tool needs to tailor a CV to one role."""
    link = a.store.get_role_link(pid, rid)
    role = a.store.get_role(rid)
    if link is None or role is None:
        raise KeyError(rid)
    comp = a.store.get_company(role.company_id) if role.company_id else None
    skills = a.store.get_skillset(pid)
    m = link.match
    have = {s.name: s.model_dump(exclude_none=True, include={"level", "years", "evidence"}) for s in skills.skills}
    return {
        "profile": a.store.get_profile(pid).model_dump(mode="json", exclude_none=True,  # type: ignore[union-attr]
                                                       include={"id", "name", "headline", "home", "languages", "links"}),
        "candidate_summary": skills.summary,
        "role": {"id": role.id, "title": role.title, "company": comp.name if comp else None, "url": role.url,
                 "location": role.location.model_dump(exclude_none=True), "work_mode": role.work_mode,
                 "seniority": role.seniority, "salary": role.salary.model_dump(exclude_none=True) if role.salary else None,
                 "description": role.description},
        "match": m.model_dump() if m else None,
        "ai_review": link.review.model_dump(mode="json", exclude_none=True) if link.review else None,
        "posting_access": role.fetch.model_dump(mode="json", exclude_none=True) if role.fetch else None,
        "matched_skills_detail": {k: v for k, v in have.items() if m and k in m.matched_skills},
        "gaps": list(dict.fromkeys([*(m.missing_skills if m else []), *(link.review.gaps if link.review else [])])),
        "keywords_to_mirror": m.keywords if m else [],
        "instructions": "Tailor the resume to this role: lead with matched skills, mirror keywords truthfully, "
                        "address gaps with adjacent experience. Never claim skills the candidate does not have.",
    }


def fetch_page(a: App, url: str, max_chars: int = 20000) -> dict[str, Any]:
    from bs4 import BeautifulSoup

    from jobfinder.services.http import RobotsDenied
    from jobfinder.sources.careers import _iter_jsonld, detect_ats

    http = a.http()
    try:
        r = http.get(url, check_robots=True)
    except RobotsDenied:
        return {"url": url, "status": None, "error": "disallowed by robots.txt"}
    finally:
        http.close()
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "noscript", "svg"]):
        if t.name == "script" and "ld+json" in (t.get("type") or ""):
            continue
        t.decompose()
    jobs = _iter_jsonld(soup)
    text = " ".join(soup.get_text(" ").split())
    ats = detect_ats(r.text)
    return {"url": str(r.url), "status": r.status_code, "title": soup.title.string if soup.title else None,
            "text": text[:max_chars], "job_postings": jobs[:20], "ats": ats.model_dump() if ats else None}


def ram_gb() -> float | None:
    try:
        if platform.system() == "Windows":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]

            ms = MS()
            ms.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))  # type: ignore[attr-defined]
            return round(ms.ullTotalPhys / 1024**3, 1)
        if platform.system() == "Darwin":
            import subprocess

            return round(int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip()) / 1024**3, 1)
        with open("/proc/meminfo") as f:
            return round(int(f.readline().split()[1]) / 1024**2, 1)
    except Exception:
        return None


def local_model_advice(ram: float | None) -> str:
    if ram is None:
        return "Unknown RAM: start with a cloud model."
    if ram < 12:
        return "Under 12 GB RAM: use a cloud model (OpenAI / DeepSeek). Local models will be slow."
    if ram < 24:
        return "12-24 GB RAM: a 7-8B local model works (ollama pull qwen2.5:7b). Use cloud for company discovery."
    return "24 GB+ RAM: a 14B local model works well (ollama pull qwen2.5:14b)."


def ollama_status(a: App) -> dict[str, Any]:
    import httpx

    base = (a.config.env.get("OLLAMA_BASE_URL") or "http://localhost:11434").rstrip("/")
    try:
        r = httpx.get(f"{base}/api/tags", timeout=1.5)
        models = [m.get("name") for m in r.json().get("models", [])]
        return {"running": True, "models": models, "url": base}
    except Exception:
        return {"running": False, "models": [], "url": base}
