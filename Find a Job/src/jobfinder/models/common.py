"""Shared types and validators used by every contract."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def fold(text: str) -> str:
    """Lowercase + strip accents: 'Ingeniero de Datos' == 'ingeniero de datos', 'Montréal' == 'montreal'."""
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in norm if not unicodedata.combining(c)).lower().strip()


def slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", fold(text)).strip("-")
    return s[:max_len].strip("-") or "x"


def short_hash(text: str, n: int = 12) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


@lru_cache(maxsize=1)
def _countries() -> set[str]:
    import pycountry

    return {c.alpha_2 for c in pycountry.countries}


@lru_cache(maxsize=1)
def _subdivisions() -> set[str]:
    import pycountry

    return {s.code for s in pycountry.subdivisions}


@lru_cache(maxsize=1)
def _currencies() -> set[str]:
    import pycountry

    return {c.alpha_3 for c in pycountry.currencies}


@lru_cache(maxsize=1)
def _languages() -> set[str]:
    import pycountry

    return {lang.alpha_2 for lang in pycountry.languages if hasattr(lang, "alpha_2")}


def _country(v: str) -> str:
    v = v.strip().upper()
    if v not in _countries():
        raise ValueError(f"'{v}' is not an ISO 3166-1 alpha-2 country code (e.g. CA, MX, US)")
    return v


def _region(v: str) -> str:
    v = v.strip().upper()
    if v not in _subdivisions():
        raise ValueError(f"'{v}' is not an ISO 3166-2 code (e.g. CA-ON, MX-NLE, US-TX)")
    return v


def _currency(v: str) -> str:
    v = v.strip().upper()
    if v not in _currencies():
        raise ValueError(f"'{v}' is not an ISO 4217 currency code (e.g. CAD, MXN, USD)")
    return v


def _language(v: str) -> str:
    v = v.strip().lower()
    if v not in _languages():
        raise ValueError(f"'{v}' is not an ISO 639-1 language code (e.g. en, es, fr)")
    return v


def _http_url(v: str) -> str:
    v = v.strip()
    if "://" not in v and re.match(r"^(www\.)?[\w-]+(\.[\w-]+)*\.[a-z]{2,}(/\S*)?$", v, re.I):
        v = "https://" + v  # bare domains are common in AI/agent output
    if not re.match(r"^https?://[^\s/$.?#][^\s]*$", v, re.I):
        raise ValueError("must be an http(s) URL")
    return v


HttpUrlStr = Annotated[str, AfterValidator(_http_url)]

CountryCode = Annotated[str, AfterValidator(_country), Field(examples=["CA", "MX", "US"])]
RegionCode = Annotated[str, AfterValidator(_region), Field(examples=["CA-ON", "MX-NLE", "US-TX"])]
CurrencyCode = Annotated[str, AfterValidator(_currency), Field(examples=["CAD", "MXN", "USD"])]
LanguageCode = Annotated[str, AfterValidator(_language), Field(examples=["en", "es", "fr"])]
CEFR = Literal["A1", "A2", "B1", "B2", "C1", "C2", "native"]

WorkMode = Literal["remote", "hybrid", "onsite"]
EmploymentType = Literal["full_time", "part_time", "contract", "temporary", "internship", "freelance", "seasonal"]
Seniority = Literal["intern", "entry", "junior", "mid", "senior", "lead", "manager", "director", "executive"]
SalaryPeriod = Literal["hour", "day", "week", "month", "year"]
SponsorshipProgram = Literal[
    "CA_LMIA", "CA_GTS", "CA_IEC", "CA_CUSMA", "US_H1B", "US_TN", "US_L1", "US_O1", "US_GREEN_CARD",
    "UK_SKILLED_WORKER", "MX_WORK_VISA", "EU_BLUE_CARD", "AU_TSS", "OTHER",
]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)


class OpenModel(BaseModel):
    """Catalog objects accept unknown keys (kept under `extra`) so AI agents can add context safely."""

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)


class Place(Model):
    country: CountryCode | None = None
    region: RegionCode | None = None
    city: str | None = None
    postal_code: str | None = None
    lat: float | None = None
    lon: float | None = None
    raw: str | None = Field(None, description="Original location text as published")


class Salary(Model):
    min: float | None = Field(None, ge=0)
    max: float | None = Field(None, ge=0)
    currency: CurrencyCode | None = None
    period: SalaryPeriod = "year"

    def annual(self, value: float | None) -> float | None:
        if value is None:
            return None
        factor = {"hour": 2080, "day": 260, "week": 52, "month": 12, "year": 1}[self.period]
        return value * factor


class SocialProfile(Model):
    network: Literal["linkedin", "github", "x", "facebook", "instagram", "xing", "website", "other"] = "linkedin"
    url: HttpUrlStr
    handle: str | None = None


class Evidence(Model):
    url: HttpUrlStr
    kind: Literal["posting", "ats_api", "job_api", "search_result", "company_site", "profile", "manual"] = "posting"
    title: str | None = None
    snippet: str | None = None
    fetched_at: datetime | None = None
    checked_at: datetime | None = None
    status: Literal["unverified", "ok", "failed", "blocked", "external"] = "unverified"
    http_status: int | None = None


class FetchStatus(Model):
    """Last attempt to read something from the web (a company's job board, a posting page)."""

    ok: bool = True
    at: datetime | None = None
    what: str | None = Field(None, description="board | posting | careers_page | review")
    url: str | None = None
    http_status: int | None = None
    error: str | None = None
    failures: int = Field(0, description="Consecutive failures; 0 after a success")
    partial: bool = Field(False, description="Read, but some pages failed or the listing was cut short")
    next_retry_at: datetime | None = None

    def label(self) -> str:
        if self.ok and not self.partial:
            return "ok"
        if self.ok:
            return f"partial: {self.error or ''}".strip()
        code = f"HTTP {self.http_status}" if self.http_status else (self.error or "error")
        return f"failed ({code}, {self.failures}x)"
