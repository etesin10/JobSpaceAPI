"""LLM routing: task -> ordered models from connections.yaml, with fallback, JSON parsing and budgets."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from jobfinder.config import Connections, ModelSpec

log = logging.getLogger("jobfinder.llm")

KEYED = {"openai", "azure", "deepseek", "anthropic", "gemini", "openrouter", "groq", "mistral", "xai", "together_ai",
         "fireworks_ai", "cohere", "perplexity"}
LOCAL_DEFAULT_BASE = {"ollama": "http://localhost:11434", "lmstudio": "http://localhost:1234/v1"}


class LLMUnavailable(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    max_tokens: int = 200_000
    max_llm_calls: int = 60
    max_search_calls: int = 60
    max_pages: int = 200
    max_minutes: float = 30
    tokens: int = 0
    llm_calls: int = 0
    search_calls: int = 0
    pages: int = 0
    started: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def charge(self, *, tokens: int = 0, llm: int = 0, search: int = 0, pages: int = 0) -> None:
        with self._lock:
            self.tokens += tokens
            self.llm_calls += llm
            self.search_calls += search
            self.pages += pages

    def check(self, kind: str = "any") -> None:
        if (time.monotonic() - self.started) / 60 > self.max_minutes:
            raise BudgetExceeded("time budget exhausted")
        if kind in ("llm", "any") and (self.tokens >= self.max_tokens or self.llm_calls >= self.max_llm_calls):
            raise BudgetExceeded("LLM budget exhausted")
        if kind in ("search", "any") and self.search_calls >= self.max_search_calls:
            raise BudgetExceeded("search budget exhausted")
        if kind in ("pages", "any") and self.pages >= self.max_pages:
            raise BudgetExceeded("page budget exhausted")

    def snapshot(self) -> dict[str, Any]:
        return {"tokens": self.tokens, "llm_calls": self.llm_calls, "search_calls": self.search_calls,
                "pages": self.pages, "minutes": round((time.monotonic() - self.started) / 60, 2)}


_LOCAL_CACHE: dict[str, tuple[float, set[str] | None]] = {}


def _local_models(base: str) -> set[str] | None:
    """Model names served by a local Ollama / LM Studio server; None when it is not running. Cached for 60 s."""
    now = time.monotonic()
    hit = _LOCAL_CACHE.get(base)
    if hit and now - hit[0] < 60:
        return hit[1]
    import httpx

    root = base.rstrip("/")
    names: set[str] | None
    try:
        if root.endswith("/v1"):
            data = httpx.get(root + "/models", timeout=1.5).json()
            names = {m.get("id", "") for m in data.get("data", [])}
        else:
            data = httpx.get(root + "/api/tags", timeout=1.5).json()
            names = {m.get("name", "") for m in data.get("models", [])}
    except Exception:
        names = None
    _LOCAL_CACHE[base] = (now, names)
    return names


def local_model_present(model: str, served: set[str]) -> bool:
    """Ollama tags: 'gemma4' matches 'gemma4:latest'; 'qwen3' matches any 'qwen3:*' when no tag was given."""
    m = model.lower()
    names = {s.lower() for s in served}
    if m in names or f"{m}:latest" in names:
        return True
    return ":" not in m and any(n.split(":")[0] == m for n in names)


def is_configured(spec: ModelSpec) -> bool:
    if spec.provider in KEYED:
        return bool(spec.api_key) and (spec.provider != "azure" or bool(spec.api_base))
    if spec.provider == "openai_compatible":
        return bool(spec.api_base)
    if spec.provider in LOCAL_DEFAULT_BASE:
        served = _local_models(spec.api_base or LOCAL_DEFAULT_BASE[spec.provider])
        if served is None:
            return False
        # LM Studio answers with whatever model is loaded when the id is a placeholder.
        return not served or spec.provider == "lmstudio" and spec.model in ("local-model", "") \
            or local_model_present(spec.model, served)
    return True


def search_kwargs(spec: ModelSpec) -> dict[str, Any]:
    """Provider-specific switches that turn on built-in web search."""
    if spec.provider == "gemini":
        return {"tools": [{"googleSearch": {}}]}
    if spec.provider in ("openai", "azure") and "search" in spec.model:
        return {"web_search_options": {"search_context_size": "low"}}
    return {}


def litellm_kwargs(spec: ModelSpec) -> dict[str, Any]:
    p = spec.provider
    kw: dict[str, Any] = {"temperature": spec.temperature, "timeout": spec.timeout_s}
    if spec.max_tokens:
        kw["max_tokens"] = spec.max_tokens
    if p == "ollama":
        kw["model"] = f"ollama_chat/{spec.model}"
        kw["api_base"] = spec.api_base or LOCAL_DEFAULT_BASE["ollama"]
    elif p == "lmstudio":
        kw["model"] = f"openai/{spec.model}"
        kw["api_base"] = spec.api_base or LOCAL_DEFAULT_BASE["lmstudio"]
        kw["api_key"] = spec.api_key or "lm-studio"
    elif p == "openai_compatible":
        kw["model"] = f"openai/{spec.model}"
        kw["api_base"] = spec.api_base
        kw["api_key"] = spec.api_key or "none"
    else:
        kw["model"] = spec.model if spec.model.startswith(f"{p}/") else f"{p}/{spec.model}"
        if spec.api_base:
            kw["api_base"] = spec.api_base
        if spec.api_key:
            kw["api_key"] = spec.api_key
        if spec.api_version:
            kw["api_version"] = spec.api_version
    return kw


def parse_json(text: str) -> Any:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for open_, close in (("{", "}"), ("[", "]")):
        s, e = t.find(open_), t.rfind(close)
        if s != -1 and e > s:
            try:
                return json.loads(t[s:e + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("model did not return valid JSON")


_litellm = None


def _lib() -> Any:
    global _litellm
    if _litellm is None:
        import os

        os.environ.setdefault("LITELLM_LOG", "ERROR")
        import litellm

        litellm.drop_params = True
        litellm.telemetry = False
        litellm.suppress_debug_info = True
        _litellm = litellm
    return _litellm


class LLM:
    def __init__(self, conn_getter: Any):
        self._get = conn_getter  # callable -> Connections (hot reloaded)

    @property
    def conn(self) -> Connections:
        return self._get()

    def models_for(self, task: str) -> list[tuple[str, ModelSpec]]:
        c = self.conn.llm
        out = []
        for n in c.route(task):
            spec = c.spec_for(n, task)
            if task == "web_search" and not spec.web_search:
                continue
            if is_configured(spec):
                out.append((n, spec))
        return out

    def available(self, task: str = "default") -> bool:
        return bool(self.models_for(task))

    def chat(self, task: str, messages: list[dict[str, str]], *, json_mode: bool = False,
             budget: Budget | None = None, extra: dict[str, Any] | None = None,
             raw: bool = False) -> tuple[Any, str]:
        """Returns (text, model_name) – or (response, model_name) with raw=True. Tries each model on the step's
        list (steps.yaml) until one answers."""
        models = self.models_for(task)
        if not models:
            raise LLMUnavailable(f"No ready model for step '{task}'. Add an API key or start Ollama and pull the model, "
                                 f"then check steps.yaml (AI & Settings > Steps).")
        errors = []
        for name, spec in models:
            if budget:
                budget.check("llm")
            kw = litellm_kwargs(spec)
            if task == "web_search":
                kw.update(search_kwargs(spec))
            if extra:
                kw.update(extra)
            if json_mode:
                kw["response_format"] = {"type": "json_object"}
            for attempt in range(2):
                try:
                    resp = _lib().completion(messages=messages, **kw)
                    text = resp.choices[0].message.content or ""
                    usage = getattr(resp, "usage", None)
                    if budget:
                        budget.charge(tokens=int(getattr(usage, "total_tokens", 0) or 0), llm=1)
                    return (resp if raw else text), name
                except Exception as e:
                    msg = f"{type(e).__name__}: {str(e)[:300]}"
                    if json_mode and attempt == 0 and "response_format" in str(e).lower():
                        kw.pop("response_format", None)
                        continue
                    errors.append(f"{name}: {msg}")
                    log.warning("model %s failed for %s: %s", name, task, msg)
                    break
        raise LLMUnavailable("All models failed: " + " | ".join(errors))

    def json(self, task: str, system: str, user: str, *, budget: Budget | None = None) -> tuple[Any, str]:
        messages = [{"role": "system", "content": system + "\nRespond with a single JSON object only."},
                    {"role": "user", "content": user}]
        text, model = self.chat(task, messages, json_mode=True, budget=budget)
        try:
            return parse_json(text), model
        except ValueError:
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "That was not valid JSON. Reply again with only the JSON object."}]
            text, model = self.chat(task, messages, json_mode=True, budget=budget)
            return parse_json(text), model

    def ping(self, name: str) -> dict[str, Any]:
        spec = self.conn.llm.models.get(name)
        if not spec:
            return {"model": name, "ok": False, "error": "not defined"}
        if not is_configured(spec):
            local = spec.provider in LOCAL_DEFAULT_BASE
            return {"model": name, "ok": False,
                    "error": f"model '{spec.model}' not pulled or server not running" if local else "missing api key / api_base"}
        t0 = time.monotonic()
        try:
            kw = litellm_kwargs(spec)
            kw["max_tokens"] = 5
            kw["timeout"] = min(spec.timeout_s, 30)
            _lib().completion(messages=[{"role": "user", "content": "Reply with OK"}], **kw)
            return {"model": name, "ok": True, "ms": int((time.monotonic() - t0) * 1000)}
        except Exception as e:
            return {"model": name, "ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
