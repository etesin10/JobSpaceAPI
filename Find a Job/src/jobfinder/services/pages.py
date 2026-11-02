"""Read one web page politely and return what JobFinder needs: text, links and schema.org JobPostings."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from jobfinder.services.http import Http
from jobfinder.sources.base import require_ok
from jobfinder.sources.careers import _iter_jsonld, detect_ats


@dataclass
class Page:
    url: str
    status: int
    title: str | None
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)  # (link text, absolute url)
    job_postings: list[dict[str, Any]] = field(default_factory=list)
    ats: Any = None


def read_page(http: Http, url: str, max_chars: int = 40000) -> Page:
    """Raises SourceError / httpx errors / RobotsDenied so callers can record why a page could not be read."""
    r = http.get(url, check_robots=True)
    require_ok(r, "page")
    soup = BeautifulSoup(r.text, "html.parser")
    postings = _iter_jsonld(soup)
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(str(r.url), a["href"])
        if href.startswith("http"):
            links.append((" ".join(a.get_text(" ").split())[:120], href.split("#")[0]))
    for t in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        t.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = " ".join(main.get_text(" ").split())
    return Page(url=str(r.url), status=r.status_code, title=soup.title.string.strip() if soup.title and soup.title.string else None,
                text=text[:max_chars], links=list(dict.fromkeys(links)), job_postings=postings, ats=detect_ats(r.text))
