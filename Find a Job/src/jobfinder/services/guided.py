"""Guided mode: one small, self-contained question at a time.

Designed for small/local models and for copy-paste into any chatbot. JobFinder does every deterministic step
(web search, reading job boards, verification, scoring); the AI only answers short questions with plain-text
replies such as "1,3", a few "Company | website" lines, or "key: value" lines. Answers are parsed leniently and
anything unclear is asked again with the reason.

Loop: task = next(profile) -> show task.prompt to the AI/user -> answer(task.id, reply) -> next task ... until
kind == "done".
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError
from rapidfuzz import fuzz

from jobfinder.models import AtsRef, Company, Contact, Criteria, Profile, RunRequest, Skill, fold, utcnow
from jobfinder.services import discovery, websearch
from jobfinder.services.identity import host_of, is_non_employer, normalize_domain
from jobfinder.services.normalize import _country_for, _region_for

if TYPE_CHECKING:
    from jobfinder.core import App
    from jobfinder.services.runs import RunCtx

TaskType = Literal["form", "text", "pick", "list", "wait", "report"]
DEFAULT_CURRENCY = {"MX": "MXN", "CA": "CAD", "US": "USD", "GB": "GBP", "ES": "EUR", "DE": "EUR", "FR": "EUR",
                    "CO": "COP", "AR": "ARS", "CL": "CLP", "PE": "PEN", "BR": "BRL", "IN": "INR", "AU": "AUD"}
SKIP_WORDS = {"skip", "pass", "omitir", "saltar", "next"}
NONE_WORDS = {"0", "none", "no", "nothing", "ninguno", "ninguna", "nada", "n/a", "na"}


class GuidedTask(BaseModel):
    id: str
    profile_id: str | None = None
    kind: str = Field(description="create_profile | choose_profile | add_skills | create_criteria | pick_companies | "
                                  "name_companies | pick_contacts | continue | done")
    type: TaskType
    needs_user: bool = Field(False, description="True: ask the human; never invent personal details")
    question: str
    items: list[str] = Field(default_factory=list)
    fields: dict[str, str] = Field(default_factory=dict)
    answer_format: str
    example: str
    prompt: str = Field("", description="Self-contained text to give any AI or chatbot")
    done_automatically: list[str] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    status: Literal["open", "answered", "skipped"] = "open"
    created_at: str | None = None


class AnswerError(ValueError):
    pass


# ----------------------------------------------------------------------------- lenient parsing


def clean_answer(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I)
    text = re.sub(r"^```\w*\s*|\s*```$", "", text.strip())
    return text.strip()


def parse_numbers(text: str, n: int) -> list[int]:
    t = fold(clean_answer(text)).strip(" .!")
    if t in NONE_WORDS or re.fullmatch(r"(none|no|0)\b.*", t) and not re.search(r"[1-9]", t):
        return []
    nums = [int(x) for x in re.findall(r"(?<![\w.])(\d{1,3})(?![\w])", t)]
    valid = [x for x in dict.fromkeys(nums) if 1 <= x <= n]
    if not valid:
        raise AnswerError(f"no item numbers between 1 and {n} found")
    return valid


_URL = re.compile(r"(https?://[^\s|,;)]+|(?:www\.)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:/[^\s|,;)]*)?)", re.I)
_CHATTER = re.compile(r"\b(sure|here (are|is)|i (don.?t|do not|can.?t|cannot|am not)|sorry|unable|as an ai|"
                      r"note that|claro|aqui (estan|tienes)|no (se|puedo))\b")


def parse_company_lines(text: str, limit: int = 12) -> list[dict[str, str | None]]:
    out: list[dict[str, str | None]] = []
    for raw in clean_answer(text).splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d{1,2}[.)])\s*", "", raw).strip()
        if not line or line.endswith(":") or (len(line) > 80 and not _URL.search(line)):
            continue
        m = _URL.search(line)
        website = m.group(1).rstrip(".") if m else None
        name = (line[: m.start()] + line[m.end():]) if m else line
        name = re.sub(r"\*\*|__|`", "", name)
        name = re.sub(r"\s*[|–—:]\s*$|^\s*[|–—:]\s*|\(\s*\)", "", name).strip(" -|,;()")
        name = re.split(r"\s+[|–—]\s+|\s+-\s+", name)[0].strip()
        if not name or len(name) > 70 or fold(name) in NONE_WORDS or _CHATTER.search(fold(name)) or len(name.split()) > 7:
            continue
        out.append({"name": name, "website": website})
        if len(out) >= limit:
            break
    if not out and fold(clean_answer(text)) not in NONE_WORDS:
        raise AnswerError("no company lines found")
    return out


FORM_KEYS: dict[str, tuple[str, ...]] = {
    "name": ("name", "full name", "nombre", "person"),
    "headline": ("headline", "title", "profession", "current job", "puesto actual"),
    "country": ("country", "pais", "pays"),
    "region": ("region", "state", "province", "estado", "provincia"),
    "city": ("city", "ciudad", "ville", "town"),
    "languages": ("languages", "language", "idiomas", "idioma"),
    "job_titles": ("job titles", "job title", "titles", "roles", "positions", "jobs", "puestos", "puesto", "job"),
    "work_mode": ("work mode", "work modes", "mode", "remote", "modalidad", "modality"),
    "min_salary": ("minimum salary", "min salary", "salary", "salario", "sueldo", "pay"),
    "sponsorship": ("sponsorship", "visa", "lmia", "work permit", "patrocinio"),
}


def parse_form(text: str, allowed: list[str]) -> dict[str, str]:
    t = clean_answer(text)
    data: dict[str, Any] = {}
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            data = {str(k): v for k, v in json.loads(m.group(0)).items()}
        except (json.JSONDecodeError, AttributeError):
            data = {}
    if not data:
        for line in t.splitlines():
            if ":" in line or "=" in line:
                k, _, v = line.partition(":") if ":" in line else line.partition("=")
                data[k] = v
    out: dict[str, str] = {}
    for k, v in data.items():
        key = re.sub(r"[^a-z ]", "", fold(str(k)).replace("_", " ")).strip()
        for canon in allowed:
            syn = FORM_KEYS.get(canon, (canon,))
            if key == canon.replace("_", " ") or key in syn or any(key.startswith(s) for s in syn):
                val = ", ".join(map(str, v)) if isinstance(v, list) else str(v)
                val = val.strip().strip("\"'").strip()
                if val and fold(val) not in ("none", "n/a", "na", "-", "any", "cualquiera", ""):
                    out.setdefault(canon, val)
                break
    if not out:
        raise AnswerError("no 'field: value' lines found")
    return out


def norm_country(v: str) -> str:
    f = fold(v).strip(" .")
    code = _country_for(f)
    if not code and len(f) == 2:
        code = f.upper()
    if not code:
        import pycountry

        try:
            code = pycountry.countries.search_fuzzy(v)[0].alpha_2
        except LookupError:
            code = None
    if not code:
        raise AnswerError(f"unknown country '{v}'")
    return code


def norm_region(v: str, country: str) -> str | None:
    s = v.strip().upper()
    if re.fullmatch(r"[A-Z]{2}-[A-Z0-9]{1,3}", s):
        return s
    return _region_for(fold(v).strip(" ."), country, allow_short=True)


def parse_salary(v: str, country: str | None) -> dict[str, Any] | None:
    t = fold(v).replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k|mil|thousand)?", t)
    if not m:
        return None
    amount = float(m.group(1)) * (1000 if m.group(2) else 1)
    cur = re.search(r"\b(mxn|cad|usd|eur|gbp|cop|ars|clp|pen|brl|inr|aud)\b", t)
    period = "year"
    for word, p in (("hour", "hour"), ("hora", "hour"), ("/h", "hour"), ("hr", "hour"), ("month", "month"),
                    ("mes", "month"), ("mensual", "month"), ("week", "week"), ("semana", "week"), ("day", "day"),
                    ("dia", "day")):
        if word in t:
            period = p
            break
    return {"min": amount, "currency": cur.group(1).upper() if cur else DEFAULT_CURRENCY.get(country or ""),
            "period": period}


# ----------------------------------------------------------------------------- engine


class Guide:
    _lock = threading.RLock()

    def __init__(self, app: App, rc: RunCtx | None = None):
        self.app = app
        self.st = app.store
        self.rc = rc

    # --------------------------------------------------------------- persistence
    def _save(self, t: GuidedTask, ctx: dict[str, Any] | None = None) -> None:
        with self.st.tx() as c:
            c.execute("INSERT INTO guided_tasks(id, profile_id, status, data, context, created_at) VALUES (?,?,?,?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data, "
                      "context=COALESCE(excluded.context, guided_tasks.context)",
                      (t.id, t.profile_id, t.status, t.model_dump_json(), json.dumps(ctx) if ctx is not None else None,
                       t.created_at))

    def get(self, tid: str) -> tuple[GuidedTask, dict[str, Any]]:
        r = self.st.conn.execute("SELECT data, context FROM guided_tasks WHERE id=?", (tid,)).fetchone()
        if not r:
            raise KeyError(tid)
        return GuidedTask.model_validate_json(r["data"]), json.loads(r["context"] or "{}")

    def _open(self, pid: str | None) -> GuidedTask | None:
        q = ("SELECT data FROM guided_tasks WHERE status='open' AND "
             + ("profile_id=?" if pid else "profile_id IS NULL") + " ORDER BY created_at DESC LIMIT 1")
        r = self.st.conn.execute(q, (pid,) if pid else ()).fetchone()
        return GuidedTask.model_validate_json(r["data"]) if r else None

    def _state(self, pid: str, key: str, default: Any = None) -> Any:
        r = self.st.conn.execute("SELECT value FROM guided_state WHERE profile_id=? AND key=?", (pid, key)).fetchone()
        return json.loads(r["value"]) if r else default

    def _set_state(self, pid: str, key: str, value: Any) -> None:
        with self.st.tx() as c:
            c.execute("INSERT OR REPLACE INTO guided_state(profile_id, key, value) VALUES (?,?,?)",
                      (pid, key, json.dumps(value)))

    def _new(self, pid: str | None, kind: str, type_: TaskType, question: str, answer_format: str, example: str, *,
             items: list[str] | None = None, fields: dict[str, str] | None = None, needs_user: bool = False,
             ctx: dict[str, Any] | None = None, done: list[str] | None = None) -> GuidedTask:
        t = GuidedTask(id=uuid.uuid4().hex[:10], profile_id=pid, kind=kind, type=type_, needs_user=needs_user,
                       question=question, items=items or [], fields=fields or {}, answer_format=answer_format,
                       example=example, done_automatically=list(done or []),
                       stats=_short_stats(self.st.stats(pid)) if pid else {}, created_at=utcnow().isoformat())
        t.prompt = _prompt(t)
        self._save(t, ctx or {})
        return t

    def _rc(self, pid: str) -> RunCtx:
        if self.rc is not None:
            return self.rc
        from jobfinder.services.runs import RunCtx

        run = self.st.create_run(pid, "guided_step", {})
        run.status, run.started_at = "running", utcnow()
        self.rc = RunCtx(self.app, run, pid, self.app.http(), self.app.budget(), threading.Event())
        self._own_rc = True
        return self.rc

    def _finish_rc(self) -> None:
        if self.rc is not None and getattr(self, "_own_rc", False):
            self.rc.run.status, self.rc.run.finished_at = "done", utcnow()
            self.rc._save(force=True)
            self.rc.http.close()
            self.rc, self._own_rc = None, False

    # --------------------------------------------------------------- next
    def next(self, pid: str | None = None, max_seconds: float = 45) -> GuidedTask:
        with self._lock:
            try:
                return self._next(pid or None, max_seconds)
            finally:
                self._finish_rc()

    def _next(self, pid: str | None, max_seconds: float) -> GuidedTask:
        if pid is None:
            t = self._open(None)
            if t:
                return t
            profiles = self.st.list_profiles()
            if not profiles:
                return self._new(None, "create_profile", "form", needs_user=True,
                                 question="Create the job seeker's profile. Ask the person for these details.",
                                 fields={"name": "full name (required)", "headline": "current or target job (optional)",
                                         "country": "country where they live, e.g. Mexico or MX",
                                         "region": "state/province (optional)", "city": "city (optional)",
                                         "languages": "e.g. Spanish native, English C1 (optional)"},
                                 answer_format="one 'field: value' per line",
                                 example="name: Ana Ruiz\nheadline: Data Engineer\ncountry: Mexico\nregion: Nuevo Leon\n"
                                         "city: Monterrey\nlanguages: Spanish native, English C1")
            if len(profiles) > 1:
                return self._new(None, "choose_profile", "pick", question="Which profile should I work on?",
                                 items=[f"{i}. {p.name} ({p.id})" for i, p in enumerate(profiles, 1)],
                                 answer_format="the number of one profile", example="1",
                                 ctx={"ids": [p.id for p in profiles]})
            pid = profiles[0].id
        if self.st.get_profile(pid) is None:  # type: ignore[arg-type]
            raise KeyError(pid)
        t = self._open(pid)
        if t:
            return t
        done: list[str] = []
        deadline = time.monotonic() + max_seconds
        for _ in range(30):
            t = self._plan(pid, done, deadline)  # type: ignore[arg-type]
            if t is not None:
                return t
        return self._wait(pid, done)  # type: ignore[arg-type]

    def _wait(self, pid: str, done: list[str]) -> GuidedTask:
        return self._new(pid, "continue", "wait", question="JobFinder is still working on the search. Reply OK to continue.",
                         answer_format="OK", example="OK", done=done)

    def _plan(self, pid: str, done: list[str], deadline: float) -> GuidedTask | None:
        st = self.st
        stopped = [r for r in self.app.runner.resumable(pid) if r.kind != "guided"]
        if stopped:
            r = stopped[0]
            cp = r.checkpoint
            return self._new(pid, "resume_run", "wait", done=done,
                             question=(f"An earlier search ({r.kind}) stopped at {cp.stopped_at}: {cp.reason}. "
                                       f"{cp.resume_hint} Reply RESUME to continue it in the background, or SKIP."),
                             answer_format="RESUME or SKIP", example="RESUME", ctx={"run_id": r.id})
        if not st.get_skillset(pid).skills and not self._state(pid, "skills_skipped"):
            return self._new(pid, "add_skills", "text", needs_user=True,
                             question="What are the person's skills? Paste their resume text, or list skills separated "
                                      "by commas. Reply SKIP to continue without skills.",
                             answer_format="resume text or a comma-separated list of skills",
                             example="Python, SQL, Power BI, Excel avanzado, customer service")
        crit = st.list_criteria(pid, active_only=True)
        if not crit:
            return self._new(pid, "create_criteria", "form", needs_user=True,
                             question="What jobs should I look for? Ask the person.",
                             fields={"job_titles": "comma-separated, any language (required)",
                                     "country": "e.g. Mexico or MX (required)", "region": "state/province (optional)",
                                     "city": "(optional)", "work_mode": "remote, hybrid and/or onsite (optional)",
                                     "min_salary": "e.g. 55000 MXN per month (optional)",
                                     "sponsorship": "yes if they need a work visa/LMIA (optional)"},
                             answer_format="one 'field: value' per line",
                             example="job_titles: data engineer, ingeniero de datos\ncountry: Mexico\nregion: Nuevo Leon\n"
                                     "city: Monterrey\nwork_mode: hybrid, onsite\nmin_salary: 55000 MXN per month")
        if time.monotonic() > deadline:
            return self._wait(pid, done)

        # 1. Automatic: read the job boards of companies not searched yet.
        new = [c for c, link in st.pipeline_companies(pid, include_excluded=False) if link.status == "new"]
        if new:
            rc = self._rc(pid)
            skills = st.get_skillset(pid)
            for comp in new[:3]:
                if time.monotonic() > deadline:
                    break
                res = discovery.company_board_roles(rc, comp, crit, skills)
                done.append(f"{comp.name}: {res.get('roles_new', 0) + res.get('roles_updated', 0)} matching roles")
            return None

        # 2. Grow the company roster per criteria.
        profile = st.get_profile(pid)
        target = profile.target_companies_per_criteria if profile else 50
        by_crit = st.stats(pid).get("companies_by_criteria", {})
        for cr in crit:
            if by_crit.get(cr.id, 0) < target and not self._state(pid, f"exhausted:{cr.id}"):
                t = self._company_task(pid, cr, done)
                if t is not None:
                    return t
                return None

        # 3. Contacts for the best roles.
        t = self._contact_task(pid, done)
        if t is True:
            return None  # handled one role automatically (no candidates); plan again
        if isinstance(t, GuidedTask):
            return t
        return self._report(pid, done)

    def _report(self, pid: str, done: list[str]) -> GuidedTask:
        s = _short_stats(self.st.stats(pid))
        return self._new(pid, "done", "report",
                         question=(f"Search complete for now: {s['companies']} companies, {s['roles']} matching roles, "
                                   f"{s['contacts']} contacts. Tell the person these numbers. Reply MORE to look for "
                                   "10 more companies, or DONE."),
                         answer_format="MORE or DONE", example="DONE", done=done)

    def _known_keys(self, pid: str) -> tuple[set[str], list[str]]:
        keys, names = set(), []
        for c, _ in self.st.pipeline_companies(pid):
            names.append(c.name)
            if c.domain:
                keys.add(f"d:{c.domain}")
            if c.ats:
                keys.add(f"a:{c.ats.type}:{c.ats.board.lower()}")
            keys.add(f"n:{fold(c.name)}")
        return keys, names

    def _company_task(self, pid: str, cr: Criteria, done: list[str]) -> GuidedTask | None:
        titles = ", ".join(discovery.localized_titles(cr, cr.constraints.locations[0].country if cr.constraints.locations else None, 3))
        where = "; ".join(filter(None, (discovery.location_text(loc.country, loc.region, loc.city)
                                        for loc in cr.constraints.locations))) or "any location"
        if "remote" in cr.constraints.work_mode and cr.constraints.work_mode != ["onsite"]:
            where += " (remote OK)"
        known, names = self._known_keys(pid)
        conn = self.app.conn
        if websearch.available(conn):
            used = set(self._state(pid, f"cq:{cr.id}", []))
            for q, country in discovery.company_queries(cr):
                if q in used:
                    continue
                used.add(q)
                self._set_state(pid, f"cq:{cr.id}", sorted(used))
                http = self.app.http()
                try:
                    hits = websearch.web_search(conn, http, q, 10, country)
                except Exception as e:
                    done.append(f"web search failed: {str(e)[:120]}")
                    continue
                finally:
                    http.close()
                cands = []
                for h in hits:
                    ats = discovery.detect_ats(h.url)
                    host = host_of(h.url)
                    if not ats and (not host or is_non_employer(host)):
                        continue
                    key = f"a:{ats.type}:{ats.board.lower()}" if ats else f"d:{normalize_domain(h.url)}"
                    if key in known or any(c["key"] == key for c in cands):
                        continue
                    cands.append({"key": key, "title": h.title[:120], "url": h.url, "snippet": h.snippet[:160],
                                  "ats": ats.model_dump() if ats else None})
                if cands:
                    return self._new(
                        pid, "pick_companies", "pick", done=done,
                        question=(f"Which of these search results belong to a company (an employer, not a job site) "
                                  f"that hires {titles} in {where}?"),
                        items=[f"{i}. {c['title']} — {c['url']}\n   {c['snippet']}" for i, c in enumerate(cands, 1)],
                        answer_format="the numbers of the good results separated by commas, or 0 if none",
                        example="1,3", ctx={"criteria_id": cr.id, "cands": cands})
        rounds = self._state(pid, f"lr:{cr.id}", 0)
        if rounds >= 3:
            self._set_state(pid, f"exhausted:{cr.id}", True)
            done.append(f"no more companies found for '{cr.name}'")
            return None
        self._set_state(pid, f"lr:{cr.id}", rounds + 1)
        avoid = f" Do not repeat: {', '.join(names[-20:])}." if names else ""
        return self._new(
            pid, "name_companies", "list", done=done,
            question=(f"Name up to 8 real companies that hire {titles} in {where}. Only companies you are sure exist; "
                      f"they will be checked.{avoid}"),
            answer_format="one company per line: Company name | website (website optional)",
            example="Acme Corp | acme.com\nGlobex | globex.com.mx", ctx={"criteria_id": cr.id})

    def _contact_task(self, pid: str, done: list[str]) -> GuidedTask | bool | None:
        """Task to ask, True if a role was settled automatically, None when there is nothing left to do."""
        conn = self.app.conn
        if not websearch.available(conn):
            return None
        st = self.st
        pairs = [(r, link) for r, link in st.pipeline_roles(pid)
                 if not link.excluded and link.match and link.match.passed and r.status == "open"
                 and (link.score or 0) >= conn.matching.min_score and link.stage in ("none", "shortlisted")]
        if sum(1 for _, link in pairs if link.status == "contacts_searched") >= conn.matching.contacts_for_top_n_roles:
            return None
        todo = [(r, link) for r, link in pairs if link.status != "contacts_searched"]
        if not todo:
            return None
        role, _ = todo[0]
        comp = st.get_company(role.company_id) if role.company_id else None
        if comp is None:
            st.link_role(pid, role.id, {"status": "contacts_searched"})  # type: ignore[arg-type]
            return True
        cache_key = f"ch:{comp.id}"
        hits = [websearch.SearchHit(**h) for h in self._state(pid, cache_key, [])]
        if not hits:
            http = self.app.http()
            try:
                for q in discovery.contact_queries(comp, role):
                    try:
                        hits += websearch.web_search(conn, http, q, 10, role.location.country)
                    except Exception as e:
                        done.append(f"contact search failed: {str(e)[:120]}")
            finally:
                http.close()
            self._set_state(pid, cache_key, [h.dict() for h in hits])
        cands: list[Contact] = []
        for h in hits:
            ct = discovery.parse_profile_hit(h, comp)
            if ct and all(ct.profiles[0].url != c.profiles[0].url for c in cands):
                cands.append(ct)
        if not cands:
            st.link_role(pid, role.id, {"status": "contacts_searched"})  # type: ignore[arg-type]
            done.append(f"no public contacts found for {comp.name}")
            return True
        cands = cands[:10]
        return self._new(
            pid, "pick_contacts", "pick", done=done,
            question=(f"Which of these people work in recruiting, talent acquisition or HR at {comp.name}, or would "
                      f"likely manage the job '{role.title}'?"),
            items=[f"{i}. {c.name} — {c.title or '?'}" for i, c in enumerate(cands, 1)],
            answer_format="the numbers separated by commas, or 0 if none", example="1,2",
            ctx={"role_id": role.id, "cands": [c.model_dump(mode="json") for c in cands]})

    # --------------------------------------------------------------- answer
    def answer(self, tid: str, text: str) -> dict[str, Any]:
        with self._lock:
            try:
                return self._answer(tid, text)
            finally:
                self._finish_rc()

    def _answer(self, tid: str, text: str) -> dict[str, Any]:
        t, ctx = self.get(tid)
        pid = t.profile_id
        if t.status != "open":
            nxt = self._next(pid, 45)
            return {"ok": False, "message": "That question was already answered.", "next": nxt.model_dump()}
        a = clean_answer(text)
        try:
            if fold(a).strip(" .!") in SKIP_WORDS and t.kind not in ("create_profile", "create_criteria"):
                msg = self._skip(t, ctx)
                t.status = "skipped"
            else:
                msg, pid = self._apply(t, ctx, a)
                t.status = "answered"
        except (AnswerError, ValidationError, ValueError) as e:
            reason = _readable(e)
            return {"ok": False, "message": f"I could not use that answer ({reason}). {t.answer_format}. "
                                            f"Example: {t.example}", "next": t.model_dump()}
        self._save(t)
        nxt = self._next(pid, 45)
        return {"ok": True, "message": msg, "next": nxt.model_dump()}

    def _skip(self, t: GuidedTask, ctx: dict[str, Any]) -> str:
        pid = t.profile_id
        if t.kind == "resume_run":
            self.app.runner.dismiss(ctx["run_id"])
            return "OK, that stopped search will not be offered again."
        if t.kind == "add_skills" and pid:
            self._set_state(pid, "skills_skipped", True)
        if t.kind == "pick_contacts" and pid:
            self.st.link_role(pid, ctx["role_id"], {"status": "contacts_searched"})
        return "Skipped."

    def _apply(self, t: GuidedTask, ctx: dict[str, Any], a: str) -> tuple[str, str | None]:
        pid = t.profile_id
        st = self.st
        if t.kind == "continue":
            return "Continuing.", pid
        if t.kind == "resume_run":
            try:
                new = self.app.runner.resume(ctx["run_id"])
            except ValueError as e:  # already resumed / still running: nothing to do
                return f"Not resumed: {e}", pid
            return f"Resumed as run {new.id}; it continues in the background.", pid
        if t.kind == "choose_profile":
            n = parse_numbers(a, len(ctx["ids"]))[0]
            return f"Working on {ctx['ids'][n - 1]}.", ctx["ids"][n - 1]
        if t.kind == "create_profile":
            f = parse_form(a, ["name", "headline", "country", "region", "city", "languages"])
            if "name" not in f:
                raise AnswerError("name is required")
            home: dict[str, Any] = {}
            if f.get("country"):
                home["country"] = norm_country(f["country"])
                if f.get("region"):
                    home["region"] = norm_region(f["region"], home["country"])
            if f.get("city"):
                home["city"] = f["city"]
            p = st.save_profile(Profile.model_validate({
                "name": f["name"], "headline": f.get("headline"), "home": {k: v for k, v in home.items() if v} or None,
                "languages": _languages(f.get("languages", ""))}))
            return f"Created profile {p.id}.", p.id
        assert pid is not None
        if t.kind == "add_skills":
            if len(a) < 3:
                raise AnswerError("too short")
            return self._add_skills(pid, a), pid
        if t.kind == "create_criteria":
            cr = self._criteria_from_form(pid, parse_form(a, list(t.fields)))
            saved = st.save_criteria(cr)
            return f"Saved criteria '{saved.name}'.", pid
        if t.kind == "pick_companies":
            picks = parse_numbers(a, len(ctx["cands"]))
            return self._add_companies(pid, ctx["criteria_id"], [_cand_to_company(ctx["cands"][i - 1]) for i in picks]), pid
        if t.kind == "name_companies":
            lines = parse_company_lines(a)
            return self._add_companies(pid, ctx["criteria_id"], [{"name": x["name"], "domain": normalize_domain(x["website"]),
                                                                   "website": x["website"]} for x in lines]), pid
        if t.kind == "pick_contacts":
            picks = parse_numbers(a, len(ctx["cands"]))
            for i in picks:
                saved, _ = st.upsert_contact(Contact.model_validate(ctx["cands"][i - 1]))
                st.link_contact(pid, saved.id, ctx["role_id"], {})  # type: ignore[arg-type]
            st.link_role(pid, ctx["role_id"], {"status": "contacts_searched"})
            return f"Saved {len(picks)} contact(s).", pid
        if t.kind == "done":
            if "more" in fold(a) or "mas" in fold(a):
                p = st.get_profile(pid)
                if p:
                    have = max(st.stats(pid)["companies_by_criteria"].values(), default=0)
                    p.target_companies_per_criteria = max(p.target_companies_per_criteria, have) + 10
                    st.save_profile(p)
                for cr in st.list_criteria(pid):
                    self._set_state(pid, f"exhausted:{cr.id}", False)
                    self._set_state(pid, f"lr:{cr.id}", 0)
                return "Looking for 10 more companies.", pid
            return "Finished.", pid
        raise AnswerError(f"unknown task kind {t.kind}")

    def _add_skills(self, pid: str, a: str) -> str:
        from jobfinder.services.profiles import ingest_skills_file
        from jobfinder.services.skills import catalog

        parts = [p.strip() for p in re.split(r"[,;\n]", a) if p.strip()]
        if len(a) < 400 and parts and all(len(p) < 50 for p in parts):
            cat = catalog()
            cur = self.st.get_skillset(pid)
            for p in parts:
                sid = cat.resolve(p)
                cur.skills.append(Skill(id=sid, name=cat.name_of(sid) if sid in cat.by_id else p, source="manual"))
            saved = self.st.save_skillset(cur)
            return f"Saved {len(saved.skills)} skills."
        res = ingest_skills_file(self.app, pid, "resume.txt", a.encode("utf-8"), use_llm=False)
        if not res["total"]:
            raise AnswerError("no skills recognised; list them separated by commas")
        return f"Read {res['extracted']} skills from the resume."

    def _criteria_from_form(self, pid: str, f: dict[str, str]) -> Criteria:
        if not f.get("job_titles"):
            raise AnswerError("job_titles is required")
        if not f.get("country"):
            raise AnswerError("country is required")
        country = norm_country(f["country"])
        loc: dict[str, Any] = {"country": country}
        if f.get("region"):
            region = norm_region(f["region"], country)
            if region:
                loc["region"] = region
        if f.get("city"):
            loc["city"] = f["city"]
        modes = [m for m in ("remote", "hybrid", "onsite") if m in fold(f.get("work_mode", ""))
                 or (m == "onsite" and re.search(r"on.?site|presencial|office|oficina", fold(f.get("work_mode", ""))))
                 or (m == "remote" and "remoto" in fold(f.get("work_mode", "")))
                 or (m == "hybrid" and "hibrido" in fold(f.get("work_mode", "")))]
        titles = [x.strip() for x in re.split(r"[,;/\n]", f["job_titles"]) if x.strip()]
        constraints: dict[str, Any] = {"locations": [loc], "work_mode": modes or ["remote", "hybrid", "onsite"]}
        if f.get("min_salary"):
            sal = parse_salary(f["min_salary"], country)
            if sal:
                constraints["salary"] = sal
        if re.search(r"\b(yes|si|required|need|lmia|visa)\b", fold(f.get("sponsorship", ""))):
            constraints["sponsorship"] = {"need": "required", "programs": ["CA_LMIA"] if country == "CA" else []}
        name = f"{titles[0]} – {f.get('city') or f.get('region') or country}"
        return Criteria.model_validate({"profile_id": pid, "name": name, "constraints": constraints,
                                        "targets": {"titles": titles}})

    def _add_companies(self, pid: str, cid: str, cands: list[dict[str, Any]]) -> str:
        from jobfinder.sources import Ctx, probe_ats

        st = self.st
        rc = self._rc(pid)
        known, _ = self._known_keys(pid)
        added, rejected = [], []
        for c in cands:
            if not c.get("name"):
                continue
            if c.get("ats") and not isinstance(c["ats"], AtsRef):
                c["ats"] = AtsRef.model_validate(c["ats"])
            if not c.get("ats") and not c.get("domain"):
                ats = probe_ats(Ctx(rc.http, {}, rc.log), Company(name=c["name"]))
                if ats:
                    c["ats"] = ats
            existing = st.find_company(c["name"], c.get("domain"))
            if existing and (f"n:{fold(existing.name)}" in known or (existing.domain and f"d:{existing.domain}" in known)):
                st.link_company(pid, existing.id, {"criteria_ids": [cid]})  # type: ignore[arg-type]
                continue
            comp = discovery._verify_company(rc, c)
            if comp is None:
                rejected.append(c["name"])
                continue
            saved, _ = st.upsert_company(comp)
            st.link_company(pid, saved.id, {"criteria_ids": [cid], "status": "new", "run_id": rc.run.id})  # type: ignore[arg-type]
            added.append(saved.name)
        msg = f"Added {len(added)} compan{'y' if len(added) == 1 else 'ies'}"
        msg += f": {', '.join(added)}." if added else "."
        if rejected:
            msg += f" Could not verify: {', '.join(rejected)}."
        return msg


# ----------------------------------------------------------------------------- helpers


def _cand_to_company(c: dict[str, Any]) -> dict[str, Any]:
    if c.get("ats"):
        ats = AtsRef.model_validate(c["ats"])
        return {"name": discovery._name_from_ats_title(c["title"], ats), "ats": ats, "evidence": c["url"],
                "title": c["title"]}
    domain = normalize_domain(c["url"])
    label = (domain or "").split(".")[0]
    parts = [p.strip() for p in re.split(r"\s[|\-–—:]\s", c["title"]) if p.strip()]
    name = max(parts, key=lambda p: fuzz.partial_ratio(fold(p), label), default=label) if parts else label
    if label and fuzz.partial_ratio(fold(name), label) < 60:
        name = label.replace("-", " ").title()
    return {"name": name[:60], "domain": domain, "evidence": c["url"], "title": c["title"]}


def _languages(text: str) -> list[dict[str, str]]:
    names = {"spanish": "es", "espanol": "es", "english": "en", "ingles": "en", "french": "fr", "frances": "fr",
             "portuguese": "pt", "german": "de", "italian": "it", "chinese": "zh", "hindi": "hi"}
    out = []
    for part in re.split(r"[,;/\n]", text):
        f = fold(part)
        code = next((c for n, c in names.items() if n in f), None)
        if not code:
            m = re.match(r"\s*([a-z]{2})\b", f)
            code = m.group(1) if m else None
        if not code:
            continue
        lvl = re.search(r"\b(a1|a2|b1|b2|c1|c2)\b", f)
        level = lvl.group(1).upper() if lvl else ("native" if re.search(r"nativ|mother", f) else
                                                  "C1" if re.search(r"fluent|fluido|advanced|avanzado", f) else "B2")
        out.append({"code": code, "level": level})
    return out


def _short_stats(s: dict[str, Any]) -> dict[str, Any]:
    return {"companies": s.get("companies", 0), "roles": s.get("roles_matched", 0), "contacts": s.get("contacts", 0)}


def _readable(e: Exception) -> str:
    if isinstance(e, ValidationError):
        return "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:3])
    return str(e)


def _prompt(t: GuidedTask) -> str:
    parts = ["You are helping a person look for a job. Answer only the question below."]
    if t.needs_user:
        parts.append("Ask the person for this information. Never invent personal details.")
    parts.append(t.question)
    if t.items:
        parts.append("\n".join(t.items))
    if t.fields:
        parts.append("Fields:\n" + "\n".join(f"- {k}: {v}" for k, v in t.fields.items()))
    parts.append(f"Answer format: {t.answer_format}.")
    parts.append(f"Example answer:\n{t.example}")
    parts.append("Reply with the answer only, no explanation.")
    return "\n\n".join(parts)


# ----------------------------------------------------------------------------- guided autopilot (internal model)


def autopilot(rc: RunCtx, req: RunRequest) -> dict[str, Any]:
    """Let JobFinder's own configured model (any size) answer the guided questions."""
    g = Guide(rc.app, rc)
    answered = errors = 0
    t = g._next(rc.pid, 60)
    for _ in range(req.limit or 40):
        rc.check()
        if t.kind == "done":
            break
        if t.needs_user:
            rc.log(f"Needs the person's input: {t.question}")
            return {"stopped": "needs_user", "question": t.question, "answered": answered}
        if t.type == "wait":
            res = g._answer(t.id, "OK")
        else:
            text, model = rc.app.llm.chat("guided", [{"role": "user", "content": t.prompt}], budget=rc.budget)
            res = g._answer(t.id, text)
            if not res["ok"]:
                errors += 1
                retry = t.prompt + f"\n\nYour previous answer could not be used: {res['message']}"
                text, model = rc.app.llm.chat("guided", [{"role": "user", "content": retry}], budget=rc.budget)
                res = g._answer(t.id, text)
                if not res["ok"]:
                    res = g._answer(t.id, "skip")
            rc.log(f"[{model}] {t.kind}: {clean_answer(text)[:80]!r} -> {res['message']}")
        answered += 1
        for line in res["next"].get("done_automatically", []):
            rc.log(line)
        t = GuidedTask.model_validate(res["next"])
    return {"answered": answered, "unusable_answers": errors, "stats": _short_stats(rc.app.store.stats(rc.pid))}
