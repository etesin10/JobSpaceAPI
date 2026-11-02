"""Excel workbook for review and control. Yellow columns are editable and can be imported back."""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from jobfinder.models import utcnow

if TYPE_CHECKING:
    from jobfinder.core import App

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
EDIT_FILL = PatternFill("solid", fgColor="FFF2CC")
HEADER_FONT = Font(color="FFFFFF", bold=True)

STAGES = ["none", "shortlisted", "applied", "interview", "offer", "hired", "declined", "rejected", "withdrawn"]
CONTACT_STATUS = ["found", "verified", "rejected", "contacted", "replied"]
YESNO = ["no", "yes"]

SHEETS: dict[str, list[tuple[str, int, bool]]] = {
    # column, width, editable
    "Companies": [("profile_id", 14, False), ("company_id", 26, False), ("name", 28, False), ("domain", 22, False),
                  ("ats", 14, False), ("careers_url", 30, False), ("status", 14, False), ("access", 26, False),
                  ("criteria", 22, False),
                  ("roles", 7, False), ("best_score", 10, False), ("sponsorship", 14, False), ("evidence", 30, False),
                  ("exclude", 9, True), ("notes", 40, True)],
    "Roles": [("profile_id", 14, False), ("role_id", 24, False), ("company", 24, False), ("title", 34, False),
              ("city", 14, False), ("region", 9, False), ("country", 8, False), ("work_mode", 10, False),
              ("employment", 11, False), ("salary", 18, False), ("posted", 11, False), ("score", 7, False),
              ("passed", 7, False), ("ai_fit", 7, False), ("matched_skills", 30, False), ("missing_skills", 30, False),
              ("issues", 30, False), ("source", 12, False), ("status", 14, False), ("access", 24, False),
              ("url", 40, False),
              ("stage", 12, True), ("exclude", 9, True), ("notes", 40, True)],
    "Contacts": [("profile_id", 14, False), ("contact_id", 24, False), ("role_id", 22, False), ("company", 22, False),
                 ("role", 28, False), ("name", 22, False), ("title", 28, False), ("type", 16, False),
                 ("profile_url", 36, False), ("work_email", 26, False), ("phone", 16, False), ("evidence", 30, False),
                 ("status", 12, True), ("notes", 40, True)],
}


def _access(f: Any) -> str:
    """'ok', 'partial: …' or 'failed (HTTP 403, 2x) – retry 2026-11-02 14:00' for the Access column."""
    if f is None:
        return ""
    label = f.label()
    if not f.ok and f.next_retry_at:
        label += f" – retry {f.next_retry_at.strftime('%Y-%m-%d %H:%M')} UTC"
    return label


def _salary(r: Any) -> str:
    s = r.salary
    if not s or (s.min is None and s.max is None):
        return ""
    rng = "-".join(f"{v:,.0f}" for v in (s.min, s.max) if v is not None)
    return f"{rng} {s.currency or ''}/{s.period}".strip()


def _header(ws: Any, cols: list[tuple[str, int, bool]]) -> None:
    for i, (name, width, editable) in enumerate(cols, start=1):
        c = ws.cell(row=1, column=i, value=name)
        c.fill = EDIT_FILL if editable else HEADER_FILL
        c.font = Font(bold=True) if editable else HEADER_FONT
        c.alignment = Alignment(vertical="center")
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "C2"


def _finish(ws: Any, cols: list[tuple[str, int, bool]], validations: dict[str, list[str]]) -> None:
    n = ws.max_row
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{max(n, 2)}"
    for i, (name, _w, editable) in enumerate(cols, start=1):
        letter = get_column_letter(i)
        if editable:
            for row in range(2, n + 1):
                ws[f"{letter}{row}"].fill = EDIT_FILL
        if name in validations and n >= 2:
            dv = DataValidation(type="list", formula1='"' + ",".join(validations[name]) + '"', allow_blank=True)
            dv.add(f"{letter}2:{letter}{max(n, 500)}")
            ws.add_data_validation(dv)
        if name in ("url", "careers_url", "profile_url", "evidence"):
            for row in range(2, n + 1):
                c = ws[f"{letter}{row}"]
                if c.value and str(c.value).startswith("http"):
                    c.hyperlink = str(c.value)
                    c.font = Font(color="0563C1", underline="single")


def build_workbook(app: App, profile_ids: list[str] | None = None) -> Workbook:
    st = app.store
    profiles = [p for p in st.list_profiles() if not profile_ids or p.id in profile_ids]
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    summary_cols = ["profile_id", "name", "criteria", "companies", "companies_excluded", "roles", "roles_matched",
                    "applied", "interview", "contacts", "last_run"]
    _header(ws, [(c, 14, False) for c in summary_cols])
    ws.column_dimensions["B"].width = 24
    wc, wr, wct = wb.create_sheet("Companies"), wb.create_sheet("Roles"), wb.create_sheet("Contacts")
    for sheet, name in ((wc, "Companies"), (wr, "Roles"), (wct, "Contacts")):
        _header(sheet, SHEETS[name])
    wcrit = wb.create_sheet("Criteria")
    crit_cols = ["profile_id", "criteria_id", "name", "active", "titles", "locations", "work_mode", "salary_min",
                 "sponsorship", "must", "should", "must_not", "strategy_version", "queries"]
    _header(wcrit, [(c, 18, False) for c in crit_cols])
    wruns = wb.create_sheet("Runs")
    run_cols = ["profile_id", "run_id", "kind", "status", "created", "finished", "result", "error"]
    _header(wruns, [(c, 16, False) for c in run_cols])
    wruns.column_dimensions["G"].width = 60

    for p in profiles:
        pid = p.id or ""
        stats = st.stats(pid)
        runs = st.list_runs(pid, 30)
        crits = st.list_criteria(pid)
        ws.append([pid, p.name, len(crits), stats["companies"], stats["companies_excluded"], stats["roles"],
                   stats["roles_matched"], stats["roles_by_stage"].get("applied", 0),
                   stats["roles_by_stage"].get("interview", 0), stats["contacts"],
                   runs[0].created_at.strftime("%Y-%m-%d %H:%M") if runs and runs[0].created_at else ""])
        roles = st.pipeline_roles(pid)
        comps = {c.id: c for c, _ in st.pipeline_companies(pid)}
        role_count: dict[str, int] = {}
        best: dict[str, float] = {}
        for role, link in roles:
            cid = role.company_id or ""
            role_count[cid] = role_count.get(cid, 0) + 1
            best[cid] = max(best.get(cid, 0), link.score or 0)
            comp = comps.get(cid)
            m = link.match
            wr.append([pid, role.id, comp.name if comp else "", role.title, role.location.city, role.location.region,
                       role.location.country, role.work_mode, role.employment_type, _salary(role),
                       role.posted_at.strftime("%Y-%m-%d") if role.posted_at else "", link.score,
                       "yes" if (m and m.passed) else "no", link.review.fit if link.review else None,
                       ", ".join(m.matched_skills) if m else "",
                       ", ".join(m.missing_skills) if m else "", "; ".join((m.failed + m.warnings)[:4]) if m else "",
                       role.source, link.status, _access(role.fetch), role.url, link.stage,
                       "yes" if link.excluded else "no", link.notes])
        for comp, link in st.pipeline_companies(pid):
            spons = ", ".join(f"{s.program}:{s.status}" for s in comp.sponsorship)
            wc.append([pid, comp.id, comp.name, comp.domain, comp.ats.type if comp.ats else "", comp.careers_url,
                       link.status, _access(comp.fetch), ", ".join(link.criteria_ids), role_count.get(comp.id or "", 0),
                       best.get(comp.id or "", None), spons, comp.evidence[0].url if comp.evidence else "",
                       "yes" if link.excluded else "no", link.notes])
        role_titles = {r.id: (r.title, comps.get(r.company_id or "")) for r, _ in roles}
        for ct, link in st.pipeline_contacts(pid):
            title, comp = role_titles.get(link.role_id, ("", None))
            wct.append([pid, ct.id, link.role_id or "", comp.name if comp else "", title, ct.name, ct.title, ct.type,
                        ct.profiles[0].url if ct.profiles else "", ct.work_email, ct.phone,
                        ct.evidence[0].url if ct.evidence else "", link.status, link.notes])
        for cr in crits:
            c = cr.constraints
            wcrit.append([pid, cr.id, cr.name, "yes" if cr.active else "no", ", ".join(cr.targets.titles),
                          "; ".join(", ".join(x for x in [loc.city, loc.region, loc.country] if x) for loc in c.locations),
                          ", ".join(c.work_mode), f"{c.salary.min:,.0f} {c.salary.currency or ''}" if c.salary and c.salary.min else "",
                          c.sponsorship.need, len(cr.rules.must), len(cr.rules.should), len(cr.rules.must_not),
                          cr.search_strategy.version, "; ".join(q.text for q in cr.search_strategy.queries[:8])])
        for r in runs:
            wruns.append([pid, r.id, r.kind, r.status, r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
                          r.finished_at.strftime("%Y-%m-%d %H:%M") if r.finished_at else "",
                          _short(r.result), r.error])

    _finish(wc, SHEETS["Companies"], {"exclude": YESNO})
    _finish(wr, SHEETS["Roles"], {"stage": STAGES, "exclude": YESNO})
    _finish(wct, SHEETS["Contacts"], {"status": CONTACT_STATUS})
    meta = wb.create_sheet("_meta")
    meta.append(["format", "jobfinder.workbook/v1"])
    meta.append(["exported_at", utcnow().isoformat()])
    meta.append(["note", "Edit only yellow columns, then Import in the app (or `jobfinder excel import`)."])
    meta.sheet_state = "hidden"
    return wb


def _short(d: dict[str, Any]) -> str:
    parts = []
    for k, v in d.items():
        if isinstance(v, (int, float)):
            parts.append(f"{k}={v}")
        elif isinstance(v, dict) and k in ("per_criteria",):
            parts.append(f"{k}={v}")
        elif isinstance(v, dict) and k in ("find_companies", "find_roles", "find_contacts"):
            parts.append(f"{k}: " + _short(v))
    return "; ".join(parts)[:500]


def export(app: App, profile_ids: list[str] | None = None, path: Path | None = None) -> Path:
    wb = build_workbook(app, profile_ids)
    target = path or app.exports_dir() / "JobFinder.xlsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        wb.save(target)
    except PermissionError:
        # Workbook open in Excel (or locked by OneDrive): write a timestamped copy instead.
        target = target.with_name(f"{target.stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}{target.suffix}")
        wb.save(target)
    return target


def export_bytes(app: App, profile_ids: list[str] | None = None) -> bytes:
    buf = io.BytesIO()
    build_workbook(app, profile_ids).save(buf)
    return buf.getvalue()


def _rows(ws: Any) -> list[dict[str, Any]]:
    it = ws.iter_rows(values_only=True)
    try:
        header = [str(h) if h is not None else "" for h in next(it)]
    except StopIteration:
        return []
    return [dict(zip(header, row, strict=False)) for row in it if any(v is not None for v in row)]


def _yes(v: Any) -> bool:
    return str(v or "").strip().lower() in ("yes", "y", "true", "1", "si", "sí", "x")


def import_workbook(app: App, data: bytes | Path) -> dict[str, Any]:
    wb = load_workbook(io.BytesIO(data) if isinstance(data, bytes) else data, read_only=True, data_only=True)
    st = app.store
    report = {"companies": 0, "roles": 0, "contacts": 0, "skipped": 0, "errors": []}
    if "Companies" in wb.sheetnames:
        for row in _rows(wb["Companies"]):
            pid, cid = row.get("profile_id"), row.get("company_id")
            if not pid or not cid or st.get_company_link(pid, cid) is None:
                report["skipped"] += 1
                continue
            st.link_company(pid, cid, {"excluded": _yes(row.get("exclude")), "notes": row.get("notes") or ""})
            report["companies"] += 1
    if "Roles" in wb.sheetnames:
        for row in _rows(wb["Roles"]):
            pid, rid = row.get("profile_id"), row.get("role_id")
            if not pid or not rid or st.get_role_link(pid, rid) is None:
                report["skipped"] += 1
                continue
            stage = str(row.get("stage") or "none").strip().lower()
            if stage not in STAGES:
                report["errors"].append(f"{rid}: invalid stage '{stage}'")
                continue
            st.link_role(pid, rid, {"stage": stage, "excluded": _yes(row.get("exclude")), "notes": row.get("notes") or ""})
            report["roles"] += 1
    if "Contacts" in wb.sheetnames:
        for row in _rows(wb["Contacts"]):
            pid, cid = row.get("profile_id"), row.get("contact_id")
            if not pid or not cid or st.get_contact(cid) is None:
                report["skipped"] += 1
                continue
            status = str(row.get("status") or "found").strip().lower()
            if status not in CONTACT_STATUS:
                report["errors"].append(f"{cid}: invalid status '{status}'")
                continue
            st.link_contact(pid, cid, row.get("role_id") or None, {"status": status, "notes": row.get("notes") or ""})
            report["contacts"] += 1
    wb.close()
    return report
