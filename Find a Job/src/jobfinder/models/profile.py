"""Profile and candidate skills contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .common import CEFR, CountryCode, LanguageCode, Model, Place, SocialProfile


class LanguageSkill(Model):
    code: LanguageCode
    level: CEFR = "B2"


class WorkAuthorization(Model):
    country: CountryCode
    status: Literal["citizen", "permanent_resident", "work_permit", "open_work_permit", "needs_sponsorship"]
    expires: str | None = Field(None, description="YYYY-MM-DD when a permit expires")


class Profile(Model):
    id: str | None = Field(None, description="Slug; generated from name when omitted", pattern=r"^[a-z0-9][a-z0-9\-]{0,62}$")
    name: str = Field(min_length=1)
    headline: str | None = None
    email: str | None = None
    phone: str | None = None
    home: Place | None = None
    languages: list[LanguageSkill] = Field(default_factory=list)
    work_authorization: list[WorkAuthorization] = Field(default_factory=list)
    links: list[SocialProfile] = Field(default_factory=list)
    target_companies_per_criteria: int = Field(50, ge=1, le=1000, description="Roster size goal used by next_actions")
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Skill(Model):
    id: str = Field(description="Catalog id (e.g. 'python', 'pharmacy-dispensing') or 'custom:<slug>'")
    name: str
    category: str | None = None
    level: int | None = Field(None, ge=1, le=5, description="1 basic .. 5 expert")
    years: float | None = Field(None, ge=0)
    last_used: int | None = Field(None, description="Year")
    source: Literal["manual", "resume", "csv", "llm"] = "manual"
    evidence: str | None = Field(None, description="Resume snippet supporting the skill")


class SkillSet(Model):
    profile_id: str
    skills: list[Skill] = Field(default_factory=list)
    summary: str | None = Field(None, description="Short professional summary extracted from the resume")
    updated_at: datetime | None = None

    def ids(self) -> set[str]:
        return {s.id for s in self.skills}
