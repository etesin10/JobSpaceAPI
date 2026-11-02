"""Workplan: the JSON template an AI (or a human) loops over and submits back.

GET  /api/profiles/{id}/workplan  -> Workplan (criteria + skills + companies -> roles -> contacts + next_actions)
POST /api/profiles/{id}/workplan  -> WorkplanSubmission (same tree, partial OK) merged by natural keys
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from .catalog import Company, CompanyLink, Contact, ContactLink, Role, RoleLink
from .common import Model
from .criteria import Criteria
from .profile import Profile, Skill

WORKPLAN_SCHEMA = "jobfinder.workplan/v1"

ActionKind = Literal["resume_run", "find_companies", "find_roles", "enrich_role", "find_contacts", "verify",
                     "tune_strategy"]


class WorkplanContact(Contact):
    pipeline: ContactLink | None = None


class WorkplanRole(Role):
    pipeline: RoleLink | None = None
    contacts: list[WorkplanContact] = Field(default_factory=list)


class WorkplanCompany(Company):
    pipeline: CompanyLink | None = None
    roles: list[WorkplanRole] = Field(default_factory=list)


class Action(Model):
    action: ActionKind
    priority: int = 50
    criteria_id: str | None = None
    company_id: str | None = None
    role_id: str | None = None
    hint: str
    params: dict[str, Any] = Field(default_factory=dict)


class Budget(Model):
    max_actions: int = 50
    max_tokens: int = 200_000
    max_search_calls: int = 60


class Workplan(Model):
    schema_: str = Field(WORKPLAN_SCHEMA, alias="schema")
    generated_at: datetime
    profile: Profile
    criteria: list[Criteria]
    skills: list[Skill]
    stats: dict[str, Any] = Field(default_factory=dict)
    companies: list[WorkplanCompany] = Field(default_factory=list)
    runs_to_resume: list[dict[str, Any]] = Field(
        default_factory=list, description="Runs that stopped before finishing: where, why, and how to resume them")
    next_actions: list[Action] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    instructions: str
    submit: dict[str, str] = Field(default_factory=dict)


class WorkplanSubmission(Model):
    schema_: str = Field(WORKPLAN_SCHEMA, alias="schema")
    agent: str | None = Field(None, description="Who produced this, e.g. 'codex/gpt-5' or 'human'")
    run_id: str | None = None
    criteria_id: str | None = Field(None, description="Criteria these results were searched for")
    companies: list[WorkplanCompany] = Field(default_factory=list)
    strategy_updates: dict[str, Any] = Field(
        default_factory=dict,
        description="criteria_id -> SearchStrategy patch (only search_strategy is AI-writable)",
    )
    require_evidence: bool = True


class MergeReport(Model):
    companies_added: int = 0
    companies_updated: int = 0
    roles_added: int = 0
    roles_updated: int = 0
    contacts_added: int = 0
    contacts_updated: int = 0
    links_updated: int = 0
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    strategy_versions: dict[str, int] = Field(default_factory=dict)


WORKPLAN_INSTRUCTIONS = """\
You are helping a job seeker. This document is the single source of truth.
1. Read `criteria` (constraints/targets/rules are owned by the user: never change them) and `skills`.
2. Work through `next_actions` in priority order, staying inside `budget`.
   - resume_run: a JobFinder run stopped before finishing (see `runs_to_resume`: where it stopped and why).
     Fix the cause if you can (e.g. a missing key), then resume it (MCP `resume_run`, or POST
     /api/runs/{run_id}/resume). It continues where it stopped and skips everything already finished.
   - find_companies: discover employers that hire for the criteria targets in the allowed locations.
     Return them under `companies` with name, domain and at least one evidence URL (careers page or posting).
   - find_roles: for the given company, return open roles that satisfy the constraints, each with the real
     posting `url`, title, location (ISO country/region codes), work_mode, and description if available.
   - enrich_role: fill description/skills/salary for the role using its posting URL.
   - find_contacts: for the given role, find recruiters / talent acquisition / hiring managers at that company
     using public business information only (name, title, public profile URL, published business email/phone).
     Never invent emails or phone numbers. Every contact needs an evidence URL where it was found.
   - tune_strategy: propose better `search_strategy.queries` for a criteria in `strategy_updates`.
3. Submit results with POST to `submit.path` using the same tree shape (only new/changed items are needed).
   Items are matched by id or natural key (company domain, role url, contact profile url), so re-submitting is safe.
4. Anything without verifiable evidence is rejected. Do not fabricate data.
"""
