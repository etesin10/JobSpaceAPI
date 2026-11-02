"""Natural keys: how companies, roles and contacts are recognised as 'the same thing' across sources."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobfinder.models.common import fold, short_hash, slugify

# Hosts that never identify an employer (job boards, ATS, social, search).
NON_EMPLOYER_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com", "workable.com", "myworkdayjobs.com",
    "workday.com", "icims.com", "taleo.net", "successfactors.com", "bamboohr.com", "recruitee.com",
    "personio.de", "personio.com", "teamtailor.com", "jobvite.com", "breezy.hr", "jazzhr.com",
    "linkedin.com", "indeed.com", "glassdoor.com", "monster.com", "ziprecruiter.com", "simplyhired.com",
    "google.com", "googleusercontent.com", "adzuna.com", "adzuna.ca", "jooble.org", "careerjet.com",
    "remotive.com", "remoteok.com", "weworkremotely.com", "arbeitnow.com", "occ.com.mx", "computrabajo.com",
    "jobbank.gc.ca", "workopolis.com", "talent.com", "facebook.com", "x.com", "twitter.com", "youtube.com",
    "wikipedia.org", "crunchbase.com", "bing.com", "duckduckgo.com", "serpapi.com", "builtin.com",
    "wellfound.com", "angel.co", "dice.com", "himalayas.app", "themuse.com", "usajobs.gov",
)

_TRACKING = re.compile(r"^(utm_.*|gh_src|gh_jid_src|ref|refid|source|src|trk|trackingid|lever-source.*|lever-origin|"
                       r"fbclid|gclid|mc_.*|_hs.*|campaign|rx_.*|from|feed_id)$", re.I)

_COMPANY_SUFFIX = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|llc|llp|plc|gmbh|ag|sa|sas|sl|srl|sapi|sab|"
    r"s de rl|s de rl de cv|sa de cv|sapi de cv|sab de cv|de cv|bv|nv|ab|oy|pty|ulc|lp)$",
    re.I,
)


def host_of(url_or_domain: str | None) -> str | None:
    if not url_or_domain:
        return None
    s = url_or_domain.strip()
    if "://" not in s:
        s = "http://" + s
    try:
        host = (urlsplit(s).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    return host or None


def normalize_domain(url_or_domain: str | None) -> str | None:
    host = host_of(url_or_domain)
    if not host or "." not in host:
        return None
    for prefix in ("www.", "www2.", "careers.", "jobs.", "career.", "work.", "join.", "apply."):
        if host.startswith(prefix) and host.count(".") >= 2:
            host = host[len(prefix):]
            break
    if is_non_employer(host):
        return None
    return host


def is_non_employer(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in NON_EMPLOYER_HOSTS)


def company_name_key(name: str) -> str:
    n = fold(name).replace(".", "")
    n = re.sub(r"[,]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    prev = None
    while prev != n:
        prev = n
        n = _COMPANY_SUFFIX.sub("", n).strip(" ,.-")
    return slugify(n)


def company_id(name: str, domain: str | None) -> str:
    return f"c:{domain}" if domain else f"c:n:{company_name_key(name)}"


def canonical_url(url: str) -> str:
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if not _TRACKING.match(k)]
    path = re.sub(r"/+$", "", p.path) or "/"
    return urlunsplit(((p.scheme or "https").lower(), (p.hostname or "").lower() + (f":{p.port}" if p.port else ""),
                       path, urlencode(sorted(query)), ""))


def role_id(url: str, source: str | None = None, source_id: str | None = None) -> str:
    if source and source_id:
        return f"r:{slugify(source, 30)}:{slugify(str(source_id), 80)}"
    return f"r:u:{short_hash(canonical_url(url), 16)}"


def role_dedup_key(company_id_: str | None, title: str, country: str | None, city: str | None) -> str:
    t = re.sub(r"\(.*?\)|\[.*?\]", "", fold(title))
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return "|".join([company_id_ or "", t, (country or "").upper(), slugify(city or "", 40)])


def linkedin_slug(url: str) -> str | None:
    m = re.search(r"linkedin\.com/(in|pub)/([^/?#]+)", url or "", re.I)
    return m.group(2).lower() if m else None


def profile_url_key(url: str) -> str:
    slug = linkedin_slug(url)
    if slug:
        return f"li:{slug}"
    return "u:" + short_hash(canonical_url(url), 16)


def contact_id(name: str, company_id_: str | None, profile_urls: list[str], email: str | None) -> str:
    for u in profile_urls:
        if u:
            return f"ct:{profile_url_key(u)}"
    if email:
        return f"ct:e:{email.strip().lower()}"
    return f"ct:n:{slugify(company_id_ or 'unknown', 40)}:{slugify(name, 40)}"
