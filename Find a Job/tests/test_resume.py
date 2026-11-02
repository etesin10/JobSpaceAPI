"""Failure resilience: runs save checkpoints, stop cleanly, say where they stopped, and resume from there."""

import asyncio
import json

import httpx
import pytest

from jobfinder.models import Company, Place, Role, RunRequest
from jobfinder.services import discovery
from jobfinder.services import workplan as wp
from jobfinder.sources import SOURCES, Found
from jobfinder.sources.base import Source


class CountingBoard(Source):
    """Greenhouse stand-in: records which boards were read; can simulate the internet going down."""

    name, kind = "greenhouse", "board"

    def __init__(self):
        self.calls: list[str] = []
        self.offline_from: int | None = None

    def board(self, ctx, board):
        self.calls.append(board)
        if self.offline_from is not None and len(self.calls) > self.offline_from:
            raise httpx.ConnectError("network unreachable")
        return [Found(Company(name=board), Role(title="Data Engineer", url=f"https://{board}.mx/jobs/1",
                                                description="Python SQL",
                                                location=Place(country="MX", region="MX-NLE", city="Monterrey")))]


@pytest.fixture
def roster(app, profile, mx_criteria, monkeypatch):
    board = CountingBoard()
    monkeypatch.setitem(SOURCES, "greenhouse", board)
    monkeypatch.setattr(discovery, "enabled_search_sources", lambda rc, wanted=None: [])
    monkeypatch.setattr(discovery, "enrich_postings", lambda rc, crit, skills, limit=25: {})
    for i in range(6):
        c, _ = app.store.upsert_company(Company(name=f"co{i}", domain=f"co{i}.mx", ats={"type": "greenhouse", "board": f"co{i}"}))
        app.store.link_company(profile.id, c.id, {"criteria_ids": [mx_criteria.id]})
    return board


def test_budget_stop_saves_checkpoint_and_resume_skips_done(app, profile, roster):
    # Budget for ~3 boards: the run stops part-way through the roster.
    run = app.runner.run_sync(profile.id, RunRequest(kind="find_roles", use_llm=False, scope="all",
                                                     budget={"max_pages": 3}))
    assert run.status == "interrupted" and len(roster.calls) == 3
    cp = run.checkpoint
    assert cp.resumable and "company 4/6" in cp.stopped_at and "page budget" in cp.reason
    assert len(cp.done["find_roles"]) == 3 and "skips the 3 item(s)" in cp.resume_hint
    assert any("Stopped at find_roles › company 4/6" in line for line in run.log)

    # The stop is visible to AI agents (workplan + top-priority action) and humans (resumable list).
    plan = wp.build(app, profile.id)
    assert plan.runs_to_resume[0]["run_id"] == run.id and plan.next_actions[0].action == "resume_run"
    assert "company 4/6" in plan.next_actions[0].hint

    roster.calls.clear()
    new = app.runner.resume_sync(run.id)
    assert new.status == "done" and new.resumed_from == run.id
    assert sorted(roster.calls) == ["co3", "co4", "co5"]  # only what was left
    assert new.progress["skipped_already_done"] == 3
    assert app.store.get_run(run.id).checkpoint.resumed_by == new.id
    assert app.runner.resumable(profile.id) == []
    with pytest.raises(ValueError, match="already resumed"):
        app.runner.resume(run.id)
    assert app.store.stats(profile.id)["companies"] == 6


def test_internet_down_pauses_without_blaming_companies(app, profile, roster, monkeypatch):
    roster.offline_from = 2
    monkeypatch.setattr(discovery, "internet_ok", lambda http: False)
    run = app.runner.run_sync(profile.id, RunRequest(kind="find_roles", use_llm=False, scope="all"))
    assert run.status == "interrupted" and "No internet" in run.checkpoint.reason
    assert "company 3/6" in run.checkpoint.stopped_at
    statuses = [link.status for _, link in app.store.pipeline_companies(profile.id)]
    assert "error" not in statuses  # the outage was not recorded as company failures
    roster.offline_from, roster.calls = None, []
    monkeypatch.setattr(discovery, "internet_ok", lambda http: True)
    new = app.runner.resume_sync(run.id)
    assert new.status == "done" and len(roster.calls) == 4


def test_single_site_down_is_recorded_and_loop_continues(app, profile, roster, monkeypatch):
    roster.offline_from = 2
    monkeypatch.setattr(discovery, "internet_ok", lambda http: True)  # internet fine: it's those sites
    run = app.runner.run_sync(profile.id, RunRequest(kind="find_roles", use_llm=False, scope="all"))
    assert run.status == "done" and len(roster.calls) == 6
    assert app.store.stats(profile.id)["companies_failed"] == 4


def test_app_closed_mid_run_is_resumable(app, profile, roster):
    st = app.store
    run = app.runner._new_run(profile.id, RunRequest(kind="find_roles", use_llm=False, scope="all"))
    run.status = "running"
    run.owner_pid = 999_999_999  # process gone (app closed / crashed)
    run.checkpoint.at = "find_roles › company 3/6: co2"
    run.checkpoint.done = {"find_roles": ["c:c:co0.mx", "c:c:co1.mx"]}
    st.save_run(run)
    assert st.fail_stale_runs() == 1
    stale = st.get_run(run.id)
    assert stale.status == "interrupted" and stale.checkpoint.stopped_at == "find_roles › company 3/6: co2"
    assert "closed or crashed" in stale.checkpoint.reason
    new = app.runner.resume_sync(run.id)
    assert new.status == "done" and sorted(roster.calls) == ["co2", "co3", "co4", "co5"]


def test_cancel_then_resume(app, profile, roster, monkeypatch):
    calls = {"n": 0}
    orig = discovery.company_board_roles

    def cancel_after_two(rc, comp, crit, skills):
        calls["n"] += 1
        if calls["n"] == 3:
            rc.cancel.set()
        return orig(rc, comp, crit, skills)

    monkeypatch.setattr(discovery, "company_board_roles", cancel_after_two)
    run = app.runner.run_sync(profile.id, RunRequest(kind="find_roles", use_llm=False, scope="all"))
    # The company being processed when the stop came (co2) is not counted as done: resume redoes it.
    assert run.status == "cancelled" and run.checkpoint.resumable and len(run.checkpoint.done["find_roles"]) == 2
    assert "company 3/6: co2" in run.checkpoint.stopped_at
    new = app.runner.resume_sync(run.id)
    assert new.status == "done" and calls["n"] == 3 + 4


def test_autopilot_skips_finished_steps_and_retries_skipped(app, profile, mx_criteria, monkeypatch):
    seen = []

    def step(name, fail=None):
        def fn(rc, req):
            seen.append(name)
            if fail:
                raise fail
            return {"ok": name}
        return fn

    from jobfinder.llm import BudgetExceeded

    monkeypatch.setattr(discovery, "find_companies", step("find_companies", RuntimeError("no web search key")))
    monkeypatch.setattr(discovery, "find_roles", step("find_roles"))
    monkeypatch.setattr(discovery, "find_contacts", step("find_contacts", BudgetExceeded("search budget exhausted")))
    run = app.runner.run_sync(profile.id, RunRequest(kind="autopilot", use_llm=False))
    assert run.status == "interrupted" and run.checkpoint.skipped == {"find_companies": "no web search key"}
    assert run.checkpoint.done["autopilot"] == ["find_roles"] and run.checkpoint.stage == "find_contacts"
    seen.clear()
    monkeypatch.setattr(discovery, "find_contacts", step("find_contacts"))
    new = app.runner.resume_sync(run.id)
    assert new.status == "done" and seen == ["find_companies", "find_contacts"]  # find_roles not repeated


def test_ai_unavailable_is_resumable(app, profile, mx_criteria, monkeypatch):
    from jobfinder.llm import LLMUnavailable

    def boom(rc, req):
        raise LLMUnavailable("All models failed: qwen_flash: connection refused")

    monkeypatch.setitem(discovery.KINDS, "review_roles", boom)
    run = app.runner.run_sync(profile.id, RunRequest(kind="review_roles"))
    assert run.status == "interrupted" and "no AI model answered" in run.checkpoint.reason


def test_resume_surfaces_in_api_mcp_and_guided(app, client, profile, roster):
    run = app.runner.run_sync(profile.id, RunRequest(kind="find_roles", use_llm=False, scope="all",
                                                     budget={"max_pages": 2}))
    lst = client.get(f"/api/runs-resumable?profile_id={profile.id}").json()
    assert lst[0]["run_id"] == run.id and lst[0]["items_done"] == 2

    from jobfinder.mcp.server import build_server

    srv = build_server(app)
    res = asyncio.run(srv.call_tool("list_runs_to_resume", {"profile_id": profile.id}))
    content = getattr(res, "content", None) or (res[0] if isinstance(res, tuple) else res)
    assert json.loads(content[0].text)["run_id"] == run.id

    from jobfinder.services.guided import Guide

    g = Guide(app)
    g._set_state(profile.id, "skills_skipped", True)
    t = g.next(profile.id)
    assert t.kind == "resume_run" and "company 3/6" in t.question
    r = g.answer(t.id, "SKIP")
    assert r["ok"] and app.runner.resumable(profile.id) == []  # dismissed, not offered again

    run2 = app.runner.run_sync(profile.id, RunRequest(kind="find_roles", use_llm=False, scope="all",
                                                      budget={"max_pages": 1}))
    resumed = client.post(f"/api/runs/{run2.id}/resume").json()
    assert resumed["resumed_from"] == run2.id
    assert client.post(f"/api/runs/{run2.id}/resume").status_code == 400  # already resumed
