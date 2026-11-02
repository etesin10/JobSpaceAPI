"""Query-based job APIs and aggregators. Google Jobs (SerpApi/JSearch) surfaces LinkedIn/Indeed/Monster listings legally."""

from __future__ import annotations

from typing import Any

from jobfinder.models import Salary, short_hash
from jobfinder.services.identity import host_of, is_non_employer
from jobfinder.services.normalize import html_to_text, parse_datetime, parse_location

from .ats import _employment, _salary
from .base import Ctx, Found, Source, clip, company_from, evidence, make_role

ADZUNA_COUNTRIES = {"at", "au", "be", "br", "ca", "ch", "de", "es", "fr", "gb", "in", "it", "mx", "nl", "nz", "pl", "sg",
                    "us", "za"}
ADZUNA_CURRENCY = {"ca": "CAD", "us": "USD", "mx": "MXN", "gb": "GBP", "au": "AUD", "de": "EUR", "fr": "EUR",
                   "es": "EUR", "it": "EUR", "nl": "EUR", "at": "EUR", "be": "EUR", "br": "BRL", "in": "INR",
                   "nz": "NZD", "pl": "PLN", "sg": "SGD", "za": "ZAR", "ch": "CHF"}
GL_LANG = {"MX": "es", "ES": "es", "CO": "es", "AR": "es", "CL": "es", "PE": "es", "FR": "fr", "BR": "pt", "DE": "de"}


class Adzuna(Source):
    name, keyless, needs = "adzuna", False, ("app_id", "app_key")

    def search(self, ctx: Ctx, query: str, location: str | None, country: str | None, limit: int) -> list[Found]:
        cc = (country or "us").lower()
        if cc not in ADZUNA_COUNTRIES:
            ctx.log(f"adzuna: country {cc} not supported")
            return []
        out: list[Found] = []
        page = 1
        while len(out) < limit and page <= 5:
            params = {"app_id": ctx.cfg["app_id"], "app_key": ctx.cfg["app_key"], "what": query,
                      "results_per_page": min(50, limit), "content-type": "application/json"}
            if location:
                params["where"] = location
            if ctx.cfg.get("max_days_old"):
                params["max_days_old"] = ctx.cfg["max_days_old"]
            data = ctx.http.get_json(f"https://api.adzuna.com/v1/api/jobs/{cc}/search/{page}", params=params)
            res = data.get("results", [])
            for j in res:
                area = (j.get("location") or {}).get("area") or []
                loc = parse_location(", ".join(reversed(area)) if area else (j.get("location") or {}).get("display_name"),
                                     cc.upper())
                if j.get("latitude") is not None:
                    loc.lat, loc.lon = j.get("latitude"), j.get("longitude")
                sal = None
                if j.get("salary_min") or j.get("salary_max"):
                    sal = Salary(min=j.get("salary_min"), max=j.get("salary_max"), currency=ADZUNA_CURRENCY.get(cc))
                role = make_role(
                    title=html_to_text(j.get("title")) or "?", url=j["redirect_url"], description=clip(html_to_text(j.get("description"))),
                    location=loc, salary=sal, posted_at=parse_datetime(j.get("created")),
                    employment_type=_employment(j.get("contract_time") or j.get("contract_type")),
                    source="adzuna", source_id=str(j.get("id")), evidence=[evidence(j["redirect_url"], "job_api", j.get("title"))],
                )
                comp = company_from((j.get("company") or {}).get("display_name") or "Unknown")
                if role:
                    out.append(Found(comp, role))
            if len(res) < params["results_per_page"]:
                break
            page += 1
        return out[:limit]


class SerpApiGoogleJobs(Source):
    name, keyless, needs = "serpapi_google_jobs", False, ("api_key",)

    def search(self, ctx: Ctx, query: str, location: str | None, country: str | None, limit: int) -> list[Found]:
        out: list[Found] = []
        token = None
        while len(out) < limit:
            params: dict[str, Any] = {"engine": "google_jobs", "q": query, "api_key": ctx.cfg["api_key"]}
            if location:
                params["location"] = location
            if country:
                params["gl"] = country.lower()
                params["hl"] = GL_LANG.get(country.upper(), "en")
            if token:
                params["next_page_token"] = token
            data = ctx.http.get_json("https://serpapi.com/search.json", params=params)
            if data.get("error"):
                ctx.log(f"serpapi: {data['error']}")
                break
            for j in data.get("jobs_results", []):
                found = self._map(j, country)
                if found:
                    out.append(found)
            token = (data.get("serpapi_pagination") or {}).get("next_page_token")
            if not token:
                break
        return out[:limit]

    @staticmethod
    def _map(j: dict[str, Any], country: str | None) -> Found | None:
        links = [o.get("link") for o in j.get("apply_options", []) if o.get("link")]
        employer_links = [u for u in links if (h := host_of(u)) and not is_non_employer(h)]
        url = (employer_links or links or [j.get("share_link") or ""])[0]
        ext = j.get("detected_extensions") or {}
        desc = j.get("description") or ""
        for h in j.get("job_highlights", []) or []:
            desc += f"\n\n{h.get('title', '')}\n" + "\n".join(h.get("items", []))
        loc = parse_location(j.get("location"), country)
        work_mode = "remote" if ext.get("work_from_home") else None
        website = next((x.get("link") for x in j.get("related_links", []) if x.get("link") and
                        (h := host_of(x["link"])) and not is_non_employer(h)), None) or (employer_links[0] if employer_links else None)
        role = make_role(
            title=j.get("title", "?"), url=url, description=clip(desc), location=loc, work_mode=work_mode,  # type: ignore[arg-type]
            employment_type=_employment(ext.get("schedule_type")), posted_at=parse_datetime(ext.get("posted_at")),
            source="google_jobs", source_id=short_hash(j.get("job_id") or url, 20),
            evidence=[evidence(u, "posting", j.get("title")) for u in links[:3]] or [evidence(url)],
            via=j.get("via"),
        )
        return Found(company_from(j.get("company_name") or "Unknown", website), role) if role else None


class JSearch(Source):
    name, keyless, needs = "jsearch", False, ("api_key",)

    def search(self, ctx: Ctx, query: str, location: str | None, country: str | None, limit: int) -> list[Found]:
        q = f"{query} in {location}" if location else query
        params: dict[str, Any] = {"query": q, "page": 1, "num_pages": max(1, min(5, limit // 10)), "date_posted": "month"}
        if country:
            params["country"] = country.lower()
        data = ctx.http.get_json("https://jsearch.p.rapidapi.com/search", params=params,
                                 headers={"X-RapidAPI-Key": ctx.cfg["api_key"], "X-RapidAPI-Host": "jsearch.p.rapidapi.com"})
        out = []
        for j in data.get("data", [])[:limit]:
            loc = parse_location(", ".join(filter(None, [j.get("job_city"), j.get("job_state"), j.get("job_country")])),
                                 (j.get("job_country") or country or "").upper() or None)
            if j.get("job_latitude") is not None:
                loc.lat, loc.lon = j["job_latitude"], j["job_longitude"]
            sal = _salary(j.get("job_min_salary"), j.get("job_max_salary"), j.get("job_salary_currency"),
                          j.get("job_salary_period"))
            url = j.get("job_apply_link") or j.get("job_google_link") or ""
            role = make_role(
                title=j.get("job_title", "?"), url=url, description=clip(j.get("job_description")), location=loc,
                work_mode="remote" if j.get("job_is_remote") else None, salary=sal,
                employment_type=_employment(j.get("job_employment_type")),
                posted_at=parse_datetime(j.get("job_posted_at_datetime_utc")), source="jsearch",
                source_id=short_hash(j.get("job_id") or url, 20), evidence=[evidence(url, "posting", j.get("job_title"))],
                via=j.get("job_publisher"),
            )
            if role:
                out.append(Found(company_from(j.get("employer_name") or "Unknown", j.get("employer_website")), role))
        return out


class Jooble(Source):
    name, keyless, needs = "jooble", False, ("api_key",)

    def search(self, ctx: Ctx, query: str, location: str | None, country: str | None, limit: int) -> list[Found]:
        data = ctx.http.post_json(f"https://jooble.org/api/{ctx.cfg['api_key']}",
                                  {"keywords": query, "location": location or "", "page": "1"})
        out = []
        for j in data.get("jobs", [])[:limit]:
            role = make_role(
                title=html_to_text(j.get("title")) or "?", url=j["link"], description=clip(html_to_text(j.get("snippet"))),
                location=parse_location(j.get("location"), country), employment_type=_employment(j.get("type")),
                posted_at=parse_datetime(j.get("updated")), source="jooble", source_id=str(j.get("id")),
                evidence=[evidence(j["link"], "job_api", j.get("title"))],
            )
            if role:
                out.append(Found(company_from(j.get("company") or "Unknown"), role))
        return out


class Remotive(Source):
    name = "remotive"

    def search(self, ctx: Ctx, query: str, location: str | None, country: str | None, limit: int) -> list[Found]:
        data = ctx.http.get_json("https://remotive.com/api/remote-jobs", params={"search": query, "limit": limit})
        out = []
        for j in data.get("jobs", [])[:limit]:
            elig = [x.strip() for x in (j.get("candidate_required_location") or "").replace("/", ",").split(",") if x.strip()]
            codes = []
            for e in elig:
                p = parse_location(e)
                if p.country:
                    codes.append(p.country)
                elif e.lower() in ("worldwide", "anywhere"):
                    codes.append("GLOBAL")
                elif e.upper() in ("LATAM", "EMEA", "APAC", "EU", "NA", "AMERICAS"):
                    codes.append(e.upper())
                elif "latin" in e.lower():
                    codes.append("LATAM")
                elif "europe" in e.lower():
                    codes.append("EU")
                elif "america" in e.lower():
                    codes.append("AMERICAS")
            role = make_role(
                title=j.get("title", "?"), url=j["url"], description=clip(html_to_text(j.get("description"))),
                work_mode="remote", remote_eligibility=codes, employment_type=_employment(j.get("job_type")),
                posted_at=parse_datetime(j.get("publication_date")), source="remotive", source_id=str(j.get("id")),
                evidence=[evidence(j["url"], "job_api", j.get("title"))], via="Remotive",
            )
            if role:
                out.append(Found(company_from(j.get("company_name") or "Unknown"), role))
        return out


class Arbeitnow(Source):
    name = "arbeitnow"

    def search(self, ctx: Ctx, query: str, location: str | None, country: str | None, limit: int) -> list[Found]:
        from jobfinder.models import fold

        data = ctx.http.get_json("https://www.arbeitnow.com/api/job-board-api")
        terms = [t for t in fold(query).split() if len(t) > 2]
        out = []
        for j in data.get("data", []):
            hay = fold(f"{j.get('title', '')} {' '.join(j.get('tags', []))}")
            if terms and not all(t in hay for t in terms):
                continue
            role = make_role(
                title=j.get("title", "?"), url=j["url"], description=clip(html_to_text(j.get("description"))),
                location=parse_location(j.get("location")), work_mode="remote" if j.get("remote") else None,
                employment_type=_employment(" ".join(j.get("job_types", []))),
                posted_at=parse_datetime(j.get("created_at")), source="arbeitnow", source_id=j.get("slug"),
                evidence=[evidence(j["url"], "job_api", j.get("title"))],
            )
            if role:
                out.append(Found(company_from(j.get("company_name") or "Unknown"), role))
            if len(out) >= limit:
                break
        return out
