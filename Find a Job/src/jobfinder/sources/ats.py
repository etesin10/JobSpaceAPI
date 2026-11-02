"""Company ATS boards with public JSON endpoints (no keys, employer-published data)."""

from __future__ import annotations

import re
from typing import Any

from jobfinder.models import AtsRef, Place, Role, Salary
from jobfinder.services.normalize import html_to_text, parse_datetime, parse_location

from .base import Ctx, Found, Source, SourceError, clip, company_from, evidence, make_role, require_ok

_ASHBY_EMP = {"FullTime": "full_time", "PartTime": "part_time", "Intern": "internship", "Contract": "contract",
              "Temporary": "temporary"}
_MODE = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite", "in office": "onsite",
         "inoffice": "onsite"}


def _mode(v: Any) -> str | None:
    return _MODE.get(str(v or "").lower().replace("_", "").replace(" ", "")) or _MODE.get(str(v or "").lower())


def _place_from_parts(city: str | None, region: str | None, country: str | None, raw: str | None) -> Place:
    from jobfinder.models.common import _countries

    cc = (country or "").upper() if country and len(country) == 2 and country.upper() in _countries() else None
    p = parse_location(raw or ", ".join(filter(None, [city, region, country])), cc)
    if city and not p.city:
        p.city = city
    return p


class Greenhouse(Source):
    name, kind = "greenhouse", "board"

    def board(self, ctx: Ctx, board: str) -> list[Found]:
        base = f"https://boards-api.greenhouse.io/v1/boards/{board}"
        meta = ctx.http.get_json(base)
        data = ctx.http.get_json(f"{base}/jobs", params={"content": "true"})
        comp = company_from(meta.get("name") or board, None, ats=AtsRef(type="greenhouse", board=board,
                                                                           url=f"https://boards.greenhouse.io/{board}"))
        out = []
        for j in data.get("jobs", []):
            loc_raw = (j.get("location") or {}).get("name")
            role = make_role(
                title=j["title"], url=j["absolute_url"], description=clip(html_to_text(j.get("content"))),
                location=parse_location(loc_raw), posted_at=parse_datetime(j.get("first_published") or j.get("updated_at")),
                department=", ".join(d["name"] for d in j.get("departments", []) if d.get("name")) or None,
                source="greenhouse", source_id=f"{board}:{j['id']}",
                evidence=[evidence(f"{base}/jobs/{j['id']}", "ats_api", j["title"])],
            )
            if role:
                out.append(Found(comp, role))
        return out


class Lever(Source):
    name, kind = "lever", "board"

    def board(self, ctx: Ctx, board: str) -> list[Found]:
        data, status = None, None
        for host in ("api.lever.co", "api.eu.lever.co"):
            r = ctx.http.get(f"https://{host}/v0/postings/{board}", params={"mode": "json"})
            if r.status_code == 200:
                data = r.json()
                break
            status = status if status and status != 404 else r.status_code
        if data is None:
            raise SourceError(f"Lever board '{board}' not readable: HTTP {status}", status,
                              f"https://jobs.lever.co/{board}")
        comp = company_from(board.replace("-", " ").title(), None,
                            ats=AtsRef(type="lever", board=board, url=f"https://jobs.lever.co/{board}"))
        out = []
        for j in data:
            cats = j.get("categories") or {}
            sal = j.get("salaryRange") or {}
            desc = j.get("descriptionPlain") or html_to_text(j.get("description"))
            for lst in j.get("lists", []):
                desc = (desc or "") + f"\n\n{lst.get('text', '')}\n" + (html_to_text(lst.get("content")) or "")
            if j.get("additionalPlain"):
                desc = (desc or "") + "\n\n" + j["additionalPlain"]
            loc = _place_from_parts(None, None, j.get("country"), cats.get("location"))
            extra = [parse_location(x, loc.country) for x in (cats.get("allLocations") or [])[1:]]
            role = make_role(
                title=j["text"], url=j["hostedUrl"], apply_url=j.get("applyUrl"), description=clip(desc),
                location=loc, locations=extra, work_mode=_mode(j.get("workplaceType")),  # type: ignore[arg-type]
                employment_type=_employment(cats.get("commitment")), department=cats.get("department") or cats.get("team"),
                salary=_salary(sal.get("min"), sal.get("max"), sal.get("currency"), sal.get("interval")),
                posted_at=parse_datetime(j.get("createdAt")), source="lever", source_id=f"{board}:{j['id']}",
                evidence=[evidence(j["hostedUrl"], "ats_api", j["text"])],
            )
            if role:
                out.append(Found(comp, role))
        return out


class Ashby(Source):
    name, kind = "ashby", "board"

    def board(self, ctx: Ctx, board: str) -> list[Found]:
        r = ctx.http.get(f"https://api.ashbyhq.com/posting-api/job-board/{board}", params={"includeCompensation": "true"})
        require_ok(r, f"Ashby board '{board}'")
        data = r.json()
        comp = company_from(board.replace("-", " ").title(), None,
                            ats=AtsRef(type="ashby", board=board, url=f"https://jobs.ashbyhq.com/{board}"))
        out = []
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            addr = ((j.get("address") or {}).get("postalAddress")) or {}
            loc = _place_from_parts(addr.get("addressLocality"), addr.get("addressRegion"), None,
                                    ", ".join(filter(None, [addr.get("addressLocality"), addr.get("addressRegion"),
                                                            addr.get("addressCountry")])) or j.get("location"))
            extra = [parse_location(s.get("location")) for s in j.get("secondaryLocations", []) if s.get("location")]
            mode = _mode(j.get("workplaceType")) or ("remote" if j.get("isRemote") else None)
            role = make_role(
                title=j["title"], url=j.get("jobUrl") or f"https://jobs.ashbyhq.com/{board}/{j['id']}",
                apply_url=j.get("applyUrl"), description=clip(j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))),
                location=loc, locations=extra, work_mode=mode,  # type: ignore[arg-type]
                employment_type=_ASHBY_EMP.get(j.get("employmentType") or ""),  # type: ignore[arg-type]
                department=j.get("department") or j.get("team"), posted_at=parse_datetime(j.get("publishedAt")),
                salary=_ashby_comp(j.get("compensation")), source="ashby", source_id=f"{board}:{j['id']}",
                evidence=[evidence(j.get("jobUrl") or f"https://jobs.ashbyhq.com/{board}", "ats_api", j["title"])],
            )
            if role:
                out.append(Found(comp, role))
        return out


class SmartRecruiters(Source):
    name, kind = "smartrecruiters", "board"

    def board(self, ctx: Ctx, board: str) -> list[Found]:
        out: list[Found] = []
        offset = 0
        comp = None
        ctx.complete = False
        while offset < 1000:
            r = ctx.http.get(f"https://api.smartrecruiters.com/v1/companies/{board}/postings",
                             params={"limit": 100, "offset": offset})
            if r.status_code != 200:
                if offset == 0:
                    require_ok(r, f"SmartRecruiters board '{board}'")
                ctx.errors.append(f"page at {offset} failed: HTTP {r.status_code}")
                break
            data = r.json()
            items = data.get("content", [])
            for j in items:
                loc = j.get("location") or {}
                comp = comp or company_from((j.get("company") or {}).get("name") or board, None,
                                            ats=AtsRef(type="smartrecruiters", board=board,
                                                       url=f"https://careers.smartrecruiters.com/{board}"))
                url = f"https://jobs.smartrecruiters.com/{board}/{j['id']}"
                mode = "remote" if loc.get("remote") else ("hybrid" if loc.get("hybrid") else None)
                role = make_role(
                    title=j["name"], url=url,
                    location=_place_from_parts(loc.get("city"), loc.get("region"), loc.get("country"),
                                               loc.get("fullLocation")),
                    work_mode=mode,  # type: ignore[arg-type]
                    employment_type=_employment((j.get("typeOfEmployment") or {}).get("label")),
                    department=(j.get("department") or {}).get("label"),
                    posted_at=parse_datetime(j.get("releasedDate")), source="smartrecruiters",
                    source_id=f"{board}:{j['id']}", evidence=[evidence(j.get("ref") or url, "ats_api", j["name"])],
                )
                if role:
                    out.append(Found(comp, role))
            offset += len(items)
            if not items or offset >= data.get("totalFound", 0):
                ctx.complete = True
                break
        return out


class Workable(Source):
    name, kind = "workable", "board"

    def board(self, ctx: Ctx, board: str) -> list[Found]:
        r = ctx.http.get(f"https://apply.workable.com/api/v1/widget/accounts/{board}", params={"details": "true"})
        require_ok(r, f"Workable board '{board}'")
        data = r.json()
        comp = company_from(data.get("name") or board, None,
                            ats=AtsRef(type="workable", board=board, url=f"https://apply.workable.com/{board}"))
        out = []
        for j in data.get("jobs", []):
            locs = [x for x in j.get("locations", []) if not x.get("hidden")]
            first = locs[0] if locs else {"city": j.get("city"), "region": j.get("state"), "countryCode": None,
                                          "country": j.get("country")}
            loc = _place_from_parts(first.get("city"), first.get("region"), first.get("countryCode"),
                                    ", ".join(filter(None, [first.get("city"), first.get("region"), first.get("country")])))
            extra = [_place_from_parts(x.get("city"), x.get("region"), x.get("countryCode"), None) for x in locs[1:]]
            url = j.get("url") or j.get("shortlink") or f"https://apply.workable.com/{board}/j/{j.get('shortcode')}"
            role = make_role(
                title=j["title"], url=url, apply_url=j.get("application_url"),
                description=clip(html_to_text(j.get("description"))), location=loc, locations=extra,
                work_mode="remote" if j.get("telecommuting") else None,
                employment_type=_employment(j.get("employment_type")), department=j.get("department"),
                posted_at=parse_datetime(j.get("published_on") or j.get("created_at")), source="workable",
                source_id=f"{board}:{j.get('shortcode') or j.get('id')}", evidence=[evidence(url, "ats_api", j["title"])],
            )
            if role:
                out.append(Found(comp, role))
        return out


class Workday(Source):
    """Workday career sites expose the JSON their own front-end uses. board = 'host|site'."""

    name, kind = "workday", "board"

    def board(self, ctx: Ctx, board: str) -> list[Found]:
        host, site = board.split("|", 1)
        tenant = host.split(".")[0]
        api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        comp = company_from(tenant.replace("-", " ").title(), None,
                            ats=AtsRef(type="workday", board=board, url=f"https://{host}/{site}"))
        out: list[Found] = []
        offset, total = 0, None
        ctx.complete = False
        while offset < int(ctx.cfg.get("max_jobs", 400)):
            r = ctx.http.request("POST", api, json_body={"appliedFacets": {}, "limit": 20, "offset": offset,
                                                         "searchText": ctx.cfg.get("search_text", "")}, use_cache=False)
            if r.status_code != 200:
                if offset == 0:
                    require_ok(r, f"Workday site '{host}/{site}'")
                ctx.errors.append(f"page at {offset} failed: HTTP {r.status_code}")
                break
            data = r.json()
            total = total or data.get("total") or 0  # later pages may report total=0
            posts = data.get("jobPostings", [])
            for j in posts:
                path = j.get("externalPath") or ""
                url = f"https://{host}/{site}{path}"
                role = make_role(
                    title=j.get("title", "?"), url=url, location=parse_location(j.get("locationsText")),
                    posted_at=parse_datetime(j.get("postedOn")), source="workday",
                    source_id=f"{tenant}:{(j.get('bulletFields') or [path])[0]}",
                    evidence=[evidence(url, "ats_api", j.get("title"))],
                )
                if role:
                    out.append(Found(comp, role))
            offset += len(posts)
            if not posts or offset >= total:
                ctx.complete = not ctx.cfg.get("search_text")  # a filtered listing is never "all roles"
                break
        return out

    @staticmethod
    def detail(ctx: Ctx, role: Role) -> Role:
        m = re.match(r"https://([^/]+)/([^/]+)(/job/.+)$", role.url)
        if not m:
            return role
        host, site, path = m.groups()
        tenant = host.split(".")[0]
        r = ctx.http.get(f"https://{host}/wday/cxs/{tenant}/{site}{path}")
        require_ok(r, "Workday posting")
        if r.status_code == 200:
            info = r.json().get("jobPostingInfo", {})
            role.description = clip(html_to_text(info.get("jobDescription")))
            role.employment_type = role.employment_type or _employment(info.get("timeType"))
            if info.get("country", {}).get("descriptor") and not role.location.country:
                role.location = parse_location(f"{info.get('location', '')}, {info['country']['descriptor']}")
        return role


def _employment(v: str | None) -> str | None:
    if not v:
        return None
    t = v.lower()
    for key, kind in (("intern", "internship"), ("part", "part_time"), ("contract", "contract"), ("temp", "temporary"),
                      ("freelance", "freelance"), ("full", "full_time"), ("permanent", "full_time"), ("regular", "full_time")):
        if key in t:
            return kind
    return None


def _salary(mn: Any, mx: Any, cur: Any, interval: Any) -> Salary | None:
    if mn is None and mx is None:
        return None
    period = {"per-year-salary": "year", "per-month-salary": "month", "per-hour-wage": "hour", "per-week-salary": "week",
              "per-day-wage": "day", "year": "year", "month": "month", "hour": "hour", "yearly": "year",
              "hourly": "hour", "monthly": "month", "annual": "year"}.get(str(interval or "year").lower(), "year")
    try:
        return Salary(min=mn, max=mx, currency=cur or None, period=period)  # type: ignore[arg-type]
    except ValueError:
        return Salary(min=mn, max=mx, period=period)  # type: ignore[arg-type]


def _ashby_comp(c: dict[str, Any] | None) -> Salary | None:
    if not c:
        return None
    for tier in c.get("compensationTiers", []) or []:
        for comp in tier.get("components", []):
            if comp.get("compensationType") == "Salary":
                return _salary(comp.get("minValue"), comp.get("maxValue"), comp.get("currencyCode"),
                               {"1 YEAR": "year", "1 MONTH": "month", "1 HOUR": "hour"}.get(comp.get("interval", ""), "year"))
    return None
