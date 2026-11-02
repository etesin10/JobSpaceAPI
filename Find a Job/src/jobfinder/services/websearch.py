"""Web search providers used for company discovery and contact lookup. First configured provider wins."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from jobfinder.config import Connections
from jobfinder.services.http import Http


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str
    provider: str

    def dict(self) -> dict[str, Any]:
        return asdict(self)


class NoSearchProvider(RuntimeError):
    pass


def _tavily(http: Http, key: str, q: str, n: int, country: str | None) -> list[SearchHit]:
    body: dict[str, Any] = {"query": q, "max_results": min(n, 20), "search_depth": "basic"}
    data = http.post_json("https://api.tavily.com/search", body, headers={"Authorization": f"Bearer {key}"})
    return [SearchHit(r.get("title", ""), r["url"], r.get("content", ""), "tavily") for r in data.get("results", [])]


def _brave(http: Http, key: str, q: str, n: int, country: str | None) -> list[SearchHit]:
    params: dict[str, Any] = {"q": q, "count": min(n, 20)}
    if country:
        params["country"] = country.upper()
    data = http.get_json("https://api.search.brave.com/res/v1/web/search", params=params,
                         headers={"X-Subscription-Token": key, "Accept": "application/json"})
    return [SearchHit(r.get("title", ""), r["url"], r.get("description", ""), "brave")
            for r in (data.get("web") or {}).get("results", [])]


def _serpapi(http: Http, key: str, q: str, n: int, country: str | None) -> list[SearchHit]:
    params: dict[str, Any] = {"engine": "google", "q": q, "num": min(n, 20), "api_key": key}
    if country:
        params["gl"] = country.lower()
    data = http.get_json("https://serpapi.com/search.json", params=params)
    return [SearchHit(r.get("title", ""), r["link"], r.get("snippet", ""), "serpapi") for r in data.get("organic_results", [])]


PROVIDERS = {"tavily": _tavily, "brave": _brave, "serpapi": _serpapi}

AI_SEARCH_PROMPT = (
    "Search the web for: {query}\n{where}"
    "Return JSON {{\"results\": [{{\"title\": str, \"url\": str, \"snippet\": str}}]}} with up to {n} results. "
    "Only include pages you actually found in this search; never guess URLs."
)


def _ai_router(conn: Connections) -> Any:
    from jobfinder.llm import LLM

    return LLM(lambda: conn)


def _cited_urls(resp: Any) -> dict[str, str]:
    """URLs the provider says it actually visited: OpenAI url_citation annotations, Perplexity citations /
    search_results, Gemini grounding metadata."""
    urls: dict[str, str] = {}
    msg = resp.choices[0].message
    for a in getattr(msg, "annotations", None) or []:
        a = a if isinstance(a, dict) else getattr(a, "model_dump", lambda: {})()
        c = a.get("url_citation") or a
        if c.get("url"):
            urls[c["url"]] = c.get("title") or ""
    for u in getattr(resp, "citations", None) or []:
        if isinstance(u, str):
            urls.setdefault(u, "")
    for r in getattr(resp, "search_results", None) or []:
        if isinstance(r, dict) and r.get("url"):
            urls.setdefault(r["url"], r.get("title") or "")
    for meta in getattr(resp, "vertex_ai_grounding_metadata", None) or []:
        for chunk in (meta or {}).get("groundingChunks", []) or []:
            web = chunk.get("web") or {}
            if web.get("uri"):
                urls.setdefault(web["uri"], web.get("title") or "")
    return urls


def _ai_search(conn: Connections, query: str, n: int, country: str | None) -> list[SearchHit]:
    from jobfinder.llm import parse_json

    llm = _ai_router(conn)
    where = f"Focus on results for country code {country}.\n" if country else ""
    resp, model = llm.chat("web_search", [{"role": "user", "content": AI_SEARCH_PROMPT.format(query=query, where=where, n=n)}],
                           raw=True)
    cited = _cited_urls(resp)
    text = resp.choices[0].message.content or ""
    hits: list[SearchHit] = []
    try:
        data = parse_json(text)
        items = data.get("results", []) if isinstance(data, dict) else data
    except ValueError:
        items = []
    for it in items if isinstance(items, list) else []:
        url = str((it or {}).get("url") or "")
        if not url.startswith("http"):
            continue
        if cited and url not in cited:
            continue  # the model listed a URL its own search did not return
        hits.append(SearchHit(it.get("title") or cited.get(url, ""), url, it.get("snippet") or "", f"ai:{model}"))
    for url, title in cited.items():
        if all(h.url != url for h in hits):
            hits.append(SearchHit(title, url, "", f"ai:{model}"))
    return hits[:n]


def ai_search_available(conn: Connections) -> bool:
    try:
        return _ai_router(conn).available("web_search")
    except Exception:
        return False


def available(conn: Connections) -> list[str]:
    out = []
    for name in conn.search.web:
        if name == "ai":
            if ai_search_available(conn):
                out.append(name)
            continue
        p = conn.search_provider(name)
        if p and p.get("api_key") and name in PROVIDERS:
            out.append(name)
    return out


def web_search(conn: Connections, http: Http, query: str, n: int = 10, country: str | None = None) -> list[SearchHit]:
    errors = []
    last: Exception | None = None
    for name in available(conn):
        try:
            if name == "ai":
                return _ai_search(conn, query, n, country)
            return PROVIDERS[name](http, conn.search_provider(name)["api_key"], query, n, country)  # type: ignore[index]
        except Exception as e:
            errors.append(f"{name}: {e}")
            last = e
    if errors:
        raise RuntimeError("; ".join(errors)) from last
    raise NoSearchProvider("No web search provider configured. Add TAVILY_API_KEY, BRAVE_API_KEY or SERPAPI_API_KEY "
                           "in Settings, or an OpenAI / Gemini / Perplexity key for the web_search step.")
