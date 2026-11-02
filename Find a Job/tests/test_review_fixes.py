"""Regression tests for issues found in code review."""

import threading

import httpx

from jobfinder.models import Company, Place, Role, RunRequest, SearchQuery, WorkplanSubmission
from jobfinder.services import discovery
from jobfinder.services import workplan as wp
from jobfinder.services.http import Http
from jobfinder.services.runs import RunCtx
from jobfinder.sources import SOURCES, Found
from jobfinder.sources.base import Source
from jobfinder.store import pid_alive


def _dead_http():
    return Http("t", per_domain_rps=0, respect_robots=False,
                transport=httpx.MockTransport(lambda req: httpx.Response(404)))


def test_client_cannot_mark_own_evidence_verified(app, profile, monkeypatch):
    monkeypatch.setattr(app, "http", _dead_http)
    sub = WorkplanSubmission.model_validate({"companies": [
        {"name": "Fake A", "domain": "fake-a.invalid",
         "evidence": [{"url": "https://fake-a.invalid/x", "status": "ok", "checked_at": "2026-01-01T00:00:00Z"}]},
        {"name": "Fake B", "domain": "fake-b.invalid", "evidence": [
            {"url": "https://fake-b.invalid/1"}, {"url": "https://fake-b.invalid/2"}, {"url": "https://fake-b.invalid/3"},
            {"url": "https://fake-b.invalid/4", "status": "ok", "checked_at": "2026-01-01T00:00:00Z"}]},
    ]})
    rep = wp.merge(app, profile.id, sub)
    assert rep.companies_added == 0 and len(rep.rejected) == 2


def test_run_does_not_revert_concurrent_criteria_edits(app, profile, mx_criteria, monkeypatch):
    from jobfinder.services.profiles import update_strategy

    q = SearchQuery(text="data engineer", location="Monterrey, Mexico", country="MX")
    update_strategy(app, mx_criteria.id, {"queries": [q.model_dump()]}, "agent-1")

    class Edit(Source):
        name, kind = "edit", "search"

        def search(self, ctx, query, location, country, limit):
            # Simulate the user and an AI editing while the run is in flight.
            cur = app.store.get_criteria(mx_criteria.id)
            cur.constraints.posted_within_days = 7
            app.store.save_criteria(cur)
            update_strategy(app, mx_criteria.id, {"notes": "newer"}, "agent-2")
            return []

    monkeypatch.setitem(SOURCES, "edit", Edit())
    monkeypatch.setattr(discovery, "enabled_search_sources", lambda rc, wanted=None: [("edit", {})])
    run = app.store.create_run(profile.id, "find_roles", {})
    rc = RunCtx(app, run, profile.id, app.http(), app.budget(), threading.Event())
    discovery.find_roles(rc, RunRequest(kind="find_roles", use_llm=False))
    after = app.store.get_criteria(mx_criteria.id)
    assert after.constraints.posted_within_days == 7
    assert after.search_strategy.notes == "newer" and after.search_strategy.version == 2
    assert after.search_strategy.queries[0].yield_total == 0 and after.search_strategy.queries[0].last_run_at


def test_incomplete_board_does_not_close_roles_and_reopen(app, profile, mx_criteria, monkeypatch):
    st = app.store
    comp, _ = st.upsert_company(Company(name="Acme", domain="acme.mx", ats={"type": "workday", "board": "acme.wd1.myworkdayjobs.com|Ext"}))
    st.link_company(profile.id, comp.id, {"criteria_ids": [mx_criteria.id]})
    mk = lambda i: Found(comp, Role(title="Data Engineer", url=f"https://acme.wd1.myworkdayjobs.com/Ext/job/{i}",  # noqa: E731
                                    location=Place(country="MX", region="MX-NLE", city=f"Monterrey{i}"[:9]),
                                    source="workday", source_id=f"acme:{i}", description="Python SQL"))
    state = {"complete": True, "ids": [1, 2]}

    class FakeWorkday(Source):
        name, kind = "workday", "board"

        def board(self, ctx, board):
            ctx.complete = state["complete"]
            return [mk(i) for i in state["ids"]]

    monkeypatch.setitem(SOURCES, "workday", FakeWorkday())
    rc = RunCtx(app, st.create_run(profile.id, "find_roles", {}), profile.id, app.http(), app.budget(), threading.Event())
    discovery.company_board_roles(rc, st.get_company(comp.id), [mx_criteria], st.get_skillset(profile.id))
    assert len(st.list_roles(comp.id)) == 1  # same title/city dedups to one role; fine for this test
    rid = st.list_roles(comp.id)[0].id
    state.update(complete=False, ids=[])
    discovery.company_board_roles(rc, st.get_company(comp.id), [mx_criteria], st.get_skillset(profile.id))
    assert st.get_role(rid).status == "open"  # partial listing never closes anything
    state.update(complete=True, ids=[99])
    discovery._close_missing(rc, st.get_company(comp.id), [mk(99)])
    assert st.get_role(rid).status == "closed"
    state.update(complete=True, ids=[1])
    discovery.company_board_roles(rc, st.get_company(comp.id), [mx_criteria], st.get_skillset(profile.id))
    assert st.get_role(rid).status == "open" and st.get_role_link(profile.id, rid).status == "scored"


def test_cancel_and_stale_runs_across_processes(app, profile):
    st = app.store
    mine = app.runner._new_run(profile.id, RunRequest(kind="find_roles"))
    mine.status = "running"
    st.save_run(mine)
    other = st.create_run(profile.id, "find_roles", {})
    other.status, other.owner_pid = "running", 999_999_999  # dead process
    st.save_run(other)
    assert app.runner.cancel(mine.id) and st.cancel_requested(mine.id)
    st.save_run(mine)  # the worker's periodic save must not clear the flag
    assert st.cancel_requested(mine.id)
    assert st.fail_stale_runs() == 1
    assert st.get_run(other.id).status == "interrupted" and st.get_run(mine.id).status == "running"
    assert st.get_run(other.id).checkpoint.resumable
    assert pid_alive(None) is False
