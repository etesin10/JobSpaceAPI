"""Company websites: detect the ATS behind a careers page and read schema.org JobPosting JSON-LD."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from jobfinder.models import AtsRef, Company, Place, Role, slugify
from jobfinder.services.identity import normalize_domain
from jobfinder.services.normalize import html_to_text, parse_datetime, parse_location

from .ats import _employment, _salary
from .base import (
    Ctx,
    Found,
    Source,
    SourceError,
    clip,
    company_from,
    error_info,
    evidence,
    make_role,
    require_ok,
)

ATS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board/js\?for=([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)")),
    ("workday", re.compile(r"([a-z0-9-]+\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")),
]
_SKIP_BOARDS = {"embed", "v1", "jobs", "job", "api", "js", "en-us", "careers", "search"}
CAREER_PATHS = ("/careers", "/jobs", "/careers/", "/en/careers", "/join-us", "/work-with-us", "/empleos", "/carreras",
                "/bolsa-de-trabajo", "/trabaja-con-nosotros", "/carrieres", "/emplois")


def detect_ats(text: str) -> AtsRef | None:
    for kind, rx in ATS_PATTERNS:
        for m in rx.finditer(text):
            if kind == "workday":
                return AtsRef(type="workday", board=f"{m.group(1)}|{m.group(2)}", url=f"https://{m.group(1)}/{m.group(2)}")
            board = m.group(1).strip("/.")
            if board.lower() in _SKIP_BOARDS:
                continue
            return AtsRef(type=kind, board=board)  # type: ignore[arg-type]
    return None


def probe_ats(ctx: Ctx, company: Company) -> AtsRef | None:
    """Guess board slugs from the company name/domain and test the keyless ATS APIs."""
    cands: list[str] = []
    if company.domain:
        cands.append(company.domain.split(".")[0])
    cands += [slugify(company.name).replace("-", ""), slugify(company.name)]
    cands = list(dict.fromkeys(c for c in cands if c and len(c) > 1))[:3]
    for slug in cands:
        tests = [
            ("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{slug}"),
            ("lever", f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1"),
            ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{slug}"),
        ]
        for kind, url in tests:
            try:
                r = ctx.http.get(url)
            except Exception:
                continue
            if r.status_code == 200 and r.content and r.content.strip() not in (b"[]", b"{}"):
                if kind == "greenhouse" and company.name and "name" in r.text:
                    gname = r.json().get("name", "")
                    from rapidfuzz import fuzz

                    if gname and fuzz.token_set_ratio(gname.lower(), company.name.lower()) < 70:
                        continue
                return AtsRef(type=kind, board=slug)  # type: ignore[arg-type]
    return None


def _iter_jsonld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                if "@graph" in d:
                    stack.extend(d["@graph"] if isinstance(d["@graph"], list) else [d["@graph"]])
                t = d.get("@type")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    out.append(d)
            elif isinstance(d, list):
                stack.extend(d)
    return out


def jobposting_to_role(d: dict[str, Any], page_url: str) -> tuple[Company | None, Role | None]:
    org = d.get("hiringOrganization") or {}
    if isinstance(org, str):
        org = {"name": org}
    locs = d.get("jobLocation") or []
    locs = locs if isinstance(locs, list) else [locs]
    places: list[Place] = []
    for loc in locs:
        addr = (loc or {}).get("address") or {}
        if isinstance(addr, str):
            places.append(parse_location(addr))
            continue
        country = addr.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        raw = ", ".join(str(x) for x in [addr.get("addressLocality"), addr.get("addressRegion"), country] if x)
        cc = country.upper() if isinstance(country, str) and len(country) == 2 else None
        places.append(parse_location(raw, cc))
    remote = "TELECOMMUTE" in str(d.get("jobLocationType", "")).upper()
    elig = []
    for req in (d.get("applicantLocationRequirements") or []) if isinstance(d.get("applicantLocationRequirements"), list) else [d.get("applicantLocationRequirements")]:
        if isinstance(req, dict) and req.get("name"):
            p = parse_location(req["name"])
            if p.country:
                elig.append(p.country)
    sal = None
    bs = d.get("baseSalary")
    if isinstance(bs, dict):
        v = bs.get("value") or {}
        if isinstance(v, dict):
            sal = _salary(v.get("minValue") or v.get("value"), v.get("maxValue"), bs.get("currency"), v.get("unitText"))
    url = d.get("url") or page_url
    if url and not url.startswith("http"):
        url = urljoin(page_url, url)
    emp = d.get("employmentType")
    ident = d.get("identifier")
    sid = ident.get("value") if isinstance(ident, dict) else (ident if isinstance(ident, str) else None)
    role = make_role(
        title=html_to_text(d.get("title")) or "?", url=url, description=clip(html_to_text(d.get("description"))),
        location=places[0] if places else Place(), locations=places[1:], work_mode="remote" if remote else None,
        remote_eligibility=elig, salary=sal, employment_type=_employment(" ".join(emp) if isinstance(emp, list) else emp),
        posted_at=parse_datetime(d.get("datePosted")), source="careers_page", source_id=sid and f"{normalize_domain(url)}:{sid}",
        evidence=[evidence(url, "posting", d.get("title"))],
    )
    comp = None
    if org.get("name"):
        comp = company_from(org["name"], org.get("sameAs") or org.get("url"))
    return comp, role


class CareersPage(Source):
    name, kind = "careers_page", "board"

    def board(self, ctx: Ctx, board: str) -> list[Found]:
        """board = a careers page URL. Follows up to N job links on the same site looking for JSON-LD."""
        ctx.url = board
        r = ctx.http.get(board, check_robots=True)  # RobotsDenied / HTTP errors are recorded by the caller
        require_ok(r, "careers page")
        if "html" not in r.headers.get("content-type", "html"):
            raise SourceError(f"careers page is not HTML ({r.headers.get('content-type')})", None, board)
        soup = BeautifulSoup(r.text, "html.parser")
        out: list[Found] = []
        for d in _iter_jsonld(soup):
            comp, role = jobposting_to_role(d, str(r.url))
            if role:
                out.append(Found(comp or company_from(normalize_domain(board) or "Unknown", board), role))
        if out:
            return out
        dom = normalize_domain(str(r.url))
        links = []
        for a in soup.find_all("a", href=True):
            href = urljoin(str(r.url), a["href"])
            if normalize_domain(href) == dom and re.search(r"/(job|jobs|careers|position|vacante|empleo|posting)s?/[^/]+", href, re.I):
                links.append(href)
        for href in list(dict.fromkeys(links))[: int(ctx.cfg.get("max_pages", 25))]:
            try:
                rr = ctx.http.get(href, check_robots=True)
            except Exception as e:
                ctx.errors.append(f"{href}: {error_info(e)[1]}")
                continue
            if rr.status_code != 200:
                ctx.errors.append(f"{href}: HTTP {rr.status_code}")
                continue
            for d in _iter_jsonld(BeautifulSoup(rr.text, "html.parser")):
                comp, role = jobposting_to_role(d, href)
                if role:
                    out.append(Found(comp or company_from(dom or "Unknown", board), role))
        return out


def discover_careers(ctx: Ctx, company: Company) -> tuple[str | None, AtsRef | None]:
    """Find the careers URL and ATS for a company website."""
    if not company.domain and not company.website:
        return None, None
    base = company.website or f"https://{company.domain}"
    if not base.startswith("http"):
        base = "https://" + base
    pages = [company.careers_url] if company.careers_url else []
    pages += [base] + [base.rstrip("/") + p for p in CAREER_PATHS[:6]]
    for url in pages:
        try:
            r = ctx.http.get(url, check_robots=True)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        ats = detect_ats(r.text) or detect_ats(str(r.url))
        if ats:
            return str(r.url), ats
        if url != base and re.search(r"job|career|empleo|vacante|carri", str(r.url), re.I):
            return str(r.url), None
        if url == base:
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                txt = (a.get_text() or "").lower()
                if re.search(r"career|jobs|join us|empleo|vacante|bolsa de trabajo|carri|trabaja con", txt + " " + a["href"].lower()):
                    href = urljoin(str(r.url), a["href"])
                    ats = detect_ats(href)
                    if ats:
                        return href, ats
                    pages.insert(pages.index(url) + 1, href)
                    break
    return None, None
