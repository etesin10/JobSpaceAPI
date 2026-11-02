import io

from openpyxl import load_workbook

from jobfinder.models import Company, Place, Role


def test_auth_and_host(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/profiles", headers={"X-JobFinder-Token": "nope"}).status_code == 401
    assert client.get("/api/profiles", headers={"host": "evil.example"}).status_code == 403
    html = client.get("/").text
    assert "jf-token" in html and "__JF_TOKEN__" not in html


def test_profile_criteria_skills_flow(client):
    p = client.post("/api/profiles", json={"name": "María López", "home": {"country": "MX", "city": "Monterrey"}}).json()
    assert p["id"] == "maria-lopez"
    bad = client.post(f"/api/profiles/{p['id']}/criteria", json={"name": "x", "targets": {"titles": ["a"]},
                                                                 "constraints": {"locations": [{"country": "ZZ"}]}})
    assert bad.status_code == 422
    cr = client.post(f"/api/profiles/{p['id']}/criteria", json={"name": "Admin MTY", "targets": {"titles": ["asistente administrativo"]},
                                                                "constraints": {"locations": [{"country": "MX", "region": "MX-NLE"}]}}).json()
    upd = dict(cr, constraints={**cr["constraints"], "posted_within_days": 10})
    for k in ("created_at", "updated_at"):
        upd.pop(k)
    assert client.put(f"/api/criteria/{cr['id']}?actor=ai", json=upd).status_code == 403
    assert client.put(f"/api/criteria/{cr['id']}", json=upd).json()["constraints"]["posted_within_days"] == 10
    s = client.patch(f"/api/criteria/{cr['id']}/strategy", json={"queries": [{"text": "auxiliar administrativo monterrey"}]}).json()
    assert s["search_strategy"]["version"] == 1 and s["constraints"]["posted_within_days"] == 10
    up = client.post(f"/api/profiles/{p['id']}/skills/upload", files={"file": ("cv.txt", "Excel avanzado, captura de datos, atención al cliente, SAP".encode())},
                     data={"use_llm": "false"}).json()
    assert up["added"] >= 4 and up["resume_id"]
    csv = client.post(f"/api/profiles/{p['id']}/skills/upload", files={"file": ("s.csv", b"skill,level\nPower BI,4\n")}).json()
    assert csv["added"] == 1 and csv["resume_id"] is None
    wpl = client.get(f"/api/profiles/{p['id']}/workplan").json()
    assert wpl["schema"] == "jobfinder.workplan/v1" and wpl["next_actions"][0]["action"] == "find_companies"
    assert client.delete(f"/api/profiles/{p['id']}").status_code == 200
    assert client.get(f"/api/profiles/{p['id']}").status_code == 404


def test_excel_round_trip(client, app, profile, mx_criteria):
    st = app.store
    comp, _ = st.upsert_company(Company(name="Acme MX", domain="acme.mx"))
    st.link_company(profile.id, comp.id, {"criteria_ids": [mx_criteria.id]})
    role, _ = st.upsert_role(Role(title="Data Engineer", url="https://acme.mx/j/1", company_id=comp.id,
                                  location=Place(country="MX", city="Monterrey")))
    st.link_role(profile.id, role.id, {"score": 80, "criteria_id": mx_criteria.id})
    data = client.get(f"/api/export/excel?profile_id={profile.id}").content
    wb = load_workbook(io.BytesIO(data))
    assert {"Summary", "Companies", "Roles", "Contacts", "Criteria", "Runs"} <= set(wb.sheetnames)
    ws = wb["Roles"]
    hdr = [c.value for c in ws[1]]
    ws.cell(row=2, column=hdr.index("stage") + 1, value="applied")
    ws.cell(row=2, column=hdr.index("notes") + 1, value="sent CV")
    wsc = wb["Companies"]
    chdr = [c.value for c in wsc[1]]
    wsc.cell(row=2, column=chdr.index("exclude") + 1, value="yes")
    buf = io.BytesIO()
    wb.save(buf)
    rep = client.post("/api/import/excel", files={"file": ("x.xlsx", buf.getvalue())}).json()
    assert rep["roles"] == 1 and rep["companies"] == 1 and not rep["errors"]
    link = st.get_role_link(profile.id, role.id)
    assert link.stage == "applied" and link.notes == "sent CV"
    assert st.get_company_link(profile.id, comp.id).excluded


def test_excel_locked_file_fallback(app, profile, tmp_path, monkeypatch):
    from openpyxl.workbook import Workbook

    from jobfinder.services import excel

    target = tmp_path / "JobFinder.xlsx"
    calls = {"n": 0}
    real = Workbook.save

    def flaky(self, filename):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("locked")
        return real(self, filename)

    monkeypatch.setattr(Workbook, "save", flaky)
    out = excel.export(app, None, target)
    assert out != target and out.exists()


def test_settings_endpoints(client):
    env = client.get("/api/admin/env").json()
    assert any(e["key"] == "OPENAI_API_KEY" for e in env)
    assert client.put("/api/admin/env", json={"NOT_ALLOWED": "x"}).status_code == 400
    assert client.put("/api/admin/env", json={"TAVILY_API_KEY": "tvly-123456789"}).status_code == 200
    conn = client.get("/api/admin/connections").json()
    assert conn["web_search"] == ["tavily"] and conn["sources"]["greenhouse"]["configured"]
    raw = client.get("/api/admin/connections/raw").json()["text"]
    assert client.put("/api/admin/connections/raw", json={"text": "llm: [bad"}).status_code == 422
    assert client.put("/api/admin/connections/raw", json={"text": raw}).json()["saved"]
    assert client.post("/api/admin/preset", json={"preset": "local"}).status_code == 200
    assert client.get("/api/admin/connections").json()["routes"]["default"] == ["local", "lmstudio"]
    assert client.get("/api/schemas/criteria").json()["title"] == "Criteria"
