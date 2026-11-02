"""Source plumbing: every connector returns (Company, Role) pairs in the shared contract."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from jobfinder.models import Company, Role, utcnow
from jobfinder.services.http import Http
from jobfinder.services.identity import normalize_domain


@dataclass
class Found:
    company: Company
    role: Role


@dataclass
class Ctx:
    http: Http
    cfg: dict[str, Any] = field(default_factory=dict)
    log: Callable[[str], None] = lambda m: None
    complete: bool = True  # board connectors set False when the listing was truncated or a page failed
    errors: list[str] = field(default_factory=list)  # non-fatal problems (a later page or one posting failed)
    url: str | None = None  # main URL read, for failure reports


class SourceError(Exception):
    """A source could not be read. http_status is set when the server answered with an error code."""

    def __init__(self, message: str, http_status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.url = url


def error_info(e: BaseException) -> tuple[int | None, str]:
    """(http_status, short human message) for any failure while reading the web."""
    import httpx

    from jobfinder.services.http import RobotsDenied

    if isinstance(e, SourceError):
        return e.http_status, str(e)
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        hint = {401: "login required", 403: "access blocked", 404: "not found", 410: "removed",
                429: "rate limited", 503: "site unavailable"}.get(code, "server error" if code >= 500 else "error")
        return code, f"HTTP {code} ({hint})"
    if isinstance(e, RobotsDenied):
        return None, "blocked by robots.txt"
    if isinstance(e, httpx.TimeoutException):
        return None, "timed out"
    if isinstance(e, httpx.ConnectError):
        return None, "could not connect"
    if isinstance(e, ValueError) and "JSON" in str(e):
        return None, "unexpected response (not JSON)"
    return None, f"{type(e).__name__}: {str(e)[:150]}"


def require_ok(r: Any, what: str) -> None:
    if r.status_code != 200:
        raise SourceError(f"{what}: HTTP {r.status_code}", r.status_code, str(r.url))


class Source:
    name: str = ""
    kind: str = "search"  # "search" (query-based) | "board" (per company ATS board)
    keyless: bool = True
    needs: tuple[str, ...] = ()
    countries: tuple[str, ...] | None = None  # None = any

    def configured(self, cfg: dict[str, Any] | None) -> bool:
        if cfg is None or cfg.get("enabled") is False:
            return False
        return all(cfg.get(k) for k in self.needs)

    def search(self, ctx: Ctx, query: str, location: str | None, country: str | None, limit: int) -> list[Found]:
        raise NotImplementedError

    def board(self, ctx: Ctx, board: str) -> list[Found]:
        raise NotImplementedError


def evidence(url: str, kind: str = "posting", title: str | None = None) -> dict[str, Any]:
    """Plain dict so the URL is validated together with the Role in make_role()."""
    return {"url": url, "kind": kind, "title": title, "fetched_at": utcnow(),
            "status": "ok" if kind in ("ats_api", "job_api") else "unverified"}


def make_role(**kw: Any) -> Role | None:
    """Build a Role; items with an invalid/missing URL are skipped instead of failing the whole source."""
    try:
        return Role(**kw)
    except ValidationError:
        return None


def company_from(name: str, website: str | None = None, **kw: Any) -> Company:
    domain = normalize_domain(website) if website else None
    if website and "://" not in website:
        website = "https://" + website
    return Company(name=name.strip() or "Unknown", domain=domain, website=website if domain else None, **kw)


def clip(text: str | None, n: int = 20000) -> str | None:
    return text[:n] if text else text
