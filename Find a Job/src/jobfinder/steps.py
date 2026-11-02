"""steps.yaml: which AI model does each step of the job search.

Lives next to connections.yaml in the workspace config folder and is hot-reloaded the same way. Each step lists
models in order of preference; the first one that is configured (key present / local server running and the
model pulled) answers, the rest are fallbacks. Optional per-step overrides: temperature, max_tokens, timeout_s,
enabled (false = the step runs without AI, using JobFinder's built-in rules).
"""

from __future__ import annotations

import json
from typing import Any

STEP_INFO: dict[str, dict[str, str]] = {
    "extract_skills": {
        "what": "Read a resume (PDF/DOCX/text) and list skills with level and years.",
        "needs": "careful reading of long, multilingual text (up to ~25,000 characters)",
        "local": "Gemma 4 – strong multilingual reading and extraction. Qwen Flash is a fast fallback.",
    },
    "plan_queries": {
        "what": "Write job-search queries in the local language (Spanish for Mexico, French for Quebec…).",
        "needs": "short output, good with languages",
        "local": "Qwen Flash or Gemma 4 – any small model works.",
    },
    "find_companies": {
        "what": "Pick real employers from web search results, or name employers when there is no web search.",
        "needs": "world knowledge; naming companies from memory needs a large model",
        "local": "Qwen Coder to pick from search results; prefer a cloud model when naming companies from memory.",
    },
    "extract_jobs": {
        "what": "Read a company careers page that has no structured data and list its job postings.",
        "needs": "long, messy page text in, strict JSON out",
        "local": "Qwen Coder – best at turning messy pages into clean JSON.",
    },
    "review_role": {
        "what": "Review a posting against the candidate: required skills, gaps, fit and a summary for the resume.",
        "needs": "reasoning and comparison over a full job description",
        "local": "Qwen Coder or Gemma 4. A cloud model gives the most nuanced reviews.",
    },
    "find_contacts": {
        "what": "Choose recruiters / HR / hiring managers from public profile search results.",
        "needs": "simple classification",
        "local": "Qwen Flash – small and fast is enough.",
    },
    "guided": {
        "what": "Answer guided-mode questions (pick numbers, short lists) – the whole search with a small model.",
        "needs": "following a strict answer format",
        "local": "Qwen Flash – the smallest model that reliably follows formats.",
    },
    "web_search": {
        "what": "AI models with built-in web search, used as a search provider (with or instead of Tavily/Brave/SerpApi).",
        "needs": "live web access",
        "local": "Local models cannot browse the web: use openai_search, gemini_search or perplexity here.",
    },
    "default": {
        "what": "Anything not listed above.",
        "needs": "",
        "local": "Your best all-round local model.",
    },
}
STEPS = list(STEP_INFO)

DEFAULT_LOCAL_MODELS: dict[str, dict[str, Any]] = {
    "gemma": {"provider": "ollama", "model": "${LOCAL_GEMMA_MODEL:-gemma4}",
              "api_base": "${OLLAMA_BASE_URL:-http://localhost:11434}", "timeout_s": 300},
    "qwen_coder": {"provider": "ollama", "model": "${LOCAL_QWEN_CODER_MODEL:-qwen3.8-coder-next}",
                   "api_base": "${OLLAMA_BASE_URL:-http://localhost:11434}", "timeout_s": 300},
    "qwen_flash": {"provider": "ollama", "model": "${LOCAL_QWEN_FLASH_MODEL:-qwen3.8-flash-next}",
                   "api_base": "${OLLAMA_BASE_URL:-http://localhost:11434}", "timeout_s": 180},
}
LOCAL_MODEL_NOTES = {
    "gemma": "Gemma 4: multilingual reading/extraction (resumes, postings in Spanish/French).",
    "qwen_coder": "Qwen 3.8 Coder Next: structured JSON from messy pages, reviewing postings, picking companies.",
    "qwen_flash": "Qwen 3.8 Flash Next: fast and small – guided questions, contacts, search queries.",
}

CLOUD = ["openai_mini", "claude", "gemini", "deepseek", "azure"]
SEARCH_MODELS = ["openai_search", "gemini_search", "perplexity"]
LOCAL_FIT: dict[str, list[str]] = {
    "extract_skills": ["gemma", "qwen_flash", "local", "lmstudio"],
    "plan_queries": ["qwen_flash", "gemma", "local", "lmstudio"],
    "find_companies": ["qwen_coder", "gemma", "local", "lmstudio"],
    "extract_jobs": ["qwen_coder", "gemma", "local", "lmstudio"],
    "review_role": ["qwen_coder", "gemma", "local", "lmstudio"],
    "find_contacts": ["qwen_flash", "gemma", "local", "lmstudio"],
    "guided": ["qwen_flash", "gemma", "local", "lmstudio"],
    "default": ["qwen_flash", "gemma", "qwen_coder", "local", "lmstudio"],
}
HYBRID_LOCAL_FIRST = {"extract_skills", "plan_queries", "extract_jobs", "review_role", "find_contacts", "guided"}
CLOUD_FIRST = {"cloud-openai": "openai_mini", "cloud-claude": "claude", "cloud-gemini": "gemini",
               "cloud-deepseek": "deepseek", "cloud-azure": "azure"}
PRESETS: dict[str, str] = {
    "auto": "Cloud models first (whichever key you added), local models as fallback.",
    "hybrid": "Local for reading/reviewing (Gemma, Qwen), cloud for finding companies and web search.",
    "local-first": "Local models first everywhere, cloud only as fallback.",
    "local-only": "Never call a cloud model (web search needs a Tavily/Brave/SerpApi key).",
    **{k: f"{v} first, other providers and local models as fallback." for k, v in CLOUD_FIRST.items()},
}


def preset_steps(name: str) -> dict[str, dict[str, Any]]:
    if name not in PRESETS:
        raise ValueError(f"unknown preset '{name}'; choose {list(PRESETS)}")
    cloud = CLOUD
    if name in CLOUD_FIRST:
        first = CLOUD_FIRST[name]
        cloud = [first, *[c for c in CLOUD if c != first]]
    steps: dict[str, dict[str, Any]] = {}
    for step in STEPS:
        if step == "web_search":
            order = [] if name == "local-only" else (
                ["gemini_search", "openai_search", "perplexity"] if name == "cloud-gemini" else SEARCH_MODELS)
            steps[step] = {"models": order}
            continue
        local = LOCAL_FIT[step]
        if name == "local-only":
            models = local
        elif name == "local-first" or (name == "hybrid" and step in HYBRID_LOCAL_FIRST):
            models = [*local, *cloud]
        else:
            models = [*cloud, *local]
        steps[step] = {"models": models}
        if step in ("extract_skills", "extract_jobs", "review_role"):
            steps[step]["temperature"] = 0.0
        if step == "extract_jobs":
            steps[step]["max_tokens"] = 4000
    return steps


HEADER = """\
# JobFinder steps file – which AI model does each step.
# ---------------------------------------------------------------------------
# Edit while the app runs: changes apply immediately (invalid files are rejected
# and the last good version keeps running). The dashboard (AI & Settings ->
# Steps) can edit this file too; saving there rewrites it with these comments.
#
# For each step, `models` is an ordered list: the first model that is ready
# answers, the rest are fallbacks. A cloud model is "ready" when its key is set
# in .env; a local model when Ollama/LM Studio is running and the model is
# pulled. Model names come from `models:` below or from llm.models in
# connections.yaml (openai_mini, claude, gemini, deepseek, azure, local,
# lmstudio, openai_search, gemini_search, perplexity).
#
# Optional per step: enabled (false = no AI, built-in rules only),
# temperature, max_tokens, timeout_s.
# ---------------------------------------------------------------------------
"""


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if v is None:
        return "null"
    return json.dumps(str(v), ensure_ascii=False)


def render(models: dict[str, dict[str, Any]], steps: dict[str, dict[str, Any]], preset: str | None = None) -> str:
    out = [HEADER, "version: 1"]
    if preset:
        out.append(f"preset: {preset}   # {PRESETS.get(preset, '')}")
    out += ["", "# Local models. Put the exact name from `ollama list` (or the model id shown in LM Studio, with",
            "# provider: lmstudio) in .env (LOCAL_GEMMA_MODEL, ...) or directly here. Add as many as you like.",
            "models:"]
    for name, spec in models.items():
        if name in LOCAL_MODEL_NOTES:
            out.append(f"  # {LOCAL_MODEL_NOTES[name]}")
        out.append(f"  {name}:")
        out += [f"    {k}: {_scalar(v)}" for k, v in spec.items()]
    out += ["", "steps:"]
    for step in [*STEPS, *[s for s in steps if s not in STEP_INFO]]:
        if step not in steps:
            continue
        cfg = steps[step]
        info = STEP_INFO.get(step)
        out.append("")
        out.append(f"  {step}:")
        if info:
            out.append(f"    # What: {info['what']}")
            if info["needs"]:
                out.append(f"    # Needs: {info['needs']}")
            out.append(f"    # Local fit: {info['local']}")
        out.append(f"    models: [{', '.join(cfg.get('models', []))}]")
        for k in ("enabled", "temperature", "max_tokens", "timeout_s"):
            if cfg.get(k) is not None:
                out.append(f"    {k}: {_scalar(cfg[k])}")
    return "\n".join(out) + "\n"


def default_steps_text(preset: str = "auto") -> str:
    return render(DEFAULT_LOCAL_MODELS, preset_steps(preset), preset)
