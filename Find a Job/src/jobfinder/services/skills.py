"""Skill catalog, deterministic skill extraction, resume/CSV ingestion."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from jobfinder.models import Skill, fold, slugify

DATA = Path(__file__).resolve().parent.parent / "data"


@dataclass
class CatalogSkill:
    id: str
    name: str
    category: str | None
    aliases: list[str] = field(default_factory=list)


@dataclass
class Occupation:
    key: str
    codes: dict[str, str]
    titles: dict[str, list[str]]
    skills: list[str]

    def all_titles(self) -> list[str]:
        return [t for ts in self.titles.values() for t in ts]


class SkillCatalog:
    def __init__(self, entries: list[CatalogSkill]):
        self.by_id = {e.id: e for e in entries}
        self._alias_to_id: dict[str, str] = {}
        for e in entries:
            for a in [e.name, e.id, *e.aliases]:
                self._alias_to_id.setdefault(fold(a), e.id)
        aliases = sorted(self._alias_to_id, key=len, reverse=True)
        self._rx = re.compile(r"(?<![a-z0-9+#])(" + "|".join(re.escape(a) for a in aliases) + r")(?![a-z0-9+#])")

    def resolve(self, name: str) -> str:
        """Map any free-text skill to a catalog id, or custom:<slug>."""
        key = fold(name)
        if key in self._alias_to_id:
            return self._alias_to_id[key]
        from rapidfuzz import fuzz, process

        best = process.extractOne(key, list(self._alias_to_id), scorer=fuzz.ratio, score_cutoff=92)
        if best:
            return self._alias_to_id[best[0]]
        return f"custom:{slugify(name, 50)}"

    def name_of(self, sid: str) -> str:
        e = self.by_id.get(sid)
        return e.name if e else sid.removeprefix("custom:").replace("-", " ")

    def find_in_text(self, text: str) -> dict[str, list[str]]:
        """skill id -> list of matched snippets."""
        hits: dict[str, list[str]] = {}
        t = fold(text)
        for m in self._rx.finditer(t):
            sid = self._alias_to_id[m.group(1)]
            snippet = t[max(0, m.start() - 40): m.end() + 40].replace("\n", " ")
            hits.setdefault(sid, []).append(snippet)
        return hits


@lru_cache(maxsize=1)
def catalog() -> SkillCatalog:
    raw = yaml.safe_load((DATA / "skills_catalog.yaml").read_text(encoding="utf-8"))
    return SkillCatalog([CatalogSkill(r["id"], r["name"], r.get("category"), r.get("aliases", [])) for r in raw])


@lru_cache(maxsize=1)
def occupations() -> list[Occupation]:
    raw = yaml.safe_load((DATA / "occupations.yaml").read_text(encoding="utf-8"))
    out = []
    for r in raw:
        codes = {k: str(r[k]) for k in ("isco", "noc", "soc") if r.get(k)}
        out.append(Occupation(r["key"], codes, r.get("titles", {}), r.get("skills", [])))
    return out


def occupation_for(system: str, code: str) -> Occupation | None:
    key = {"ISCO-08": "isco", "NOC-2021": "noc", "SOC-2018": "soc", "ONET": "soc"}.get(system)
    if not key:
        return None
    for o in occupations():
        if o.codes.get(key) and code.startswith(o.codes[key]):
            return o
    return None


def occupation_for_title(title: str) -> Occupation | None:
    t = fold(title)
    for o in occupations():
        if any(fold(x) == t for x in o.all_titles()):
            return o
    for o in occupations():
        if any(fold(x) in t or t in fold(x) for x in o.all_titles()):
            return o
    return None


def extract_skills(text: str, source: str = "resume") -> list[Skill]:
    cat = catalog()
    out = []
    for sid, snippets in cat.find_in_text(text).items():
        e = cat.by_id[sid]
        years = _years_near(text, e)
        out.append(Skill(id=sid, name=e.name, category=e.category, source=source, years=years,  # type: ignore[arg-type]
                         evidence=snippets[0][:160]))
    return out


def _years_near(text: str, e: CatalogSkill) -> float | None:
    t = fold(text)
    for a in [e.name, *e.aliases]:
        a = re.escape(fold(a))
        m = re.search(rf"(\d{{1,2}})\+?\s*(?:years?|anos|ans)\s+(?:of\s+|de\s+|d')?(?:experience\s+(?:with|in)\s+|experiencia\s+(?:en|con)\s+)?{a}", t)
        if m:
            return float(m.group(1))
    return None


# ----------------------------------------------------------------------------- documents


def text_from_upload(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    if ext == ".docx":
        import docx

        d = docx.Document(io.BytesIO(data))
        parts = [p.text for p in d.paragraphs]
        for tbl in d.tables:
            for row in tbl.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts)
    if ext in (".txt", ".md", ".csv", ".tsv", ".json", ".rtf", ""):
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
    if ext == ".doc":
        raise ValueError("Legacy .doc is not supported; save as .docx or PDF")
    raise ValueError(f"Unsupported file type '{ext}'. Use PDF, DOCX, TXT or CSV.")


CSV_COLUMNS = {
    "name": ("skill", "name", "habilidad", "competencia", "competence"),
    "level": ("level", "nivel", "niveau", "rating"),
    "years": ("years", "anos", "years_experience", "experience", "experiencia", "annees"),
    "category": ("category", "categoria", "type", "tipo"),
    "last_used": ("last_used", "last used", "ultimo uso"),
}


def skills_from_csv(text: str) -> list[Skill]:
    """CSV with a header row. Required: skill/name. Optional: level (1-5 or words), years, category, last_used."""
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        return []
    cols = {fold(c): c for c in reader.fieldnames}
    pick = {k: next((cols[a] for a in aliases if a in cols), None) for k, aliases in CSV_COLUMNS.items()}
    if not pick["name"]:
        pick["name"] = reader.fieldnames[0]
    cat = catalog()
    out = []
    for row in reader:
        name = (row.get(pick["name"]) or "").strip()
        if not name:
            continue
        sid = cat.resolve(name)
        out.append(Skill(
            id=sid, name=cat.name_of(sid) if not sid.startswith("custom:") else name,
            category=(row.get(pick["category"]) or None) if pick["category"] else (cat.by_id[sid].category if sid in cat.by_id else None),
            level=_level(row.get(pick["level"])) if pick["level"] else None,
            years=_num(row.get(pick["years"])) if pick["years"] else None,
            last_used=int(_num(row.get(pick["last_used"])) or 0) or None if pick["last_used"] else None,
            source="csv",
        ))
    return out


def _num(v: str | None) -> float | None:
    if not v:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", v)
    return float(m.group(0).replace(",", ".")) if m else None


def _level(v: str | None) -> int | None:
    if not v:
        return None
    n = _num(v)
    if n is not None:
        return max(1, min(5, round(n)))
    words = {"basic": 1, "basico": 1, "beginner": 1, "intermediate": 3, "intermedio": 3, "advanced": 4,
             "avanzado": 4, "expert": 5, "experto": 5}
    return words.get(fold(v))
