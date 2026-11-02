"""Evidence verification: nothing from an AI is stored unless its source URL checks out."""

from __future__ import annotations

from jobfinder.models import Evidence, utcnow
from jobfinder.services.http import Http
from jobfinder.services.identity import host_of

# Sites that block automated checks; a 403/999 there is expected, not proof of fabrication.
GUARDED = ("linkedin.com", "indeed.com", "glassdoor.com", "monster.com", "ziprecruiter.com", "facebook.com",
           "instagram.com", "x.com", "twitter.com", "computrabajo.com", "occ.com.mx")


def verify_evidence(http: Http, ev: Evidence) -> Evidence:
    host = host_of(ev.url) or ""
    if not ev.url.startswith(("http://", "https://")) or "." not in host:
        ev.status, ev.checked_at = "failed", utcnow()
        return ev
    guarded = any(host == g or host.endswith("." + g) for g in GUARDED)
    try:
        r = http.get(ev.url)
        ev.http_status = r.status_code
        if r.status_code < 400:
            ev.status = "ok"
        elif r.status_code in (401, 403, 429, 999) or (guarded and r.status_code < 500):
            ev.status = "blocked"
        else:
            ev.status = "failed"
    except Exception:
        ev.status = "blocked" if guarded else "failed"
    ev.checked_at = utcnow()
    return ev


def accepted(ev: Evidence) -> bool:
    return ev.status in ("ok", "blocked")
