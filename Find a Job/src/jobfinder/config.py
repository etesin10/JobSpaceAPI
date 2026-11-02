"""connections.yaml: the single file that wires models, web search and job sources.

Edit the file while the app runs; it is re-validated and swapped in atomically. If the new
version is invalid, the last good configuration stays active and the error is reported at
GET /api/admin/connections.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger("jobfinder.config")

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

LLM_TASKS = (
    "default",
    "extract_skills",
    "plan_queries",
    "extract_jobs",
    "find_companies",
    "find_contacts",
    "review_role",
    "guided",
    "web_search",
)


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    provider: str = Field(description="openai | azure | ollama | lmstudio | deepseek | anthropic | gemini | openrouter | perplexity | openai_compatible | <any litellm prefix>")
    model: str
    api_base: str | None = None
    api_key: str | None = None
    api_version: str | None = None
    temperature: float = 0.1
    max_tokens: int | None = 2000
    timeout_s: float = 120
    web_search: bool = Field(False, description="Model can search the web itself (OpenAI search models, Gemini, Perplexity)")


class StepOptions(BaseModel):
    model_config = ConfigDict(extra="ignore")
    models: list[str] = Field(default_factory=list)
    enabled: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_s: float | None = None


class LLMConfig(BaseModel):
    models: dict[str, ModelSpec] = Field(default_factory=dict)
    routes: dict[str, list[str]] = Field(default_factory=dict)
    steps: dict[str, StepOptions] = Field(default_factory=dict, description="From steps.yaml")

    def route(self, task: str) -> list[str]:
        opt = self.steps.get(task)
        if opt is not None and not opt.enabled:
            return []
        names = (opt.models if opt and opt.models else None) or self.routes.get(task)
        if not names and task != "web_search":
            d = self.steps.get("default")
            names = (d.models if d and d.models else None) or self.routes.get("default") or list(self.models)
        return [n for n in (names or []) if n in self.models]

    def spec_for(self, name: str, task: str) -> ModelSpec:
        """Model spec with the step's temperature / max_tokens / timeout overrides applied."""
        spec = self.models[name]
        opt = self.steps.get(task)
        if not opt:
            return spec
        upd = {k: getattr(opt, k) for k in ("temperature", "max_tokens", "timeout_s") if getattr(opt, k) is not None}
        return spec.model_copy(update=upd) if upd else spec


class SearchConfig(BaseModel):
    web: list[str] = Field(default_factory=lambda: ["tavily", "brave", "serpapi", "ai"])
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: bool = True


class BudgetConfig(BaseModel):
    max_tokens: int = 200_000
    max_llm_calls: int = 60
    max_search_calls: int = 60
    max_pages: int = 200
    max_minutes: int = 30


class LimitsConfig(BaseModel):
    per_domain_rps: float = 1.0
    http_timeout_s: float = 25
    user_agent: str = "Mozilla/5.0 (compatible; JobFinder/0.1; personal job search)"
    respect_robots: bool = True
    cache_ttl_hours: float = 12
    run_budget: BudgetConfig = Field(default_factory=BudgetConfig)


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class ExportConfig(BaseModel):
    dir: str | None = None


class MatchingConfig(BaseModel):
    min_score: float = 50
    contacts_for_top_n_roles: int = 10


class Connections(BaseModel):
    version: int = 1
    llm: LLMConfig = Field(default_factory=LLMConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    sources: dict[str, SourceConfig] = Field(default_factory=dict)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)

    @field_validator("sources", mode="before")
    @classmethod
    def _sources_none(cls, v: Any) -> Any:
        return {k: (val or {}) for k, val in (v or {}).items()}

    def source(self, name: str) -> SourceConfig | None:
        s = self.sources.get(name)
        return s if s and s.enabled else None

    def search_provider(self, name: str) -> dict[str, Any] | None:
        p = self.search.providers.get(name)
        if p is None or p.get("enabled", True) is False:
            return None
        return p


def expand_env(obj: Any, env: dict[str, str]) -> Any:
    """Replace ${VAR} / ${VAR:-default} in all strings. Unset vars become empty -> None."""
    if isinstance(obj, dict):
        return {k: expand_env(v, env) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(v, env) for v in obj]
    if isinstance(obj, str):
        def sub(m: re.Match[str]) -> str:
            val = env.get(m.group(1)) or os.environ.get(m.group(1))
            return val if val else (m.group(2) or "")

        out = _ENV_RE.sub(sub, obj)
        return out if out != "" or obj == "" else None
    return obj


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def parse_connections(text: str, env: dict[str, str], steps_text: str | None = None) -> Connections:
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ValueError("connections.yaml must be a mapping at the top level")
    conn = Connections.model_validate(expand_env(raw, env))
    # Older connections.yaml files lack newer built-in models (claude, gemini_search, ...): add the defaults.
    for name, spec in _template_models(env).items():
        conn.llm.models.setdefault(name, spec)
    if steps_text:
        apply_steps(conn, parse_steps(steps_text, env))
    return conn


def _template_models(env: dict[str, str]) -> dict[str, ModelSpec]:
    raw = yaml.safe_load((Path(__file__).parent / "data" / "connections.template.yaml").read_text(encoding="utf-8")
                         .replace("__ROUTES__", "    default: []"))
    return {k: ModelSpec.model_validate(v) for k, v in expand_env(raw["llm"]["models"], env).items()}


def parse_steps(text: str, env: dict[str, str]) -> dict[str, Any]:
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ValueError("steps.yaml must be a mapping at the top level")
    raw = expand_env(raw, env)
    models = {k: ModelSpec.model_validate(v) for k, v in (raw.get("models") or {}).items()}
    steps = {k: StepOptions.model_validate(v or {}) for k, v in (raw.get("steps") or {}).items()}
    return {"models": models, "steps": steps, "preset": raw.get("preset")}


def apply_steps(conn: Connections, parsed: dict[str, Any]) -> None:
    conn.llm.models.update(parsed["models"])
    conn.llm.steps = parsed["steps"]
    unknown = {m for s in parsed["steps"].values() for m in s.models if m not in conn.llm.models}
    if unknown:
        raise ValueError(f"steps.yaml uses unknown model name(s): {sorted(unknown)}. Define them under `models:` "
                         "in steps.yaml or llm.models in connections.yaml.")


def default_connections_text(preset: str = "cloud-openai") -> str:
    tpl = (Path(__file__).parent / "data" / "connections.template.yaml").read_text(encoding="utf-8")
    routes = PRESETS.get(preset, PRESETS["cloud-openai"])
    return tpl.replace("__ROUTES__", routes)


def write_default_connections(path: Path, preset: str = "cloud-openai") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(default_connections_text(preset), encoding="utf-8")


def _cloud_preset(first: list[str]) -> str:
    """Preferred provider first; every other provider stays as a fallback (unconfigured ones are skipped)."""
    order = first + [m for m in ("openai_mini", "claude", "gemini", "deepseek", "azure") if m not in first]
    fast = ", ".join([*order, "local"])
    return "\n".join(f"    {task + ':':15} [{fast}]" for task in
                     ("default", "extract_skills", "plan_queries", "extract_jobs", "find_companies", "find_contacts"))


PRESETS: dict[str, str] = {
    "cloud-openai": _cloud_preset(["openai_mini"]),
    "cloud-claude": _cloud_preset(["claude"]),
    "cloud-gemini": _cloud_preset(["gemini"]),
    "cloud-deepseek": _cloud_preset(["deepseek"]),
    "cloud-azure": _cloud_preset(["azure"]),
    "local": """    default:        [local, lmstudio]
    extract_skills: [local, lmstudio]
    plan_queries:   [local, lmstudio]
    extract_jobs:   [local, lmstudio]
    find_companies: [local, lmstudio]
    find_contacts:  [local, lmstudio]""",
    "hybrid": """    default:        [openai_mini, claude, gemini, deepseek, local]
    extract_skills: [local, lmstudio, openai_mini, claude, gemini, deepseek]
    plan_queries:   [openai_mini, claude, gemini, deepseek, local]
    extract_jobs:   [local, lmstudio, openai_mini, claude, gemini, deepseek]
    find_companies: [openai_mini, claude, gemini, deepseek, local]
    find_contacts:  [openai_mini, claude, gemini, deepseek, local]""",
}


class ConfigManager:
    """Holds the active Connections and reloads it when connections.yaml, steps.yaml or .env change."""

    def __init__(self, connections_file: Path, env_file: Path, steps_file: Path | None = None):
        self.connections_file = connections_file
        self.env_file = env_file
        self.steps_file = steps_file
        self._lock = threading.RLock()
        self._current: Connections = Connections()
        self.env: dict[str, str] = {}
        self.last_error: str | None = None
        self.loaded_at: datetime | None = None
        self.steps_preset: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.reload()

    @property
    def current(self) -> Connections:
        with self._lock:
            return self._current

    def reload(self) -> bool:
        which = "connections.yaml"
        try:
            env = load_env_file(self.env_file)
            text = self.connections_file.read_text(encoding="utf-8") if self.connections_file.exists() else ""
            conn = parse_connections(text, env)
            preset = None
            if self.steps_file and self.steps_file.exists():
                which = "steps.yaml"
                parsed = parse_steps(self.steps_file.read_text(encoding="utf-8"), env)
                apply_steps(conn, parsed)
                preset = parsed["preset"]
        except Exception as e:  # keep last good config
            self.last_error = f"{which}: {type(e).__name__}: {e}"
            log.error("%s invalid, keeping previous config: %s", which, self.last_error)
            return False
        with self._lock:
            self._current = conn
            self.env = env
            self.last_error = None
            self.steps_preset = preset
            self.loaded_at = datetime.now(UTC)
        log.info("connections loaded from %s", self.connections_file)
        return True

    def start_watching(self) -> None:
        if self._thread:
            return

        def run() -> None:
            from watchfiles import watch

            files = [self.connections_file, self.env_file, *([self.steps_file] if self.steps_file else [])]
            targets = {f.resolve() for f in files}
            dirs = list(dict.fromkeys(f.parent for f in files))
            for changes in watch(*dirs, stop_event=self._stop, recursive=False, debounce=500):
                if any(Path(p).resolve() in targets for _, p in changes):
                    self.reload()

        self._thread = threading.Thread(target=run, name="config-watch", daemon=True)
        self._thread.start()

    def stop_watching(self) -> None:
        self._stop.set()

    def set_env_values(self, values: dict[str, str]) -> None:
        """Upsert KEY=VALUE pairs into .env (preserving other lines) and reload."""
        lines = self.env_file.read_text(encoding="utf-8").splitlines() if self.env_file.exists() else []
        pending = {k: v for k, v in values.items() if v is not None}
        out: list[str] = []
        for line in lines:
            key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
            if key and key in pending:
                out.append(f"{key}={_quote(pending.pop(key))}")
            else:
                out.append(line)
        for k, v in pending.items():
            out.append(f"{k}={_quote(v)}")
        self.env_file.write_text("\n".join(out) + "\n", encoding="utf-8")
        self.reload()

    def redacted(self) -> dict[str, Any]:
        return _redact(self.current.model_dump())


def _quote(v: str) -> str:
    return f'"{v}"' if any(c in v for c in " #'\"") else v


def _redact(obj: Any, key: str = "") -> Any:
    if isinstance(obj, dict):
        return {k: _redact(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    if isinstance(obj, str) and any(s in key.lower() for s in ("key", "token", "secret", "password")):
        return (obj[:4] + "…" + obj[-2:]) if len(obj) > 8 else "***"
    return obj
