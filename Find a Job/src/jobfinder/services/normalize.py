"""Normalisation helpers that turn messy source data into structured Role fields."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from functools import lru_cache

from bs4 import BeautifulSoup

from jobfinder.models import Place, Role, fold

_REMOTE = re.compile(r"\b(remote|remoto|remota|teletrabajo|home office|work from home|wfh|teletravail|a distance|100% remote|fully remote)\b")
_HYBRID = re.compile(r"\b(hybrid|hibrido|hibrida|hybride|mixto)\b")
_ONSITE = re.compile(r"\b(on-site|onsite|on site|presencial|in office|in-office|sur place)\b")

_SENIORITY = [
    ("intern", r"\b(intern|internship|practicante|becario|stagiaire|co-op|coop)\b"),
    ("director", r"\b(director|directora|vp|vice president|head of)\b"),
    ("manager", r"\b(manager|gerente|gestionnaire)\b"),
    ("lead", r"\b(lead|principal|staff|lider|jefe)\b"),
    ("senior", r"\b(senior|sr\.?|semi[- ]senior|ssr)\b"),
    ("junior", r"\b(junior|jr\.?|trainee)\b"),
    ("entry", r"\b(entry[- ]level|graduate|new grad|recien egresado)\b"),
]

_EMPLOYMENT = [
    ("internship", r"\b(internship|practicas profesionales|stage)\b"),
    ("part_time", r"\b(part[- ]time|medio tiempo|tiempo parcial|temps partiel)\b"),
    ("contract", r"\b(contract|contractor|contrato|por proyecto|freelance|contractuel)\b"),
    ("temporary", r"\b(temporary|temp|temporal|temporaire)\b"),
    ("full_time", r"\b(full[- ]time|tiempo completo|permanent|permanente|temps plein|indefinido)\b"),
]


def html_to_text(s: str | None) -> str | None:
    if not s:
        return s
    s = html.unescape(s)
    if "<" in s and ">" in s:
        soup = BeautifulSoup(s, "html.parser")
        for br in soup.find_all(["br", "p", "li", "div", "h1", "h2", "h3", "h4"]):
            br.insert_after("\n")
        s = soup.get_text(" ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def detect_work_mode(*texts: str | None) -> str | None:
    t = fold(" ".join(x for x in texts if x))
    if _HYBRID.search(t):
        return "hybrid"
    if _REMOTE.search(t):
        return "remote"
    if _ONSITE.search(t):
        return "onsite"
    return None


def detect_seniority(title: str) -> str | None:
    t = fold(title)
    for level, rx in _SENIORITY:
        if re.search(rx, t):
            return level
    return None


def detect_employment(*texts: str | None) -> str | None:
    t = fold(" ".join(x for x in texts if x))[:3000]
    for kind, rx in _EMPLOYMENT:
        if re.search(rx, t):
            return kind
    return None


def parse_datetime(v: object) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, (int, float)):
        ts = v / 1000 if v > 10_000_000_000 else v
        return datetime.fromtimestamp(ts, UTC)
    s = str(v).strip()
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        pass
    m = re.match(r"(\d+)\s*(hour|hora|day|dia|week|semana|month|mes)", fold(s))
    if m:
        from datetime import timedelta

        n = int(m.group(1))
        unit = m.group(2)
        delta = {"hour": timedelta(hours=n), "hora": timedelta(hours=n), "day": timedelta(days=n), "dia": timedelta(days=n),
                 "week": timedelta(weeks=n), "semana": timedelta(weeks=n), "month": timedelta(days=30 * n),
                 "mes": timedelta(days=30 * n)}[unit]
        return datetime.now(UTC) - delta
    return None


# Common city/region text -> ISO hints for the three focus markets (extend as needed).
_REGION_HINTS = {
    # Canada
    "ontario": "CA-ON", "on": "CA-ON", "quebec": "CA-QC", "qc": "CA-QC", "british columbia": "CA-BC", "bc": "CA-BC",
    "alberta": "CA-AB", "ab": "CA-AB", "manitoba": "CA-MB", "mb": "CA-MB", "saskatchewan": "CA-SK", "sk": "CA-SK",
    "nova scotia": "CA-NS", "ns": "CA-NS", "new brunswick": "CA-NB", "nb": "CA-NB",
    "newfoundland and labrador": "CA-NL", "nl": "CA-NL", "prince edward island": "CA-PE", "pei": "CA-PE",
    # Mexico
    "nuevo leon": "MX-NLE", "n.l": "MX-NLE", "nl.": "MX-NLE", "jalisco": "MX-JAL", "ciudad de mexico": "MX-CMX", "cdmx": "MX-CMX",
    "mexico city": "MX-CMX", "estado de mexico": "MX-MEX", "edomex": "MX-MEX", "queretaro": "MX-QUE",
    "guanajuato": "MX-GUA", "puebla": "MX-PUE", "chihuahua": "MX-CHH", "baja california": "MX-BCN",
    "coahuila": "MX-COA", "sonora": "MX-SON", "yucatan": "MX-YUC", "tamaulipas": "MX-TAM", "san luis potosi": "MX-SLP",
    "aguascalientes": "MX-AGU",
}
_CITY_HINTS = {
    "toronto": ("CA", "CA-ON"), "mississauga": ("CA", "CA-ON"), "ottawa": ("CA", "CA-ON"), "waterloo": ("CA", "CA-ON"),
    "kitchener": ("CA", "CA-ON"), "hamilton": ("CA", "CA-ON"), "london": (None, None), "montreal": ("CA", "CA-QC"),
    "quebec city": ("CA", "CA-QC"), "vancouver": ("CA", "CA-BC"), "calgary": ("CA", "CA-AB"), "edmonton": ("CA", "CA-AB"),
    "winnipeg": ("CA", "CA-MB"), "halifax": ("CA", "CA-NS"), "monterrey": ("MX", "MX-NLE"),
    "san pedro garza garcia": ("MX", "MX-NLE"), "guadalajara": ("MX", "MX-JAL"), "zapopan": ("MX", "MX-JAL"),
    "queretaro": ("MX", "MX-QUE"), "puebla": ("MX", "MX-PUE"), "tijuana": ("MX", "MX-BCN"), "merida": ("MX", "MX-YUC"),
    "leon": ("MX", "MX-GUA"), "saltillo": ("MX", "MX-COA"), "chihuahua": ("MX", "MX-CHH"),
    "ciudad juarez": ("MX", "MX-CHH"), "cdmx": ("MX", "MX-CMX"), "mexico city": ("MX", "MX-CMX"),
}
_COUNTRY_HINTS = {
    "canada": "CA", "mexico": "MX", "united states": "US", "usa": "US", "us": "US", "u.s.": "US", "estados unidos": "US",
    "united kingdom": "GB", "uk": "GB", "spain": "ES", "espana": "ES", "colombia": "CO", "argentina": "AR",
    "chile": "CL", "peru": "PE", "brazil": "BR", "brasil": "BR", "germany": "DE", "france": "FR", "ireland": "IE",
    "netherlands": "NL", "india": "IN", "costa rica": "CR", "guatemala": "GT", "uruguay": "UY",
}


@lru_cache(maxsize=8)
def _subdivision_index(country: str) -> dict[str, str]:
    import pycountry

    idx: dict[str, str] = {}
    for s in pycountry.subdivisions.get(country_code=country) or []:
        if getattr(s, "parent_code", None):
            continue
        idx[fold(s.name)] = s.code
        idx[s.code.split("-", 1)[1].lower()] = s.code
    return idx


def _region_for(part: str, country: str | None, allow_short: bool = False) -> str | None:
    if part in _REGION_HINTS:
        r = _REGION_HINTS[part]
        if not country or r.startswith(country):
            return r
    for cc in ([country] if country else ["CA", "US", "MX"]):
        idx = _subdivision_index(cc)
        if part in idx and (country or allow_short or len(part) > 3):
            return idx[part]
    return None


@lru_cache(maxsize=1)
def _country_names() -> dict[str, str]:
    import pycountry

    out: dict[str, str] = {}
    for c in pycountry.countries:
        for attr in ("name", "common_name", "official_name"):
            v = getattr(c, attr, None)
            if v:
                out[fold(v)] = c.alpha_2
    return out


def _country_for(part: str) -> str | None:
    if part in _COUNTRY_HINTS:
        return _COUNTRY_HINTS[part]
    if part == "can":
        return "CA"
    if part in ("mx", "mex"):
        return "MX"
    code = _country_names().get(part)
    if code and _region_for(part, None) is not None:
        return None  # "Georgia" etc.: prefer the North American region over the country
    return code


def parse_location(raw: str | None, default_country: str | None = None) -> Place:
    """Best-effort 'City, Region, Country' parsing into ISO codes."""
    if not raw or re.fullmatch(r"\s*\d+\s+(locations?|ubicaciones|lieux)\s*", raw, re.I):
        return Place(country=default_country, raw=raw) if default_country else Place(raw=raw)
    parts = [fold(p).strip(" .") for p in re.split(r"[,/|;–]|\s-\s", raw) if p.strip()]
    country, region, city = None, None, None
    for p in parts:
        country = _country_for(p)
        if country:
            break
    country = country or default_country
    region_from_first = False
    for p in parts[1:] + parts[:1]:
        if (p in _CITY_HINTS and p == parts[0]) or _country_for(p):
            continue
        r = _region_for(p, country, allow_short=p != parts[0])
        if r:
            region = r
            country = country or r[:2]
            region_from_first = p == parts[0]
            break
    for p in parts:
        if p in _CITY_HINTS:
            c, r = _CITY_HINTS[p]
            if c and (not country or country == c):
                city = p.title()
                country = country or c
                region = region or r
                break
    if not city and parts:
        if region_from_first and len(parts) > 1 and country not in ("CA", "US", "MX"):
            city = parts[0].title()  # city-states such as Berlin, Hamburg, Vienna
        for cand in parts:
            if city:
                break
            if _country_for(cand) or _REMOTE.search(cand) or (cand == parts[0] and region_from_first):
                continue
            if _region_for(cand, country, allow_short=True) and (len(parts) == 1 or cand != parts[0]):
                continue
            city = cand.title()
            break
    country = country or default_country
    try:
        return Place(country=country, region=region, city=city, raw=raw)
    except ValueError:
        return Place(raw=raw)


def enrich_role(role: Role) -> Role:
    """Fill derivable fields (work mode, seniority, employment type, skills) from text."""
    from jobfinder.services.skills import catalog

    role.description = html_to_text(role.description)
    if not role.work_mode:
        role.work_mode = detect_work_mode(role.title, role.location.raw, (role.description or "")[:4000])  # type: ignore[assignment]
    if not role.seniority:
        role.seniority = detect_seniority(role.title)  # type: ignore[assignment]
    if not role.employment_type:
        role.employment_type = detect_employment(role.title, role.description)  # type: ignore[assignment]
    if not role.skills:
        cat = catalog()
        title = f"{role.title}"
        hits = cat.find_in_text(f"{title}\n{role.description or ''}")
        title_hits = set(cat.find_in_text(title))
        skills = []
        for sid, snippets in hits.items():
            category = cat.by_id[sid].category if sid in cat.by_id else None
            if sid in title_hits:
                skills.append(sid)
            elif category == "soft":
                continue  # every ad asks for communication/teamwork; it does not discriminate
            elif category in NOISY_CATEGORIES and len(snippets) < 2:
                continue  # one mention is usually boilerplate ("our recruiting process", "sales team")
            else:
                skills.append(sid)
        role.skills = skills
    return role


NOISY_CATEGORIES = {"hr", "commercial", "service", "general", "language"}
