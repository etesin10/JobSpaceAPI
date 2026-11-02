"""Guided mode: the flow small models (or a person) follow, one plain-text answer at a time."""

import json

import pytest

from jobfinder.models import Company, MatchResult, Place, Role, RunRequest
from jobfinder.services import discovery, guided, websearch
from jobfinder.services.guided import (
    AnswerError,
    Guide,
    norm_country,
    norm_region,
    parse_company_lines,
    parse_form,
    parse_numbers,
    parse_salary,
)


def test_parse_numbers_is_lenient():
    assert parse_numbers("1,3", 5) == [1, 3]
    assert parse_numbers("Results 2 and 5.", 5) == [2, 5]
    assert parse_numbers("<think>maybe 4?</think>\n1. Acme\n3. Beta", 5) == [1, 3]
    assert parse_numbers("None of them", 5) == [] and parse_numbers("0", 5) == []
    with pytest.raises(AnswerError):
        parse_numbers("Acme", 5)
    assert parse_numbers("7, 2", 5) == [2]


def test_parse_company_lines_ignores_chatter():
    text = ("Sure! Here are some companies:\n1. **Acme MX** - acme.mx\n2. Globex | https://www.globex.com.mx/careers\n"
            "- Initech\nI don't know any more.")
    got = parse_company_lines(text)
    assert [g["name"] for g in got] == ["Acme MX", "Globex", "Initech"]
    assert got[0]["website"] == "acme.mx" and got[2]["website"] is None


def test_parse_form_variants():
    lines = parse_form("Job titles: data engineer, ingeniero de datos\nPaís: México\nEstado: Nuevo León\nSalary: 55k MXN al mes",
                       ["job_titles", "country", "region", "min_salary"])
    assert lines == {"job_titles": "data engineer, ingeniero de datos", "country": "México", "region": "Nuevo León",
                     "min_salary": "55k MXN al mes"}
    js = parse_form('```json\n{"name": "Ana", "country": "MX", "city": "N/A"}\n```', ["name", "country", "city"])
    assert js == {"name": "Ana", "country": "MX"}
    with pytest.raises(AnswerError):
        parse_form("just some words", ["name"])


def test_normalisers():
    assert norm_country("México") == "MX" and norm_country("canada") == "CA" and norm_country("us") == "US"
    assert norm_region("Nuevo León", "MX") == "MX-NLE" and norm_region("ON", "CA") == "CA-ON"
    assert parse_salary("55k MXN al mes", "MX") == {"min": 55000, "currency": "MXN", "period": "month"}
    assert parse_salary("$25/hour", "CA") == {"min": 25, "currency": "CAD", "period": "hour"}


@pytest.fixture
def fake_net(monkeypatch):
    """No internet: verification accepts acme/globex, board reading adds one matching role per company."""
    def verify(rc, c):
        name = c["name"]
        if "ghost" in name.lower():
            return None
        return Company(name=name, domain=c.get("domain") or f"{name.split()[0].lower()}.mx")

    def boards(rc, comp, crit, skills):
        st = rc.app.store
        role, _ = st.upsert_role(Role(title="Data Engineer", url=f"https://{comp.domain}/jobs/1", company_id=comp.id,
                                      location=Place(country="MX", region="MX-NLE", city="Monterrey")))
        st.link_role(rc.pid, role.id, {"score": 80, "status": "scored", "criteria_id": crit[0].id,
                                        "match": MatchResult(score=80, passed=True)})
        st.link_company(rc.pid, comp.id, {"status": "roles_searched"})
        return {"roles_new": 1}

    monkeypatch.setattr(discovery, "_verify_company", verify)
    monkeypatch.setattr(discovery, "company_board_roles", boards)
    monkeypatch.setattr("jobfinder.sources.probe_ats", lambda ctx, comp: None)


def test_full_guided_flow_from_scratch(app, fake_net):
    g = Guide(app)
    t = g.next()
    assert t.kind == "create_profile" and t.needs_user and "Never invent" in t.prompt
    r = g.answer(t.id, "name: Ana Ruiz\ncountry: Mexico\nregion: Nuevo Leon\ncity: Monterrey\nlanguages: Spanish native, English C1")
    assert r["ok"], r["message"]
    pid = r["next"]["profile_id"]
    p = app.store.get_profile(pid)
    assert p.home.region == "MX-NLE" and [x.code for x in p.languages] == ["es", "en"]
    assert r["next"]["kind"] == "add_skills"

    r = g.answer(r["next"]["id"], "Python, SQL, Power BI, Excel avanzado")
    assert r["ok"] and r["next"]["kind"] == "create_criteria"
    bad = g.answer(r["next"]["id"], "data engineer jobs please")
    assert not bad["ok"] and "field: value" in bad["message"] and bad["next"]["id"] == r["next"]["id"]
    r = g.answer(r["next"]["id"], "Job titles: data engineer, ingeniero de datos\nCountry: México\nState: Nuevo León\n"
                                  "City: Monterrey\nWork mode: híbrido, presencial\nSalary: 55k MXN al mes")
    assert r["ok"], r["message"]
    cr = app.store.list_criteria(pid)[0]
    assert cr.constraints.locations[0].region == "MX-NLE" and set(cr.constraints.work_mode) == {"hybrid", "onsite"}
    assert cr.constraints.salary.min == 55000 and cr.constraints.salary.period == "month"

    p.target_companies_per_criteria = 1
    app.store.save_profile(p)
    t = r["next"]
    assert t["kind"] == "name_companies" and t["type"] == "list"  # no web-search key: ask the model for names
    r = g.answer(t["id"], "Sure! Here you go:\n1. **Acme MX** - acme.mx\n2. Ghost Co")
    assert r["ok"] and "Added 1 company: Acme MX" in r["message"] and "Could not verify: Ghost Co" in r["message"]
    nxt = r["next"]
    assert any("Acme MX: 1 matching roles" in x for x in nxt["done_automatically"])  # boards read automatically
    assert nxt["kind"] == "done" and "1 companies, 1 matching roles" in nxt["question"]

    r = g.answer(nxt["id"], "MORE")
    assert r["ok"] and r["next"]["kind"] == "name_companies"
    assert "Do not repeat: Acme MX" in r["next"]["prompt"]


def test_pick_companies_and_contacts_with_search(app, profile, mx_criteria, fake_net, monkeypatch):
    app.store.save_skillset(app.store.get_skillset(profile.id).model_copy(update={"skills": []}))
    g = Guide(app)
    g._set_state(profile.id, "skills_skipped", True)
    monkeypatch.setattr(websearch, "available", lambda conn: ["tavily"])

    def search(conn, http, q, n=10, country=None):
        if "linkedin" in q:
            return [websearch.SearchHit("Luz Pérez - Talent Acquisition - Acme MX | LinkedIn",
                                        "https://mx.linkedin.com/in/luzp", "Acme MX · Monterrey", "tavily"),
                    websearch.SearchHit("Bo Li - Data Lead - Acme MX | LinkedIn", "https://linkedin.com/in/boli",
                                        "Acme MX", "tavily")]
        return [websearch.SearchHit("Data Engineer at Acme MX", "https://boards.greenhouse.io/acmemx/jobs/1", "Monterrey", "tavily"),
                websearch.SearchHit("Indeed: data jobs", "https://mx.indeed.com/q-data", "x", "tavily"),
                websearch.SearchHit("Globex careers", "https://www.globex.com.mx/careers", "Monterrey", "tavily")]

    monkeypatch.setattr(websearch, "web_search", search)
    p = app.store.get_profile(profile.id)
    p.target_companies_per_criteria = 2
    app.store.save_profile(p)
    t = g.next(profile.id)
    assert t.kind == "pick_companies" and len(t.items) == 2  # job board result filtered out before asking
    r = g.answer(t.id, "Acme")
    assert not r["ok"] and "numbers" in r["message"]
    r = g.answer(t.id, "1 and 2")
    assert r["ok"] and "Added 2 companies" in r["message"]
    nxt = r["next"]
    assert nxt["kind"] == "pick_contacts" and len(nxt["items"]) == 2
    r = g.answer(nxt["id"], "1")
    assert r["ok"] and app.store.stats(profile.id)["contacts"] == 1


def test_guided_autopilot_with_small_model(app, profile, mx_criteria, fake_net, monkeypatch):
    p = app.store.get_profile(profile.id)
    p.target_companies_per_criteria = 1
    app.store.save_profile(p)
    Guide(app)._set_state(profile.id, "skills_skipped", True)
    prompts = []

    def tiny_model(task, messages, json_mode=False, budget=None):
        prompts.append(messages[-1]["content"])
        if "Name up to 8" in prompts[-1]:
            return ("<think>hmm</think>Acme MX | acme.mx" if len(prompts) > 1 else "I am not sure."), "tiny-local"
        return "DONE", "tiny-local"

    monkeypatch.setattr(app.llm, "chat", tiny_model)
    run = app.runner.run_sync(profile.id, RunRequest(kind="guided", limit=10))
    assert run.status == "done", run.error
    assert run.result["stats"]["companies"] == 1 and run.result["unusable_answers"] == 1
    assert len(prompts[0]) < 1500  # small-context friendly


def test_guided_autopilot_stops_for_personal_details(app, profile):
    run = app.runner.run_sync(profile.id, RunRequest(kind="guided"))
    assert run.result["stopped"] == "needs_user"


def test_lite_mcp_exposes_only_guided_tools(app):
    import asyncio

    from jobfinder.mcp.server import build_server

    names = {t.name for t in asyncio.run(build_server(app, lite=True).list_tools())}
    assert names == {"next_task", "answer_task", "get_stats", "export_excel"}
    full = {t.name for t in asyncio.run(build_server(app).list_tools())}
    assert {"next_task", "answer_task", "create_profile"} <= full


def test_http_guided_endpoints(client):
    t = client.get("/api/guided/next").json()
    assert t["kind"] == "create_profile"
    r = client.post(f"/api/guided/{t['id']}/answer", json={"answer": "name: Bo\ncountry: CA"}).json()
    assert r["ok"] and r["next"]["kind"] == "add_skills"
    assert json.dumps(r)  # serialisable
    assert guided.GuidedTask.model_validate(r["next"])
