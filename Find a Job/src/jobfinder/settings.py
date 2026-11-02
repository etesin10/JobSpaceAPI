"""Workspace paths and process-wide runtime settings.

A *workspace* is a folder holding everything user-specific: config, secrets (.env), the SQLite
database, logs and runtime state. It lives outside cloud-synced folders by default because
SQLite and OneDrive/iCloud sync do not mix.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_dir, user_documents_dir

APP_NAME = "JobFinder"
DEFAULT_PORT = 8765
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent.parent


def _default_home() -> Path:
    return Path(user_data_dir(APP_NAME, appauthor=False, roaming=False)) / "workspace"


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def connections_file(self) -> Path:
        return self.config_dir / "connections.yaml"

    @property
    def steps_file(self) -> Path:
        return self.config_dir / "steps.yaml"

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobfinder.db"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def token_file(self) -> Path:
        return self.state_dir / "api_token"

    @property
    def server_file(self) -> Path:
        return self.state_dir / "server.json"

    def ensure(self) -> Workspace:
        for d in (self.config_dir, self.data_dir, self.logs_dir, self.state_dir, self.uploads_dir):
            d.mkdir(parents=True, exist_ok=True)
        if not self.connections_file.exists():
            from jobfinder.config import write_default_connections

            write_default_connections(self.connections_file)
        if not self.steps_file.exists():
            from jobfinder.steps import default_steps_text

            self.steps_file.write_text(default_steps_text("auto"), encoding="utf-8")
        if not self.env_file.exists():
            tpl = PACKAGE_DIR / "data" / "env.template"
            self.env_file.write_text(tpl.read_text(encoding="utf-8"), encoding="utf-8")
        return self

    def api_token(self) -> str:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.token_file.exists():
            tok = self.token_file.read_text(encoding="utf-8").strip()
            if tok:
                return tok
        tok = secrets.token_urlsafe(32)
        self.token_file.write_text(tok, encoding="utf-8")
        return tok


def default_exports_dir() -> Path:
    return Path(user_documents_dir()) / "JobFinder Exports"


_current: Workspace | None = None


def get_workspace() -> Workspace:
    global _current
    if _current is None:
        home = os.environ.get("JOBFINDER_HOME")
        _current = Workspace(Path(home).expanduser().resolve() if home else _default_home())
    return _current


def set_workspace(path: Path | str) -> Workspace:
    global _current
    _current = Workspace(Path(path).expanduser().resolve())
    return _current
