"""Shared catalog (facts) and per-profile pipeline (state) contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field

from .common import (
    CountryCode,
    EmploymentType,
    Evidence,
    FetchStatus,
    HttpUrlStr,
    Model,
    OpenModel,
    Place,
    Salary,
    Seniority,
    SocialProfile,
    SponsorshipProgram,
    WorkMode,
)

AtsType = Literal["greenhouse", "lever", "ashby", "smartrecruiters", "workable", "workday", "icims",
                  "taleo", "successfactors", "bamboohr", "recruitee", "personio", "teamtailor", "other"]


class AtsRef(Model):
    type: AtsType
    board: str = Field(description="Board token / company slug used by the ATS API")
    url: str | None = None


class SponsorshipFact(Model):
    program: SponsorshipProgram
    status: Literal["yes", "no", "unknown"] = "unknown"
    source_url: str | None = None
    as_of: str | None = None


# ----------------------------------------------------------------------------- catalog


class Company(OpenModel):
    id: str | None = Field(None, description="c:<domain>; computed when omitted")
    name: str
    domain: str | None = Field(None, description="Registrable domain, e.g. shopify.com")
    website: HttpUrlStr | None = None
    careers_url: HttpUrlStr | None = None
    ats: AtsRef | None = None
    hq: CountryCode | None = None
    countries: list[CountryCode] = Field(default_factory=list, description="Countries where it hires")
    industries: list[str] = Field(default_factory=list)
    size: str | None = None
    description: str | None = None
    linkedin_url: HttpUrlStr | None = None
    sponsorship: list[SponsorshipFact] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    source: str | None = None
    fetch: FetchStatus | None = Field(None, description="Last attempt to read this company's job board / careers page")
    first_seen: datetime | None = None
    last_seen: datetime | None = None


class RoleReview(Model):
    """AI review of a posting against the candidate (review_role step)."""

    model: str | None = None
    at: datetime | None = None
    fit: float | None = Field(None, ge=0, le=100)
    required_skills: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    summary: str | None = None


class Role(OpenModel):
    id: str | None = Field(None, description="r:<source>:<source_id> or r:url:<hash>; computed when omitted")
    company_id: str | None = None
    title: str
    url: HttpUrlStr = Field(description="Canonical posting URL (evidence)")
    apply_url: HttpUrlStr | None = None
    description: str | None = None
    location: Place = Field(default_factory=Place)
    locations: list[Place] = Field(default_factory=list, description="Extra locations for multi-site postings")
    work_mode: WorkMode | None = None
    remote_eligibility: list[str] = Field(default_factory=list)
    employment_type: EmploymentType | None = None
    seniority: Seniority | None = None
    salary: Salary | None = None
    languages: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list, description="Skill ids required/mentioned")
    department: str | None = None
    posted_at: datetime | None = None
    status: Literal["open", "closed", "unknown"] = "open"
    source: str | None = None
    source_id: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    fetch: FetchStatus | None = Field(None, description="Last attempt to open this posting (details, recheck, review)")
    first_seen: datetime | None = None
    last_seen: datetime | None = None


class Contact(OpenModel):
    id: str | None = Field(None, description="ct:<profile-url> / ct:email:<email>; computed when omitted")
    company_id: str | None = None
    name: str
    title: str | None = None
    type: Literal["recruiter", "talent_acquisition", "hr", "hiring_manager", "team_member", "executive", "other"] = "other"
    profiles: list[SocialProfile] = Field(default_factory=list)
    work_email: str | None = None
    phone: str | None = Field(None, description="Only business numbers published by the company")
    location: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    lawful_basis: str = "legitimate_interest:job_application"
    source: str | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None


# ----------------------------------------------------------------------------- pipeline

CompanyStatus = Literal["new", "roles_searched", "exhausted", "excluded", "error"]
RoleStatus = Literal["found", "verified", "scored", "contacts_searched", "closed", "rejected"]
RoleStage = Literal["none", "shortlisted", "applied", "interview", "offer", "hired", "declined", "rejected", "withdrawn"]
ContactStatus = Literal["found", "verified", "rejected", "contacted", "replied"]


class MatchResult(Model):
    score: float = Field(0, ge=0, le=100)
    passed: bool = True
    failed: list[str] = Field(default_factory=list, description="Hard constraint / must rule failures")
    warnings: list[str] = Field(default_factory=list, description="Unknown values accepted via include_unknown")
    components: dict[str, float] = Field(default_factory=dict)
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    rules_hit: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list, description="JD keywords to mirror in a tailored resume")
    jd_summary: str | None = None


class CompanyLink(Model):
    status: CompanyStatus = "new"
    excluded: bool = False
    notes: str | None = None
    criteria_ids: list[str] = Field(default_factory=list)
    run_id: str | None = None
    added_at: datetime | None = None
    updated_at: datetime | None = None


class RoleLink(Model):
    status: RoleStatus = "found"
    stage: RoleStage = "none"
    excluded: bool = False
    notes: str | None = None
    criteria_id: str | None = None
    score: float | None = None
    match: MatchResult | None = None
    review: RoleReview | None = Field(None, description="AI review for this profile (review_role step)")
    run_id: str | None = None
    added_at: datetime | None = None
    updated_at: datetime | None = None


class ContactLink(Model):
    status: ContactStatus = "found"
    notes: str | None = None
    role_id: str | None = None
    run_id: str | None = None
    added_at: datetime | None = None
    updated_at: datetime | None = None


# ----------------------------------------------------------------------------- runs


RunKind = Literal["find_roles", "find_companies", "find_contacts", "autopilot", "recheck", "guided", "review_roles"]


class RunRequest(Model):
    kind: RunKind
    criteria_ids: list[str] = Field(default_factory=list, description="Empty = all active criteria")
    company_ids: list[str] = Field(default_factory=list)
    role_ids: list[str] = Field(default_factory=list)
    scope: Literal["new", "all", "failed"] = Field(
        "new", description="find_roles: companies not searched yet (+ failed ones due for retry) | every company "
                           "in the roster (refresh) | only companies whose board could not be read")
    limit: int | None = Field(None, ge=1, le=1000)
    use_llm: bool = True
    budget: dict[str, Any] = Field(default_factory=dict)


class Checkpoint(Model):
    """Saved progress of a run, so a stopped run can be resumed where it stopped."""

    model_config = ConfigDict(extra="allow")
    stage: str | None = Field(None, description="Current step, e.g. find_roles")
    at: str | None = Field(None, description="Human description of the current item, e.g. 'company 7/40: NVIDIA'")
    done: dict[str, list[str]] = Field(default_factory=dict, description="stage -> keys of finished items")
    skipped: dict[str, str] = Field(default_factory=dict, description="steps skipped and why (retried on resume)")
    stopped_at: str | None = Field(None, description="Where the run stopped (set when it did not finish)")
    reason: str | None = None
    resume_hint: str | None = None
    resumable: bool = False
    resumed_by: str | None = None
    dismissed: bool = False


class Run(Model):
    id: str
    profile_id: str | None
    kind: str
    status: Literal["queued", "running", "done", "failed", "cancelled", "interrupted"]
    params: dict[str, Any] = Field(default_factory=dict)
    progress: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    log: list[str] = Field(default_factory=list)
    owner_pid: int | None = Field(None, description="Process executing the run (API server or MCP process)")
    checkpoint: Checkpoint = Field(default_factory=Checkpoint)
    resumed_from: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
