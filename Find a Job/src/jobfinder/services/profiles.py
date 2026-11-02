"""Profile helpers: resume/skills ingestion and criteria ownership rules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jobfinder.models import USER_OWNED_CRITERIA, Criteria, SearchStrategy, Skill, SkillSet
from jobfinder.services.skills import catalog, extract_skills, skills_from_csv, text_from_upload

if TYPE_CHECKING:
    from jobfinder.core import App

LLM_SKILLS_PROMPT = (
    "Extract the candidate's professional skills from this resume. Return {\"summary\": str (2 sentences), "
    "\"skills\": [{\"name\": str, \"category\": str, \"level\": 1-5|null, \"years\": number|null, \"evidence\": str}]}. "
    "Include tools, technologies, domain knowledge, certifications and languages. Max 60 skills. Do not invent."
)


def ingest_skills_file(app: App, pid: str, filename: str, data: bytes, *, mode: str = "merge",
                       use_llm: bool = True) -> dict[str, Any]:
    st = app.store
    ext = Path(filename).suffix.lower()
    text = text_from_upload(filename, data)
    llm_model = None
    resume_id = None
    summary = None
    if ext in (".csv", ".tsv"):
        found = skills_from_csv(text)
    else:
        if len(text.strip()) < 30:
            raise ValueError("Could not read text from the file (scanned PDF?). Try a DOCX or text-based PDF.")
        resume_id = st.add_resume(pid, filename, text)
        found = extract_skills(text)
        if use_llm and app.llm.available("extract_skills"):
            try:
                out, llm_model = app.llm.json("extract_skills", LLM_SKILLS_PROMPT, text[:24000])
                summary = out.get("summary")
                cat = catalog()
                for s in out.get("skills", []) or []:
                    if not s.get("name"):
                        continue
                    sid = cat.resolve(s["name"])
                    try:
                        found.append(Skill(id=sid, name=cat.name_of(sid) if sid in cat.by_id else s["name"],
                                           category=s.get("category"), level=s.get("level"), years=s.get("years"),
                                           source="llm", evidence=(s.get("evidence") or "")[:200] or None))
                    except ValueError:
                        continue
            except Exception as e:
                summary = None
                llm_model = f"skipped: {str(e)[:120]}"
    current = st.get_skillset(pid)
    if mode == "replace":
        keep = [s for s in current.skills if s.source == "manual"]
    else:
        keep = list(current.skills)
    by_id = {s.id: s for s in keep}
    added = 0
    for s in found:
        if s.id in by_id:
            prev = by_id[s.id]
            by_id[s.id] = prev.model_copy(update={k: v for k, v in s.model_dump().items()
                                                  if v is not None and getattr(prev, k) is None})
        else:
            by_id[s.id] = s
            added += 1
    new = SkillSet(profile_id=pid, skills=list(by_id.values()), summary=summary or current.summary)
    st.save_skillset(new)
    return {"resume_id": resume_id, "extracted": len(found), "added": added, "total": len(new.skills),
            "llm": llm_model, "skills": [s.model_dump(exclude_none=True) for s in new.skills]}


BUNDLE_FORMAT = "jobfinder.profile-bundle/v1"


def export_bundle(app: App, pid: str) -> dict[str, Any]:
    st = app.store
    p = st.get_profile(pid)
    if p is None:
        raise KeyError(pid)
    return {"format": BUNDLE_FORMAT, "profile": p.model_dump(mode="json", exclude_none=True, exclude={"created_at", "updated_at"}),
            "criteria": [c.model_dump(mode="json", exclude_none=True, exclude={"created_at", "updated_at"})
                         for c in st.list_criteria(pid)],
            "skills": st.get_skillset(pid).model_dump(mode="json", exclude_none=True, exclude={"updated_at"})}


def import_bundle(app: App, bundle: dict[str, Any], new_id: str | None = None) -> dict[str, Any]:
    """Load profile + criteria + skills. Existing ids get a numeric suffix instead of being overwritten."""
    from jobfinder.models import Profile

    st = app.store
    p = Profile.model_validate({k: v for k, v in bundle["profile"].items() if k not in ("created_at", "updated_at")})
    if new_id:
        p.id = new_id
    if p.id and st.get_profile(p.id):
        p.id = None  # save_profile assigns a free slug
    p = st.save_profile(p)
    crits = []
    for c in bundle.get("criteria", []):
        data = {k: v for k, v in c.items() if k not in ("created_at", "updated_at", "id")}
        crits.append(st.save_criteria(Criteria.model_validate({**data, "profile_id": p.id})).id)
    if bundle.get("skills"):
        st.save_skillset(SkillSet.model_validate({**bundle["skills"], "profile_id": p.id}))
    return {"profile": p.id, "criteria": crits}


def update_strategy(app: App, criteria_id: str, patch: dict[str, Any], actor: str) -> Criteria:
    """AI-writable path: only search_strategy can change. Read-modify-write is atomic."""
    with app.store.tx():
        cr = app.store.get_criteria(criteria_id)
        if cr is None:
            raise KeyError(criteria_id)
        merged = {**cr.search_strategy.model_dump(), **patch, "updated_by": actor}
        merged.pop("version", None)
        cr.search_strategy = SearchStrategy.model_validate({**merged, "version": cr.search_strategy.version})
        return app.store.save_criteria(cr)


def guard_user_fields(existing: Criteria, incoming: Criteria) -> list[str]:
    """Names of user-owned sections an AI tried to change (used to reject AI writes)."""
    a = json.loads(existing.model_dump_json(include=set(USER_OWNED_CRITERIA)))
    b = json.loads(incoming.model_dump_json(include=set(USER_OWNED_CRITERIA)))
    return [k for k in USER_OWNED_CRITERIA if a.get(k) != b.get(k)]
