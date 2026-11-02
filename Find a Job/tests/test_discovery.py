import threading

from jobfinder.models import Company, Place, Role, RunRequest
from jobfinder.services import discovery, websearch
from jobfinder.services.runs import RunCtx
from jobfinder.sources import SOURCES, Found
from jobfinder.sources.base import Source


class FakeSearch(Source):
    name, kind = "fake", "search"

    def search(self, ctx, query, location, country, limit):
        mk = lambda t, city, country_, url: Found(  # noqa: E731
            Company(name="Acme MX", domain="acme.mx"),
            Role(title=t, url=url, location=Place(country=country_, city=city), description="Python SQL Airflow"))
        return [mk("Data Engineer", "Monterrey", "MX", "https://acme.mx/j/1"),
                mk("Sales Manager", "Monterrey", "MX", "https://acme.mx/j/2"),
                mk("Data Engineer", "Toronto", "CA", "https://acme.mx/j/3")]


def _rc(app, pid, kind="find_roles"):
    run = app.store.create_run(pid, kind, {})
    return RunCtx(app, run, pid, app.http(), app.budget(), threading.Event())


def test_find_roles_filters_and_links(app, profile, mx_criteria, monkeypatch):
    monkeypatch.setitem(SOURCES, "fake", FakeSearch())
    monkeypatch.setattr(discovery, "enabled_search_sources", lambda rc, wanted=None: [("fake", {})])
    monkeypatch.setattr(discovery, "company_board_roles", lambda rc, comp, crit, skills: {})
    res = discovery.find_roles(_rc(app, profile.id), RunRequest(kind="find_roles", use_llm=False))
    assert res["roles_added"] == 1 and res["filtered"] == 6  # 3 queries x (Sales Manager + Toronto)
    roles = app.store.pipeline_roles(profile.id)
    assert [r.title for r, _ in roles] == ["Data Engineer"]
    assert roles[0][0].location.city == "Monterrey"
    assert app.store.stats(profile.id)["companies_by_criteria"] == {mx_criteria.id: 1}


def test_role_queries_localised(mx_criteria):
    qs = discovery.role_queries(mx_criteria)
    texts = [q.text for q in qs]
    assert "ingeniero de datos" in texts and "ingeniera de datos" not in texts
    assert all(q.country == "MX" for q in qs if q.location)


def test_parse_linkedin_hit():
    comp = Company(id="c:acme.mx", name="Acme MX", domain="acme.mx")
    hit = websearch.SearchHit("Luz Pérez - Talent Acquisition Lead - Acme MX | LinkedIn",
                              "https://mx.linkedin.com/in/luz-perez?trk=x", "Monterrey · Acme MX · Reclutamiento", "tavily")
    ct = discovery.parse_profile_hit(hit, comp)
    assert ct.name == "Luz Pérez" and ct.type == "talent_acquisition" and ct.profiles[0].url.endswith("/in/luz-perez")
    other = websearch.SearchHit("John Roe - Recruiter - OtherCo | LinkedIn", "https://linkedin.com/in/jroe", "OtherCo", "tavily")
    assert discovery.parse_profile_hit(other, comp) is None


def test_find_contacts_requires_search(app, profile, mx_criteria):
    import pytest

    with pytest.raises(RuntimeError, match="web search"):
        discovery.find_contacts(_rc(app, profile.id, "find_contacts"), RunRequest(kind="find_contacts"))


def test_runner_records_failure(app, profile):
    run = app.runner.run_sync(profile.id, RunRequest(kind="find_roles"))
    assert run.status == "failed" and "No active criteria" in run.error
