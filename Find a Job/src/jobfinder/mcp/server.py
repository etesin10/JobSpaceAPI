"""MCP server (stdio). Lets Codex, Claude, Copilot, OpenCode, Cursor… drive JobFinder as tools.

Run:  jobfinder mcp        (clients start this command themselves; see `jobfinder mcp install`)
"""

from __future__ import annotations

import json
import time
from typing import Any

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

from jobfinder.core import App
from jobfinder.models import SCHEMA_EXPORTS, RunRequest, WorkplanSubmission
from jobfinder.services import excel, websearch
from jobfinder.services import profiles as profile_svc
from jobfinder.services import workplan as wp_svc

INSTRUCTIONS = """JobFinder holds a job seeker's profiles, filtering criteria, skills and a roster of
companies -> roles -> contacts. Full workflow:
0. Setup (only with the user's explicit details): create_profile -> import_resume (file path or text) or
   set_skills -> occupation_templates (optional) -> save_criteria (ISO codes: country MX, region MX-NLE,
   currency MXN). save_criteria returns validation errors and the search queries it will use.
1. list_profiles, then get_workplan(profile_id).
2. Follow workplan.next_actions:
   - find_companies / find_contacts: use your own web search/browsing, or JobFinder web_search if configured,
     and fetch_page to read pages. Or start_run(kind=...) to let JobFinder do it with its own keys.
   - find_roles: start_run(kind="find_roles", company_ids=[...]) reads company job boards (no keys needed).
3. submit_workplan(profile_id, submission) with the same tree shape. Every new item needs a real evidence URL;
   the server checks it. Never invent companies, postings, emails or phone numbers.
4. get_run to watch runs (they take minutes; poll every ~20 s). get_stats for counts per profile;
   role_brief for resume tailoring; export_excel for the review workbook.
5. Runs save checkpoints. If a run stops early (budget, no internet, AI unavailable, crash, app closed) it appears
   in list_runs_to_resume / workplan.runs_to_resume with where and why it stopped: fix the cause if needed, then
   resume_run(run_id) – it continues where it stopped and skips finished items.
Criteria constraints/targets/rules belong to the user: change them only when the user asks. While searching,
tune only search_strategy (update_search_strategy)."""


LITE_INSTRUCTIONS = """JobFinder helps a person find a job. It asks you one short question at a time.
1. Call next_task (profile_id can be empty).
2. Read the task's "prompt" and reply exactly in its answer format (for example "1,3").
   If "needs_user" is true, ask the person and pass their words as the answer. Never invent personal details.
3. Call answer_task with the task id and your answer. The reply contains the next task.
4. Repeat until the task kind is "done", then tell the person the numbers in the question."""

GUIDED_TOOLS = {"next_task", "answer_task", "get_stats", "export_excel"}


def build_server(app: App | None = None, lite: bool = False) -> FastMCP:
    state: dict[str, App | None] = {"app": app}

    def A() -> App:  # noqa: N802
        if state["app"] is None:
            state["app"] = App()
        return state["app"]

    mcp = FastMCP("jobfinder", instructions=LITE_INSTRUCTIONS if lite else INSTRUCTIONS)
    _register = mcp.tool

    def _tool(*dargs: Any, **dkw: Any) -> Any:
        """Like mcp.tool(), but expected failures reach the agent as readable messages instead of a generic crash.
        In lite mode only the guided tools are registered, so small models see a tiny tool list."""
        def deco(fn: Any) -> Any:
            import functools

            from pydantic import ValidationError

            if lite and fn.__name__ not in GUIDED_TOOLS:
                return fn

            @functools.wraps(fn)
            def wrapper(*a: Any, **k: Any) -> Any:
                try:
                    return fn(*a, **k)
                except ToolError:
                    raise
                except ValidationError as e:
                    msgs = [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()]
                    raise ToolError("Invalid input: " + "; ".join(msgs[:8])) from e
                except KeyError as e:
                    raise ToolError(f"Not found: {e.args[0] if e.args else e}") from e
                except (ValueError, RuntimeError) as e:  # includes LLMUnavailable / NoSearchProvider
                    raise ToolError(str(e)) from e

            return _register(*dargs, **dkw)(wrapper)

        return deco

    mcp.tool = _tool  # type: ignore[method-assign]

    # ------------------------------------------------------------------ guided mode
    @mcp.tool()
    def next_task(profile_id: str = "") -> dict[str, Any]:
        """Get the next small question. Give its "prompt" to the model or person, then call answer_task."""
        from jobfinder.services.guided import Guide

        t = Guide(A()).next(profile_id or None)
        return t.model_dump(include={"id", "profile_id", "kind", "type", "needs_user", "prompt", "done_automatically",
                                     "stats"})

    @mcp.tool()
    def answer_task(task_id: str, answer: str) -> dict[str, Any]:
        """Answer a guided task in plain text (e.g. "1,3" or "name: Ana"). Returns a message and the next task."""
        from jobfinder.services.guided import Guide

        res = Guide(A()).answer(task_id, answer)
        nxt = res["next"]
        res["next"] = {k: nxt[k] for k in ("id", "profile_id", "kind", "type", "needs_user", "prompt",
                                           "done_automatically", "stats")}
        return res

    # ------------------------------------------------------------------ setup: profiles, skills, criteria
    @mcp.tool()
    def create_profile(name: str, headline: str | None = None, home_country: str | None = None,
                       home_region: str | None = None, home_city: str | None = None,
                       languages: list[str] | None = None, email: str | None = None,
                       target_companies_per_criteria: int = 50, profile_id: str | None = None) -> dict[str, Any]:
        """Create a job-seeker profile. home_country ISO 3166-1 (MX, CA, US); home_region ISO 3166-2 (MX-NLE, CA-ON);
        languages like ["es:native", "en:C1"]. Returns the profile with its id (used by every other tool)."""
        from jobfinder.models import Profile

        st = A().store
        if profile_id and st.get_profile(profile_id):
            raise ValueError(f"profile {profile_id} already exists; use update_profile")
        home = {k: v for k, v in {"country": home_country, "region": home_region, "city": home_city}.items() if v}
        p = Profile.model_validate({
            "id": profile_id, "name": name, "headline": headline, "email": email, "home": home or None,
            "languages": [_lang(x) for x in languages or []],
            "target_companies_per_criteria": target_companies_per_criteria})
        return st.save_profile(p).model_dump(mode="json", exclude_none=True)

    @mcp.tool()
    def update_profile(profile_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Change profile fields, e.g. {"headline": "...", "home": {"country": "CA", "region": "CA-ON"},
        "work_authorization": [{"country": "CA", "status": "needs_sponsorship"}]}. See get_schema("profile")."""
        from jobfinder.models import Profile

        st = A().store
        cur = st.get_profile(profile_id)
        if cur is None:
            raise ValueError(f"no profile {profile_id}")
        data = {**cur.model_dump(mode="json", exclude={"created_at", "updated_at"}), **changes, "id": profile_id}
        if "languages" in changes:
            data["languages"] = [_lang(x) if isinstance(x, str) else x for x in changes["languages"]]
        return st.save_profile(Profile.model_validate(data)).model_dump(mode="json", exclude_none=True)

    @mcp.tool()
    def delete_profile(profile_id: str, confirm: bool = False) -> dict[str, Any]:
        """Delete a profile with its criteria, skills and pipeline. Requires confirm=true (ask the user first)."""
        if not confirm:
            raise ValueError("Deleting is permanent. Ask the user, then call again with confirm=true.")
        return {"deleted": A().store.delete_profile(profile_id)}

    @mcp.tool()
    def import_resume(profile_id: str, file_path: str | None = None, text: str | None = None,
                      mode: str = "merge") -> dict[str, Any]:
        """Read skills from a resume or skills file on this computer (PDF, DOCX, TXT, CSV) or from pasted text.
        mode: merge | replace. Returns the extracted skills."""
        from pathlib import Path

        a = A()
        if a.store.get_profile(profile_id) is None:
            raise ValueError(f"no profile {profile_id}")
        if file_path:
            p = Path(file_path).expanduser()
            if not p.is_file():
                raise ValueError(f"file not found: {p}")
            if p.stat().st_size > 15_000_000:
                raise ValueError("file too large (max 15 MB)")
            name, data = p.name, p.read_bytes()
        elif text:
            name, data = "resume.txt", text.encode("utf-8")
        else:
            raise ValueError("give file_path or text")
        res = profile_svc.ingest_skills_file(a, profile_id, name, data, mode=mode)
        return {k: res[k] for k in ("extracted", "added", "total", "llm")} | {
            "skills": [s["name"] for s in res["skills"]]}

    @mcp.tool()
    def set_skills(profile_id: str, skills: list[str | dict[str, Any]], replace: bool = False,
                   summary: str | None = None) -> dict[str, Any]:
        """Add skills by name (any language: "Python", "Excel avanzado", "dispensing") or as objects
        {"name", "level" 1-5, "years"}. Names are mapped to the skills catalog automatically."""
        from jobfinder.models import Skill
        from jobfinder.services.skills import catalog

        st, cat = A().store, catalog()
        cur = st.get_skillset(profile_id)
        new = []
        for s in skills:
            d = {"name": s} if isinstance(s, str) else dict(s)
            sid = d.get("id") or cat.resolve(d["name"])
            new.append(Skill(id=sid, name=cat.name_of(sid) if sid in cat.by_id else d["name"],
                             level=d.get("level"), years=d.get("years"), category=d.get("category"), source="manual"))
        cur.skills = new if replace else [*cur.skills, *new]
        if summary:
            cur.summary = summary
        saved = st.save_skillset(cur)
        return {"total": len(saved.skills), "skills": [f"{s.name} ({s.id})" for s in saved.skills]}

    @mcp.tool()
    def occupation_templates() -> list[dict[str, Any]]:
        """Built-in occupations with ISCO/NOC/SOC codes, titles in en/es/fr and typical skills – a starting point
        for save_criteria targets."""
        from jobfinder.services.skills import occupations

        return [{"key": o.key, "codes": o.codes, "titles": o.titles, "skills": o.skills} for o in occupations()]

    @mcp.tool()
    def save_criteria(profile_id: str, criteria: dict[str, Any]) -> dict[str, Any]:
        """Create (no "id") or replace (with "id") a filtering criteria – only with details the user gave you.
        Example: {"name": "Data engineer Monterrey", "constraints": {"locations": [{"country": "MX",
        "region": "MX-NLE", "city": "Monterrey"}], "work_mode": ["hybrid", "onsite"], "salary": {"min": 55000,
        "currency": "MXN", "period": "month"}, "sponsorship": {"need": "not_required"}, "posted_within_days": 30},
        "targets": {"titles": ["data engineer", "ingeniero de datos"]}, "rules": {"should": [{"field": "skills",
        "op": "any", "value": ["Python", "SQL"], "weight": 2}], "must_not": [{"field": "title", "op": "contains",
        "value": ["intern"]}]}}. Full contract: get_schema("criteria"). Returns errors or the saved criteria + queries."""
        from pydantic import ValidationError

        from jobfinder.models import Criteria
        from jobfinder.services import discovery

        st = A().store
        if st.get_profile(profile_id) is None:
            raise ValueError(f"no profile {profile_id}")
        body = {k: v for k, v in criteria.items() if k not in ("created_at", "updated_at")}
        body["profile_id"] = profile_id
        existing = st.get_criteria(body["id"]) if body.get("id") else None
        if existing and existing.profile_id != profile_id:
            raise ValueError(f"criteria {body['id']} belongs to another profile")
        if existing and "search_strategy" not in body:
            body["search_strategy"] = existing.search_strategy.model_dump(mode="json")
        try:
            cr = Criteria.model_validate(body)
        except ValidationError as e:
            return {"saved": False, "errors": [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()]}
        saved = st.save_criteria(cr)
        return {"saved": True, "criteria": saved.model_dump(mode="json", exclude_none=True),
                "role_queries": [f"{q.text} @ {q.location or 'any'}" for q in discovery.role_queries(saved)]}

    @mcp.tool()
    def delete_criteria(criteria_id: str, confirm: bool = False) -> dict[str, Any]:
        """Delete a criteria. Requires confirm=true (ask the user first)."""
        if not confirm:
            raise ValueError("Ask the user, then call again with confirm=true.")
        return {"deleted": A().store.delete_criteria(criteria_id)}

    @mcp.tool()
    def add_company(profile_id: str, name: str, website: str | None = None, criteria_id: str | None = None,
                    ats_type: str | None = None, ats_board: str | None = None) -> dict[str, Any]:
        """Add one company the user names (no evidence check). For discovered companies use submit_workplan.
        ats_type: greenhouse | lever | ashby | smartrecruiters | workable | workday (board 'host|site')."""
        from jobfinder.models import AtsRef, Company

        st = A().store
        if st.get_profile(profile_id) is None:
            raise ValueError(f"no profile {profile_id}")
        comp = Company(name=name, website=website, domain=website,
                       ats=AtsRef(type=ats_type, board=ats_board) if ats_type and ats_board else None)  # type: ignore[arg-type]
        saved, _ = st.upsert_company(comp)
        link, _ = st.link_company(profile_id, saved.id, {"criteria_ids": [criteria_id] if criteria_id else []})  # type: ignore[arg-type]
        return {"company_id": saved.id, "name": saved.name, "domain": saved.domain, "status": link.status}

    # ------------------------------------------------------------------ search loop

    @mcp.tool()
    def list_profiles() -> list[dict[str, Any]]:
        """List job-seeker profiles with roster counts."""
        a = A()
        return [{"id": p.id, "name": p.name, "headline": p.headline, "stats": a.store.stats(p.id or "")}
                for p in a.store.list_profiles()]

    @mcp.tool()
    def get_workplan(profile_id: str, company_ids: list[str] | None = None, include_closed: bool = False,
                     max_roles_per_company: int = 25) -> dict[str, Any]:
        """The JSON template to work from: criteria, skills, companies -> roles -> contacts, next_actions, budget."""
        wp = wp_svc.build(A(), profile_id, include_closed=include_closed, company_ids=company_ids,
                          max_roles_per_company=max_roles_per_company, max_description=600)
        return wp.model_dump(mode="json", by_alias=True, exclude_none=True)

    @mcp.tool()
    def submit_workplan(profile_id: str, submission: dict[str, Any]) -> dict[str, Any]:
        """Merge results back. Shape: {agent, criteria_id?, companies:[{name, domain, evidence:[{url}], roles:[{title,
        url, location:{country, region, city}, work_mode, description, contacts:[{name, title, type, profiles:[{network,
        url}], evidence:[{url}]}]}]}], strategy_updates:{criteria_id: {queries:[...]}}}. Returns a merge report."""
        sub = WorkplanSubmission.model_validate(submission)
        return wp_svc.merge(A(), profile_id, sub).model_dump()

    @mcp.tool()
    def get_criteria(profile_id: str) -> list[dict[str, Any]]:
        """Active filtering criteria (constraints, targets, rules, search_strategy) for a profile."""
        return [c.model_dump(mode="json", exclude_none=True) for c in A().store.list_criteria(profile_id)]

    @mcp.tool()
    def update_search_strategy(criteria_id: str, strategy: dict[str, Any], agent: str = "mcp-agent") -> dict[str, Any]:
        """Tune the AI-owned search_strategy (queries, sources, notes). Versioned; user-owned fields are untouched."""
        cr = profile_svc.update_strategy(A(), criteria_id, strategy, agent)
        return cr.search_strategy.model_dump(mode="json")

    @mcp.tool()
    def get_stats(profile_id: str) -> dict[str, Any]:
        """Counts: companies (by criteria/status), roles (matched, by stage), contacts."""
        return A().store.stats(profile_id)

    @mcp.tool()
    def web_search(query: str, n: int = 10, country: str | None = None) -> list[dict[str, Any]]:
        """Web search through the configured provider (Tavily/Brave/SerpApi)."""
        a = A()
        http = a.http()
        try:
            return [h.dict() for h in websearch.web_search(a.conn, http, query, n, country)]
        finally:
            http.close()

    @mcp.tool()
    def fetch_page(url: str, max_chars: int = 15000) -> dict[str, Any]:
        """Fetch a page politely (robots.txt respected): text, schema.org JobPosting data and detected ATS."""
        from jobfinder.api.app import fetch_page as _fetch

        return _fetch(A(), url, max_chars)

    @mcp.tool()
    def start_run(profile_id: str, kind: str = "autopilot", criteria_ids: list[str] | None = None,
                  company_ids: list[str] | None = None, scope: str = "new", limit: int | None = None,
                  wait_seconds: int = 0) -> dict[str, Any]:
        """Start a background run: find_roles | find_companies | find_contacts | review_roles | recheck | autopilot |
        guided. scope (find_roles): new = companies not read yet + failed ones due for retry; all = re-read every
        company's board; failed = retry only boards that could not be read. Failures never stop the loop: they are
        recorded on the company/role ('fetch') and shown by get_workplan."""
        a = A()
        run = a.runner.submit(profile_id, RunRequest(kind=kind, criteria_ids=criteria_ids or [],  # type: ignore[arg-type]
                                                     company_ids=company_ids or [], limit=limit, scope=scope))  # type: ignore[arg-type]
        deadline = time.monotonic() + min(wait_seconds, 600)
        while wait_seconds and time.monotonic() < deadline:
            cur = a.store.get_run(run.id)
            if cur and cur.status in ("done", "failed", "cancelled"):
                return cur.model_dump(mode="json")
            time.sleep(2)
        return (a.store.get_run(run.id) or run).model_dump(mode="json", exclude={"log"})

    @mcp.tool()
    def get_run(run_id: str, log_tail: int = 30) -> dict[str, Any]:
        """Status, progress, result, checkpoint (where it is / where it stopped) and the last log lines of a run."""
        r = A().store.get_run(run_id)
        if r is None:
            raise ValueError(f"run {run_id} not found")
        d = r.model_dump(mode="json")
        d["log"] = d["log"][-log_tail:]
        d["checkpoint"]["done"] = {k: len(v) for k, v in d["checkpoint"]["done"].items()}
        return d

    @mcp.tool()
    def list_runs_to_resume(profile_id: str | None = None) -> list[dict[str, Any]]:
        """Runs that stopped before finishing (budget, cancel, no internet, AI unavailable, crash, app closed):
        where they stopped, why, and how many items were already done."""
        return [wp_svc.run_summary(r) for r in A().runner.resumable(profile_id)]

    @mcp.tool()
    def resume_run(run_id: str, wait_seconds: int = 0) -> dict[str, Any]:
        """Continue a stopped run from its checkpoint (finished items are skipped). Returns the new run."""
        a = A()
        run = a.runner.resume(run_id)
        deadline = time.monotonic() + min(wait_seconds, 600)
        while wait_seconds and time.monotonic() < deadline:
            cur = a.store.get_run(run.id)
            if cur and cur.status not in ("queued", "running"):
                return cur.model_dump(mode="json", exclude={"log"})
            time.sleep(2)
        return (a.store.get_run(run.id) or run).model_dump(mode="json", exclude={"log"})

    @mcp.tool()
    def role_brief(profile_id: str, role_id: str) -> dict[str, Any]:
        """Role description + match (matched/missing skills, keywords) for tailoring a resume outside JobFinder."""
        from jobfinder.api.app import resume_brief

        return resume_brief(A(), profile_id, role_id)

    @mcp.tool()
    def update_pipeline(profile_id: str, kind: str, item_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Change per-profile state. kind=company (excluded, notes, status) | role (stage, excluded, notes, status)
        | contact (status, notes, role_id)."""
        st = A().store
        if kind == "company":
            return st.link_company(profile_id, item_id, patch)[0].model_dump(mode="json")
        if kind == "role":
            return st.link_role(profile_id, item_id, patch)[0].model_dump(mode="json")
        if kind == "contact":
            return st.link_contact(profile_id, item_id, patch.get("role_id"), patch)[0].model_dump(mode="json")
        raise ValueError("kind must be company, role or contact")

    @mcp.tool()
    def export_excel(profile_ids: list[str] | None = None) -> dict[str, str]:
        """Write the review workbook (Summary, Companies, Roles, Contacts, Criteria, Runs). Returns the file path."""
        return {"path": str(excel.export(A(), profile_ids))}

    @mcp.tool()
    def get_schema(name: str) -> dict[str, Any]:
        """JSON Schema of a contract: profile, skillset, criteria, company, role, contact, workplan,
        workplan_submission, run_request."""
        if name not in SCHEMA_EXPORTS:
            raise ValueError(f"unknown schema; choose {sorted(SCHEMA_EXPORTS)}")
        return SCHEMA_EXPORTS[name].model_json_schema(by_alias=True)

    @mcp.prompt()
    def job_search_loop(profile_id: str, goal: str = "Find 20 more companies and their best matching roles") -> str:
        """A ready-made instruction for an agent session."""
        return (f"{INSTRUCTIONS}\n\nProfile: {profile_id}\nGoal: {goal}\n"
                "Start with get_workplan, work through next_actions, submit results in small batches (5-10 companies), "
                "then report counts with get_stats.")

    @mcp.resource("jobfinder://schemas/{name}")
    def schema_resource(name: str) -> str:
        return json.dumps(SCHEMA_EXPORTS[name].model_json_schema(by_alias=True), indent=2)

    return mcp


def _lang(s: str) -> dict[str, str]:
    code, _, level = s.partition(":")
    return {"code": code.strip(), "level": (level or "B2").strip()}


def run_stdio(lite: bool = False) -> None:
    build_server(lite=lite).run("stdio")
