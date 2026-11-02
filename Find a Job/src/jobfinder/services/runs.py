"""Background runs with checkpoints.

Long searches execute in a worker thread; clients poll GET /api/runs/{id}. While a run works through its items
(search queries, company boards, postings, contacts) it records which ones are finished in run.checkpoint and where
it currently is. If it stops before the end – budget used up, cancelled, internet down, every AI model failing, a bug,
or the app being closed – the run is marked resumable with a "stopped at …" note. Runner.resume() starts a new run
with the same settings that skips everything already finished.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jobfinder.llm import Budget, BudgetExceeded, LLMUnavailable
from jobfinder.models import Checkpoint, Run, RunRequest, utcnow
from jobfinder.services.http import Http

if TYPE_CHECKING:
    from jobfinder.core import App

log = logging.getLogger("jobfinder.runs")


class Cancelled(Exception):
    pass


class Interrupted(RuntimeError):
    """Stop the run now but keep it resumable (e.g. the internet connection is down)."""


@dataclass
class RunCtx:
    app: App
    run: Run
    pid: str
    http: Http
    budget: Budget
    cancel: threading.Event = field(default_factory=threading.Event)
    _last_save: float = 0
    _last_cancel_poll: float = 0
    _done: dict[str, set[str]] = field(default_factory=dict)

    # ------------------------------------------------------------------ logging / progress
    def log(self, msg: str) -> None:
        line = f"{utcnow().strftime('%H:%M:%S')} {msg}"
        self.run.log.append(line)
        self.run.log = self.run.log[-400:]
        log.info("[%s] %s", self.run.id, msg)
        self._save()

    def progress(self, **kw: Any) -> None:
        self.run.progress.update(kw)
        self._save()

    def check(self) -> None:
        if not self.cancel.is_set() and time.monotonic() - self._last_cancel_poll > 2:
            self._last_cancel_poll = time.monotonic()
            if self.app.store.cancel_requested(self.run.id):
                self.cancel.set()
        if self.cancel.is_set():
            raise Cancelled()
        self.budget.check("time")

    # ------------------------------------------------------------------ checkpoint
    @property
    def cp(self) -> Checkpoint:
        return self.run.checkpoint

    def at(self, stage: str, where: str) -> None:
        """Say what the run is working on now; becomes 'stopped_at' if the run stops here."""
        self.cp.stage, self.cp.at = stage, f"{stage} › {where}"
        self._save()

    def is_done(self, stage: str, key: str) -> bool:
        if stage not in self._done:
            self._done[stage] = set(self.cp.done.get(stage, []))
        return key in self._done[stage]

    def mark_done(self, stage: str, key: str) -> None:
        if not self.is_done(stage, key):
            self._done[stage].add(key)
            self.cp.done.setdefault(stage, []).append(key)
            self._save()

    def skip(self, stage: str, key: str, label: str) -> bool:
        """True (and a log line) when this item was finished by the run being resumed."""
        if self.is_done(stage, key):
            self.run.progress["skipped_already_done"] = self.run.progress.get("skipped_already_done", 0) + 1
            return True
        return False

    def stopped(self, reason: str) -> None:
        cp = self.cp
        cp.stopped_at = cp.at or cp.stage or "start"
        cp.reason = reason
        n_done = sum(len(v) for v in cp.done.values())
        cp.resumable = True
        cp.resume_hint = (f"Resume continues from '{cp.stopped_at}' with the same settings and skips the {n_done} item(s) "
                          f"already finished." if n_done else "Resume runs it again with the same settings.")
        self.log(f"Stopped at {cp.stopped_at}: {reason}. {cp.resume_hint}")

    def _save(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_save > 1.0:
            self.run.progress["budget"] = self.budget.snapshot()
            self.app.store.save_run(self.run)
            self._last_save = now


class Runner:
    def __init__(self, app: App, workers: int = 2):
        self.app = app
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="run")
        self.cancels: dict[str, threading.Event] = {}

    def submit(self, profile_id: str, req: RunRequest) -> Run:
        from jobfinder.services.discovery import KINDS

        if req.kind not in KINDS:
            raise ValueError(f"unknown run kind {req.kind}")
        if self.app.store.get_profile(profile_id) is None:
            raise KeyError(profile_id)
        return self._start(self._new_run(profile_id, req), req)

    def _start(self, run: Run, req: RunRequest) -> Run:
        ev = threading.Event()
        self.cancels[run.id] = ev
        self.pool.submit(self._execute, run, req, ev)
        return run

    def _new_run(self, profile_id: str, req: RunRequest, resumed_from: Run | None = None) -> Run:
        run = self.app.store.create_run(profile_id, req.kind, req.model_dump(mode="json"))
        run.owner_pid = os.getpid()
        if resumed_from is not None:
            run.resumed_from = resumed_from.id
            run.checkpoint = Checkpoint(done={k: list(v) for k, v in resumed_from.checkpoint.done.items()})
        self.app.store.save_run(run)
        return run

    def run_sync(self, profile_id: str, req: RunRequest) -> Run:
        run = self._new_run(profile_id, req)
        self._execute(run, req, threading.Event())
        return self.app.store.get_run(run.id) or run

    # ------------------------------------------------------------------ resume
    def _resume_prepare(self, run_id: str) -> tuple[Run, RunRequest]:
        old = self.app.store.get_run(run_id)
        if old is None:
            raise KeyError(run_id)
        if old.status in ("queued", "running"):
            raise ValueError(f"run {run_id} is still {old.status}")
        if old.status == "done" and not old.checkpoint.resumable:
            raise ValueError(f"run {run_id} finished; nothing to resume")
        if old.checkpoint.resumed_by:
            raise ValueError(f"run {run_id} was already resumed by run {old.checkpoint.resumed_by}")
        req = RunRequest.model_validate(old.params)
        if old.profile_id is None or self.app.store.get_profile(old.profile_id) is None:
            raise ValueError("the profile of this run no longer exists")
        new = self._new_run(old.profile_id, req, resumed_from=old)
        old.checkpoint.resumed_by = new.id
        self.app.store.save_run(old)
        new.log.append(f"{utcnow().strftime('%H:%M:%S')} Resuming run {old.id}, which stopped at "
                       f"{old.checkpoint.stopped_at or '?'} ({old.checkpoint.reason or old.error or old.status}).")
        self.app.store.save_run(new)
        return new, req

    def resume(self, run_id: str) -> Run:
        new, req = self._resume_prepare(run_id)
        return self._start(new, req)

    def resume_sync(self, run_id: str) -> Run:
        new, req = self._resume_prepare(run_id)
        self._execute(new, req, threading.Event())
        return self.app.store.get_run(new.id) or new

    def dismiss(self, run_id: str) -> bool:
        run = self.app.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        run.checkpoint.dismissed = True
        self.app.store.save_run(run)
        return True

    def resumable(self, profile_id: str | None = None) -> list[Run]:
        return [r for r in self.app.store.list_runs(profile_id, 50)
                if r.checkpoint.resumable and not r.checkpoint.resumed_by and not r.checkpoint.dismissed
                and r.status in ("interrupted", "failed", "cancelled")]

    def cancel(self, run_id: str) -> bool:
        """Works across processes: the owning process polls the flag in RunCtx.check()."""
        ev = self.cancels.get(run_id)
        if ev:
            ev.set()
        return self.app.store.request_cancel(run_id) or bool(ev)

    # ------------------------------------------------------------------ execution
    def _execute(self, run: Run, req: RunRequest, cancel: threading.Event) -> None:
        from jobfinder.services.discovery import KINDS

        http = self.app.http()
        rc = RunCtx(self.app, run, run.profile_id or "", http, self.app.budget(req.budget), cancel)
        run.status = "running"
        run.started_at = utcnow()
        rc._save(force=True)
        try:
            run.result = KINDS[req.kind](rc, req)
            run.status = "done"
            run.checkpoint.stage, run.checkpoint.at = None, None
        except Cancelled:
            run.status = "cancelled"
            rc.stopped("cancelled by the user")
        except BudgetExceeded as e:
            run.status = "interrupted"
            run.error = f"stopped early: {e}"
            rc.stopped(f"{e} (raise the budget in connections.yaml › limits.run_budget, or just resume)")
        except Interrupted as e:
            run.status = "interrupted"
            run.error = str(e)
            rc.stopped(str(e))
        except LLMUnavailable as e:
            run.status = "interrupted"
            run.error = f"AI model unavailable: {e}"
            rc.stopped(f"no AI model answered ({str(e)[:200]}). Check AI & Settings › steps, then resume")
        except Exception as e:
            run.status = "failed"
            run.error = f"{type(e).__name__}: {e}"
            log.error("run %s failed\n%s", run.id, traceback.format_exc())
            rc.stopped(f"unexpected error: {type(e).__name__}: {str(e)[:200]}")
        finally:
            http.close()
            run.finished_at = utcnow()
            rc._save(force=True)
            self.cancels.pop(run.id, None)

    def shutdown(self) -> None:
        for ev in self.cancels.values():
            ev.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
