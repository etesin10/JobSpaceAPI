"""Process-wide application context shared by the HTTP API, the MCP server and the CLI."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from jobfinder.config import ConfigManager, Connections
from jobfinder.llm import LLM, Budget
from jobfinder.services.http import Http
from jobfinder.settings import Workspace, default_exports_dir, get_workspace
from jobfinder.store import Store


class App:
    def __init__(self, ws: Workspace | None = None, watch_config: bool = False):
        self.ws = (ws or get_workspace()).ensure()
        _setup_logging(self.ws)
        from jobfinder.services.http import fix_broken_ipv6, use_os_trust_store

        use_os_trust_store()
        fix_broken_ipv6()
        self.config = ConfigManager(self.ws.connections_file, self.ws.env_file, self.ws.steps_file)
        if watch_config:
            self.config.start_watching()
        self.store = Store(self.ws.db_path)
        self.llm = LLM(lambda: self.config.current)
        from jobfinder.services.runs import Runner

        self.runner = Runner(self)

    @property
    def conn(self) -> Connections:
        return self.config.current

    def http(self) -> Http:
        lim = self.conn.limits
        return Http(lim.user_agent, lim.http_timeout_s, lim.per_domain_rps, lim.respect_robots, self.store,
                    lim.cache_ttl_hours * 3600)

    def budget(self, overrides: dict | None = None) -> Budget:
        b = self.conn.limits.run_budget.model_dump()
        b.update({k: v for k, v in (overrides or {}).items() if k in b and v})
        return Budget(**b)

    def exports_dir(self):
        from pathlib import Path

        d = Path(self.conn.export.dir).expanduser() if self.conn.export.dir else default_exports_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def close(self) -> None:
        self.config.stop_watching()
        self.runner.shutdown()
        self.store.close()


_configured = False


def _setup_logging(ws: Workspace) -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger("jobfinder")
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(ws.logs_dir / "jobfinder.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    _configured = True
