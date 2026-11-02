import json

import httpx

from jobfinder.services.http import Http
from jobfinder.sources import SOURCES, Ctx, detect_ats
from jobfinder.sources.careers import _iter_jsonld, jobposting_to_role

ROUTES = {
    "https://boards-api.greenhouse.io/v1/boards/acme": {"name": "Acme Corp"},
    "https://boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [
        {"id": 11, "title": "Data Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/11",
         "location": {"name": "Toronto, ON"}, "updated_at": "2026-10-20T10:00:00-04:00",
         "content": "&lt;p&gt;Python &amp;amp; SQL. Hybrid.&lt;/p&gt;", "departments": [{"name": "Data"}]}]},
    "https://api.lever.co/v0/postings/beta": [
        {"id": "x1", "text": "Analista de Datos", "hostedUrl": "https://jobs.lever.co/beta/x1", "applyUrl": "https://jobs.lever.co/beta/x1/apply",
         "categories": {"location": "Monterrey", "commitment": "Full-time", "team": "BI"}, "workplaceType": "hybrid",
         "country": "MX", "descriptionPlain": "Power BI y SQL", "lists": [], "createdAt": 1790000000000,
         "salaryRange": {"min": 40000, "max": 55000, "currency": "MXN", "interval": "per-month-salary"}}],
    "https://api.ashbyhq.com/posting-api/job-board/gamma": {"jobs": [
        {"id": "a1", "title": "Pharmacy Assistant", "location": "Mississauga", "isRemote": False, "workplaceType": "OnSite",
         "employmentType": "PartTime", "descriptionPlain": "Dispensing, Kroll", "publishedAt": "2026-10-28T00:00:00Z",
         "jobUrl": "https://jobs.ashbyhq.com/gamma/a1",
         "address": {"postalAddress": {"addressLocality": "Mississauga", "addressRegion": "Ontario", "addressCountry": "Canada"}}}]},
    "https://api.adzuna.com/v1/api/jobs/mx/search/1": {"results": [
        {"id": "777", "title": "Ingeniero de <strong>Datos</strong>", "redirect_url": "https://www.adzuna.com.mx/land/ad/777",
         "description": "Python, Spark", "created": "2026-10-30T00:00:00Z", "company": {"display_name": "Delta SA de CV"},
         "location": {"display_name": "Monterrey, Nuevo León", "area": ["México", "Nuevo León", "Monterrey"]},
         "salary_min": 500000, "salary_max": 700000, "contract_time": "full_time"}]},
    "https://serpapi.com/search.json": {"jobs_results": [
        {"title": "Administrative Assistant", "company_name": "Omega Ltd", "location": "Calgary, AB, Canada", "via": "LinkedIn",
         "description": "Calendar management and Excel", "job_id": "abc",
         "detected_extensions": {"posted_at": "3 days ago", "schedule_type": "Full-time"},
         "apply_options": [{"title": "LinkedIn", "link": "https://www.linkedin.com/jobs/view/1"},
                           {"title": "Omega", "link": "https://careers.omega.ca/jobs/55"}]}]},
}


def mock_http():
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url).split("?")[0]
        if url in ROUTES:
            return httpx.Response(200, json=ROUTES[url])
        return httpx.Response(404, json={})

    return Http("test", per_domain_rps=0, respect_robots=False, transport=httpx.MockTransport(handler))


def test_greenhouse():
    f = SOURCES["greenhouse"].board(Ctx(mock_http()), "acme")
    assert f[0].company.name == "Acme Corp" and f[0].company.ats.board == "acme"
    r = f[0].role
    assert r.location.region == "CA-ON" and "Python & SQL" in r.description and r.source_id == "acme:11"


def test_lever_salary_and_mode():
    r = SOURCES["lever"].board(Ctx(mock_http()), "beta")[0].role
    assert r.work_mode == "hybrid" and r.location.country == "MX" and r.salary.period == "month"
    assert r.employment_type == "full_time"


def test_ashby():
    r = SOURCES["ashby"].board(Ctx(mock_http()), "gamma")[0].role
    assert (r.location.country, r.location.region, r.location.city) == ("CA", "CA-ON", "Mississauga")
    assert r.employment_type == "part_time" and r.work_mode == "onsite"


def test_adzuna():
    f = SOURCES["adzuna"].search(Ctx(mock_http(), {"app_id": "i", "app_key": "k"}), "data", "Monterrey", "MX", 10)
    assert f[0].role.title == "Ingeniero de Datos" and f[0].role.salary.currency == "MXN"
    assert f[0].role.location.region == "MX-NLE" and f[0].company.name == "Delta SA de CV"


def test_google_jobs_prefers_employer_link():
    f = SOURCES["serpapi_google_jobs"].search(Ctx(mock_http(), {"api_key": "k"}), "admin", None, "CA", 10)
    assert f[0].role.url == "https://careers.omega.ca/jobs/55" and f[0].company.domain == "omega.ca"
    assert f[0].role.posted_at is not None and f[0].role.location.region == "CA-AB"


def test_detect_ats():
    assert detect_ats('<a href="https://boards.greenhouse.io/embed/job_board?for=acme">').board == "acme"
    assert detect_ats("https://jobs.lever.co/beta/123").type == "lever"
    wd = detect_ats("https://masterbrand.wd5.myworkdayjobs.com/en-US/MasterBrand/job/x")
    assert wd.type == "workday" and wd.board == "masterbrand.wd5.myworkdayjobs.com|MasterBrand"
    assert detect_ats("https://example.com") is None


def test_jsonld_jobposting():
    from bs4 import BeautifulSoup

    doc = {"@context": "https://schema.org", "@graph": [{"@type": "JobPosting", "title": "Auxiliar de Farmacia",
           "datePosted": "2026-10-25", "employmentType": ["FULL_TIME"], "hiringOrganization": {"name": "Farmacias X", "sameAs": "https://farmaciasx.mx"},
           "jobLocation": {"address": {"addressLocality": "Guadalajara", "addressRegion": "Jalisco", "addressCountry": "MX"}},
           "baseSalary": {"currency": "MXN", "value": {"minValue": 9000, "maxValue": 11000, "unitText": "MONTH"}},
           "description": "<p>Surtido de recetas</p>"}]}
    html = f'<script type="application/ld+json">{json.dumps(doc)}</script>'
    items = _iter_jsonld(BeautifulSoup(html, "html.parser"))
    comp, role = jobposting_to_role(items[0], "https://farmaciasx.mx/empleos/1")
    assert comp.domain == "farmaciasx.mx" and role.location.region == "MX-JAL" and role.salary.period == "month"
    assert role.employment_type == "full_time" and role.url == "https://farmaciasx.mx/empleos/1"
