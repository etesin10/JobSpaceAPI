"""Workplan: build the per-profile JSON tree and merge submissions (AI or human) back into the store."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jobfinder.models import (
    WORKPLAN_INSTRUCTIONS,
    Action,
    Budget,
    Company,
    Contact,
    Criteria,
    Evidence,
    MergeReport,
    Role,
    Workplan,
    WorkplanCompany,
    WorkplanContact,
    WorkplanRole,
    WorkplanSubmission,
    utcnow,
)
from jobfinder.services import matching
from jobfinder.services.normalize import enrich_role
from jobfinder.services.verify import accepted, verify_evidence

if TYPE_CHECKING:
    from jobfinder.core import App


def build(app: App, pid: str, *, include_excluded: bool = False, include_closed: bool = False,
          company_ids: list[str] | None = None, max_roles_per_company: int = 50,
          max_description: int | None = 800) -> Workplan:
    st = app.store
    profile = st.get_profile(pid)
    if profile is None:
        raise KeyError(pid)
    criteria = st.list_criteria(pid)
    skills = st.get_skillset(pid)
    contacts_by_role: dict[str, list[WorkplanContact]] = {}
    for ct, link in st.pipeline_contacts(pid):
        contacts_by_role.setdefault(link.role_id or "", []).append(
            WorkplanContact.model_validate({**ct.model_dump(), "pipeline": link.model_dump()}))
    roles_by_company: dict[str, list[WorkplanRole]] = {}
    for role, link in st.pipeline_roles(pid):
        if (link.excluded and not include_excluded) or (link.status == "closed" and not include_closed):
            continue
        lst = roles_by_company.setdefault(role.company_id or "", [])
        if len(lst) >= max_roles_per_company:
            continue
        if max_description is not None and role.description and len(role.description) > max_description:
            role.description = role.description[:max_description] + "… (full text: GET /api/roles/{id})"
        if link.match and max_description is not None:
            link.match.jd_summary = None
        lst.append(WorkplanRole.model_validate({**role.model_dump(), "pipeline": link.model_dump(),
                                                "contacts": [c.model_dump() for c in contacts_by_role.get(role.id or "", [])]}))
    companies = []
    for comp, link in st.pipeline_companies(pid, include_excluded=include_excluded):
        if company_ids and comp.id not in company_ids:
            continue
        companies.append(WorkplanCompany.model_validate({
            **comp.model_dump(), "pipeline": link.model_dump(),
            "roles": [r.model_dump() for r in roles_by_company.get(comp.id or "", [])]}))
    stats = st.stats(pid)
    wp = Workplan(
        generated_at=utcnow(), profile=profile, criteria=criteria, skills=skills.skills, stats=stats,
        companies=companies, instructions=WORKPLAN_INSTRUCTIONS,
        budget=Budget(max_tokens=app.conn.limits.run_budget.max_tokens,
                      max_search_calls=app.conn.limits.run_budget.max_search_calls),
        submit={"method": "POST", "path": f"/api/profiles/{pid}/workplan", "schema": "/api/schemas/workplan_submission",
                "mcp_tool": "submit_workplan"},
    )
    wp.runs_to_resume = runs_to_resume(app, pid)
    wp.next_actions = next_actions(app, pid, wp)
    return wp


def run_summary(r: Any) -> dict[str, Any]:
    cp = r.checkpoint
    return {"run_id": r.id, "kind": r.kind, "status": r.status, "stopped_at": cp.stopped_at, "reason": cp.reason,
            "items_done": sum(len(v) for v in cp.done.values()), "skipped_steps": cp.skipped or None,
            "resume_hint": cp.resume_hint, "started": r.created_at.isoformat() if r.created_at else None,
            "resume": {"mcp_tool": "resume_run", "http": f"POST /api/runs/{r.id}/resume"}}


def runs_to_resume(app: App, pid: str) -> list[dict[str, Any]]:
    return [run_summary(r) for r in app.runner.resumable(pid)]


def next_actions(app: App, pid: str, wp: Workplan) -> list[Action]:
    acts: list[Action] = []
    for r in wp.runs_to_resume:
        acts.append(Action(action="resume_run", priority=99,
                           hint=f"Run {r['run_id']} ({r['kind']}) stopped at {r['stopped_at']}: {r['reason']}. "
                                f"{r['resume_hint']}", params={"run_id": r["run_id"]}))
    min_score = app.conn.matching.min_score
    by_crit = wp.stats.get("companies_by_criteria", {})
    for cr in wp.criteria:
        if not cr.active:
            continue
        have = by_crit.get(cr.id, 0)
        target = wp.profile.target_companies_per_criteria
        if have < target:
            acts.append(Action(action="find_companies", priority=90 if have < 10 else 70, criteria_id=cr.id,
                               hint=f"Roster has {have}/{target} companies for '{cr.name}'. Find up to {min(20, target - have)} "
                                    f"more employers hiring {', '.join(cr.targets.titles[:3]) or 'the targets'} in "
                                    f"{', '.join(_loc(loc) for loc in cr.constraints.locations) or 'any location'}.",
                               params={"needed": target - have}))
        if not cr.search_strategy.queries:
            acts.append(Action(action="tune_strategy", priority=40, criteria_id=cr.id,
                               hint="No search queries yet: propose 4-10 queries (local language + English) in "
                                    "strategy_updates."))
    for comp in wp.companies:
        p = comp.pipeline
        if p and not p.excluded and p.status == "error":
            f = comp.fetch
            why = f"{f.error or 'error'}" + (f", {f.failures} attempt(s)" if f and f.failures else "") if f else "unknown error"
            acts.append(Action(action="find_roles", priority=75, company_id=comp.id,
                               criteria_id=(p.criteria_ids or [None])[0],
                               hint=f"JobFinder could not read {comp.name}'s job board ({why}). Open "
                                    f"{(f.url if f else None) or comp.careers_url or comp.website or comp.domain} yourself and "
                                    "submit the matching open roles with their posting URLs.",
                               params={"fetch": f.model_dump(mode='json') if f else None}))
        if p and not p.excluded and p.status == "new":
            acts.append(Action(action="find_roles", priority=80, company_id=comp.id,
                               criteria_id=(p.criteria_ids or [None])[0],
                               hint=f"List open roles at {comp.name} matching the criteria. "
                                    + (f"ATS: {comp.ats.type} board '{comp.ats.board}'." if comp.ats else
                                       f"Careers: {comp.careers_url or comp.website or comp.domain or 'unknown'}."),
                               params={"ats": comp.ats.model_dump() if comp.ats else None, "careers_url": comp.careers_url}))
        for r in comp.roles:
            rp = r.pipeline
            if not rp or rp.excluded or rp.status == "closed":
                continue
            if not r.description or len(r.description) < 300:
                why = (f" JobFinder could not open it ({r.fetch.error})." if r.fetch and not r.fetch.ok else "")
                acts.append(Action(action="enrich_role", priority=66 if why else 65, company_id=comp.id, role_id=r.id,
                                   hint=f"Open {r.url} and fill description, skills, salary and work_mode for "
                                        f"'{r.title}'.{why}"))
            if rp.match and rp.match.passed and (rp.score or 0) >= min_score and rp.status != "contacts_searched" \
                    and rp.stage in ("none", "shortlisted"):
                acts.append(Action(action="find_contacts", priority=50 + int((rp.score or 0) / 10), company_id=comp.id,
                                   role_id=r.id,
                                   hint=f"Find recruiter / talent acquisition / hiring manager at {comp.name} for "
                                        f"'{r.title}' ({_loc_place(r)}). Public business info only, with evidence URLs."))
    acts.sort(key=lambda a: -a.priority)
    return acts[: wp.budget.max_actions]


def _loc(loc: Any) -> str:
    return ", ".join(x for x in [loc.city, loc.region, loc.country] if x)


def _loc_place(r: Role) -> str:
    return ", ".join(x for x in [r.location.city, r.location.region, r.location.country] if x) or (r.work_mode or "?")


# ----------------------------------------------------------------------------- merge


def merge(app: App, pid: str, sub: WorkplanSubmission, *, verify: bool | None = None) -> MergeReport:
    st = app.store
    if st.get_profile(pid) is None:
        raise KeyError(pid)
    rep = MergeReport()
    do_verify = sub.require_evidence if verify is None else verify
    http = app.http() if do_verify else None
    criteria = st.list_criteria(pid, active_only=True)
    skills = st.get_skillset(pid)
    crit_by_id = {c.id: c for c in st.list_criteria(pid)}
    try:
        for wc in sub.companies:
            comp = Company.model_validate(wc.model_dump(exclude={"pipeline", "roles"}))
            _untrust(comp.evidence)
            existing = st.find_company(comp.name, comp.domain or comp.website, comp.id)
            excluding = bool(wc.pipeline and wc.pipeline.excluded)
            if existing is None and not excluding:
                if not _evidence_ok(comp.evidence, comp.domain or comp.website or comp.careers_url, http, rep,
                                    {"company": comp.name}, sub.require_evidence):
                    continue
            saved, created = st.upsert_company(comp)
            rep.companies_added += int(created)
            rep.companies_updated += int(not created)
            patch: dict[str, Any] = {"run_id": sub.run_id}
            if sub.criteria_id:
                patch["criteria_ids"] = [sub.criteria_id]
            if wc.pipeline:
                patch.update(wc.pipeline.model_dump(exclude_unset=True, exclude={"added_at", "updated_at"}))
            comp_link, _ = st.link_company(pid, saved.id, patch)  # type: ignore[arg-type]
            rep.links_updated += 1

            for wr in wc.roles:
                role = Role.model_validate(wr.model_dump(exclude={"pipeline", "contacts"}))
                _untrust(role.evidence)
                role.company_id = saved.id
                role.source = role.source or (f"agent:{sub.agent}" if sub.agent else "agent")
                existing_role = st.find_role(role)
                if existing_role is None:
                    if not role.evidence:
                        role.evidence = [Evidence(url=role.url, kind="posting")]
                    if not _evidence_ok(role.evidence, role.url, http, rep, {"role": role.title, "company": saved.name},
                                        sub.require_evidence):
                        continue
                    if any(e.http_status in (404, 410) for e in role.evidence):
                        role.status = "closed"
                role = enrich_role(role)
                saved_role, rcreated = st.upsert_role(role)
                rep.roles_added += int(rcreated)
                rep.roles_updated += int(not rcreated)
                cr = crit_by_id.get(sub.criteria_id) if sub.criteria_id else None
                if cr:
                    m = matching.evaluate(cr, saved_role, saved, skills)
                else:
                    cr, m = matching.best_match(criteria, saved_role, saved, skills)
                rpatch: dict[str, Any] = {"run_id": sub.run_id}
                if m is not None:
                    rpatch.update({"score": m.score, "match": m, "criteria_id": cr.id if cr else None})
                    cur = st.get_role_link(pid, saved_role.id)  # type: ignore[arg-type]
                    if cur is None or cur.status in ("found", "verified", "scored"):
                        rpatch["status"] = "scored" if m.passed else "rejected"
                if wr.pipeline:
                    rpatch.update(wr.pipeline.model_dump(exclude_unset=True, exclude={"added_at", "updated_at", "match", "score"}))
                st.link_role(pid, saved_role.id, rpatch)  # type: ignore[arg-type]
                rep.links_updated += 1

                for wct in wr.contacts:
                    ct = Contact.model_validate(wct.model_dump(exclude={"pipeline"}))
                    _untrust(ct.evidence)
                    ct.company_id = saved.id
                    if ct.work_email and saved.domain and not ct.work_email.lower().endswith("@" + saved.domain) \
                            and not ct.work_email.lower().split("@")[-1].endswith("." + saved.domain):
                        rep.warnings.append(f"dropped email {ct.work_email} for {ct.name}: not on {saved.domain}")
                        ct.work_email = None
                    if st.find_contact(ct) is None:
                        evs = list(ct.evidence) or [Evidence(url=p.url, kind="profile") for p in ct.profiles]
                        if not _evidence_ok(evs, None, http, rep, {"contact": ct.name, "company": saved.name},
                                            sub.require_evidence):
                            continue
                        ct.evidence = evs
                    saved_ct, ccreated = st.upsert_contact(ct)
                    rep.contacts_added += int(ccreated)
                    rep.contacts_updated += int(not ccreated)
                    cpatch = wct.pipeline.model_dump(exclude_unset=True, exclude={"added_at", "updated_at", "role_id"}) if wct.pipeline else {}
                    cpatch["run_id"] = sub.run_id
                    st.link_contact(pid, saved_ct.id, saved_role.id, cpatch)  # type: ignore[arg-type]
                    rep.links_updated += 1
                if wr.contacts:
                    cur_link = st.get_role_link(pid, saved_role.id)  # type: ignore[arg-type]
                    if cur_link and cur_link.status in ("scored", "verified", "found"):
                        st.link_role(pid, saved_role.id, {"status": "contacts_searched"})  # type: ignore[arg-type]
            if wc.roles and comp_link.status in ("new", "error"):
                st.link_company(pid, saved.id, {"status": "roles_searched"})  # type: ignore[arg-type]

        for cid, patch in sub.strategy_updates.items():
            cr = crit_by_id.get(cid)
            if cr is None or cr.profile_id != pid:
                rep.warnings.append(f"strategy update for unknown criteria {cid}")
                continue
            from jobfinder.services.profiles import update_strategy

            saved_cr = update_strategy(app, cid, dict(patch or {}), sub.agent or "agent")
            rep.strategy_versions[cid] = saved_cr.search_strategy.version
    finally:
        if http:
            http.close()
    return rep


def _untrust(evs: list[Evidence]) -> None:
    for ev in evs:
        ev.status, ev.checked_at, ev.http_status = "unverified", None, None


def _evidence_ok(evs: list[Evidence], fallback_url: str | None, http: Any, rep: MergeReport, item: dict[str, Any],
                 required: bool) -> bool:
    """Only for items new to the catalog. Client-supplied verification fields are never trusted."""
    _untrust(evs)
    if not evs and fallback_url:
        url = fallback_url if fallback_url.startswith("http") else f"https://{fallback_url}"
        evs.append(Evidence(url=url, kind="company_site"))
    if not evs:
        if required:
            rep.rejected.append({**item, "reason": "no evidence URL"})
            return False
        return True
    if http is None:
        return True
    checked = evs[:3]
    for ev in checked:
        verify_evidence(http, ev)
    if any(accepted(e) for e in checked):
        if not any(e.status == "ok" for e in checked):
            rep.warnings.append(f"{item}: evidence site blocks automated checks; accepted unverified")
        return True
    rep.rejected.append({**item, "reason": "evidence URL not reachable", "urls": [e.url for e in checked]})
    return False


def criteria_for_ai(app: App, pid: str) -> list[Criteria]:
    return app.store.list_criteria(pid, active_only=True)
