from jobfinder.models import (
    Company,
    Contact,
    Criteria,
    Evidence,
    Place,
    Profile,
    Role,
    SearchQuery,
    SocialProfile,
    WorkplanSubmission,
)
from jobfinder.services import workplan as wp


def test_company_dedup_by_domain_and_name(app):
    st = app.store
    a, created = st.upsert_company(Company(name="Acme Inc.", website="https://www.acme.com/careers"))
    assert created and a.id == "c:acme.com"
    b, created = st.upsert_company(Company(name="ACME", domain="acme.com", industries=["Retail"]))
    assert not created and b.id == a.id and b.industries == ["Retail"]
    c, created = st.upsert_company(Company(name="Acme Inc"))  # no domain -> name match
    assert not created and c.id == a.id


def test_role_dedup_across_sources(app):
    st = app.store
    comp, _ = st.upsert_company(Company(name="Acme", domain="acme.com"))
    r1, created = st.upsert_role(Role(title="Data Engineer", url="https://boards.greenhouse.io/acme/jobs/1?gh_src=x",
                                      company_id=comp.id, location=Place(country="MX", city="Monterrey"),
                                      source="greenhouse", source_id="acme:1", skills=["python"]))
    assert created
    r2, created = st.upsert_role(Role(title="Data Engineer", url="https://www.google.com/jobs/abc", company_id=comp.id,
                                      location=Place(country="MX", city="Monterrey"), source="google_jobs",
                                      skills=["sql"], evidence=[Evidence(url="https://x.com/1")]))
    assert not created and r2.id == r1.id and set(r2.skills) == {"python", "sql"}
    r3, created = st.upsert_role(Role(title="Data Engineer", url="https://boards.greenhouse.io/acme/jobs/1"))
    assert not created and r3.id == r1.id


def test_pipeline_isolation_and_purge(app):
    st = app.store
    mx = st.save_profile(Profile(name="MX search"))
    ca = st.save_profile(Profile(name="CA search"))
    comp, _ = st.upsert_company(Company(name="Acme", domain="acme.com"))
    st.link_company(mx.id, comp.id, {"criteria_ids": ["a"]})
    assert len(st.pipeline_companies(mx.id)) == 1 and st.pipeline_companies(ca.id) == []
    ct, _ = st.upsert_contact(Contact(name="Jane Doe", company_id=comp.id,
                                      profiles=[SocialProfile(url="https://linkedin.com/in/jane-doe")]))
    st.link_contact(mx.id, ct.id, None)
    same, created = st.upsert_contact(Contact(name="Jane D.", profiles=[SocialProfile(url="https://www.linkedin.com/in/Jane-Doe/")]))
    assert not created and same.id == ct.id
    st.delete_profile(mx.id)
    assert st.get_contact(ct.id) is None  # personal data removed once no profile links it
    assert st.get_company(comp.id) is not None  # shared facts remain


def test_strategy_versioning(app, mx_criteria):
    st = app.store
    mx_criteria.search_strategy.queries = [SearchQuery(text="ingeniero de datos", country="MX")]
    v1 = st.save_criteria(mx_criteria).search_strategy.version
    assert v1 == 1
    same = st.save_criteria(st.get_criteria(mx_criteria.id))
    assert same.search_strategy.version == 1  # no change -> no bump
    assert len(st.strategy_history(mx_criteria.id)) == 2


def test_workplan_build_and_merge(app, profile, mx_criteria):
    sub = WorkplanSubmission.model_validate({
        "agent": "test", "criteria_id": mx_criteria.id, "require_evidence": False,
        "companies": [{"name": "Acme MX", "domain": "acme.mx", "evidence": [{"url": "https://acme.mx/careers"}],
                       "roles": [{"title": "Ingeniero de Datos", "url": "https://acme.mx/jobs/9",
                                  "location": {"country": "MX", "region": "MX-NLE", "city": "Monterrey"},
                                  "description": "Python y SQL", "work_mode": "onsite",
                                  "contacts": [{"name": "Luz Pérez", "title": "Reclutadora", "type": "recruiter",
                                                "work_email": "luz@gmail.com",
                                                "profiles": [{"network": "linkedin", "url": "https://linkedin.com/in/luzp"}]}]}]}],
        "strategy_updates": {mx_criteria.id: {"queries": [{"text": "ingeniero de datos monterrey"}]}},
    })
    rep = wp.merge(app, profile.id, sub)
    assert (rep.companies_added, rep.roles_added, rep.contacts_added) == (1, 1, 1)
    assert any("dropped email" in w for w in rep.warnings)
    assert rep.strategy_versions[mx_criteria.id] == 1
    plan = wp.build(app, profile.id)
    comp = plan.companies[0]
    role = comp.roles[0]
    assert role.pipeline.match.passed and role.pipeline.status == "contacts_searched"
    assert role.contacts[0].work_email is None
    assert plan.stats["companies_by_criteria"][mx_criteria.id] == 1
    # Re-submitting is idempotent and human edits apply to the pipeline only.
    rep2 = wp.merge(app, profile.id, WorkplanSubmission.model_validate({
        "require_evidence": False,
        "companies": [{"name": "Acme MX", "domain": "acme.mx", "pipeline": {"excluded": True, "notes": "not now"}}]}))
    assert rep2.companies_added == 0 and rep2.companies_updated == 1
    assert wp.build(app, profile.id).companies == []
    assert wp.build(app, profile.id, include_excluded=True).companies[0].pipeline.notes == "not now"


def test_merge_rejects_without_evidence(app, profile):
    rep = wp.merge(app, profile.id, WorkplanSubmission.model_validate(
        {"companies": [{"name": "Ghost Corp"}]}), verify=False)
    assert rep.rejected and rep.companies_added == 0


def test_next_actions(app, profile, mx_criteria):
    st = app.store
    comp, _ = st.upsert_company(Company(name="Beta", domain="beta.mx"))
    st.link_company(profile.id, comp.id, {"criteria_ids": [mx_criteria.id]})
    acts = [a.action for a in wp.build(app, profile.id).next_actions]
    assert acts[0] == "find_companies" and "find_roles" in acts and "tune_strategy" in acts


def test_user_owned_guard(app, mx_criteria):
    from jobfinder.services.profiles import guard_user_fields

    changed = mx_criteria.model_copy(deep=True)
    changed.constraints.posted_within_days = 5
    assert guard_user_fields(mx_criteria, changed) == ["constraints"]
    tuned = mx_criteria.model_copy(deep=True)
    tuned.search_strategy.notes = "x"
    assert guard_user_fields(mx_criteria, tuned) == []
    assert isinstance(mx_criteria, Criteria)
