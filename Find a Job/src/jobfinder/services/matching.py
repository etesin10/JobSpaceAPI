"""Criteria evaluation: hard constraints, rules DSL and explainable scoring."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

from rapidfuzz import fuzz

from jobfinder.models import Company, Criteria, MatchResult, Role, Rule, SkillSet, fold
from jobfinder.services.skills import catalog, occupation_for, occupation_for_title

BLOCS: dict[str, set[str]] = {
    "NA": {"CA", "US", "MX"},
    "LATAM": {"MX", "GT", "SV", "HN", "NI", "CR", "PA", "CO", "VE", "EC", "PE", "BO", "CL", "AR", "UY", "PY", "BR",
              "DO", "CU", "PR"},
    "EU": {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU",
           "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"},
    "APAC": {"AU", "NZ", "JP", "KR", "CN", "HK", "SG", "IN", "PH", "MY", "ID", "TH", "VN", "TW"},
}
BLOCS["EMEA"] = BLOCS["EU"] | {"GB", "CH", "NO", "AE", "SA", "IL", "ZA", "EG", "NG", "KE", "TR", "MA"}
BLOCS["AMERICAS"] = BLOCS["NA"] | BLOCS["LATAM"]


def expand_regions(items: list[str]) -> set[str]:
    out: set[str] = set()
    for it in items:
        u = it.strip().upper()
        if u in ("GLOBAL", "WORLDWIDE", "ANYWHERE"):
            out.add("GLOBAL")
        elif u in BLOCS:
            out |= BLOCS[u]
        elif len(u) == 2:
            out.add(u)
    return out


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


# ----------------------------------------------------------------------------- facts


def facts(role: Role, company: Company | None) -> dict[str, Any]:
    sal = role.salary
    posted_days = None
    if role.posted_at:
        p = role.posted_at if role.posted_at.tzinfo else role.posted_at.replace(tzinfo=UTC)
        posted_days = (datetime.now(UTC) - p).total_seconds() / 86400
    return {
        "title": role.title,
        "description": role.description or "",
        "skills": list(role.skills),
        "company": company.name if company else None,
        "company_domain": company.domain if company else None,
        "industry": list(company.industries) if company else [],
        "country": role.location.country,
        "region": role.location.region,
        "city": role.location.city,
        "work_mode": role.work_mode,
        "employment_type": role.employment_type,
        "seniority": role.seniority,
        "salary_min": sal.annual(sal.min) if sal else None,
        "salary_max": sal.annual(sal.max) if sal else None,
        "salary_currency": sal.currency if sal else None,
        "posted_days_ago": posted_days,
        "languages": list(role.languages),
        "source": role.source,
        "sponsorship": [s.program for s in (company.sponsorship if company else []) if s.status == "yes"],
        "_lat": role.location.lat,
        "_lon": role.location.lon,
    }


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _text_has(text: str, term: str) -> bool:
    t, q = fold(text), fold(str(term))
    return re.search(r"(?<![a-z0-9])" + re.escape(q) + r"(?![a-z0-9])", t) is not None


def _norm_values(field: str, values: list[Any]) -> list[Any]:
    if field == "skills":
        cat = catalog()
        return [cat.resolve(str(v)) if not str(v).startswith("custom:") and str(v) not in cat.by_id else str(v) for v in values]
    return values


def eval_rule(rule: Rule, f: dict[str, Any]) -> bool | None:
    """True/False, or None when the fact is unknown."""
    val = f.get(rule.field)
    op = rule.op
    target = _norm_values(rule.field, _as_list(rule.value))
    if op == "exists":
        present = val not in (None, "", [])
        return present if rule.value in (None, True) else not present
    if op == "within_km":
        if f.get("_lat") is None or not isinstance(rule.value, dict):
            return None
        v = rule.value
        return haversine_km((f["_lat"], f["_lon"]), (v["lat"], v["lon"])) <= float(v.get("km", 50))
    if val is None or val == "" or (val == [] and op not in ("none", "nin", "not_contains")):
        return None
    is_text = isinstance(val, str)
    is_list = isinstance(val, list)
    if op in ("eq", "ne"):
        hit = fold(str(val)) == fold(str(rule.value)) if not isinstance(val, (int, float)) else val == rule.value
        return hit if op == "eq" else not hit
    if op in ("in", "nin", "any", "none"):
        if is_list:
            vals = {fold(str(x)) for x in val}
            hit = any(fold(str(t)) in vals for t in target)
        elif is_text and rule.field in ("title", "description", "company"):
            hit = any(_text_has(val, t) for t in target)
        else:
            hit = fold(str(val)) in {fold(str(t)) for t in target}
        return hit if op in ("in", "any") else not hit
    if op == "all":
        if is_list:
            vals = {fold(str(x)) for x in val}
            return all(fold(str(t)) in vals for t in target)
        return all(_text_has(str(val), t) for t in target)
    if op in ("contains", "not_contains"):
        text = " ".join(map(str, val)) if is_list else str(val)
        hit = any(_text_has(text, t) for t in target)
        return hit if op == "contains" else not hit
    if op == "prefix":
        return any(fold(str(val)).startswith(fold(str(t))) for t in target)
    if op == "regex":
        return any(re.search(str(t), str(val), re.I) for t in target)
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    if op == "gte":
        return num >= float(rule.value)
    if op == "lte":
        return num <= float(rule.value)
    if op == "between":
        lo, hi = rule.value
        return float(lo) <= num <= float(hi)
    return None


def _label(rule: Rule) -> str:
    return rule.label or f"{rule.field} {rule.op} {rule.value}"


# ----------------------------------------------------------------------------- constraints


def check_constraints(cr: Criteria, role: Role, company: Company | None) -> tuple[list[str], list[str]]:
    c = cr.constraints
    fails: list[str] = []
    warns: list[str] = []

    def unknown(what: str) -> None:
        (warns if c.include_unknown else fails).append(f"{what} unknown")

    if company and c.exclude_companies:
        names = {fold(x) for x in c.exclude_companies}
        if fold(company.name) in names or (company.domain and fold(company.domain) in names):
            fails.append(f"company {company.name} excluded")

    allowed_countries = {loc.country for loc in c.locations}
    places = [role.location, *role.locations]
    if role.work_mode == "remote":
        elig = expand_regions(role.remote_eligibility)
        wanted = allowed_countries | expand_regions(c.remote_eligibility)
        if not wanted:
            pass
        elif "GLOBAL" in elig or elig & wanted:
            pass
        elif any(p.country in wanted for p in places if p.country):
            pass
        elif not elig and not any(p.country for p in places):
            unknown("remote eligibility")
        else:
            fails.append(f"remote role not open to {sorted(wanted)}")
    elif c.locations:
        ok, unk = False, False
        for loc in c.locations:
            for p in places:
                if not p.country:
                    unk = True
                    continue
                if p.country != loc.country:
                    continue
                if loc.region and p.region and p.region != loc.region:
                    continue
                if loc.region and not p.region and not p.city:
                    unk = True
                    continue
                if loc.city:
                    if not p.city:
                        unk = True
                        continue
                    if fuzz.ratio(fold(p.city), fold(loc.city)) < 85:
                        # No geocoder yet: with a radius, the same region stands in for the metro area.
                        if loc.radius_km and loc.region and p.region == loc.region:
                            warns.append(f"{p.city} accepted as within {loc.radius_km:g} km of {loc.city} (same region)")
                        else:
                            continue
                ok = True
        if not ok:
            if unk:
                unknown("location")
            else:
                where = ", ".join(filter(None, [role.location.city, role.location.region, role.location.country])) or "?"
                fails.append(f"location {where} outside criteria")

    if role.work_mode and c.work_mode and role.work_mode not in c.work_mode:
        fails.append(f"work mode {role.work_mode} not in {c.work_mode}")
    elif not role.work_mode and set(c.work_mode) != {"remote", "hybrid", "onsite"}:
        unknown("work mode")

    if c.employment_type and role.employment_type and role.employment_type not in c.employment_type:
        fails.append(f"employment type {role.employment_type} not wanted")
    if c.seniority and role.seniority and role.seniority not in c.seniority:
        fails.append(f"seniority {role.seniority} not wanted")

    if c.salary and c.salary.min:
        floor = c.salary.annual(c.salary.min)
        s = role.salary
        top = (s.annual(s.max) or s.annual(s.min)) if s else None
        if top is None:
            unknown("salary")
        elif c.salary.currency and s and s.currency and s.currency != c.salary.currency:
            warns.append(f"salary in {s.currency}, floor in {c.salary.currency} (not compared)")
        elif floor and top < floor:
            fails.append(f"salary {top:,.0f} below floor {floor:,.0f}")

    if c.posted_within_days and role.posted_at:
        p = role.posted_at if role.posted_at.tzinfo else role.posted_at.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - p).days
        if age > c.posted_within_days:
            fails.append(f"posted {age} days ago")

    if c.sponsorship.need != "not_required":
        facts_ = company.sponsorship if company else []
        wanted = set(c.sponsorship.programs)
        relevant = [s for s in facts_ if not wanted or s.program in wanted]
        if any(s.status == "yes" for s in relevant):
            pass
        elif relevant and all(s.status == "no" for s in relevant) and c.sponsorship.need == "required":
            fails.append("company does not sponsor")
        elif c.sponsorship.need == "required":
            unknown("sponsorship")
        else:
            warns.append("sponsorship unknown")
    return fails, warns


# ----------------------------------------------------------------------------- scoring


# Words that say what a person *is* in a job title (head nouns). If the target names one and the posting has none of
# the same family, it is a different job ("software engineer" vs "product manager, software").
ROLE_NOUNS: dict[str, set[str]] = {
    "engineer": {"engineer", "ingeniero", "ingeniera", "ingenieur", "ingenieure", "developer", "desarrollador",
                 "desarrolladora", "developpeur", "programmer", "programador", "sre"},
    "developer": {"developer", "desarrollador", "desarrolladora", "developpeur", "engineer", "ingeniero", "programmer"},
    "analyst": {"analyst", "analista", "analyste"},
    "scientist": {"scientist", "cientifico", "cientifica", "scientifique"},
    "manager": {"manager", "gerente", "gestionnaire", "lead", "head", "director", "jefe", "coordinador"},
    "assistant": {"assistant", "asistente", "auxiliar", "adjoint", "adjointe", "aide", "clerk", "dependiente",
                  "commis", "helper"},
    "technician": {"technician", "tecnico", "tecnica", "technicien", "technicienne", "tech"},
    "administrator": {"administrator", "administrador", "administradora", "admin"},
    "accountant": {"accountant", "contador", "contadora", "comptable", "bookkeeper"},
    "designer": {"designer", "disenador", "disenadora"},
    "consultant": {"consultant", "consultor", "consultora"},
    "coordinator": {"coordinator", "coordinador", "coordinadora", "coordonnateur"},
    "recruiter": {"recruiter", "reclutador", "reclutadora"},
    "receptionist": {"receptionist", "recepcionista", "receptionniste"},
    "representative": {"representative", "representante", "agent", "agente", "associate"},
    "architect": {"architect", "arquitecto", "arquitecta"},
    "specialist": {"specialist", "especialista", "specialiste"},
}


def title_similarity(role_title: str, target: str) -> float:
    """0-100. Fuzzy token overlap, halved when the target's role noun (engineer, analyst…) is missing from the title."""
    score = fuzz.token_set_ratio(role_title, target)
    rt_words = set(re.findall(r"[a-z]+", role_title))
    tgt_words = set(re.findall(r"[a-z]+", target))
    for noun, family in ROLE_NOUNS.items():
        if noun in tgt_words or tgt_words & family and noun in family:
            if not rt_words & (family | {noun}):
                return score * 0.5
            break
    return float(score)


def target_titles(cr: Criteria) -> list[str]:
    titles = list(cr.targets.titles)
    for oc in cr.targets.occupations:
        o = occupation_for(oc.system, oc.code)
        if o:
            titles += o.all_titles()
    return list(dict.fromkeys(t for t in titles if t))


def evaluate(cr: Criteria, role: Role, company: Company | None, skills: SkillSet | None,
             default_min_score: float = 50) -> MatchResult:
    f = facts(role, company)
    fails, warns = check_constraints(cr, role, company)
    hits: list[str] = []

    for r in cr.rules.must:
        res = eval_rule(r, f)
        if res is False or (res is None and not cr.constraints.include_unknown):
            fails.append(f"must: {_label(r)}")
        elif res is None:
            warns.append(f"must unknown: {_label(r)}")
        else:
            hits.append(_label(r))
    for r in cr.rules.must_not:
        if eval_rule(r, f) is True:
            fails.append(f"must_not: {_label(r)}")

    components: dict[str, float] = {}
    w = cr.scoring.weights

    cand = skills.ids() if skills else set()
    role_sk = set(role.skills)
    cat = catalog()
    matched = sorted(cand & role_sk)
    missing = sorted(role_sk - cand)
    if role_sk and cand:
        components["skills"] = len(matched) / len(role_sk)

    total_w = sum(r.weight for r in cr.rules.should)
    if total_w > 0:
        got = 0.0
        for r in cr.rules.should:
            if eval_rule(r, f) is True:
                got += r.weight
                hits.append(_label(r))
        components["rules"] = got / total_w

    titles = target_titles(cr)
    if titles:
        rt = fold(role.title)
        best = max(title_similarity(rt, fold(t)) for t in titles)
        o = occupation_for_title(role.title)
        if o and any(fold(t) in {fold(x) for x in o.all_titles()} for t in titles):
            best = max(best, 90)
        components["title"] = best / 100

    weights = {"skills": w.skills, "rules": w.rules, "title": w.title}
    denom = sum(weights[k] for k in components)
    score = 100 * sum(components[k] * weights[k] for k in components) / denom if denom else 50.0

    kw = [cat.name_of(s) for s in sorted(role_sk)]
    for k in cr.targets.keywords:
        if role.description and _text_has(role.description, k):
            kw.append(k)
    summary = None
    if role.description:
        summary = re.sub(r"\s+", " ", role.description)[:400]

    return MatchResult(
        score=round(score, 1), passed=not fails, failed=fails, warnings=warns,
        components={k: round(v, 3) for k, v in components.items()},
        matched_skills=[cat.name_of(s) for s in matched], missing_skills=[cat.name_of(s) for s in missing],
        rules_hit=hits, keywords=list(dict.fromkeys(kw))[:30], jd_summary=summary,
    )


def best_match(criteria: list[Criteria], role: Role, company: Company | None, skills: SkillSet | None
               ) -> tuple[Criteria | None, MatchResult | None]:
    """Evaluate a role against every active criteria and keep the best (passing first)."""
    best: tuple[Criteria | None, MatchResult | None] = (None, None)
    for cr in criteria:
        if not cr.active:
            continue
        m = evaluate(cr, role, company, skills)
        cur = best[1]
        if cur is None or (m.passed, m.score) > (cur.passed, cur.score):
            best = (cr, m)
    return best
