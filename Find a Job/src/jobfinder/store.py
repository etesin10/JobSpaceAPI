"""SQLite persistence. JSON documents live in `data` columns; keys/status are columns for querying.

Shared catalog: companies, roles, contacts (facts, deduplicated by natural keys).
Per-profile pipeline: profile_companies, profile_roles, profile_contacts (status, score, notes).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jobfinder.models import (
    Company,
    CompanyLink,
    Contact,
    ContactLink,
    Criteria,
    FetchStatus,
    Profile,
    Role,
    RoleLink,
    Run,
    SkillSet,
    slugify,
    utcnow,
)
from jobfinder.services import identity

MIGRATIONS: list[str] = [
    # 1
    """
    CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS criteria (
        id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        active INTEGER NOT NULL DEFAULT 1, data TEXT NOT NULL, created_at TEXT, updated_at TEXT);
    CREATE INDEX IF NOT EXISTS ix_criteria_profile ON criteria(profile_id);
    CREATE TABLE IF NOT EXISTS strategy_history (
        criteria_id TEXT NOT NULL REFERENCES criteria(id) ON DELETE CASCADE, version INTEGER NOT NULL,
        data TEXT NOT NULL, updated_by TEXT, created_at TEXT, PRIMARY KEY (criteria_id, version));
    CREATE TABLE IF NOT EXISTS skillsets (
        profile_id TEXT PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE, data TEXT NOT NULL, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS resumes (
        id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        filename TEXT, text TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS companies (
        id TEXT PRIMARY KEY, domain TEXT UNIQUE, name_key TEXT, data TEXT NOT NULL, first_seen TEXT, last_seen TEXT);
    CREATE INDEX IF NOT EXISTS ix_companies_name ON companies(name_key);
    CREATE TABLE IF NOT EXISTS roles (
        id TEXT PRIMARY KEY, company_id TEXT REFERENCES companies(id) ON DELETE CASCADE, url_key TEXT UNIQUE,
        dedup_key TEXT, status TEXT, data TEXT NOT NULL, first_seen TEXT, last_seen TEXT);
    CREATE INDEX IF NOT EXISTS ix_roles_company ON roles(company_id);
    CREATE INDEX IF NOT EXISTS ix_roles_dedup ON roles(dedup_key);
    CREATE TABLE IF NOT EXISTS contacts (
        id TEXT PRIMARY KEY, company_id TEXT REFERENCES companies(id) ON DELETE SET NULL, name_key TEXT,
        data TEXT NOT NULL, first_seen TEXT, last_seen TEXT);
    CREATE INDEX IF NOT EXISTS ix_contacts_company ON contacts(company_id);
    CREATE TABLE IF NOT EXISTS contact_keys (key TEXT PRIMARY KEY, contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS profile_companies (
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
        status TEXT, excluded INTEGER DEFAULT 0, data TEXT NOT NULL, PRIMARY KEY (profile_id, company_id));
    CREATE TABLE IF NOT EXISTS profile_roles (
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
        status TEXT, stage TEXT, score REAL, excluded INTEGER DEFAULT 0, data TEXT NOT NULL,
        PRIMARY KEY (profile_id, role_id));
    CREATE TABLE IF NOT EXISTS profile_contacts (
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        role_id TEXT NOT NULL DEFAULT '', status TEXT, data TEXT NOT NULL,
        PRIMARY KEY (profile_id, contact_id, role_id));
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY, profile_id TEXT, kind TEXT, status TEXT, data TEXT NOT NULL, created_at TEXT);
    CREATE INDEX IF NOT EXISTS ix_runs_profile ON runs(profile_id, created_at);
    CREATE TABLE IF NOT EXISTS http_cache (key TEXT PRIMARY KEY, status INTEGER, body BLOB, headers TEXT, fetched_at TEXT);
    """,
    # 2: cancellation must work across processes (dashboard cancelling a run started by the MCP server)
    """
    ALTER TABLE runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
    """,
    # 3: guided mode (one small question at a time)
    """
    CREATE TABLE IF NOT EXISTS guided_tasks (
        id TEXT PRIMARY KEY, profile_id TEXT, status TEXT NOT NULL, data TEXT NOT NULL, context TEXT, created_at TEXT);
    CREATE INDEX IF NOT EXISTS ix_guided_profile ON guided_tasks(profile_id, status);
    CREATE TABLE IF NOT EXISTS guided_state (profile_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
        PRIMARY KEY (profile_id, key));
    """,
]


def pid_alive(pid: int | None) -> bool:
    import os
    import sys

    if not pid:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _dump(model: Any) -> str:
    if hasattr(model, "model_dump"):
        return model.model_dump_json(exclude_none=True, by_alias=True)
    return json.dumps(model, default=str)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def merge_docs(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Incoming non-empty values win; lists of evidence/profiles/skills are unioned."""
    out = dict(old)
    for k, v in new.items():
        if v is None or v == [] or v == {} or v == "":
            continue
        if k in ("evidence", "profiles", "sponsorship") and isinstance(v, list):
            key = "program" if k == "sponsorship" else "url"
            seen = {(e.get(key) if isinstance(e, dict) else e): e for e in out.get(k, [])}
            for e in v:
                if isinstance(e, dict) and k == "evidence" and e.get("status") == "unverified":
                    e = {kk: vv for kk, vv in e.items() if kk not in ("status", "checked_at", "http_status")}
                    e = {"status": "unverified", **seen.get(e.get(key), {}), **e}
                seen[e.get(key) if isinstance(e, dict) else e] = {**seen.get(e.get(key), {}), **e} if isinstance(e, dict) else e
            out[k] = list(seen.values())
        elif k in ("skills", "countries", "industries", "languages", "remote_eligibility") and isinstance(v, list):
            out[k] = list(dict.fromkeys([*out.get(k, []), *v]))
        elif isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("ats",):
            out[k] = merge_docs(out[k], v)
        else:
            out[k] = v
    return out


def _strategy_sig(cr: Criteria) -> Any:
    """What makes a strategy different: queries' intent, sources, notes. Yield counters and metadata do not count."""
    s = cr.search_strategy
    return ([(q.text, q.location, q.country, tuple(q.sources), q.purpose, q.disabled) for q in s.queries],
            tuple(s.sources), s.notes)


class Store:
    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self.migrate()

    # ------------------------------------------------------------------ plumbing
    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            c = self.conn
            if c.in_transaction:
                yield c
                return
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def migrate(self) -> int:
        c = self.conn
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        current = int(row[0]) if row else 0
        for i, sql in enumerate(MIGRATIONS[current:], start=current + 1):
            with self.tx():
                for stmt in [s for s in sql.split(";") if s.strip()]:
                    c.execute(stmt)
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(i),))
        return len(MIGRATIONS)

    def schema_version(self) -> int:
        row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------ profiles
    def list_profiles(self) -> list[Profile]:
        return [Profile.model_validate_json(r["data"]) for r in self.conn.execute("SELECT data FROM profiles ORDER BY id")]

    def get_profile(self, pid: str) -> Profile | None:
        r = self.conn.execute("SELECT data FROM profiles WHERE id=?", (pid,)).fetchone()
        return Profile.model_validate_json(r["data"]) if r else None

    def save_profile(self, p: Profile) -> Profile:
        now = utcnow()
        with self.tx() as c:
            if not p.id:
                base = slugify(p.name, 40)
                pid, n = base, 1
                while c.execute("SELECT 1 FROM profiles WHERE id=?", (pid,)).fetchone():
                    n += 1
                    pid = f"{base}-{n}"
                p.id = pid
            existing = self.get_profile(p.id)
            p.created_at = existing.created_at if existing and existing.created_at else (p.created_at or now)
            p.updated_at = now
            c.execute(
                "INSERT INTO profiles(id, data, created_at, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (p.id, _dump(p), _iso(p.created_at), _iso(p.updated_at)),
            )
        return p

    def delete_profile(self, pid: str) -> bool:
        with self.tx() as c:
            n = c.execute("DELETE FROM profiles WHERE id=?", (pid,)).rowcount
            c.execute("DELETE FROM runs WHERE profile_id=?", (pid,))
            c.execute("DELETE FROM guided_tasks WHERE profile_id=?", (pid,))
            c.execute("DELETE FROM guided_state WHERE profile_id=?", (pid,))
            self._purge_orphan_contacts(c)
        return n > 0

    def _purge_orphan_contacts(self, c: sqlite3.Connection) -> int:
        """Personal data minimisation: contacts not linked to any profile are deleted."""
        return c.execute("DELETE FROM contacts WHERE id NOT IN (SELECT contact_id FROM profile_contacts)").rowcount

    # ------------------------------------------------------------------ criteria
    def list_criteria(self, pid: str, active_only: bool = False) -> list[Criteria]:
        q = "SELECT data FROM criteria WHERE profile_id=?" + (" AND active=1" if active_only else "") + " ORDER BY id"
        return [Criteria.model_validate_json(r["data"]) for r in self.conn.execute(q, (pid,))]

    def get_criteria(self, cid: str) -> Criteria | None:
        r = self.conn.execute("SELECT data FROM criteria WHERE id=?", (cid,)).fetchone()
        return Criteria.model_validate_json(r["data"]) if r else None

    def save_criteria(self, cr: Criteria, track_strategy: bool = True) -> Criteria:
        now = utcnow()
        with self.tx() as c:
            if not cr.id:
                base = slugify(f"{cr.profile_id}-{cr.name}", 50)
                cid, n = base, 1
                while c.execute("SELECT 1 FROM criteria WHERE id=?", (cid,)).fetchone():
                    n += 1
                    cid = f"{base}-{n}"
                cr.id = cid
            existing = self.get_criteria(cr.id)
            cr.created_at = existing.created_at if existing and existing.created_at else now
            cr.updated_at = now
            changed = track_strategy and (existing is None or _strategy_sig(existing) != _strategy_sig(cr))
            if changed:
                if existing is not None:
                    cr.search_strategy.version = existing.search_strategy.version + 1
                cr.search_strategy.updated_at = now
            elif existing is not None and track_strategy:
                cr.search_strategy = existing.search_strategy
            c.execute(
                "INSERT INTO criteria(id, profile_id, active, data, created_at, updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET profile_id=excluded.profile_id, active=excluded.active, "
                "data=excluded.data, updated_at=excluded.updated_at",
                (cr.id, cr.profile_id, int(cr.active), _dump(cr), _iso(cr.created_at), _iso(cr.updated_at)),
            )
            if changed:
                c.execute(
                    "INSERT OR REPLACE INTO strategy_history(criteria_id, version, data, updated_by, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (cr.id, cr.search_strategy.version, _dump(cr.search_strategy), cr.search_strategy.updated_by, _iso(now)),
                )
        return cr

    def record_query_yield(self, cid: str, yields: dict[str, tuple[int, int]]) -> None:
        """Atomically add run yields to matching queries of the *current* criteria (no version bump)."""
        if not yields:
            return
        with self.tx() as c:
            r = c.execute("SELECT data FROM criteria WHERE id=?", (cid,)).fetchone()
            if not r:
                return
            cr = Criteria.model_validate_json(r["data"])
            now = utcnow()
            hit = False
            for q in cr.search_strategy.queries:
                key = f"{q.text}|{q.location or ''}"
                if key in yields:
                    q.yield_total += yields[key][0]
                    q.yield_matched += yields[key][1]
                    q.last_run_at = now
                    hit = True
            if hit:
                c.execute("UPDATE criteria SET data=? WHERE id=?", (_dump(cr), cid))

    def strategy_history(self, cid: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT version, data, updated_by, created_at FROM strategy_history WHERE criteria_id=? ORDER BY version", (cid,))
        return [{"version": r["version"], "updated_by": r["updated_by"], "created_at": r["created_at"],
                 "strategy": json.loads(r["data"])} for r in rows]

    def delete_criteria(self, cid: str) -> bool:
        with self.tx() as c:
            return c.execute("DELETE FROM criteria WHERE id=?", (cid,)).rowcount > 0

    # ------------------------------------------------------------------ skills / resumes
    def get_skillset(self, pid: str) -> SkillSet:
        r = self.conn.execute("SELECT data FROM skillsets WHERE profile_id=?", (pid,)).fetchone()
        return SkillSet.model_validate_json(r["data"]) if r else SkillSet(profile_id=pid)

    def save_skillset(self, s: SkillSet) -> SkillSet:
        s.updated_at = utcnow()
        dedup: dict[str, Any] = {}
        for sk in s.skills:
            prev = dedup.get(sk.id)
            if prev is None:
                dedup[sk.id] = sk
            else:
                dedup[sk.id] = prev.model_copy(update={k: v for k, v in sk.model_dump().items() if v is not None})
        s.skills = list(dedup.values())
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO skillsets(profile_id, data, updated_at) VALUES (?,?,?)",
                      (s.profile_id, _dump(s), _iso(s.updated_at)))
        return s

    def add_resume(self, pid: str, filename: str, text: str) -> str:
        rid = uuid.uuid4().hex[:12]
        with self.tx() as c:
            c.execute("INSERT INTO resumes(id, profile_id, filename, text, created_at) VALUES (?,?,?,?,?)",
                      (rid, pid, filename, text, _iso(utcnow())))
        return rid

    def list_resumes(self, pid: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT id, filename, length(text) AS chars, created_at FROM resumes "
                                 "WHERE profile_id=? ORDER BY created_at DESC", (pid,))
        return [dict(r) for r in rows]

    def latest_resume_text(self, pid: str) -> str | None:
        r = self.conn.execute("SELECT text FROM resumes WHERE profile_id=? ORDER BY created_at DESC LIMIT 1", (pid,)).fetchone()
        return r["text"] if r else None

    # ------------------------------------------------------------------ companies
    def get_company(self, cid: str) -> Company | None:
        r = self.conn.execute("SELECT data FROM companies WHERE id=?", (cid,)).fetchone()
        return Company.model_validate_json(r["data"]) if r else None

    def find_company(self, name: str | None = None, domain: str | None = None, cid: str | None = None) -> Company | None:
        c = self.conn
        if cid:
            got = self.get_company(cid)
            if got:
                return got
        d = identity.normalize_domain(domain) if domain else None
        if d:
            r = c.execute("SELECT data FROM companies WHERE domain=?", (d,)).fetchone()
            if r:
                return Company.model_validate_json(r["data"])
        if name:
            rows = c.execute("SELECT data FROM companies WHERE name_key=?", (identity.company_name_key(name),)).fetchall()
            for r in rows:
                comp = Company.model_validate_json(r["data"])
                if not d or not comp.domain or comp.domain == d:
                    return comp
        return None

    def upsert_company(self, comp: Company) -> tuple[Company, bool]:
        now = utcnow()
        comp.domain = identity.normalize_domain(comp.domain or comp.website) or None
        with self.tx() as c:
            existing = self.find_company(comp.name, comp.domain, comp.id)
            if existing:
                merged = merge_docs(existing.model_dump(mode="json", exclude_none=True),
                                    comp.model_dump(mode="json", exclude_none=True, exclude={"id", "first_seen"}))
                merged["id"] = existing.id
                if existing.domain and comp.domain and existing.domain != comp.domain:
                    merged["domain"] = existing.domain
                out = Company.model_validate(merged)
                out.last_seen = now
                created = False
            else:
                out = comp.model_copy()
                out.id = comp.id if comp.id and comp.id.startswith("c:") else identity.company_id(comp.name, comp.domain)
                out.first_seen = out.first_seen or now
                out.last_seen = now
                created = True
            c.execute(
                "INSERT INTO companies(id, domain, name_key, data, first_seen, last_seen) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET domain=excluded.domain, name_key=excluded.name_key, data=excluded.data, "
                "last_seen=excluded.last_seen",
                (out.id, out.domain, identity.company_name_key(out.name), _dump(out), _iso(out.first_seen), _iso(out.last_seen)),
            )
        return out, created

    def list_companies(self, ids: list[str] | None = None) -> list[Company]:
        if ids is not None:
            if not ids:
                return []
            q = f"SELECT data FROM companies WHERE id IN ({','.join('?' * len(ids))})"
            return [Company.model_validate_json(r["data"]) for r in self.conn.execute(q, ids)]
        return [Company.model_validate_json(r["data"]) for r in self.conn.execute("SELECT data FROM companies ORDER BY id")]

    # ------------------------------------------------------------------ roles
    def get_role(self, rid: str) -> Role | None:
        r = self.conn.execute("SELECT data FROM roles WHERE id=?", (rid,)).fetchone()
        return Role.model_validate_json(r["data"]) if r else None

    def find_role(self, role: Role) -> Role | None:
        c = self.conn
        if role.id:
            got = self.get_role(role.id)
            if got:
                return got
        r = c.execute("SELECT data FROM roles WHERE url_key=?", (identity.canonical_url(role.url),)).fetchone()
        if r:
            return Role.model_validate_json(r["data"])
        if role.company_id:
            key = identity.role_dedup_key(role.company_id, role.title, role.location.country, role.location.city)
            r = c.execute("SELECT data FROM roles WHERE dedup_key=?", (key,)).fetchone()
            if r:
                return Role.model_validate_json(r["data"])
        return None

    def upsert_role(self, role: Role) -> tuple[Role, bool]:
        now = utcnow()
        with self.tx() as c:
            existing = self.find_role(role)
            if existing:
                merged = merge_docs(existing.model_dump(mode="json", exclude_none=True),
                                    role.model_dump(mode="json", exclude_none=True, exclude={"id", "first_seen", "url"}))
                merged["id"] = existing.id
                out = Role.model_validate(merged)
                out.last_seen = now
                created = False
            else:
                out = role.model_copy()
                out.id = role.id if role.id and role.id.startswith("r:") else identity.role_id(role.url, role.source, role.source_id)
                out.first_seen = out.first_seen or now
                out.last_seen = now
                created = True
            c.execute(
                "INSERT INTO roles(id, company_id, url_key, dedup_key, status, data, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET company_id=excluded.company_id, "
                "dedup_key=excluded.dedup_key, status=excluded.status, data=excluded.data, last_seen=excluded.last_seen",
                (out.id, out.company_id, identity.canonical_url(out.url),
                 identity.role_dedup_key(out.company_id, out.title, out.location.country, out.location.city),
                 out.status, _dump(out), _iso(out.first_seen), _iso(out.last_seen)),
            )
        return out, created

    def list_roles(self, company_id: str | None = None) -> list[Role]:
        if company_id:
            rows = self.conn.execute("SELECT data FROM roles WHERE company_id=? ORDER BY id", (company_id,))
        else:
            rows = self.conn.execute("SELECT data FROM roles ORDER BY id")
        return [Role.model_validate_json(r["data"]) for r in rows]

    def set_role_status(self, rid: str, status: str) -> None:
        role = self.get_role(rid)
        if role:
            role.status = status  # type: ignore[assignment]
            with self.tx() as c:
                c.execute("UPDATE roles SET status=?, data=? WHERE id=?", (status, _dump(role), rid))

    def record_fetch(self, kind: str, item_id: str, *, ok: bool, what: str, url: str | None = None,
                     http_status: int | None = None, error: str | None = None, partial: bool = False,
                     retry: bool = True) -> FetchStatus | None:
        """Remember the last attempt to read a company board or a posting. Failures back off 30 min, 1 h, 2 h ...
        up to 24 h before automatic runs try again (retry=False: no automatic retry, e.g. no careers page)."""
        table, model = {"company": ("companies", Company), "role": ("roles", Role)}[kind]
        now = utcnow()
        with self.tx() as c:
            r = c.execute(f"SELECT data FROM {table} WHERE id=?", (item_id,)).fetchone()
            if not r:
                return None
            obj = model.model_validate_json(r["data"])
            prev = obj.fetch
            failures = 0 if ok else (prev.failures if prev and not prev.ok else 0) + 1
            obj.fetch = FetchStatus(
                ok=ok, at=now, what=what, url=url, http_status=http_status, error=(error or None) and error[:300],
                failures=failures, partial=partial,
                next_retry_at=None if ok or not retry else now + timedelta(minutes=min(24 * 60, 30 * 2 ** (failures - 1))))
            c.execute(f"UPDATE {table} SET data=? WHERE id=?", (_dump(obj), item_id))
        return obj.fetch

    # ------------------------------------------------------------------ contacts
    def get_contact(self, cid: str) -> Contact | None:
        r = self.conn.execute("SELECT data FROM contacts WHERE id=?", (cid,)).fetchone()
        return Contact.model_validate_json(r["data"]) if r else None

    @staticmethod
    def _contact_keys(ct: Contact) -> list[str]:
        keys = [f"p:{identity.profile_url_key(p.url)}" for p in ct.profiles if p.url]
        if ct.work_email:
            keys.append(f"e:{ct.work_email.strip().lower()}")
        keys.append(f"n:{ct.company_id or ''}:{slugify(ct.name, 60)}")
        return keys

    def find_contact(self, ct: Contact) -> Contact | None:
        if ct.id:
            got = self.get_contact(ct.id)
            if got:
                return got
        for k in self._contact_keys(ct):
            r = self.conn.execute("SELECT contact_id FROM contact_keys WHERE key=?", (k,)).fetchone()
            if r:
                return self.get_contact(r["contact_id"])
        return None

    def upsert_contact(self, ct: Contact) -> tuple[Contact, bool]:
        now = utcnow()
        with self.tx() as c:
            existing = self.find_contact(ct)
            if existing:
                merged = merge_docs(existing.model_dump(mode="json", exclude_none=True),
                                    ct.model_dump(mode="json", exclude_none=True, exclude={"id", "first_seen"}))
                merged["id"] = existing.id
                out = Contact.model_validate(merged)
                out.last_seen = now
                created = False
            else:
                out = ct.model_copy()
                out.id = ct.id if ct.id and ct.id.startswith("ct:") else identity.contact_id(
                    ct.name, ct.company_id, [p.url for p in ct.profiles], ct.work_email)
                out.first_seen = out.first_seen or now
                out.last_seen = now
                created = True
            c.execute(
                "INSERT INTO contacts(id, company_id, name_key, data, first_seen, last_seen) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET company_id=excluded.company_id, name_key=excluded.name_key, "
                "data=excluded.data, last_seen=excluded.last_seen",
                (out.id, out.company_id, slugify(out.name, 60), _dump(out), _iso(out.first_seen), _iso(out.last_seen)),
            )
            for k in self._contact_keys(out):
                c.execute("INSERT OR IGNORE INTO contact_keys(key, contact_id) VALUES (?,?)", (k, out.id))
        return out, created

    # ------------------------------------------------------------------ pipeline links
    def get_company_link(self, pid: str, cid: str) -> CompanyLink | None:
        r = self.conn.execute("SELECT data FROM profile_companies WHERE profile_id=? AND company_id=?", (pid, cid)).fetchone()
        return CompanyLink.model_validate_json(r["data"]) if r else None

    def link_company(self, pid: str, cid: str, patch: dict[str, Any] | None = None) -> tuple[CompanyLink, bool]:
        now = utcnow()
        with self.tx() as c:
            cur = self.get_company_link(pid, cid)
            created = cur is None
            cur = cur or CompanyLink(added_at=now)
            patch = dict(patch or {})
            if "criteria_ids" in patch:
                patch["criteria_ids"] = list(dict.fromkeys([*cur.criteria_ids, *patch["criteria_ids"]]))
            link = CompanyLink.model_validate({**cur.model_dump(), **{k: v for k, v in patch.items() if v is not None}})
            if link.excluded:
                link.status = "excluded"
            elif link.status == "excluded":
                link.status = "new"
            link.updated_at = now
            c.execute(
                "INSERT INTO profile_companies(profile_id, company_id, status, excluded, data) VALUES (?,?,?,?,?) "
                "ON CONFLICT(profile_id, company_id) DO UPDATE SET status=excluded.status, excluded=excluded.excluded, "
                "data=excluded.data",
                (pid, cid, link.status, int(link.excluded), _dump(link)),
            )
        return link, created

    def get_role_link(self, pid: str, rid: str) -> RoleLink | None:
        r = self.conn.execute("SELECT data FROM profile_roles WHERE profile_id=? AND role_id=?", (pid, rid)).fetchone()
        return RoleLink.model_validate_json(r["data"]) if r else None

    def link_role(self, pid: str, rid: str, patch: dict[str, Any] | None = None) -> tuple[RoleLink, bool]:
        now = utcnow()
        with self.tx() as c:
            cur = self.get_role_link(pid, rid)
            created = cur is None
            cur = cur or RoleLink(added_at=now)
            data = cur.model_dump()
            for k, v in (patch or {}).items():
                if v is not None:
                    data[k] = v.model_dump() if hasattr(v, "model_dump") else v
            link = RoleLink.model_validate(data)
            link.updated_at = now
            c.execute(
                "INSERT INTO profile_roles(profile_id, role_id, status, stage, score, excluded, data) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(profile_id, role_id) DO UPDATE SET status=excluded.status, stage=excluded.stage, "
                "score=excluded.score, excluded=excluded.excluded, data=excluded.data",
                (pid, rid, link.status, link.stage, link.score, int(link.excluded), _dump(link)),
            )
        return link, created

    def link_contact(self, pid: str, contact_id: str, role_id: str | None, patch: dict[str, Any] | None = None) -> tuple[ContactLink, bool]:
        now = utcnow()
        rk = role_id or ""
        with self.tx() as c:
            r = c.execute("SELECT data FROM profile_contacts WHERE profile_id=? AND contact_id=? AND role_id=?",
                          (pid, contact_id, rk)).fetchone()
            created = r is None
            cur = ContactLink.model_validate_json(r["data"]) if r else ContactLink(added_at=now, role_id=role_id)
            link = ContactLink.model_validate({**cur.model_dump(), **{k: v for k, v in (patch or {}).items() if v is not None}})
            link.updated_at = now
            c.execute(
                "INSERT INTO profile_contacts(profile_id, contact_id, role_id, status, data) VALUES (?,?,?,?,?) "
                "ON CONFLICT(profile_id, contact_id, role_id) DO UPDATE SET status=excluded.status, data=excluded.data",
                (pid, contact_id, rk, link.status, _dump(link)),
            )
        return link, created

    def unlink(self, pid: str, kind: str, item_id: str) -> bool:
        table, col = {"company": ("profile_companies", "company_id"), "role": ("profile_roles", "role_id"),
                      "contact": ("profile_contacts", "contact_id")}[kind]
        with self.tx() as c:
            n = c.execute(f"DELETE FROM {table} WHERE profile_id=? AND {col}=?", (pid, item_id)).rowcount
            if kind == "contact":
                self._purge_orphan_contacts(c)
        return n > 0

    def pipeline_companies(self, pid: str, include_excluded: bool = True) -> list[tuple[Company, CompanyLink]]:
        q = ("SELECT c.data AS c, pc.data AS l FROM profile_companies pc JOIN companies c ON c.id=pc.company_id "
             "WHERE pc.profile_id=?" + ("" if include_excluded else " AND pc.excluded=0") + " ORDER BY c.name_key")
        return [(Company.model_validate_json(r["c"]), CompanyLink.model_validate_json(r["l"]))
                for r in self.conn.execute(q, (pid,))]

    def pipeline_roles(self, pid: str, company_id: str | None = None) -> list[tuple[Role, RoleLink]]:
        q = ("SELECT r.data AS r, pr.data AS l FROM profile_roles pr JOIN roles r ON r.id=pr.role_id WHERE pr.profile_id=?"
             + (" AND r.company_id=?" if company_id else "") + " ORDER BY pr.score DESC NULLS LAST, r.id")
        args = (pid, company_id) if company_id else (pid,)
        return [(Role.model_validate_json(r["r"]), RoleLink.model_validate_json(r["l"])) for r in self.conn.execute(q, args)]

    def pipeline_contacts(self, pid: str, role_id: str | None = None) -> list[tuple[Contact, ContactLink]]:
        q = ("SELECT ct.data AS c, pc.data AS l FROM profile_contacts pc JOIN contacts ct ON ct.id=pc.contact_id "
             "WHERE pc.profile_id=?" + (" AND pc.role_id=?" if role_id is not None else "") + " ORDER BY ct.id")
        args = (pid, role_id) if role_id is not None else (pid,)
        return [(Contact.model_validate_json(r["c"]), ContactLink.model_validate_json(r["l"])) for r in self.conn.execute(q, args)]

    def stats(self, pid: str) -> dict[str, Any]:
        c = self.conn
        one = lambda q, *a: c.execute(q, a).fetchone()[0]  # noqa: E731
        by = lambda q, *a: {r[0] or "none": r[1] for r in c.execute(q, a)}  # noqa: E731
        return {
            "companies": one("SELECT count(*) FROM profile_companies WHERE profile_id=? AND excluded=0", pid),
            "companies_excluded": one("SELECT count(*) FROM profile_companies WHERE profile_id=? AND excluded=1", pid),
            "companies_by_status": by("SELECT status, count(*) FROM profile_companies WHERE profile_id=? GROUP BY status", pid),
            "roles": one("SELECT count(*) FROM profile_roles WHERE profile_id=? AND excluded=0", pid),
            "roles_by_status": by("SELECT status, count(*) FROM profile_roles WHERE profile_id=? GROUP BY status", pid),
            "roles_by_stage": by("SELECT stage, count(*) FROM profile_roles WHERE profile_id=? GROUP BY stage", pid),
            "roles_matched": one("SELECT count(*) FROM profile_roles pr WHERE profile_id=? AND excluded=0 "
                                 "AND json_extract(data, '$.match.passed')=1", pid),
            "contacts": one("SELECT count(DISTINCT contact_id) FROM profile_contacts WHERE profile_id=?", pid),
            "companies_failed": one("SELECT count(*) FROM profile_companies WHERE profile_id=? AND excluded=0 "
                                    "AND status='error'", pid),
            "roles_unreadable": one("SELECT count(*) FROM profile_roles pr JOIN roles r ON r.id=pr.role_id "
                                    "WHERE pr.profile_id=? AND pr.excluded=0 AND r.status != 'closed' "
                                    "AND json_extract(r.data, '$.fetch.ok')=0", pid),
            "companies_by_criteria": by(
                "SELECT j.value, count(*) FROM profile_companies pc, json_each(pc.data, '$.criteria_ids') j "
                "WHERE pc.profile_id=? AND pc.excluded=0 GROUP BY j.value", pid),
            "roles_by_criteria": by("SELECT json_extract(data, '$.criteria_id'), count(*) FROM profile_roles "
                                    "WHERE profile_id=? AND excluded=0 GROUP BY 1", pid),
        }

    # ------------------------------------------------------------------ runs
    def create_run(self, profile_id: str | None, kind: str, params: dict[str, Any]) -> Run:
        run = Run(id=uuid.uuid4().hex[:12], profile_id=profile_id, kind=kind, status="queued", params=params,
                  created_at=utcnow())
        self.save_run(run)
        return run

    def save_run(self, run: Run) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO runs(id, profile_id, kind, status, data, created_at) VALUES (?,?,?,?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data",
                      (run.id, run.profile_id, run.kind, run.status, _dump(run), _iso(run.created_at)))

    def request_cancel(self, rid: str) -> bool:
        with self.tx() as c:
            return c.execute("UPDATE runs SET cancel_requested=1 WHERE id=? AND status IN ('queued','running')",
                             (rid,)).rowcount > 0

    def cancel_requested(self, rid: str) -> bool:
        r = self.conn.execute("SELECT cancel_requested FROM runs WHERE id=?", (rid,)).fetchone()
        return bool(r and r[0])

    def get_run(self, rid: str) -> Run | None:
        r = self.conn.execute("SELECT data FROM runs WHERE id=?", (rid,)).fetchone()
        return Run.model_validate_json(r["data"]) if r else None

    def list_runs(self, profile_id: str | None = None, limit: int = 50) -> list[Run]:
        if profile_id:
            rows = self.conn.execute("SELECT data FROM runs WHERE profile_id=? ORDER BY created_at DESC LIMIT ?", (profile_id, limit))
        else:
            rows = self.conn.execute("SELECT data FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [Run.model_validate_json(r["data"]) for r in rows]

    def fail_stale_runs(self) -> int:
        """Runs left 'running' by a process that no longer exists (app closed, crash, power off) become
        'interrupted' and resumable from their last saved checkpoint. Runs owned by another live process
        (e.g. the MCP server) are left alone."""
        n = 0
        for r in self.conn.execute("SELECT data FROM runs WHERE status IN ('queued','running')").fetchall():
            run = Run.model_validate_json(r["data"])
            if pid_alive(run.owner_pid):
                continue
            cp = run.checkpoint
            run.status = "interrupted"
            run.error = "the app was closed or crashed while this run was working"
            cp.stopped_at = cp.at or cp.stage or "start"
            cp.reason = run.error
            cp.resumable = True
            done = sum(len(v) for v in cp.done.values())
            cp.resume_hint = (f"Resume continues from '{cp.stopped_at}' and skips the {done} item(s) already finished."
                              if done else "Resume runs it again with the same settings.")
            run.finished_at = utcnow()
            self.save_run(run)
            n += 1
        return n

    # ------------------------------------------------------------------ http cache
    def cache_get(self, key: str, max_age_s: float) -> tuple[int, bytes] | None:
        r = self.conn.execute("SELECT status, body, fetched_at FROM http_cache WHERE key=?", (key,)).fetchone()
        if not r:
            return None
        age = (utcnow() - datetime.fromisoformat(r["fetched_at"])).total_seconds()
        return (r["status"], r["body"]) if age <= max_age_s else None

    def cache_put(self, key: str, status: int, body: bytes) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO http_cache(key, status, body, fetched_at) VALUES (?,?,?,?)",
                      (key, status, body, _iso(utcnow())))

    def cache_prune(self, max_age_hours: float = 72) -> int:
        rows = self.conn.execute("SELECT key, fetched_at FROM http_cache").fetchall()
        old = [r["key"] for r in rows if (utcnow() - datetime.fromisoformat(r["fetched_at"])).total_seconds() > max_age_hours * 3600]
        with self.tx() as c:
            for k in old:
                c.execute("DELETE FROM http_cache WHERE key=?", (k,))
        return len(old)
