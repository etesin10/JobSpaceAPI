"""Per-step models (steps.yaml), AI web search, and access-failure tracking on companies and roles."""

import threading
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from jobfinder.config import ConfigManager
from jobfinder.models import Company, MatchResult, Place, Role, RunRequest, utcnow
from jobfinder.services import discovery, websearch
from jobfinder.services.runs import RunCtx
from jobfinder.sources import SOURCES, Found
from jobfinder.sources.base import Source, SourceError
from jobfinder.steps import PRESETS, STEPS, default_steps_text, preset_steps


def fake_completion(seen, text='{"ok": 1}', extra=None):
    def complete(messages, **kw):
        seen.append(kw)
        msg = SimpleNamespace(content=text, annotations=(extra or {}).get("annotations"))
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None, **{k: v for k, v in (extra or {}).items()
                                                                                       if k != "annotations"})
    return complete


# ----------------------------------------------------------------------------- steps.yaml


def test_every_preset_renders_valid_yaml_with_comments():
    from jobfinder.config import apply_steps, parse_connections, parse_steps

    for name in PRESETS:
        text = default_steps_text(name)
        assert "# Local fit:" in text and "qwen_coder" in text
        apply_steps(parse_connections("", {}), parse_steps(text, {}))  # all model names resolve
    local_only = preset_steps("local-only")
    assert all(not set(v["models"]) & {"openai_mini", "claude"} for v in local_only.values())
    assert preset_steps("hybrid")["extract_skills"]["models"][0] == "gemma"
    assert preset_steps("hybrid")["find_companies"]["models"][0] == "openai_mini"
    assert set(STEPS) >= {"extract_skills", "review_role", "web_search", "guided"}


def test_each_step_uses_its_own_local_model(app, monkeypatch):
    """Gemma reads resumes, Qwen Coder reviews, Qwen Flash answers guided questions – all on one Ollama."""
    from jobfinder.llm import LLM
    from jobfinder.llm import router as r

    app.ws.steps_file.write_text(default_steps_text("local-first"), encoding="utf-8")
    app.config.set_env_values({"LOCAL_GEMMA_MODEL": "gemma4:12b"})
    monkeypatch.setattr(r, "_local_models", lambda base: {"gemma4:12b", "qwen3.8-coder-next:latest", "qwen3.8-flash-next"})
    seen = []
    monkeypatch.setattr(r, "_lib", lambda: SimpleNamespace(completion=fake_completion(seen)))
    llm = LLM(lambda: app.conn)
    used = {}
    for step in ("extract_skills", "review_role", "guided", "extract_jobs", "plan_queries"):
        _, model = llm.json(step, "s", "u")
        used[step] = model
    assert used == {"extract_skills": "gemma", "review_role": "qwen_coder", "guided": "qwen_flash",
                    "extract_jobs": "qwen_coder", "plan_queries": "qwen_flash"}
    assert seen[0]["model"] == "ollama_chat/gemma4:12b" and seen[0]["temperature"] == 0.0  # step override


def test_cloud_for_one_step_local_for_others_and_disabled_step(app, monkeypatch):
    from jobfinder.llm import LLM, LLMUnavailable
    from jobfinder.llm import router as r

    app.ws.steps_file.write_text(
        "models:\n  qwen_flash: {provider: ollama, model: qwen3.8-flash-next}\n"
        "steps:\n"
        "  find_companies: {models: [openai_mini]}\n"
        "  guided: {models: [qwen_flash], max_tokens: 50}\n"
        "  review_role: {models: [qwen_flash], enabled: false}\n", encoding="utf-8")
    app.config.set_env_values({"OPENAI_API_KEY": "sk-x"})
    monkeypatch.setattr(r, "_local_models", lambda base: {"qwen3.8-flash-next"})
    seen = []
    monkeypatch.setattr(r, "_lib", lambda: SimpleNamespace(completion=fake_completion(seen)))
    llm = LLM(lambda: app.conn)
    assert llm.json("find_companies", "s", "u")[1] == "openai_mini"
    assert llm.json("guided", "s", "u")[1] == "qwen_flash" and seen[-1]["max_tokens"] == 50
    assert not llm.available("review_role")
    with pytest.raises(LLMUnavailable, match="steps.yaml"):
        llm.chat("review_role", [{"role": "user", "content": "x"}])


def test_local_model_not_pulled_is_skipped(app, monkeypatch):
    from jobfinder.llm import LLM
    from jobfinder.llm import router as r

    app.ws.steps_file.write_text(default_steps_text("local-only"), encoding="utf-8")
    app.config.reload()
    monkeypatch.setattr(r, "_local_models", lambda base: {"gemma4:latest"} if "11434" in base else None)  # only Gemma pulled
    assert [n for n, _ in LLM(lambda: app.conn).models_for("review_role")] == ["gemma"]


def test_bad_steps_file_keeps_last_good(tmp_path):
    conn, env, steps = tmp_path / "c.yaml", tmp_path / ".env", tmp_path / "steps.yaml"
    conn.write_text("", encoding="utf-8")
    env.write_text("", encoding="utf-8")
    steps.write_text("steps:\n  guided: {models: [claude]}\n", encoding="utf-8")
    cm = ConfigManager(conn, env, steps)
    assert cm.current.llm.route("guided") == ["claude"]  # built-in model available even with an empty connections.yaml
    steps.write_text("steps:\n  guided: {models: [no_such_model]}\n", encoding="utf-8")
    assert cm.reload() is False and "no_such_model" in cm.last_error and "steps.yaml" in cm.last_error
    assert cm.current.llm.route("guided") == ["claude"]


def test_steps_api(client, app):
    info = client.get("/api/admin/steps").json()
    assert info["preset"] == "auto" and "review_role" in info["steps"] and "Local fit" not in info["steps"]["guided"]
    assert info["steps"]["guided"]["local"].startswith("Qwen Flash")
    r = client.put("/api/admin/steps", json={"steps": {"guided": {"models": ["my_phi"]}},
                                             "add_models": {"my_phi": {"provider": "ollama", "model": "phi4-mini"}}})
    assert r.status_code == 200, r.text
    text = app.ws.steps_file.read_text(encoding="utf-8")
    assert "my_phi:" in text and "models: [my_phi]" in text and "# Local fit:" in text
    assert "${LOCAL_GEMMA_MODEL:-gemma4}" in text  # placeholders preserved, not expanded
    assert client.put("/api/admin/steps", json={"steps": {"guided": {"models": ["ghost"]}}}).status_code == 422
    assert client.post("/api/admin/preset", json={"preset": "hybrid"}).json()["saved"]
    assert client.get("/api/admin/steps").json()["preset"] == "hybrid"
    t = client.post("/api/admin/steps/test", json={"step": "guided"}).json()
    assert t["ok"] is False and "No ready model" in t["error"]


# ----------------------------------------------------------------------------- AI web search


def test_ai_web_search_keeps_only_cited_urls(app, monkeypatch):
    from jobfinder.llm import router as r

    app.ws.steps_file.write_text("steps:\n  web_search: {models: [openai_search]}\n", encoding="utf-8")
    app.config.set_env_values({"OPENAI_API_KEY": "sk-x"})
    seen = []
    content = ('{"results": [{"title": "Acme careers", "url": "https://acme.mx/careers", "snippet": "Data jobs"},'
               '{"title": "Made up", "url": "https://invented.example/jobs"}]}')
    ann = [{"type": "url_citation", "url_citation": {"url": "https://acme.mx/careers", "title": "Acme"}},
           {"type": "url_citation", "url_citation": {"url": "https://globex.mx/empleos", "title": "Globex empleos"}}]
    monkeypatch.setattr(r, "_lib", lambda: SimpleNamespace(completion=fake_completion(seen, content, {"annotations": ann})))
    assert websearch.available(app.conn) == ["ai"]
    hits = websearch.web_search(app.conn, None, "data engineer Monterrey", 5, "MX")
    assert [h.url for h in hits] == ["https://acme.mx/careers", "https://globex.mx/empleos"]
    assert hits[0].snippet == "Data jobs" and hits[0].provider == "ai:openai_search"
    assert seen[0]["web_search_options"] and seen[0]["model"].startswith("openai/gpt-4o-mini-search")


def test_gemini_search_uses_google_grounding(app, monkeypatch):
    from jobfinder.llm import router as r

    app.ws.steps_file.write_text("steps:\n  web_search: {models: [gemini_search]}\n", encoding="utf-8")
    app.config.set_env_values({"GEMINI_API_KEY": "g"})
    seen = []
    grounding = [{"groundingChunks": [{"web": {"uri": "https://beta.mx/jobs", "title": "Beta"}}]}]
    monkeypatch.setattr(r, "_lib", lambda: SimpleNamespace(
        completion=fake_completion(seen, "no json", {"vertex_ai_grounding_metadata": grounding})))
    hits = websearch.web_search(app.conn, None, "q", 5)
    assert [h.url for h in hits] == ["https://beta.mx/jobs"] and seen[0]["tools"] == [{"googleSearch": {}}]


# ----------------------------------------------------------------------------- failures


def _rc(app, pid):
    return RunCtx(app, app.store.create_run(pid, "find_roles", {}), pid, app.http(), app.budget(), threading.Event())


class FlakyBoard(Source):
    name, kind = "greenhouse", "board"
    calls: dict = {}

    def board(self, ctx, board):
        self.calls[board] = self.calls.get(board, 0) + 1
        if board == "blocked":
            raise httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://x"),
                                        response=httpx.Response(403))
        if board == "gone":
            raise SourceError("Lever board 'gone' not readable: HTTP 404", 404, "https://jobs.lever.co/gone")
        ctx.errors.append("page 2 failed: HTTP 502")
        comp = Company(name=board.title())
        return [Found(comp, Role(title="Data Engineer", url=f"https://{board}.mx/jobs/1", description="Python SQL",
                                 location=Place(country="MX", region="MX-NLE", city="Monterrey")))]


def test_loop_continues_and_failures_are_recorded(app, profile, mx_criteria, monkeypatch):
    st = app.store
    monkeypatch.setitem(SOURCES, "greenhouse", FlakyBoard())
    monkeypatch.setattr(discovery, "enabled_search_sources", lambda rc, wanted=None: [])
    monkeypatch.setattr(discovery, "enrich_postings", lambda rc, crit, skills, limit=25: {})
    ids = {}
    for board in ("blocked", "okco", "gone"):
        c, _ = st.upsert_company(Company(name=board, domain=f"{board}.mx", ats={"type": "greenhouse", "board": board}))
        st.link_company(profile.id, c.id, {"criteria_ids": [mx_criteria.id]})
        ids[board] = c.id
    res = discovery.find_roles(_rc(app, profile.id), RunRequest(kind="find_roles", use_llm=False))
    assert res["companies_failed"] == 2 and res["roles_added"] == 1  # the good one in the middle was still read
    blocked = st.get_company(ids["blocked"])
    assert blocked.fetch.ok is False and blocked.fetch.http_status == 403 and "access blocked" in blocked.fetch.error
    assert blocked.fetch.failures == 1 and blocked.fetch.next_retry_at > utcnow()
    assert st.get_company_link(profile.id, ids["blocked"]).status == "error"
    ok = st.get_company(ids["okco"])
    assert ok.fetch.ok and ok.fetch.partial and "HTTP 502" in ok.fetch.error
    assert st.stats(profile.id)["companies_failed"] == 2

    # Default scope skips failed boards until their retry time; scope=failed retries them now.
    FlakyBoard.calls.clear()
    discovery.find_roles(_rc(app, profile.id), RunRequest(kind="find_roles", use_llm=False))
    assert FlakyBoard.calls == {}
    discovery.find_roles(_rc(app, profile.id), RunRequest(kind="find_roles", use_llm=False, scope="failed"))
    assert FlakyBoard.calls == {"blocked": 1, "gone": 1}
    assert st.get_company(ids["blocked"]).fetch.failures == 2  # back-off grows
    # scope=all refreshes every company, and a retry that is due happens automatically
    blocked = st.get_company(ids["blocked"])
    blocked.fetch.next_retry_at = utcnow() - timedelta(minutes=1)
    st.upsert_company(blocked)
    FlakyBoard.calls.clear()
    discovery.find_roles(_rc(app, profile.id), RunRequest(kind="find_roles", use_llm=False))
    assert FlakyBoard.calls == {"blocked": 1}
    FlakyBoard.calls.clear()
    discovery.find_roles(_rc(app, profile.id), RunRequest(kind="find_roles", use_llm=False, scope="all"))
    assert set(FlakyBoard.calls) == {"blocked", "okco", "gone"}


def test_posting_that_cannot_be_opened_is_kept_and_marked(app, profile, mx_criteria, monkeypatch):
    st = app.store
    comp, _ = st.upsert_company(Company(name="Acme", domain="acme.mx"))
    st.link_company(profile.id, comp.id, {"criteria_ids": [mx_criteria.id]})
    roles = {}
    for slug in ("blocked", "readable", "removed"):
        r, _ = st.upsert_role(Role(title=f"Data Engineer {slug}", url=f"https://acme.mx/jobs/{slug}", company_id=comp.id,
                                   location=Place(country="MX", region="MX-NLE", city="Monterrey")))
        st.link_role(profile.id, r.id, {"criteria_id": mx_criteria.id, "score": 70,
                                        "match": MatchResult(score=70, passed=True)})
        roles[slug] = r.id

    def fake_text(rc, role):
        if role.url.endswith("blocked"):
            raise httpx.HTTPStatusError("x", request=httpx.Request("GET", role.url), response=httpx.Response(403))
        if role.url.endswith("removed"):
            raise SourceError("page: HTTP 410", 410, role.url)
        return "We need Python, SQL, Airflow and Spark. " * 20

    monkeypatch.setattr(discovery, "_posting_text", fake_text)
    res = discovery.enrich_postings(_rc(app, profile.id), [mx_criteria], st.get_skillset(profile.id))
    assert res == {"postings_read": 1, "postings_failed": 2}
    blocked = st.get_role(roles["blocked"])
    assert blocked.fetch.ok is False and blocked.fetch.http_status == 403 and blocked.status == "open"
    assert st.get_role_link(profile.id, roles["blocked"]).status != "closed"  # still in the pipeline
    assert st.get_role(roles["removed"]).status == "closed"
    readable = st.get_role(roles["readable"])
    assert "spark" in readable.skills and readable.fetch.ok
    assert st.stats(profile.id)["roles_unreadable"] == 1
    # Not retried before its back-off time
    res2 = discovery.enrich_postings(_rc(app, profile.id), [mx_criteria], st.get_skillset(profile.id))
    assert res2 == {"postings_read": 0, "postings_failed": 0}
    # Agents get a hint to open it themselves
    from jobfinder.services import workplan as wp

    hints = [a.hint for a in wp.build(app, profile.id).next_actions if a.role_id == roles["blocked"]]
    assert any("could not open it" in h and "403" in h for h in hints)


def test_ai_extract_jobs_can_only_use_links_from_the_page(app, profile, monkeypatch):
    from jobfinder.services import pages

    page = pages.Page(url="https://acme.mx/careers", status=200, title="Careers", text="Join us. Data Engineer. Analyst.",
                      links=[("Home", "https://acme.mx/"), ("Data Engineer", "https://acme.mx/careers/de-1"),
                             ("Analyst", "https://acme.mx/careers/an-2"), ("Twitter", "https://x.com/acme")])
    monkeypatch.setattr(pages, "read_page", lambda http, url, max_chars=40000: page)
    monkeypatch.setattr(app.llm, "json", lambda task, s, u, budget=None: (
        {"jobs": [{"title": "Data Engineer", "link": 1, "location": "Monterrey, Mexico"},
                  {"title": "Invented", "link": 99}, {"title": "Analyst", "link": "2"}]}, "qwen_coder"))
    comp = Company(id="c:acme.mx", name="Acme", domain="acme.mx", careers_url="https://acme.mx/careers")
    found = discovery.ai_extract_jobs(_rc(app, profile.id), comp)
    assert [f.role.url for f in found] == ["https://acme.mx/careers/de-1", "https://acme.mx/careers/an-2"]
    assert found[0].role.location.region == "MX-NLE" and found[0].role.source == "careers_page_ai"


def test_review_roles_and_unreadable_posting(app, profile, mx_criteria, monkeypatch):
    st = app.store
    comp, _ = st.upsert_company(Company(name="Acme", domain="acme.mx"))
    good, _ = st.upsert_role(Role(title="Data Engineer", url="https://acme.mx/j/1", company_id=comp.id,
                                  description="Build Airflow + Spark pipelines in Python. English required. " * 10))
    bad, _ = st.upsert_role(Role(title="BI Engineer", url="https://acme.mx/j/2", company_id=comp.id))
    for r in (good, bad):
        st.link_role(profile.id, r.id, {"score": 80, "match": MatchResult(score=80, passed=True)})
    monkeypatch.setattr(discovery, "_posting_text", lambda rc, role: (_ for _ in ()).throw(
        httpx.ConnectError("boom")))
    monkeypatch.setattr(app.llm, "available", lambda task="default": True)
    monkeypatch.setattr(app.llm, "json", lambda task, s, u, budget=None: (
        {"fit": "82", "required_skills": ["Python", "Airflow"], "gaps": ["Spark"], "concerns": [], "summary": "Good fit."},
        "qwen_coder"))
    res = discovery.review_roles(_rc(app, profile.id), RunRequest(kind="review_roles"))
    assert res == {"roles_reviewed": 1, "roles_unreadable": 1}
    rv = st.get_role_link(profile.id, good.id).review
    assert rv.fit == 82 and rv.gaps == ["Spark"] and rv.model == "qwen_coder"
    assert st.get_role(bad.id).fetch.error == "could not connect"


def test_contacts_filtered_by_small_model(app, profile, monkeypatch):
    from jobfinder.models import Contact, SocialProfile

    cands = [Contact(name=n, title=t, profiles=[SocialProfile(url=f"https://linkedin.com/in/{n.lower()}")])
             for n, t in (("Ana", "Recruiter"), ("Bo", "Software Engineer"), ("Cy", "HR Business Partner"))]
    monkeypatch.setattr(app.llm, "available", lambda task="default": task == "find_contacts")
    monkeypatch.setattr(app.llm, "chat", lambda task, messages, budget=None, **kw: ("1, 3", "qwen_flash"))
    kept = discovery._ai_pick_contacts(_rc(app, profile.id), Company(name="Acme"), Role(title="DE", url="https://a.mx/j"), cands)
    assert [c.name for c in kept] == ["Ana", "Cy"]
    monkeypatch.setattr(app.llm, "chat", lambda *a, **k: ("I think the recruiter", "qwen_flash"))
    assert len(discovery._ai_pick_contacts(_rc(app, profile.id), Company(name="Acme"), Role(title="DE", url="https://a.mx/j"), cands)) == 3
