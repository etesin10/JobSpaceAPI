"""Source registry."""

from __future__ import annotations

from .aggregators import Adzuna, Arbeitnow, Jooble, JSearch, Remotive, SerpApiGoogleJobs
from .ats import Ashby, Greenhouse, Lever, SmartRecruiters, Workable, Workday
from .base import Ctx, Found, Source, SourceError
from .careers import CareersPage, detect_ats, discover_careers, probe_ats

SOURCES: dict[str, Source] = {s.name: s for s in [
    Greenhouse(), Lever(), Ashby(), SmartRecruiters(), Workable(), Workday(), CareersPage(),
    Adzuna(), SerpApiGoogleJobs(), JSearch(), Jooble(), Remotive(), Arbeitnow(),
]}

BOARD_FOR_ATS = {"greenhouse": "greenhouse", "lever": "lever", "ashby": "ashby", "smartrecruiters": "smartrecruiters",
                 "workable": "workable", "workday": "workday"}

__all__ = ["BOARD_FOR_ATS", "SOURCES", "Ctx", "Found", "Source", "SourceError", "detect_ats", "discover_careers", "probe_ats"]
