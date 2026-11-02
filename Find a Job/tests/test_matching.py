from datetime import timedelta

from jobfinder.models import (
    Company,
    Criteria,
    Place,
    Role,
    Rule,
    Salary,
    Skill,
    SkillSet,
    SponsorshipFact,
    utcnow,
)
from jobfinder.services.matching import eval_rule, evaluate, facts
from jobfinder.services.normalize import enrich_role
from jobfinder.services.skills import catalog, extract_skills, skills_from_csv


def _role(**kw):
    base = {"title": "Data Engineer", "url": "https://acme.mx/jobs/1",
            "location": Place(country="MX", region="MX-NLE", city="Monterrey"), "work_mode": "hybrid",
            "description": "We use Python, SQL and Airflow. Experiencia con Azure Data Factory.",
            "posted_at": utcnow() - timedelta(days=3)}
    base.update(kw)
    return enrich_role(Role(**base))


SKILLS = SkillSet(profile_id="p", skills=[Skill(id="python", name="Python"), Skill(id="sql", name="SQL")])


def test_match_passes_and_explains(mx_criteria):
    m = evaluate(mx_criteria, _role(), Company(name="Acme"), SKILLS)
    assert m.passed, m.failed
    assert "Python" in m.matched_skills and "Apache Airflow" in m.missing_skills
    assert set(m.components) == {"skills", "rules", "title"}
    assert m.score > 60
    assert "salary unknown" in m.warnings


def test_location_and_remote_rules(mx_criteria):
    assert not evaluate(mx_criteria, _role(location=Place(country="CA", region="CA-ON", city="Toronto")), None, SKILLS).passed
    remote_us = _role(work_mode="remote", location=Place(country="US"))
    assert any("remote role not open" in f for f in evaluate(mx_criteria, remote_us, None, SKILLS).failed)
    remote_latam = _role(work_mode="remote", location=Place(), remote_eligibility=["LATAM"])
    assert evaluate(mx_criteria, remote_latam, None, SKILLS).passed


def test_posted_and_must_not(mx_criteria):
    old = _role(posted_at=utcnow() - timedelta(days=200))
    assert any("posted" in f for f in evaluate(mx_criteria, old, None, SKILLS).failed)
    intern = _role(title="Data Engineer Intern")
    assert any("must_not" in f for f in evaluate(mx_criteria, intern, None, SKILLS).failed)


def test_salary_floor_annualised(mx_criteria):
    low = _role(salary=Salary(min=20000, max=30000, currency="MXN", period="month"))
    m = evaluate(mx_criteria, low, None, SKILLS)
    assert any("below floor" in f for f in m.failed)
    ok = _role(salary=Salary(min=60000, max=80000, currency="MXN", period="month"))
    assert evaluate(mx_criteria, ok, None, SKILLS).passed


def test_sponsorship_required(app, profile):
    cr = Criteria.model_validate({"name": "ca", "targets": {"titles": ["pharmacy assistant"]},
                                  "constraints": {"locations": [{"country": "CA"}],
                                                  "sponsorship": {"need": "required", "programs": ["CA_LMIA"]},
                                                  "include_unknown": False}})
    role = _role(title="Pharmacy Assistant", location=Place(country="CA", region="CA-ON", city="Toronto"))
    assert any("sponsorship unknown" in f for f in evaluate(cr, role, Company(name="X"), None).failed)
    yes = Company(name="X", sponsorship=[SponsorshipFact(program="CA_LMIA", status="yes")])
    assert evaluate(cr, role, yes, None).passed


def test_rule_ops():
    f = facts(_role(salary=Salary(min=50000, currency="MXN")), Company(name="Acme", industries=["Retail"]))
    assert eval_rule(Rule(field="skills", op="any", value=["python"]), f)
    assert eval_rule(Rule(field="skills", op="all", value=["Python", "SQL"]), f)
    assert eval_rule(Rule(field="title", op="contains", value=["engineer"]), f)
    assert eval_rule(Rule(field="title", op="not_contains", value=["manager"]), f)
    assert eval_rule(Rule(field="country", op="in", value=["mx", "ca"]), f)
    assert eval_rule(Rule(field="salary_min", op="gte", value=40000), f)
    assert eval_rule(Rule(field="posted_days_ago", op="lte", value=7), f)
    assert eval_rule(Rule(field="industry", op="any", value=["retail"]), f)
    assert eval_rule(Rule(field="seniority", op="eq", value="senior"), f) is None  # unknown


def test_skills_extraction_multilingual():
    found = {s.id for s in extract_skills("5 years of experience with Python. Manejo de inventario y atención al cliente. Excel avanzado")}
    assert {"python", "inventory-management", "customer-service", "excel-advanced"} <= found
    py = next(s for s in extract_skills("5 years of experience with Python") if s.id == "python")
    assert py.years == 5
    assert catalog().resolve("Dispensación de medicamentos") == "pharmacy-dispensing"
    assert catalog().resolve("Underwater basket weaving").startswith("custom:")


def test_skills_csv():
    rows = skills_from_csv("Skill;Nivel;Años\nPython;avanzado;6\nPower BI;3;2\nMy Thing;;\n")
    by = {s.id: s for s in rows}
    assert by["python"].level == 4 and by["python"].years == 6
    assert by["power-bi"].level == 3
    assert "custom:my-thing" in by


def test_noisy_skills_filtered():
    r = _role(description="Join our sales team. Our recruiting process is fast. Strong communication skills. Python.")
    assert "python" in r.skills
    assert "sales" not in r.skills and "communication" not in r.skills


def test_title_needs_the_same_kind_of_job():
    from jobfinder.models import fold
    from jobfinder.services.matching import title_similarity as ts

    assert ts(fold("Principal Product Manager, AI Software"), "software engineer") < 40
    assert ts(fold("Data Analyst"), "data engineer") < 40
    assert ts(fold("Senior Backend Engineer (Go)"), "backend engineer") == 100
    assert ts(fold("Ingeniero de Software Sr"), "software engineer") > 60
    assert ts(fold("Engineering Manager"), "engineering manager") == 100
