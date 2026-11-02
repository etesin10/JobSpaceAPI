"""Find roles / companies / contacts. Each function runs inside a background Run (see runs.py)."""

from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from rapidfuzz import fuzz

from jobfinder.models import (
    AtsRef,
    Company,
    Contact,
    Criteria,
    Evidence,
    Role,
    RunRequest,
    SearchQuery,
    SkillSet,
    SocialProfile,
    fold,
    utcnow,
)
from jobfinder.services import matching, websearch
from jobfinder.services.identity import host_of, is_non_employer, linkedin_slug, normalize_domain
from jobfinder.services.normalize import enrich_role
from jobfinder.services.runs import Interrupted
from jobfinder.services.skills import occupation_for, occupation_for_title
from jobfinder.sources import BOARD_FOR_ATS, SOURCES, Ctx, Found, detect_ats, discover_careers, probe_ats
from jobfinder.sources.base import error_info

if TYPE_CHECKING:
    from jobfinder.services.runs import RunCtx

COUNTRY_LANGS = {"MX": ["es", "en"], "CA": ["en", "fr"], "US": ["en", "es"], "ES": ["es"], "CO": ["es"], "AR": ["es"],
                 "CL": ["es"], "PE": ["es"], "FR": ["fr"], "BR": ["pt", "en"]}


@lru_cache(maxsize=512)
def region_name(code: str | None) -> str | None:
    if not code:
        return None
    import pycountry

    s = pycountry.subdivisions.get(code=code)
    return s.name if s else None


@lru_cache(maxsize=256)
def country_name(code: str | None) -> str | None:
    if not code:
        return None
    import pycountry

    c = pycountry.countries.get(alpha_2=code)
    return getattr(c, "common_name", None) or (c.name if c else None)


# ----------------------------------------------------------------------------- ingest


def _relevant(m: Any, cr: Criteria) -> bool:
    """Keep only roles that pass constraints and look like the target job (boards return every role)."""
    if not m.passed:
        return False
    c = m.components
    support = c.get("skills", 0) >= 0.5 or c.get("rules", 0) >= 0.5
    if "title" in c:
        return c["title"] >= 0.6 or (c["title"] >= 0.45 and support)
    return support or not c


def ingest(rc: RunCtx, founds: list[Found], criteria: list[Criteria], skills: SkillSet,
           known_company: Company | None = None) -> dict[str, Any]:
    st = rc.app.store
    stats: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for f in founds:
        rc.check()
        role = enrich_role(f.role)
        comp = known_company or f.company
        if not role.url:
            stats["no_url"] += 1
            continue
        if known_company and f.company.ats and not known_company.ats:
            known_company.ats = f.company.ats
        cr, m = matching.best_match(criteria, role, comp, skills)
        if cr is None or m is None:
            continue
        if not _relevant(m, cr):
            stats["filtered"] += 1
            for r in m.failed[:1] or ["low relevance"]:
                reasons[re.sub(r"\d[\d,.]*", "#", r)[:60]] += 1
            continue
        if known_company is None:
            comp, _ = st.upsert_company(comp)
        role.company_id = comp.id
        saved, created = st.upsert_role(role)
        stats["roles_new" if created else "roles_updated"] += 1
        _, link_created = st.link_company(rc.pid, comp.id, {"criteria_ids": [cr.id], "run_id": rc.run.id})  # type: ignore[arg-type]
        if link_created:
            stats["companies_added"] += 1
        cur = st.get_role_link(rc.pid, saved.id)  # type: ignore[arg-type]
        if cur and cur.excluded:
            continue
        if saved.status == "closed":
            status = "closed"
        elif not cur or cur.status in ("found", "verified", "scored", "closed"):
            status = "scored"  # a posting seen again on its board is reopened
        else:
            status = cur.status
        _, rl_created = st.link_role(rc.pid, saved.id, {"criteria_id": cr.id, "score": m.score, "match": m,  # type: ignore[arg-type]
                                                          "status": status, "run_id": rc.run.id})
        if rl_created:
            stats["roles_added"] += 1
    out: dict[str, Any] = dict(stats)
    if reasons:
        out["filtered_reasons"] = dict(reasons.most_common(5))
    return out


def _merge(total: dict[str, Any], part: dict[str, Any]) -> None:
    for k, v in part.items():
        if isinstance(v, int):
            total[k] = total.get(k, 0) + v
        elif isinstance(v, dict):
            d = total.setdefault(k, {})
            for kk, vv in v.items():
                d[kk] = d.get(kk, 0) + vv


# ----------------------------------------------------------------------------- queries


def localized_titles(cr: Criteria, country: str | None, limit: int = 3) -> list[str]:
    langs = COUNTRY_LANGS.get(country or "", ["en"])
    titles = list(cr.targets.titles)
    occs = [occupation_for(o.system, o.code) for o in cr.targets.occupations]
    occs += [occupation_for_title(t) for t in cr.targets.titles]
    for o in occs:
        if o:
            for lang in langs:
                titles += o.titles.get(lang, [])[:2]
    if not titles:
        titles = cr.targets.activities[:2] or cr.targets.keywords[:2]
    seen, out = set(), []
    for t in titles:
        k = re.sub(r"(?<=[a-z])[oa]\b", "", fold(t))  # ingeniero/ingeniera count once
        if k not in seen:
            seen.add(k)
            out.append(t)
    # Prefer one title per language before taking more of the same language.
    return out[:limit]


def location_text(country: str | None, region: str | None, city: str | None) -> str | None:
    parts = [city, region_name(region) if region and not city else None, country_name(country)]
    return ", ".join(p for p in parts if p) or None


def role_queries(cr: Criteria) -> list[SearchQuery]:
    qs = [q for q in cr.search_strategy.queries if q.purpose == "roles" and not q.disabled]
    if qs:
        return qs
    locs = cr.constraints.locations or []
    out: list[SearchQuery] = []
    if not locs:
        for t in localized_titles(cr, None):
            out.append(SearchQuery(text=t))
    for loc in locs:
        for t in localized_titles(cr, loc.country):
            out.append(SearchQuery(text=t, location=location_text(loc.country, loc.region, loc.city), country=loc.country))
    if "remote" in cr.constraints.work_mode:
        for t in localized_titles(cr, None, 2):
            out.append(SearchQuery(text=t, sources=["remotive"]))
    return out


def plan_queries_llm(rc: RunCtx, cr: Criteria) -> list[SearchQuery]:
    """Ask the LLM to propose search queries; stored in search_strategy (AI-owned)."""
    system = ("You design job-search queries. Given criteria JSON, return {\"queries\": [{\"text\": str, \"location\": str|null, "
              "\"country\": ISO2|null, \"purpose\": \"roles\"|\"companies\"}]}. Use the local language of each location "
              "(e.g. Spanish titles for Mexico, French for Quebec) plus English. 4-10 queries, short, no boolean operators.")
    user = cr.model_dump_json(include={"constraints", "targets"})
    data, model = rc.app.llm.json("plan_queries", system, user, budget=rc.budget)
    out = []
    for q in (data.get("queries") or [])[:12]:
        try:
            out.append(SearchQuery.model_validate({k: q.get(k) for k in ("text", "location", "country", "purpose") if q.get(k)}))
        except Exception:
            continue
    if out:
        from jobfinder.services.profiles import update_strategy

        saved = update_strategy(rc.app, cr.id, {"queries": [q.model_dump() for q in out]}, f"llm:{model}")  # type: ignore[arg-type]
        cr.search_strategy = saved.search_strategy
        rc.log(f"{model} proposed {len(out)} queries for {cr.id} (strategy v{saved.search_strategy.version})")
    return out


def enabled_search_sources(rc: RunCtx, wanted: list[str] | None = None) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for name, src in SOURCES.items():
        if src.kind != "search":
            continue
        cfg = rc.app.conn.sources.get(name)
        cfg_d = cfg.model_dump() if cfg else None
        if not src.configured(cfg_d):
            continue
        if wanted and name not in wanted:
            continue
        out.append((name, cfg_d or {}))
    return out


# ----------------------------------------------------------------------------- find roles


def _criteria(rc: RunCtx, req: RunRequest) -> list[Criteria]:
    crit = rc.app.store.list_criteria(rc.pid, active_only=True)
    if req.criteria_ids:
        crit = [c for c in crit if c.id in req.criteria_ids]
    if not crit:
        raise ValueError("No active criteria. Create one for this profile first.")
    return crit


def find_roles(rc: RunCtx, req: RunRequest) -> dict[str, Any]:
    st = rc.app.store
    crit = _criteria(rc, req)
    skills = st.get_skillset(rc.pid)
    total: dict[str, Any] = {}
    per_query: list[dict[str, Any]] = []

    if not req.company_ids:
        for cr in crit:
            queries = role_queries(cr)
            if req.use_llm and not cr.search_strategy.queries and rc.app.llm.available("plan_queries") \
                    and not rc.is_done("find_roles", f"plan:{cr.id}"):
                try:
                    queries = [q for q in plan_queries_llm(rc, cr) if q.purpose == "roles"] or queries
                except Exception as e:
                    rc.log(f"query planning skipped: {e}")
                rc.mark_done("find_roles", f"plan:{cr.id}")
            sources = enabled_search_sources(rc, cr.search_strategy.sources or None)
            if not sources:
                rc.log("No query-based job sources enabled.")
            yields: dict[str, tuple[int, int]] = {}
            plan = [(q, name, cfg) for q in queries for name, cfg in sources
                    if not (q.sources and name not in q.sources) and not (name == "remotive" and not q.sources and q.location)]
            for qi, (q, name, cfg) in enumerate(plan, 1):
                key = f"q:{cr.id}|{name}|{q.text}|{q.location or ''}"
                if rc.skip("find_roles", key, q.text):
                    continue
                rc.check()
                rc.budget.check("search")
                rc.at("find_roles", f"{name} search {qi}/{len(plan)}: '{q.text}' @ {q.location or 'any'} ({cr.name})")
                try:
                    founds = SOURCES[name].search(Ctx(rc.http, cfg, rc.log), q.text, q.location, q.country,
                                                  req.limit or 50)
                except Exception as e:
                    _stop_if_offline(rc, e)
                    rc.log(f"{name} '{q.text}': {error_info(e)[1]}")
                    rc.mark_done("find_roles", key)
                    continue
                rc.budget.charge(search=1)
                res = ingest(rc, founds, [cr], skills)
                _merge(total, res)
                kept = res.get("roles_new", 0) + res.get("roles_updated", 0)
                ykey = f"{q.text}|{q.location or ''}"
                prev = yields.get(ykey, (0, 0))
                yields[ykey] = (prev[0] + len(founds), prev[1] + kept)
                per_query.append({"criteria": cr.id, "source": name, "query": q.text, "location": q.location,
                                  "found": len(founds), "kept": kept})
                rc.log(f"{name}: '{q.text}' @ {q.location or 'any'} -> {len(founds)} found, {kept} kept")
                rc.mark_done("find_roles", key)
                rc.progress(queries_done=len(per_query))
            st.record_query_yield(cr.id, yields)  # type: ignore[arg-type]

    # Company boards (ATS / careers pages) for companies in this profile's roster.
    links = st.pipeline_companies(rc.pid, include_excluded=False)
    now = utcnow()
    if req.company_ids:
        links = [(c, link) for c, link in links if c.id in req.company_ids]
        missing = set(req.company_ids) - {c.id for c, _ in links}
        for cid in missing:
            comp = st.get_company(cid)
            if comp:
                links.append((comp, st.link_company(rc.pid, cid, {})[0]))
    elif req.scope == "all":
        pass  # refresh every company in the roster
    elif req.scope == "failed":
        links = [(c, link) for c, link in links if link.status == "error"]
    else:
        links = [(c, link) for c, link in links if link.status == "new" or (link.status == "error" and _retry_due(c, now))]
    links = links[: (req.limit or (500 if req.scope == "all" else 40))]
    failed: list[str] = []
    for i, (comp, _link) in enumerate(links):
        key = f"c:{comp.id}"
        if rc.skip("find_roles", key, comp.name):
            continue
        rc.check()
        rc.at("find_roles", f"company {i + 1}/{len(links)}: {comp.name}")
        rc.budget.check("pages")
        res = company_board_roles(rc, comp, crit, skills)
        if res.get("companies_failed"):
            failed.append(comp.name)
        _merge(total, res)
        rc.mark_done("find_roles", key)
        rc.progress(companies_done=i + 1, companies_total=len(links), companies_failed=len(failed))
    if failed:
        rc.log(f"Could not read {len(failed)} job board(s): {', '.join(failed[:10])}. They will be retried later "
               "(or run Find roles with scope 'failed').")
    if links or not req.company_ids:
        rc.at("find_roles", "reading posting details")
        _merge(total, enrich_postings(rc, crit, skills, limit=min(req.limit or 25, 60)))
    total["queries"] = per_query[-50:]
    return total


_NETWORK_PROBES = ("https://www.google.com/generate_204", "https://www.cloudflare.com/cdn-cgi/trace",
                   "https://www.microsoft.com/")


def internet_ok(http: Any) -> bool:
    for url in _NETWORK_PROBES:
        try:
            if http.client.get(url, timeout=6).status_code < 500:
                return True
        except Exception:
            continue
    return False


def _stop_if_offline(rc: RunCtx, e: BaseException) -> None:
    """A connection error might be one site, or the whole internet. In the second case stop the run (resumable)
    instead of marking every remaining company as failed."""
    import httpx

    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)) and not internet_ok(rc.http):
        raise Interrupted("No internet connection (no site could be reached). Nothing was marked as failed; "
                          "resume when the connection is back.")


def _retry_due(comp: Company, now: Any) -> bool:
    f = comp.fetch
    return f is None or f.next_retry_at is None or f.next_retry_at <= now


def _board_url(comp: Company) -> str | None:
    if comp.ats:
        return comp.ats.url or {"greenhouse": f"https://boards.greenhouse.io/{comp.ats.board}",
                                "lever": f"https://jobs.lever.co/{comp.ats.board}",
                                "ashby": f"https://jobs.ashbyhq.com/{comp.ats.board}",
                                "smartrecruiters": f"https://careers.smartrecruiters.com/{comp.ats.board}",
                                "workable": f"https://apply.workable.com/{comp.ats.board}"}.get(comp.ats.type)
    return comp.careers_url


def company_board_roles(rc: RunCtx, comp: Company, crit: list[Criteria], skills: SkillSet) -> dict[str, Any]:
    """Read one company's job board. Never raises: a failure is recorded on the company (fetch status, link status
    'error' with a retry time) so loops over many companies keep going."""
    st = rc.app.store
    ctx = Ctx(rc.http, {}, rc.log)
    if not comp.ats:
        try:
            url, ats = discover_careers(ctx, comp) if (comp.domain or comp.website) else (None, None)
            rc.budget.charge(pages=2)
            ats = ats or probe_ats(ctx, comp)
        except Exception as e:
            url, ats = None, None
            ctx.errors.append(error_info(e)[1])
        if url or ats:
            comp.careers_url = comp.careers_url or url
            comp.ats = ats
            comp, _ = st.upsert_company(comp)
    founds: list[Found] = []
    board_ctx: Ctx | None = None
    try:
        if comp.ats and comp.ats.type in BOARD_FOR_ATS:
            src = rc.app.conn.source(BOARD_FOR_ATS[comp.ats.type])
            if src is None:
                rc.log(f"{comp.name}: source {comp.ats.type} disabled")
            else:
                board_ctx = Ctx(rc.http, src.model_dump(), rc.log)
                founds = SOURCES[BOARD_FOR_ATS[comp.ats.type]].board(board_ctx, comp.ats.board)
        elif comp.careers_url and rc.app.conn.source("careers_page"):
            board_ctx = Ctx(rc.http, rc.app.conn.source("careers_page").model_dump(), rc.log)  # type: ignore[union-attr]
            founds = SOURCES["careers_page"].board(board_ctx, comp.careers_url)
            if not founds and rc.app.llm.available("extract_jobs"):
                try:
                    founds = ai_extract_jobs(rc, comp)
                except Exception as e:  # the page was readable; only the AI step failed
                    board_ctx.errors.append(f"AI extraction failed: {error_info(e)[1]}")
                board_ctx.complete = False  # AI extraction is best effort: never close roles because of it
        else:
            msg = "no careers page or job board found" + (f" ({ctx.errors[0]})" if ctx.errors else "")
            st.record_fetch("company", comp.id, ok=False, what="careers_page", url=comp.website, error=msg,  # type: ignore[arg-type]
                            retry=False)
            st.link_company(rc.pid, comp.id, {"status": "exhausted"})  # type: ignore[arg-type]
            rc.log(f"{comp.name}: {msg}")
            return {"companies_no_board": 1}
    except Exception as e:
        _stop_if_offline(rc, e)  # whole internet down: stop (resumable) without blaming this company
        status, msg = error_info(e)
        fs = st.record_fetch("company", comp.id, ok=False, what="board", url=getattr(e, "url", None) or _board_url(comp),  # type: ignore[arg-type]
                             http_status=status, error=msg)
        st.link_company(rc.pid, comp.id, {"status": "error"})  # type: ignore[arg-type]
        when = fs.next_retry_at.strftime("%Y-%m-%d %H:%M UTC") if fs and fs.next_retry_at else "later"
        rc.log(f"{comp.name}: could not read job board – {msg}. Will retry after {when}.")
        rc.budget.charge(pages=1)
        return {"companies_failed": 1}
    rc.budget.charge(pages=1)
    if board_ctx is not None:
        st.record_fetch("company", comp.id, ok=True, what="board", url=board_ctx.url or _board_url(comp),  # type: ignore[arg-type]
                        partial=bool(board_ctx.errors), error="; ".join(board_ctx.errors[:3]) or None)
    if founds and comp.ats:  # boards know the real employer name; slug-derived names get replaced
        real = founds[0].company.name
        if real and fold(comp.name) in (fold(comp.ats.board.split("|")[-1].replace("-", " ")), fold(comp.ats.board)) \
                and fold(real) != fold(comp.name):
            comp.name = real
            comp, _ = st.upsert_company(comp)
    res = ingest(rc, founds, crit, skills, known_company=comp) if founds else {}
    kept = res.get("roles_new", 0) + res.get("roles_updated", 0)
    has_roles = kept or any(r.company_id == comp.id for r, _ in st.pipeline_roles(rc.pid, comp.id))
    st.link_company(rc.pid, comp.id, {"status": "roles_searched" if has_roles else "exhausted"})  # type: ignore[arg-type]
    rc.log(f"{comp.name}: {len(founds)} roles on board, {kept} match"
           + (f" (partial: {board_ctx.errors[0]})" if board_ctx and board_ctx.errors else ""))
    if board_ctx is not None and board_ctx.complete and not board_ctx.errors:
        _close_missing(rc, comp, founds)
    return res


def _close_missing(rc: RunCtx, comp: Company, founds: list[Found]) -> None:
    """Board roles that disappeared from the employer's board are closed."""
    if not founds or not comp.ats:
        return
    from jobfinder.services.identity import canonical_url

    live = {canonical_url(f.role.url) for f in founds}
    src = BOARD_FOR_ATS.get(comp.ats.type)
    for role in rc.app.store.list_roles(comp.id):  # type: ignore[arg-type]
        if role.source == src and role.status == "open" and canonical_url(role.url) not in live:
            rc.app.store.set_role_status(role.id, "closed")  # type: ignore[arg-type]
            link = rc.app.store.get_role_link(rc.pid, role.id)  # type: ignore[arg-type]
            if link:
                rc.app.store.link_role(rc.pid, role.id, {"status": "closed"})  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- find companies

_ATS_HOST = re.compile(r"greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com|workable\.com|myworkdayjobs\.com")


def _name_from_ats_title(title: str, ats: AtsRef) -> str:
    t = title.strip()
    for rx in (r"\bat\s+(.+?)(?:\s*[|\-–]\s*.*)?$", r"@\s*(.+)$", r"\ben\s+(.+)$"):
        m = re.search(rx, t)
        if m and len(m.group(1)) < 60:
            return m.group(1).strip()
    parts = [p.strip() for p in re.split(r"\s[|\-–]\s", t) if p.strip()]
    if ats.type == "lever" and parts:
        return parts[0]
    return ats.board.split("|")[-1].replace("-", " ").title()


def company_queries(cr: Criteria) -> list[tuple[str, str | None]]:
    qs = [(q.text, q.country) for q in cr.search_strategy.queries if q.purpose == "companies" and not q.disabled]
    if qs:
        return qs
    out: list[tuple[str, str | None]] = []
    for loc in cr.constraints.locations or [None]:  # type: ignore[list-item]
        country = loc.country if loc else None
        where = location_text(country, loc.region, loc.city) if loc else ""
        for t in localized_titles(cr, country, 2):
            out.append((f'"{t}" {where} (site:boards.greenhouse.io OR site:jobs.lever.co OR site:jobs.ashbyhq.com OR '
                        f'site:myworkdayjobs.com OR site:apply.workable.com)', country))
            out.append((f"companies hiring {t} in {where} careers", country))
    return out


def find_companies(rc: RunCtx, req: RunRequest) -> dict[str, Any]:
    st = rc.app.store
    crit = _criteria(rc, req)
    skills = st.get_skillset(rc.pid)
    profile = st.get_profile(rc.pid)
    has_search = bool(websearch.available(rc.app.conn))
    has_llm = req.use_llm and rc.app.llm.available("find_companies")
    if not has_search and not has_llm:
        raise RuntimeError("find_companies needs a web search key (Tavily/Brave/SerpApi) or an AI model. "
                           "Roles-first discovery (find_roles) still grows the roster without them.")
    result: dict[str, Any] = {"per_criteria": {}}
    linked = {c.id for c, _ in st.pipeline_companies(rc.pid)}
    for cr in crit:
        if rc.skip("find_companies", f"crit:{cr.id}", cr.name):
            continue
        have = sum(1 for _, link in st.pipeline_companies(rc.pid) if cr.id in link.criteria_ids)
        target = profile.target_companies_per_criteria if profile else 50
        need = min(req.limit or 15, max(0, target - have))
        if need == 0:
            rc.log(f"{cr.id}: roster already has {have}/{target} companies")
            rc.mark_done("find_companies", f"crit:{cr.id}")
            continue
        cands: dict[str, dict[str, Any]] = {}
        hits_all: list[websearch.SearchHit] = []
        if has_search:
            queries = company_queries(cr)
            for qi, (q, country) in enumerate(queries, 1):
                qkey = f"q:{cr.id}|{q}"
                if rc.skip("find_companies", qkey, q):
                    continue
                rc.check()
                rc.budget.check("search")
                rc.at("find_companies", f"web search {qi}/{len(queries)} for '{cr.name}' (roster {have}/{target})")
                try:
                    hits = websearch.web_search(rc.app.conn, rc.http, q, 10, country)
                except Exception as e:
                    _stop_if_offline(rc, e.__cause__ or e)
                    rc.log(f"web search failed: {str(e)[:160]}")
                    continue
                rc.budget.charge(search=1)
                rc.mark_done("find_companies", qkey)
                hits_all += hits
                for h in hits:
                    ats = detect_ats(h.url)
                    if ats:
                        key = f"ats:{ats.type}:{ats.board.lower()}"
                        cands.setdefault(key, {"name": _name_from_ats_title(h.title, ats), "ats": ats,
                                               "evidence": h.url, "title": h.title})
                if len(cands) >= need * 2:
                    break
        if has_llm:
            try:
                for c in _llm_companies(rc, cr, hits_all, need * 2 - len(cands)):
                    key = f"dom:{c['domain']}" if c.get("domain") else f"name:{fold(c['name'])}"
                    cands.setdefault(key, c)
            except Exception as e:
                rc.log(f"AI company extraction skipped: {str(e)[:200]}")
        added = 0
        for c in cands.values():
            if added >= need:
                break
            rc.check()
            comp = _verify_company(rc, c)
            if comp is None:
                continue
            existing = st.find_company(comp.name, comp.domain)
            if existing and existing.id in linked:
                if cr.id not in (st.get_company_link(rc.pid, existing.id).criteria_ids):  # type: ignore[union-attr, arg-type]
                    st.link_company(rc.pid, existing.id, {"criteria_ids": [cr.id]})  # type: ignore[arg-type]
                continue
            comp, _ = st.upsert_company(comp)
            st.link_company(rc.pid, comp.id, {"criteria_ids": [cr.id], "status": "new", "run_id": rc.run.id})  # type: ignore[arg-type]
            linked.add(comp.id)  # type: ignore[arg-type]
            added += 1
            rc.log(f"+ {comp.name} ({comp.domain or (comp.ats.type if comp.ats else '?')})")
            if req.kind == "find_companies" and comp.ats:
                company_board_roles(rc, comp, [cr], skills)
            rc.progress(companies_added=added)
            rc.at("find_companies", f"verifying candidates for '{cr.name}' ({have + added}/{target} in roster)")
        result["per_criteria"][cr.id] = {"added": added, "had": have, "target": target}
        rc.mark_done("find_companies", f"crit:{cr.id}")
    result["companies_added"] = sum(v["added"] for v in result["per_criteria"].values())
    return result


def _llm_companies(rc: RunCtx, cr: Criteria, hits: list[websearch.SearchHit], n: int) -> list[dict[str, Any]]:
    if n <= 0:
        return []
    allowed_urls = {h.url for h in hits}
    system = ("You identify real employers for a job seeker. Return {\"companies\": [{\"name\": str, \"domain\": str, "
              "\"careers_url\": str|null, \"evidence_url\": str|null, \"reason\": str}]}. Only real companies that hire "
              "for the target roles in the allowed locations. domain is the company's own website domain (not a job board). "
              + ("evidence_url MUST be one of the provided search result URLs." if hits else
                 "No search results are available: only list companies you are confident exist; they will be verified."))
    payload = {"criteria": cr.model_dump(include={"constraints", "targets"}, mode="json"), "max": n,
               "search_results": [h.dict() for h in hits[:40]]}
    import json

    data, model = rc.app.llm.json("find_companies", system, json.dumps(payload, ensure_ascii=False), budget=rc.budget)
    out = []
    for c in (data.get("companies") or [])[: n * 2]:
        if not c.get("name"):
            continue
        ev = c.get("evidence_url")
        if hits and ev not in allowed_urls:
            ev = None
        out.append({"name": c["name"], "domain": normalize_domain(c.get("domain")), "careers_url": c.get("careers_url"),
                    "evidence": ev, "reason": c.get("reason"), "llm": model})
    rc.log(f"{model} suggested {len(out)} companies")
    return out


def _verify_company(rc: RunCtx, c: dict[str, Any]) -> Company | None:
    """A company is accepted only if an ATS board or its own website/careers page answers."""
    ctx = Ctx(rc.http, {}, rc.log)
    ev: list[Evidence] = []
    if c.get("evidence"):
        ev.append(Evidence(url=c["evidence"], kind="search_result", title=c.get("title"), fetched_at=utcnow(), status="ok"))
    ats: AtsRef | None = c.get("ats")
    if ats:
        comp = Company(name=c["name"], ats=ats, evidence=ev, source="web_search")
        return comp
    domain = c.get("domain")
    if not domain:
        return None
    comp = Company(name=c["name"], domain=domain, website=f"https://{domain}", careers_url=c.get("careers_url"),
                   evidence=ev, source="llm" if c.get("llm") else "web_search", description=c.get("reason"))
    rc.budget.check("pages")
    url, found_ats = discover_careers(ctx, comp)
    rc.budget.charge(pages=2)
    if not url and not found_ats:
        try:
            r = rc.http.get(f"https://{domain}")
            ok = r.status_code < 400 and fuzz.partial_ratio(fold(c["name"]), fold(r.text[:20000])) >= 85
        except Exception:
            ok = False
        if not ok:
            rc.log(f"- rejected {c['name']} ({domain}): website not verifiable")
            return None
        ev.append(Evidence(url=f"https://{domain}", kind="company_site", fetched_at=utcnow(), status="ok"))
    else:
        comp.careers_url = url or comp.careers_url
        comp.ats = found_ats
        ev.append(Evidence(url=url or f"https://{domain}", kind="company_site", fetched_at=utcnow(), status="ok"))
    comp.evidence = ev
    return comp


# ----------------------------------------------------------------------------- find contacts

_LI_TITLE = re.compile(r"^(?P<name>[^\-–|]+?)\s*[\-–|]\s*(?P<rest>.+?)(?:\s*[|]\s*LinkedIn.*)?$", re.I)
CONTACT_TYPES = [
    ("talent_acquisition", r"talent acquisition|adquisicion de talento|acquisition de talents|sourcer"),
    ("recruiter", r"recruit|reclut|headhunt|recrut"),
    ("hr", r"human resources|recursos humanos|\bhr\b|\brh\b|people (partner|operations)|hrbp|ressources humaines"),
    ("hiring_manager", r"manager|director|head of|gerente|lead|jefe|lider|vp\b"),
]


def _contact_type(title: str) -> str:
    t = fold(title)
    for kind, rx in CONTACT_TYPES:
        if re.search(rx, t):
            return kind
    return "team_member"


def contact_queries(comp: Company, role: Role) -> list[str]:
    where = country_name(role.location.country) or ""
    dept = role.department or re.sub(r"\(.*?\)", "", role.title)
    return [
        f'site:linkedin.com/in "{comp.name}" (recruiter OR "talent acquisition" OR "human resources" OR reclutador) {where}',
        f'site:linkedin.com/in "{comp.name}" "{dept.strip()}" (manager OR lead OR director OR gerente)',
    ]


def parse_profile_hit(h: websearch.SearchHit, comp: Company) -> Contact | None:
    if not linkedin_slug(h.url):
        return None
    m = _LI_TITLE.match(h.title.strip())
    if not m:
        return None
    name = m.group("name").strip()
    rest = m.group("rest").strip()
    if len(name.split()) < 2 or len(name) > 60:
        return None
    hay = fold(f"{h.title} {h.snippet}")
    if fuzz.partial_ratio(fold(comp.name), hay) < 85:
        return None
    title = re.split(r"\s+(?:at|en|chez|@)\s+", rest, maxsplit=1)[0].strip(" -|")
    return Contact(
        name=name, title=title or None, type=_contact_type(f"{title} {h.snippet[:120]}"), company_id=comp.id,  # type: ignore[arg-type]
        profiles=[SocialProfile(network="linkedin", url=h.url.split("?")[0])],
        evidence=[Evidence(url=h.url, kind="search_result", title=h.title, snippet=h.snippet[:300], fetched_at=utcnow(),
                           status="external")],
        source=f"web_search:{h.provider}",
    )


def find_contacts(rc: RunCtx, req: RunRequest) -> dict[str, Any]:
    st = rc.app.store
    if not websearch.available(rc.app.conn):
        raise RuntimeError("find_contacts needs a web search key (Tavily, Brave or SerpApi).")
    n = req.limit or rc.app.conn.matching.contacts_for_top_n_roles
    pairs = st.pipeline_roles(rc.pid)
    if req.role_ids:
        pairs = [(r, link) for r, link in pairs if r.id in req.role_ids]
    else:
        min_score = rc.app.conn.matching.min_score
        pairs = [(r, link) for r, link in pairs if not link.excluded and link.status in ("scored", "verified", "found")
                 and link.stage in ("none", "shortlisted") and (link.score or 0) >= min_score and r.status == "open"
                 and link.match and link.match.passed]
    pairs = pairs[:n]
    found_total, per_company_cache = 0, {}
    for i, (role, _link) in enumerate(pairs):
        if rc.skip("find_contacts", f"r:{role.id}", role.title):
            continue
        rc.check()
        comp = st.get_company(role.company_id) if role.company_id else None
        if comp is None:
            continue
        rc.at("find_contacts", f"role {i + 1}/{len(pairs)}: {role.title} at {comp.name}")
        hits: list[websearch.SearchHit] = []
        if comp.id in per_company_cache:
            hits = per_company_cache[comp.id]
        else:
            for q in contact_queries(comp, role):
                rc.budget.check("search")
                try:
                    hits += websearch.web_search(rc.app.conn, rc.http, q, 10, role.location.country)
                except Exception as e:
                    _stop_if_offline(rc, e.__cause__ or e)
                    rc.log(f"contact search failed: {str(e)[:160]}")
                rc.budget.charge(search=1)
            per_company_cache[comp.id] = hits
        n_role = 0
        cands: list[Contact] = []
        for h in hits:
            ct = parse_profile_hit(h, comp)
            if not ct or any(c.profiles[0].url == ct.profiles[0].url for c in cands):
                continue
            if ct.type == "hiring_manager" and fuzz.token_set_ratio(fold(ct.title or ""), fold(f"{role.title} {role.department or ''}")) < 40:
                ct.type = "team_member"
            cands.append(ct)
        cands = _ai_pick_contacts(rc, comp, role, cands)
        for ct in cands:
            saved, _ = st.upsert_contact(ct)
            _, created = st.link_contact(rc.pid, saved.id, role.id, {"run_id": rc.run.id})  # type: ignore[arg-type]
            n_role += int(created)
        found_total += n_role
        st.link_role(rc.pid, role.id, {"status": "contacts_searched"})  # type: ignore[arg-type]
        rc.mark_done("find_contacts", f"r:{role.id}")
        rc.log(f"{comp.name} / {role.title}: {n_role} contacts")
        rc.progress(roles_done=i + 1, roles_total=len(pairs))
    return {"roles_searched": len(pairs), "contacts_added": found_total}


def _ai_pick_contacts(rc: RunCtx, comp: Company, role: Role, cands: list[Contact]) -> list[Contact]:
    """find_contacts step: a (small) model keeps only recruiters / HR / likely managers. Without a model, or if the
    model answers badly, the rule-based candidates are kept."""
    if len(cands) < 2 or not rc.app.llm.available("find_contacts"):
        return cands
    from jobfinder.services.guided import AnswerError, parse_numbers

    items = "\n".join(f"{i}. {c.name} — {c.title or '?'}" for i, c in enumerate(cands, 1))
    prompt = (f"Which of these people work in recruiting, talent acquisition or HR at {comp.name}, or would likely "
              f"manage the job '{role.title}'?\n{items}\n\nAnswer with the numbers separated by commas, or 0 if none. "
              "Reply with the numbers only.")
    try:
        text, model = rc.app.llm.chat("find_contacts", [{"role": "user", "content": prompt}], budget=rc.budget)
        picks = parse_numbers(text, len(cands))
    except (AnswerError, Exception) as e:  # noqa: BLE001 – keep rule-based candidates on any model problem
        rc.log(f"contact review skipped: {str(e)[:120]}")
        return cands
    rc.log(f"{model} kept {len(picks)}/{len(cands)} contacts at {comp.name}")
    return [cands[i - 1] for i in picks]


# ----------------------------------------------------------------------------- posting details


def _posting_text(rc: RunCtx, role: Role) -> str | None:
    """Full description of one posting. Raises when the page cannot be read (recorded by the caller)."""
    from jobfinder.services.pages import read_page
    from jobfinder.sources.ats import Workday
    from jobfinder.sources.careers import jobposting_to_role

    if role.source == "workday":
        return Workday.detail(Ctx(rc.http, {}, rc.log), role.model_copy()).description
    page = read_page(rc.http, role.url)
    for d in page.job_postings:
        _, jr = jobposting_to_role(d, page.url)
        if jr and jr.description:
            return jr.description
    return page.text if len(page.text) > 200 else None


def _record_role_failure(rc: RunCtx, role: Role, e: BaseException, what: str) -> str:
    status, msg = error_info(e)
    st = rc.app.store
    st.record_fetch("role", role.id, ok=False, what=what, url=role.url, http_status=status, error=msg)  # type: ignore[arg-type]
    if status in (404, 410):
        st.set_role_status(role.id, "closed")  # type: ignore[arg-type]
        if st.get_role_link(rc.pid, role.id):  # type: ignore[arg-type]
            st.link_role(rc.pid, role.id, {"status": "closed"})  # type: ignore[arg-type]
    return msg


def enrich_postings(rc: RunCtx, crit: list[Criteria], skills: SkillSet, limit: int = 25) -> dict[str, Any]:
    """Open matching postings that arrived without a real description (Workday lists, careers listings, job-API
    snippets) so skills can be compared. A posting that cannot be opened keeps its place: the failure is recorded on
    the role (Role.fetch) and retried later with back-off."""
    from jobfinder.llm import BudgetExceeded

    st = rc.app.store
    now = utcnow()
    by_id = {c.id: c for c in crit}
    todo = []
    for role, link in st.pipeline_roles(rc.pid):
        if link.excluded or role.status == "closed" or not (link.match and link.match.passed):
            continue
        if role.description and len(role.description) >= 400:
            continue
        f = role.fetch
        if f and f.what == "posting" and (f.ok or (f.next_retry_at and f.next_retry_at > now)):
            continue
        todo.append((role, link))
    read = failed = 0
    for role, link in todo[:limit]:
        rc.check()
        try:
            rc.budget.check("pages")
        except BudgetExceeded:
            break
        try:
            desc = _posting_text(rc, role)
        except Exception as e:
            msg = _record_role_failure(rc, role, e, "posting")
            failed += 1
            rc.log(f"Could not open posting '{role.title}': {msg}")
            continue
        finally:
            rc.budget.charge(pages=1)
        st.record_fetch("role", role.id, ok=True, what="posting", url=role.url)  # type: ignore[arg-type]
        read += 1
        if desc and len(desc) > len(role.description or ""):
            fresh = st.get_role(role.id)  # type: ignore[arg-type]
            fresh.description, fresh.skills = desc[:20000], []  # type: ignore[union-attr]
            saved, _ = st.upsert_role(enrich_role(fresh))  # type: ignore[arg-type]
            cr = by_id.get(link.criteria_id) or crit[0]  # type: ignore[arg-type]
            comp = st.get_company(saved.company_id) if saved.company_id else None
            m = matching.evaluate(cr, saved, comp, skills)
            st.link_role(rc.pid, saved.id, {"score": m.score, "match": m})  # type: ignore[arg-type]
    if read or failed:
        rc.log(f"Posting details: {read} read, {failed} could not be opened (kept, retried later)")
    return {"postings_read": read, "postings_failed": failed}


def ai_extract_jobs(rc: RunCtx, comp: Company) -> list[Found]:
    """extract_jobs step: a model reads a careers page without structured data. It answers with link numbers from
    the page, so it cannot invent posting URLs."""
    from jobfinder.services.normalize import parse_location
    from jobfinder.services.pages import read_page
    from jobfinder.sources.base import evidence, make_role

    page = read_page(rc.http, comp.careers_url)  # type: ignore[arg-type]
    site = {normalize_domain(page.url), comp.domain}
    links = [(t, u) for t, u in page.links if t and (normalize_domain(u) in site or detect_ats(u))][:150]
    if not links:
        return []
    listing = "\n".join(f"[{i}] {t} -> {u}" for i, (t, u) in enumerate(links))
    system = ("You read a company careers page and list its open job postings. Return {\"jobs\": [{\"title\": str, "
              "\"link\": int, \"location\": str|null}]} where link is the [number] of the link that opens that posting. "
              "Only real job postings (ignore navigation, blog, benefits). Return {\"jobs\": []} if there are none.")
    data, model = rc.app.llm.json("extract_jobs", system, f"Company: {comp.name}\nPage text:\n{page.text[:12000]}\n\n"
                                                          f"Links:\n{listing}", budget=rc.budget)
    out: list[Found] = []
    for j in (data.get("jobs") or [])[:80] if isinstance(data, dict) else []:
        try:
            idx = int(j.get("link"))
        except (TypeError, ValueError):
            continue
        if not 0 <= idx < len(links):
            continue
        url = links[idx][1]
        role = make_role(title=str(j.get("title") or links[idx][0])[:200], url=url, source="careers_page_ai",
                         location=parse_location(j.get("location")), evidence=[evidence(url, "posting", j.get("title"))])
        if role:
            out.append(Found(comp, role))
    rc.log(f"{comp.name}: {model} found {len(out)} postings on the careers page")
    return out


# ----------------------------------------------------------------------------- review


REVIEW_PROMPT = (
    "You review one job posting for one candidate. Use only the posting text. Return {\"fit\": 0-100, "
    "\"required_skills\": [str], \"nice_to_have\": [str], \"gaps\": [required skills the candidate lacks], "
    "\"concerns\": [seniority, language, location, visa or other blockers], \"summary\": \"2-3 sentences: what the "
    "job is and why it fits or not\"}."
)


def review_roles(rc: RunCtx, req: RunRequest) -> dict[str, Any]:
    """review_role step: a model compares each top posting with the candidate. Postings that cannot be opened are
    recorded on the role and skipped."""
    import json as _json

    from jobfinder.models import RoleReview

    st = rc.app.store
    if not rc.app.llm.available("review_role"):
        raise RuntimeError("No model is ready for the review_role step (see steps.yaml).")
    skills = st.get_skillset(rc.pid)
    pairs = st.pipeline_roles(rc.pid)
    if req.role_ids:
        pairs = [(r, link) for r, link in pairs if r.id in req.role_ids]
    else:
        min_score = rc.app.conn.matching.min_score
        pairs = [(r, link) for r, link in pairs if not link.excluded and r.status == "open" and link.review is None
                 and link.match and link.match.passed and (link.score or 0) >= min_score]
    cand = ", ".join(f"{s.name}{f' ({s.years:g} y)' if s.years else ''}" for s in skills.skills[:80]) or "unknown"
    reviewed = failed = 0
    todo = pairs[: req.limit or 10]
    for i, (role, _link) in enumerate(todo):
        if rc.skip("review_roles", f"r:{role.id}", role.title):
            continue
        rc.check()
        rc.at("review_roles", f"role {i + 1}/{len(todo)}: {role.title}")
        desc = role.description
        if not desc or len(desc) < 300:
            try:
                desc = _posting_text(rc, role) or desc
                st.record_fetch("role", role.id, ok=True, what="review", url=role.url)  # type: ignore[arg-type]
            except Exception as e:
                msg = _record_role_failure(rc, role, e, "review")
                failed += 1
                rc.log(f"Review skipped for '{role.title}': posting could not be opened ({msg})")
                continue
        if not desc:
            failed += 1
            continue
        comp = st.get_company(role.company_id) if role.company_id else None
        payload = {"job_title": role.title, "company": comp.name if comp else None,
                   "location": role.location.model_dump(exclude_none=True, exclude={"raw"}), "work_mode": role.work_mode,
                   "description": desc[:8000], "candidate_skills": cand, "candidate_summary": skills.summary}
        try:
            data, model = rc.app.llm.json("review_role", REVIEW_PROMPT, _json.dumps(payload, ensure_ascii=False),
                                          budget=rc.budget)
        except ValueError as e:
            rc.log(f"Review of '{role.title}' failed: {e}")
            failed += 1
            continue
        def _strs(v: Any) -> list[str]:  # noqa: E306
            return [str(x)[:80] for x in v][:25] if isinstance(v, list) else []
        try:
            fit = max(0.0, min(100.0, float(data.get("fit"))))
        except (TypeError, ValueError):
            fit = None
        review = RoleReview(model=model, at=utcnow(), fit=fit, required_skills=_strs(data.get("required_skills")),
                            nice_to_have=_strs(data.get("nice_to_have")), gaps=_strs(data.get("gaps")),
                            concerns=_strs(data.get("concerns")), summary=str(data.get("summary") or "")[:800] or None)
        st.link_role(rc.pid, role.id, {"review": review})  # type: ignore[arg-type]
        rc.mark_done("review_roles", f"r:{role.id}")
        reviewed += 1
        rc.log(f"{model} reviewed '{role.title}' at {comp.name if comp else '?'}: fit {fit}")
    return {"roles_reviewed": reviewed, "roles_unreadable": failed}


# ----------------------------------------------------------------------------- recheck


def recheck(rc: RunCtx, req: RunRequest) -> dict[str, Any]:
    st = rc.app.store
    closed = checked = 0
    todo = st.pipeline_roles(rc.pid)[: req.limit or 100]
    for i, (role, link) in enumerate(todo):
        if role.status == "closed" or link.stage in ("hired", "rejected", "withdrawn", "declined"):
            continue
        if rc.skip("recheck", f"r:{role.id}", role.title):
            continue
        rc.check()
        rc.budget.check("pages")
        h = host_of(role.url) or ""
        if is_non_employer(h) and not _ATS_HOST.search(h):
            continue
        rc.at("recheck", f"posting {i + 1}/{len(todo)}: {role.title}")
        try:
            r = rc.http.get(role.url, use_cache=False)
        except Exception as e:
            _stop_if_offline(rc, e)
            _record_role_failure(rc, role, e, "recheck")
            rc.mark_done("recheck", f"r:{role.id}")
            continue
        rc.budget.charge(pages=1)
        checked += 1
        rc.mark_done("recheck", f"r:{role.id}")
        if r.status_code in (404, 410):
            st.set_role_status(role.id, "closed")  # type: ignore[arg-type]
            st.link_role(rc.pid, role.id, {"status": "closed"})  # type: ignore[arg-type]
            st.record_fetch("role", role.id, ok=False, what="recheck", url=role.url, http_status=r.status_code,  # type: ignore[arg-type]
                            error=f"HTTP {r.status_code} (posting removed)")
            closed += 1
        elif r.status_code >= 400:
            st.record_fetch("role", role.id, ok=False, what="recheck", url=role.url, http_status=r.status_code,  # type: ignore[arg-type]
                            error=f"HTTP {r.status_code}")
        else:
            st.record_fetch("role", role.id, ok=True, what="recheck", url=role.url)  # type: ignore[arg-type]
    return {"checked": checked, "closed": closed}


def autopilot(rc: RunCtx, req: RunRequest) -> dict[str, Any]:
    """Companies → roles → (AI review) → contacts. Steps finished before an interruption are skipped on resume;
    stops that should pause the whole run (budget, cancel, no internet, no AI answering) propagate so the run is
    saved as resumable. A step that cannot run at all (e.g. no search key) is skipped and retried on resume."""
    from jobfinder.llm import BudgetExceeded, LLMUnavailable
    from jobfinder.services.runs import Cancelled

    out: dict[str, Any] = {}
    steps = [("find_companies", find_companies), ("find_roles", find_roles)]
    if req.use_llm and rc.app.llm.available("review_role"):
        steps.append(("review_roles", review_roles))
    steps.append(("find_contacts", find_contacts))
    for name, fn in steps:
        if rc.skip("autopilot", name, name):
            rc.log(f"== {name}: already finished before the interruption, skipping")
            continue
        rc.check()
        rc.log(f"== {name}")
        rc.at(name, "starting")
        try:
            out[name] = fn(rc, req)
        except (BudgetExceeded, Cancelled, Interrupted, LLMUnavailable):
            raise
        except Exception as e:
            out[name] = {"skipped": str(e)[:300]}
            rc.cp.skipped[name] = str(e)[:300]
            rc.log(f"{name} skipped: {str(e)[:300]}")
            continue
        rc.cp.skipped.pop(name, None)
        rc.mark_done("autopilot", name)
    if rc.cp.skipped:
        out["skipped_steps"] = dict(rc.cp.skipped)
    return out


def guided(rc: RunCtx, req: RunRequest) -> dict[str, Any]:
    from jobfinder.services.guided import autopilot as guided_autopilot

    return guided_autopilot(rc, req)


KINDS = {"find_roles": find_roles, "find_companies": find_companies, "find_contacts": find_contacts,
         "recheck": recheck, "autopilot": autopilot, "guided": guided, "review_roles": review_roles}
