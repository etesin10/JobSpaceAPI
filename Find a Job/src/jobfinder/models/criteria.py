"""Filtering criteria contract.

Ownership:
  constraints / targets / rules / scoring  -> user (AI may read, never write)
  search_strategy                           -> AI (tunable, versioned)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from .common import (
    CEFR,
    CountryCode,
    EmploymentType,
    LanguageCode,
    Model,
    RegionCode,
    Salary,
    Seniority,
    SponsorshipProgram,
    WorkMode,
)

RuleField = Literal[
    "title", "description", "skills", "company", "company_domain", "industry",
    "country", "region", "city", "work_mode", "employment_type", "seniority",
    "salary_min", "salary_max", "salary_currency", "posted_days_ago", "languages",
    "source", "sponsorship",
]
RuleOp = Literal[
    "eq", "ne", "in", "nin", "any", "all", "none", "contains", "not_contains",
    "prefix", "regex", "gte", "lte", "between", "exists", "within_km",
]


class Rule(Model):
    field: RuleField
    op: RuleOp
    value: Any = None
    weight: float = Field(1.0, ge=0, description="Only used by `should` rules")
    label: str | None = None


class Rules(Model):
    must: list[Rule] = Field(default_factory=list, description="All must match or the role is rejected")
    should: list[Rule] = Field(default_factory=list, description="Each match adds its weight to the rules score")
    must_not: list[Rule] = Field(default_factory=list, description="Any match rejects the role")


class LocationConstraint(Model):
    country: CountryCode
    region: RegionCode | None = None
    city: str | None = None
    radius_km: float | None = Field(None, ge=0, le=500)

    @model_validator(mode="after")
    def _region_in_country(self) -> LocationConstraint:
        if self.region and not self.region.startswith(self.country + "-"):
            raise ValueError(f"region {self.region} is not inside country {self.country}")
        return self


class SponsorshipConstraint(Model):
    need: Literal["not_required", "preferred", "required"] = "not_required"
    programs: list[SponsorshipProgram] = Field(default_factory=list)


class LanguageConstraint(Model):
    code: LanguageCode
    min: CEFR = "B2"


class Constraints(Model):
    locations: list[LocationConstraint] = Field(default_factory=list, description="Any of these (OR)")
    work_mode: list[WorkMode] = Field(default_factory=lambda: ["remote", "hybrid", "onsite"])
    remote_eligibility: list[str] = Field(
        default_factory=list,
        description="For remote roles: countries (ISO) or blocs (LATAM, NA, EU, EMEA, APAC, GLOBAL) the role must accept",
    )
    employment_type: list[EmploymentType] = Field(default_factory=list, description="Empty = any")
    seniority: list[Seniority] = Field(default_factory=list, description="Empty = any")
    salary: Salary | None = Field(None, description="Floor: roles with known max below salary.min are rejected")
    sponsorship: SponsorshipConstraint = Field(default_factory=SponsorshipConstraint)
    languages: list[LanguageConstraint] = Field(default_factory=list)
    posted_within_days: int | None = Field(30, ge=1, le=365)
    exclude_companies: list[str] = Field(default_factory=list, description="Domains or names to never include")
    include_unknown: bool = Field(True, description="Keep roles whose value is unknown (e.g. no salary published)")


class OccupationCode(Model):
    system: Literal["ISCO-08", "NOC-2021", "SOC-2018", "ONET", "SINCO-2019", "ESCO", "OTHER"]
    code: str
    label: str | None = None


class Targets(Model):
    occupations: list[OccupationCode] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list, description="Job titles in every language you accept")
    activities: list[str] = Field(default_factory=list, description="What you want to do day to day")
    industries: list[str] = Field(default_factory=list, description="NAICS codes or names")
    keywords: list[str] = Field(default_factory=list)


class ScoringWeights(Model):
    skills: float = Field(0.6, ge=0)
    rules: float = Field(0.25, ge=0)
    title: float = Field(0.15, ge=0)


class Scoring(Model):
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    min_score: float | None = Field(None, ge=0, le=100, description="Override matching.min_score")


class SearchQuery(Model):
    text: str
    location: str | None = None
    country: CountryCode | None = None
    sources: list[str] = Field(default_factory=list, description="Empty = all enabled")
    purpose: Literal["roles", "companies", "contacts"] = "roles"
    yield_total: int = 0
    yield_matched: int = 0
    last_run_at: datetime | None = None
    disabled: bool = False


class SearchStrategy(Model):
    version: int = 0
    updated_by: str | None = None
    updated_at: datetime | None = None
    queries: list[SearchQuery] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list, description="Preferred sources; empty = all enabled")
    notes: str | None = None


class Criteria(Model):
    id: str | None = Field(None, pattern=r"^[a-z0-9][a-z0-9\-]{0,62}$")
    profile_id: str | None = None
    name: str
    active: bool = True
    constraints: Constraints = Field(default_factory=Constraints)
    targets: Targets = Field(default_factory=Targets)
    rules: Rules = Field(default_factory=Rules)
    scoring: Scoring = Field(default_factory=Scoring)
    search_strategy: SearchStrategy = Field(default_factory=SearchStrategy)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def _needs_target(self) -> Criteria:
        if not (self.targets.titles or self.targets.occupations or self.targets.activities or self.targets.keywords):
            raise ValueError("criteria needs at least one of targets.titles / occupations / activities / keywords")
        return self


USER_OWNED = ("name", "active", "constraints", "targets", "rules", "scoring")
