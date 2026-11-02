"use strict";
// JobFinder dashboard: plain JS, no build step. Talks to the local API with the workspace token.

const TOKEN = document.querySelector('meta[name="jf-token"]').content;
const S = { profiles: [], pid: localStorage.getItem("jf.pid") || "", criteria: [], editing: null, runPoll: null };
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
// Value for an inline handler argument: a JSON string literal, then HTML-escaped for the attribute.
const js = (v) => esc(JSON.stringify(String(v ?? "")));
const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? esc(u) : "#");
const lines = (v) => String(v || "").split(/\n|,/).map((x) => x.trim()).filter(Boolean);

async function api(path, opts = {}) {
  const headers = { "X-JobFinder-Token": TOKEN, ...(opts.headers || {}) };
  let body = opts.body;
  if (body && !(body instanceof FormData) && typeof body !== "string") { body = JSON.stringify(body); headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, { ...opts, headers, body });
  if (opts.raw) return r;
  const text = await r.text();
  let data; try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) {
    let msg = data && data.detail ? data.detail : text;
    if (Array.isArray(msg)) msg = msg.map((e) => `${(e.loc || []).join(".")}: ${e.msg}`).join("\n");
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

function toast(msg, err = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), err ? 9000 : 3500);
}
const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); console.error(e); } };

// ----------------------------------------------------------------------------- navigation
$$("#tabs button").forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));
function show(tab) {
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab").forEach((s) => s.classList.toggle("active", s.id === "tab-" + tab));
  localStorage.setItem("jf.tab", tab);
  if (location.hash.slice(1).split(":")[0] !== tab) history.replaceState(null, "", "#" + tab);
  const r = RENDER[tab]; if (r) return guard(r)();
}
$("#profileSelect").addEventListener("change", (e) => { S.pid = e.target.value; localStorage.setItem("jf.pid", S.pid); show(localStorage.getItem("jf.tab") || "home"); });

async function loadProfiles() {
  S.profiles = await api("/api/profiles");
  if (!S.profiles.find((p) => p.id === S.pid)) S.pid = S.profiles[0]?.id || "";
  $("#profileSelect").innerHTML = S.profiles.length
    ? S.profiles.map((p) => `<option value="${esc(p.id)}" ${p.id === S.pid ? "selected" : ""}>${esc(p.name)}</option>`).join("")
    : `<option value="">(no profiles yet)</option>`;
}
function needProfile(el) {
  if (S.pid) return false;
  el.innerHTML = `<div class="card"><h2>No profile selected</h2><p>Create a profile on the <a href="#" onclick="show('home')">Home</a> tab first.</p></div>`;
  return true;
}

// ----------------------------------------------------------------------------- home
async function renderHome() {
  await loadProfiles();
  const [conn, sys] = await Promise.all([api("/api/admin/connections"), api("/api/admin/system")]);
  const models = Object.entries(conn.models).filter(([, m]) => m.configured).map(([n]) => n);
  const banner = $("#banner");
  if (conn.error) { banner.textContent = "connections.yaml has an error (last good config still active): " + conn.error; banner.classList.remove("hidden"); }
  else banner.classList.add("hidden");
  const p = S.profiles.find((x) => x.id === S.pid);
  const crit = S.pid ? await api(`/api/profiles/${S.pid}/criteria`) : [];
  const skills = S.pid ? await api(`/api/profiles/${S.pid}/skills`) : { skills: [] };
  const st = p?.stats || {};
  const steps = [
    [models.length > 0, `Connect an AI model (${models.join(", ") || "none yet"}) – <a href="#" onclick="show('settings')">AI &amp; Settings</a>. Optional but recommended.`],
    [conn.web_search.length > 0, `Add a web search key for company &amp; contact discovery (${conn.web_search.join(", ") || "none"}).`],
    [S.profiles.length > 0, "Create a profile (below)."],
    [skills.skills.length > 0, `Upload a resume or skills CSV – <a href="#" onclick="show('skills')">Skills</a>.`],
    [crit.length > 0, `Define search criteria – <a href="#" onclick="show('criteria')">Criteria</a>.`],
    [(st.roles || 0) > 0, `Run a search – <a href="#" onclick="show('runs')">Search runs</a>.`],
    [false, `Review in Excel – <a href="#" onclick="show('excel')">Excel</a>.`],
  ];
  const stoppedHtml = await stoppedRunsHtml();
  $("#tab-home").innerHTML = `${stoppedHtml}
    <div class="grid2">
      <div class="card"><h2>Getting started</h2><ol class="steps">${steps.map(([d, t]) => `<li class="${d ? "done" : ""}">${d ? "✔ " : ""}${t}</li>`).join("")}</ol>
        <p class="muted">Keyless sources (company ATS boards, Remotive) work without any key. ${esc(sys.local_model_advice)}</p></div>
      <div class="card"><h2>${p ? esc(p.name) : "No profile"}</h2>
        ${p ? `<div class="stats">
          <div class="stat"><b>${st.companies || 0}</b>companies</div><div class="stat"><b>${st.roles || 0}</b>roles</div>
          <div class="stat"><b>${st.roles_matched || 0}</b>matching</div><div class="stat"><b>${st.contacts || 0}</b>contacts</div>
          <div class="stat"><b>${(st.roles_by_stage || {}).applied || 0}</b>applied</div></div>
          <h3>Companies per criteria</h3>${crit.map((c) => `<div>${esc(c.name)}: <b>${(st.companies_by_criteria || {})[c.id] || 0}</b> / ${p.target_companies_per_criteria} target</div>`).join("") || '<span class="muted">no criteria yet</span>'}` : ""}
      </div>
    </div>
    <div class="card"><h2>Profiles</h2>
      <table><tr><th>Name</th><th>Companies</th><th>Roles</th><th>Matching</th><th>Contacts</th><th></th></tr>
      ${S.profiles.map((x) => `<tr><td>${esc(x.name)} <span class="muted">${esc(x.id)}</span></td><td>${x.stats.companies}</td><td>${x.stats.roles}</td><td>${x.stats.roles_matched}</td><td>${x.stats.contacts}</td>
        <td><button class="btn small sec" onclick="pick(${js(x.id)})">Open</button> <button class="btn small danger" onclick="delProfile(${js(x.id)})">Delete</button></td></tr>`).join("")}
      </table>
      <h3>New profile</h3>
      <div class="row"><input id="newName" placeholder="Full name" style="min-width:240px"><input id="newHeadline" placeholder="Headline (e.g. Data Engineer)" style="min-width:280px">
      <button class="btn" id="btnNewProfile">Create</button></div>
      <h3>Share / move profiles</h3>
      <div class="row"><input type="file" id="bundleFile" accept=".json"><button class="btn sec" id="btnImportBundle">Import profile bundle</button>
      ${S.pid ? `<button class="btn sec" id="btnExportBundle">Download "${esc(p?.name || S.pid)}" bundle</button>` : ""}</div>
      <p class="muted">A bundle holds profile + criteria + skills (no companies/contacts). Examples ship in the <code>examples</code> folder.</p>
    </div>`;
  $("#btnImportBundle").onclick = guard(async () => {
    const f = $("#bundleFile").files[0]; if (!f) return toast("Choose a .json bundle", true);
    const r = await api("/api/profiles/import", { method: "POST", body: JSON.parse(await f.text()) });
    S.pid = r.profile; localStorage.setItem("jf.pid", r.profile); toast(`Imported ${r.profile} with ${r.criteria.length} criteria`); renderHome();
  });
  if ($("#btnExportBundle")) $("#btnExportBundle").onclick = guard(async () => {
    const b = await api(`/api/profiles/${S.pid}/bundle`);
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([JSON.stringify(b, null, 2)], { type: "application/json" }));
    a.download = `${S.pid}.profile.json`; a.click();
  });
  $("#btnNewProfile").onclick = guard(async () => {
    const name = $("#newName").value.trim(); if (!name) return toast("Name is required", true);
    const pr = await api("/api/profiles", { method: "POST", body: { name, headline: $("#newHeadline").value.trim() || null } });
    S.pid = pr.id; localStorage.setItem("jf.pid", pr.id); toast("Profile created"); renderHome();
  });
}
window.pick = (id) => { S.pid = id; localStorage.setItem("jf.pid", id); loadProfiles().then(() => show("home")); };
window.delProfile = guard(async (id) => {
  if (!confirm(`Delete profile ${id} and its pipeline (companies/roles links, contacts, criteria)?`)) return;
  await api(`/api/profiles/${id}`, { method: "DELETE" }); toast("Deleted"); renderHome();
});
window.show = show;

// ----------------------------------------------------------------------------- profile
async function renderProfile() {
  const el = $("#tab-profile"); if (needProfile(el)) return;
  const p = await api(`/api/profiles/${S.pid}`);
  el.innerHTML = `<div class="card"><h2>Profile</h2>
    <div class="grid">
      <label>Name<input id="pf_name" value="${esc(p.name)}"></label>
      <label>Headline<input id="pf_headline" value="${esc(p.headline)}"></label>
      <label>Email<input id="pf_email" value="${esc(p.email)}"></label>
      <label>Phone<input id="pf_phone" value="${esc(p.phone)}"></label>
      <label>Home country (ISO)<input id="pf_country" value="${esc(p.home?.country)}" placeholder="MX"></label>
      <label>Home region (ISO 3166-2)<input id="pf_region" value="${esc(p.home?.region)}" placeholder="MX-NLE"></label>
      <label>Home city<input id="pf_city" value="${esc(p.home?.city)}"></label>
      <label>Target companies per criteria<input id="pf_target" type="number" value="${p.target_companies_per_criteria}"></label>
    </div>
    <h3>Languages <span class="muted">(code:level, e.g. es:native, en:C1)</span></h3>
    <input id="pf_langs" style="width:100%" value="${esc((p.languages || []).map((l) => l.code + ":" + l.level).join(", "))}">
    <h3>Work authorization <span class="muted">(COUNTRY:status – citizen, permanent_resident, work_permit, open_work_permit, needs_sponsorship)</span></h3>
    <input id="pf_auth" style="width:100%" value="${esc((p.work_authorization || []).map((w) => w.country + ":" + w.status).join(", "))}">
    <h3>Links <span class="muted">(one URL per line)</span></h3>
    <textarea id="pf_links" style="width:100%">${esc((p.links || []).map((l) => l.url).join("\n"))}</textarea>
    <label>Notes<textarea id="pf_notes">${esc(p.notes)}</textarea></label>
    <div class="row"><button class="btn" id="pfSave">Save profile</button></div></div>`;
  $("#pfSave").onclick = guard(async () => {
    const home = { country: $("#pf_country").value.trim() || null, region: $("#pf_region").value.trim() || null, city: $("#pf_city").value.trim() || null };
    const body = {
      ...p, name: $("#pf_name").value.trim(), headline: $("#pf_headline").value.trim() || null, email: $("#pf_email").value.trim() || null,
      phone: $("#pf_phone").value.trim() || null, home: home.country || home.city ? home : null,
      target_companies_per_criteria: +$("#pf_target").value || 50, notes: $("#pf_notes").value || null,
      languages: lines($("#pf_langs").value).map((x) => { const [code, level] = x.split(":"); return { code: code.trim(), level: (level || "B2").trim() }; }),
      work_authorization: lines($("#pf_auth").value).map((x) => { const [country, status] = x.split(":"); return { country: country.trim(), status: (status || "").trim() }; }),
      links: lines($("#pf_links").value).map((url) => ({ network: /linkedin/.test(url) ? "linkedin" : /github/.test(url) ? "github" : "website", url })),
    };
    delete body.created_at; delete body.updated_at;
    await api(`/api/profiles/${S.pid}`, { method: "PUT", body }); toast("Saved"); loadProfiles();
  });
}

// ----------------------------------------------------------------------------- criteria
const WORK_MODES = ["remote", "hybrid", "onsite"];
const EMP = ["full_time", "part_time", "contract", "temporary", "internship", "freelance", "seasonal"];
const SEN = ["intern", "entry", "junior", "mid", "senior", "lead", "manager", "director", "executive"];
const PROGRAMS = ["CA_LMIA", "CA_GTS", "CA_IEC", "CA_CUSMA", "US_H1B", "US_TN", "US_L1", "US_O1", "US_GREEN_CARD", "UK_SKILLED_WORKER", "MX_WORK_VISA", "EU_BLUE_CARD", "AU_TSS", "OTHER"];
const FIELDS = ["title", "description", "skills", "company", "company_domain", "industry", "country", "region", "city", "work_mode", "employment_type", "seniority", "salary_min", "salary_max", "salary_currency", "posted_days_ago", "languages", "source", "sponsorship"];
const OPS = ["any", "all", "none", "contains", "not_contains", "eq", "ne", "in", "nin", "prefix", "regex", "gte", "lte", "between", "exists"];
const blankCriteria = () => ({ name: "New criteria", active: true, constraints: { locations: [], work_mode: [...WORK_MODES], employment_type: [], seniority: [], sponsorship: { need: "not_required", programs: [] }, languages: [], posted_within_days: 30, include_unknown: true, exclude_companies: [], remote_eligibility: [] }, targets: { titles: [], activities: [], keywords: [], occupations: [], industries: [] }, rules: { must: [], should: [], must_not: [] } });

async function renderCriteria() {
  const el = $("#tab-criteria"); if (needProfile(el)) return;
  S.criteria = await api(`/api/profiles/${S.pid}/criteria`);
  const occ = await api("/api/catalog/occupations");
  el.innerHTML = `<div class="card"><h2>Criteria for this profile</h2>
    <p class="muted">One criteria per role family (e.g. "Data engineer – Monterrey" and "Pharmacy assistant – Ontario"). Constraints, targets and rules are yours; AI may only tune the search strategy.</p>
    <table><tr><th>Name</th><th>Titles</th><th>Locations</th><th>Active</th><th>Strategy</th><th></th></tr>
    ${S.criteria.map((c) => `<tr><td>${esc(c.name)}<br><span class="muted">${esc(c.id)}</span></td><td>${esc(c.targets.titles.join(", "))}</td>
      <td>${esc(c.constraints.locations.map((l) => [l.city, l.region, l.country].filter(Boolean).join(", ")).join(" | ") || "any")}</td>
      <td>${c.active ? "yes" : "no"}</td><td>v${c.search_strategy.version} · ${c.search_strategy.queries.length} queries</td>
      <td><button class="btn small sec" onclick="editCriteria(${js(c.id)})">Edit</button> <button class="btn small danger" onclick="delCriteria(${js(c.id)})">Delete</button></td></tr>`).join("")}
    </table><div class="row"><button class="btn" onclick="editCriteria(null)">+ New criteria</button>
    <select id="occPick"><option value="">…or start from an occupation template</option>${occ.map((o) => `<option value="${esc(o.key)}">${esc(o.key)}</option>`).join("")}</select></div></div>
    <div id="critEditor"></div>`;
  $("#occPick").onchange = (e) => {
    const o = occ.find((x) => x.key === e.target.value); if (!o) return;
    const c = blankCriteria(); c.name = o.key.replace(/-/g, " ");
    c.targets.titles = [...(o.titles.en || []).slice(0, 2), ...(o.titles.es || []).slice(0, 1), ...(o.titles.fr || []).slice(0, 1)];
    c.targets.occupations = Object.entries(o.codes).map(([k, v]) => ({ system: { isco: "ISCO-08", noc: "NOC-2021", soc: "SOC-2018" }[k], code: v }));
    c.rules.should = [{ field: "skills", op: "any", value: o.skills, weight: 2, label: "core skills" }];
    S.editing = c; drawCriteriaEditor();
  };
}
window.editCriteria = (id) => { S.editing = id ? JSON.parse(JSON.stringify(S.criteria.find((c) => c.id === id))) : blankCriteria(); drawCriteriaEditor(); };
window.delCriteria = guard(async (id) => { if (!confirm("Delete criteria " + id + "?")) return; await api(`/api/criteria/${id}`, { method: "DELETE" }); renderCriteria(); });

function ruleRows(kind, rules) {
  return rules.map((r, i) => `<div class="rulerow" data-kind="${kind}" data-i="${i}">
    <select class="r_field">${FIELDS.map((f) => `<option ${f === r.field ? "selected" : ""}>${f}</option>`).join("")}</select>
    <select class="r_op">${OPS.map((o) => `<option ${o === r.op ? "selected" : ""}>${o}</option>`).join("")}</select>
    <input class="r_value" value="${esc(Array.isArray(r.value) ? r.value.join(", ") : r.value ?? "")}" placeholder="values, comma separated">
    <input class="r_weight" type="number" step="0.5" value="${r.weight ?? 1}" ${kind === "should" ? "" : "disabled"}>
    <button class="btn small danger" onclick="this.parentNode.remove()">×</button></div>`).join("");
}
function locRows(locs) {
  return locs.map((l) => `<div class="locrow"><input class="l_country" value="${esc(l.country)}" placeholder="MX">
    <input class="l_region" value="${esc(l.region)}" placeholder="MX-NLE"><input class="l_city" value="${esc(l.city)}" placeholder="Monterrey">
    <input class="l_radius" type="number" value="${esc(l.radius_km)}" placeholder="km"><button class="btn small danger" onclick="this.parentNode.remove()">×</button></div>`).join("");
}
const chips = (name, all, sel) => `<div class="chips">${all.map((v) => `<label><input type="checkbox" name="${name}" value="${v}" ${sel.includes(v) ? "checked" : ""}>${v}</label>`).join("")}</div>`;

function drawCriteriaEditor() {
  const c = S.editing, k = c.constraints, t = c.targets;
  $("#critEditor").innerHTML = `<div class="card"><h2>${c.id ? "Edit " + esc(c.id) : "New criteria"}</h2>
    <div class="grid"><label>Name<input id="c_name" value="${esc(c.name)}"></label>
      <label class="inline"><input type="checkbox" id="c_active" ${c.active ? "checked" : ""}> active</label>
      <label>Posted within (days)<input id="c_days" type="number" value="${k.posted_within_days ?? ""}"></label>
      <label class="inline"><input type="checkbox" id="c_unknown" ${k.include_unknown ? "checked" : ""}> keep roles with unknown values</label></div>
    <h3>Target titles <span class="muted">(one per line, every language you accept)</span></h3>
    <div class="grid2"><textarea id="c_titles">${esc(t.titles.join("\n"))}</textarea>
      <div><label>Activities (one per line)<textarea id="c_acts">${esc(t.activities.join("\n"))}</textarea></label>
      <label>Keywords<input id="c_kw" value="${esc(t.keywords.join(", "))}"></label>
      <label>Occupation codes <span class="muted">(SYSTEM:code e.g. ISCO-08:2521, NOC-2021:21223)</span><input id="c_occ" value="${esc(t.occupations.map((o) => o.system + ":" + o.code).join(", "))}"></label></div></div>
    <h3>Locations <span class="muted">(ISO country, ISO 3166-2 region, city, radius km) – any of them</span></h3>
    <div id="c_locs">${locRows(k.locations)}</div><button class="btn small sec" id="addLoc">+ location</button>
    <h3>Work mode</h3>${chips("wm", WORK_MODES, k.work_mode)}
    <label>Remote roles must accept (countries or blocs: LATAM, NA, EU, EMEA, APAC, GLOBAL)<input id="c_relig" value="${esc(k.remote_eligibility.join(", "))}"></label>
    <h3>Employment type <span class="muted">(none = any)</span></h3>${chips("emp", EMP, k.employment_type)}
    <h3>Seniority <span class="muted">(none = any)</span></h3>${chips("sen", SEN, k.seniority)}
    <div class="grid"><label>Salary floor<input id="c_smin" type="number" value="${esc(k.salary?.min)}"></label>
      <label>Currency (ISO)<input id="c_scur" value="${esc(k.salary?.currency)}" placeholder="MXN"></label>
      <label>Period<select id="c_sper">${["year", "month", "week", "day", "hour"].map((p) => `<option ${p === (k.salary?.period || "year") ? "selected" : ""}>${p}</option>`).join("")}</select></label>
      <label>Sponsorship / LMIA<select id="c_spneed">${["not_required", "preferred", "required"].map((p) => `<option ${p === k.sponsorship.need ? "selected" : ""}>${p}</option>`).join("")}</select></label></div>
    ${chips("prog", PROGRAMS, k.sponsorship.programs)}
    <div class="grid"><label>Languages required (code:CEFR)<input id="c_langs" value="${esc(k.languages.map((l) => l.code + ":" + l.min).join(", "))}"></label>
      <label>Exclude companies (names or domains)<input id="c_excl" value="${esc(k.exclude_companies.join(", "))}"></label></div>
    <h3>Rules</h3><p class="muted"><b>must</b>: all required · <b>should</b>: adds weight to the score · <b>must_not</b>: any rejects. Skills accept names (Python, Excel, dispensing…).</p>
    ${["must", "should", "must_not"].map((kind) => `<div><b>${kind}</b><div id="r_${kind}">${ruleRows(kind, c.rules[kind])}</div><button class="btn small sec" onclick="addRule('${kind}')">+ ${kind}</button></div>`).join("")}
    <details><summary>Advanced: edit as JSON</summary><textarea id="c_json" class="code"></textarea>
      <div class="row"><button class="btn sec" id="cFromJson">Apply JSON to form</button></div></details>
    <div class="row"><button class="btn" id="cSave">Save criteria</button><button class="btn sec" id="cPreview">Validate &amp; preview queries</button></div>
    <div id="c_preview"></div>
    ${c.search_strategy ? `<details><summary>Search strategy (AI-owned) v${c.search_strategy.version}</summary><pre class="log">${esc(JSON.stringify(c.search_strategy, null, 2))}</pre></details>` : ""}
    </div>`;
  $("#addLoc").onclick = () => $("#c_locs").insertAdjacentHTML("beforeend", locRows([{}]));
  $("#c_json").value = JSON.stringify(c, null, 2);
  $("#cFromJson").onclick = guard(() => { S.editing = JSON.parse($("#c_json").value); drawCriteriaEditor(); });
  $("#cSave").onclick = guard(saveCriteria);
  $("#cPreview").onclick = guard(async () => {
    const res = await api("/api/criteria/validate", { method: "POST", body: collectCriteria() });
    $("#c_preview").innerHTML = res.valid
      ? `<div class="card"><b class="ok">Valid.</b> Role queries:<ul>${res.role_queries.map((q) => `<li>${esc(q.text)} @ ${esc(q.location || "any")} ${q.sources?.length ? "(" + esc(q.sources.join(",")) + ")" : ""}</li>`).join("")}</ul>
         Company discovery queries:<ul>${res.company_queries.map((q) => `<li>${esc(q)}</li>`).join("")}</ul></div>`
      : `<div class="card bad">${res.errors.map((e) => esc(e.loc.join(".") + ": " + e.msg)).join("<br>")}</div>`;
  });
}
window.addRule = (kind) => $("#r_" + kind).insertAdjacentHTML("beforeend", ruleRows(kind, [{ field: "skills", op: "any", value: [], weight: 1 }]));

function parseVal(op, raw) {
  const v = raw.trim();
  if (["gte", "lte"].includes(op)) return v === "" ? null : +v;
  if (op === "between") return lines(v).map(Number);
  if (op === "exists") return v === "" ? true : v === "true";
  return lines(v);
}
function collectCriteria() {
  const c = JSON.parse(JSON.stringify(S.editing));
  const checked = (n) => $$(`input[name="${n}"]:checked`).map((x) => x.value);
  c.name = $("#c_name").value.trim(); c.active = $("#c_active").checked;
  c.targets.titles = lines($("#c_titles").value.replace(/,/g, "\n")); c.targets.activities = $("#c_acts").value.split("\n").map((x) => x.trim()).filter(Boolean);
  c.targets.keywords = lines($("#c_kw").value);
  c.targets.occupations = lines($("#c_occ").value).map((x) => { const i = x.lastIndexOf(":"); return { system: x.slice(0, i).trim(), code: x.slice(i + 1).trim() }; });
  const k = c.constraints;
  k.locations = $$("#c_locs .locrow").map((r) => ({ country: $(".l_country", r).value.trim().toUpperCase(), region: $(".l_region", r).value.trim().toUpperCase() || null, city: $(".l_city", r).value.trim() || null, radius_km: $(".l_radius", r).value ? +$(".l_radius", r).value : null })).filter((l) => l.country);
  k.work_mode = checked("wm"); k.employment_type = checked("emp"); k.seniority = checked("sen");
  k.remote_eligibility = lines($("#c_relig").value).map((x) => x.toUpperCase());
  k.posted_within_days = $("#c_days").value ? +$("#c_days").value : null; k.include_unknown = $("#c_unknown").checked;
  k.salary = $("#c_smin").value ? { min: +$("#c_smin").value, currency: $("#c_scur").value.trim().toUpperCase() || null, period: $("#c_sper").value } : null;
  k.sponsorship = { need: $("#c_spneed").value, programs: checked("prog") };
  k.languages = lines($("#c_langs").value).map((x) => { const [code, min] = x.split(":"); return { code: code.trim(), min: (min || "B2").trim() }; });
  k.exclude_companies = lines($("#c_excl").value);
  for (const kind of ["must", "should", "must_not"]) {
    c.rules[kind] = $$(`#r_${kind} .rulerow`).map((r) => {
      const op = $(".r_op", r).value;
      return { field: $(".r_field", r).value, op, value: parseVal(op, $(".r_value", r).value), weight: +$(".r_weight", r).value || 1 };
    });
  }
  delete c.created_at; delete c.updated_at;
  return c;
}
async function saveCriteria() {
  const c = collectCriteria();
  const saved = c.id ? await api(`/api/criteria/${c.id}`, { method: "PUT", body: c }) : await api(`/api/profiles/${S.pid}/criteria`, { method: "POST", body: c });
  toast("Criteria saved"); S.editing = saved; await renderCriteria(); S.editing = saved; drawCriteriaEditor();
}

// ----------------------------------------------------------------------------- skills
async function renderSkills() {
  const el = $("#tab-skills"); if (needProfile(el)) return;
  const [s, resumes] = await Promise.all([api(`/api/profiles/${S.pid}/skills`), api(`/api/profiles/${S.pid}/resumes`)]);
  el.innerHTML = `<div class="grid2"><div class="card"><h2>Upload resume or skills file</h2>
      <p class="muted">PDF, DOCX or TXT resumes are scanned against the skills catalog (and by AI when a model is configured). CSV columns: skill, level (1-5), years, category.</p>
      <div class="row"><input type="file" id="sk_file" accept=".pdf,.docx,.txt,.md,.csv,.tsv">
      <select id="sk_mode"><option value="merge">merge with current</option><option value="replace">replace (keep manual)</option></select>
      <label class="inline"><input type="checkbox" id="sk_llm" checked> use AI</label><button class="btn" id="sk_up">Upload</button></div>
      <div class="muted">${resumes.map((r) => `${esc(r.filename)} · ${r.chars} chars · ${esc(r.created_at)}`).join("<br>")}</div></div>
    <div class="card"><h2>Summary</h2><textarea id="sk_summary" style="width:100%">${esc(s.summary)}</textarea></div></div>
    <div class="card"><h2>Skills (${s.skills.length})</h2>
      <table id="sk_table"><tr><th>Skill</th><th>Category</th><th>Level 1-5</th><th>Years</th><th>Source</th><th></th></tr>
      ${s.skills.map((k) => skillRow(k)).join("")}</table>
      <div class="row"><input id="sk_new" placeholder="Add skill (e.g. Power BI, dispensación)"><button class="btn sec" id="sk_add">Add</button>
      <button class="btn" id="sk_save">Save skills</button></div></div>`;
  $("#sk_add").onclick = () => { const n = $("#sk_new").value.trim(); if (!n) return; $("#sk_table").insertAdjacentHTML("beforeend", skillRow({ id: "", name: n, source: "manual" })); $("#sk_new").value = ""; };
  $("#sk_save").onclick = guard(async () => {
    const skills = $$("#sk_table tr[data-id]").map((r) => ({ id: r.dataset.id, name: $(".s_name", r).value.trim(), category: $(".s_cat", r).value.trim() || null, level: $(".s_lvl", r).value ? +$(".s_lvl", r).value : null, years: $(".s_yrs", r).value ? +$(".s_yrs", r).value : null, source: r.dataset.source || "manual" }));
    await api(`/api/profiles/${S.pid}/skills`, { method: "PUT", body: { profile_id: S.pid, skills, summary: $("#sk_summary").value || null } });
    toast("Skills saved"); renderSkills();
  });
  $("#sk_up").onclick = guard(async () => {
    const f = $("#sk_file").files[0]; if (!f) return toast("Choose a file", true);
    const fd = new FormData(); fd.append("file", f); fd.append("mode", $("#sk_mode").value); fd.append("use_llm", $("#sk_llm").checked);
    $("#sk_up").disabled = true; $("#sk_up").textContent = "Reading…";
    try { const r = await api(`/api/profiles/${S.pid}/skills/upload`, { method: "POST", body: fd }); toast(`Found ${r.extracted}, added ${r.added} (total ${r.total})${r.llm ? " · AI: " + r.llm : ""}`); }
    finally { renderSkills(); }
  });
}
const skillRow = (k) => `<tr data-id="${esc(k.id)}" data-source="${esc(k.source)}"><td><input class="s_name" value="${esc(k.name)}"></td><td><input class="s_cat" value="${esc(k.category)}"></td>
  <td><input class="s_lvl" type="number" min="1" max="5" value="${esc(k.level)}" style="width:60px"></td><td><input class="s_yrs" type="number" step="0.5" value="${esc(k.years)}" style="width:70px"></td>
  <td><span class="pill">${esc(k.source)}</span></td><td><button class="btn small danger" onclick="this.closest('tr').remove()">×</button></td></tr>`;

// ----------------------------------------------------------------------------- pipeline
const STAGES = ["none", "shortlisted", "applied", "interview", "offer", "hired", "declined", "rejected", "withdrawn"];
const scoreCls = (s) => (s >= 70 ? "hi" : s >= 50 ? "mid" : "lo");
async function renderPipeline() {
  const el = $("#tab-pipeline"); if (needProfile(el)) return;
  const [comps, roles, contacts] = await Promise.all([api(`/api/profiles/${S.pid}/companies`), api(`/api/profiles/${S.pid}/roles`), api(`/api/profiles/${S.pid}/contacts`)]);
  const cname = Object.fromEntries(comps.map((c) => [c.id, c.name]));
  const byRole = {}; contacts.forEach((c) => (byRole[c.pipeline.role_id] = (byRole[c.pipeline.role_id] || 0) + 1));
  el.innerHTML = `<div class="card"><h2>Companies (${comps.length})</h2>
    <div class="row"><input id="ac_name" placeholder="Company name"><input id="ac_domain" placeholder="website domain (acme.com)">
      <select id="ac_ats"><option value="">ATS (optional)</option>${["greenhouse", "lever", "ashby", "smartrecruiters", "workable", "workday"].map((a) => `<option>${a}</option>`).join("")}</select>
      <input id="ac_board" placeholder="board slug (workday: host|site)"><button class="btn sec" id="ac_add">Add company</button></div>
    <div class="scroll"><table><tr><th>Company</th><th>ATS / careers</th><th>Status</th><th>Roles</th><th>Criteria</th><th>Exclude</th><th>Notes</th><th></th></tr>
    ${comps.map((c) => `<tr><td><b>${esc(c.name)}</b><br><span class="muted">${esc(c.domain || c.id)}</span></td>
      <td>${c.ats ? `<span class="pill">${esc(c.ats.type)}:${esc(c.ats.board)}</span>` : ""} ${c.careers_url ? `<a href="${safeUrl(c.careers_url)}" target="_blank">careers</a>` : ""}</td>
      <td><span class="pill ${c.pipeline.status === "roles_searched" ? "ok" : c.pipeline.status === "excluded" || c.pipeline.status === "error" ? "bad" : ""}">${esc(c.pipeline.status)}</span><br>${accessPill(c.fetch)}</td>
      <td>${roles.filter((r) => r.company_id === c.id).length}</td><td class="muted">${esc((c.pipeline.criteria_ids || []).join(", "))}</td>
      <td class="edit"><input type="checkbox" ${c.pipeline.excluded ? "checked" : ""} onchange="patchLink('companies',${js(c.id)},{excluded:this.checked})"></td>
      <td class="edit"><input value="${esc(c.pipeline.notes)}" onchange="patchLink('companies',${js(c.id)},{notes:this.value})"></td>
      <td><button class="btn small sec" onclick="runFor('find_roles',{company_ids:[${js(c.id)}]})">Find roles</button></td></tr>`).join("")}
    </table></div></div>
    <div class="card"><h2>Roles (${roles.length})</h2>
    <div class="row"><label class="inline"><input type="checkbox" id="rf_pass" checked> only passing</label><input id="rf_q" placeholder="filter text"></div>
    <div class="scroll"><table id="rolesTable"><tr><th>Score</th><th>Role</th><th>Company</th><th>Where</th><th>Skills</th><th>Contacts</th><th>Stage</th><th>Exclude</th><th>Notes</th><th></th></tr>
    ${roles.map((r) => { const m = r.pipeline.match || {}; return `<tr data-pass="${m.passed ? 1 : 0}" data-text="${esc((r.title + " " + (cname[r.company_id] || "")).toLowerCase())}">
      <td class="score ${scoreCls(r.pipeline.score || 0)}">${r.pipeline.score ?? ""}</td>
      <td><a href="${safeUrl(r.url)}" target="_blank">${esc(r.title)}</a><br><span class="muted">${esc(r.source || "")} ${r.posted_at ? "· " + esc(r.posted_at.slice(0, 10)) : ""}</span>
        ${accessPill(r.fetch)}${r.pipeline.review ? `<span class="pill ${r.pipeline.review.fit >= 70 ? "ok" : "warn"}" title="${esc(r.pipeline.review.summary || "")}">AI fit ${esc(r.pipeline.review.fit ?? "?")} · ${esc(r.pipeline.review.model || "")}</span>` : ""}
        ${(m.failed || []).map((f) => `<span class="pill bad">${esc(f)}</span>`).join("")}${(m.warnings || []).slice(0, 2).map((f) => `<span class="pill warn">${esc(f)}</span>`).join("")}</td>
      <td>${esc(cname[r.company_id] || r.company_id)}</td>
      <td>${esc([r.location?.city, r.location?.region, r.location?.country].filter(Boolean).join(", "))}<br><span class="muted">${esc(r.work_mode || "")}</span></td>
      <td>${(m.matched_skills || []).map((s) => `<span class="pill ok">${esc(s)}</span>`).join("")}${(m.missing_skills || []).slice(0, 5).map((s) => `<span class="pill">${esc(s)}</span>`).join("")}</td>
      <td>${byRole[r.id] || 0}</td>
      <td class="edit"><select onchange="patchLink('roles',${js(r.id)},{stage:this.value})">${STAGES.map((s) => `<option ${s === r.pipeline.stage ? "selected" : ""}>${s}</option>`).join("")}</select></td>
      <td class="edit"><input type="checkbox" ${r.pipeline.excluded ? "checked" : ""} onchange="patchLink('roles',${js(r.id)},{excluded:this.checked})"></td>
      <td class="edit"><input value="${esc(r.pipeline.notes)}" onchange="patchLink('roles',${js(r.id)},{notes:this.value})"></td>
      <td><button class="btn small sec" onclick="brief(${js(r.id)})">Brief</button> <button class="btn small sec" onclick="runFor('find_contacts',{role_ids:[${js(r.id)}]})">Contacts</button></td></tr>`; }).join("")}
    </table></div></div>
    <div class="card"><h2>Contacts (${contacts.length})</h2><p class="muted">Public business information only. Reach out personally – nothing is sent automatically.</p>
    <div class="scroll"><table><tr><th>Name</th><th>Title</th><th>Type</th><th>Company</th><th>Profile</th><th>Status</th><th>Notes</th></tr>
    ${contacts.map((c) => `<tr><td>${esc(c.name)}</td><td>${esc(c.title)}</td><td><span class="pill">${esc(c.type)}</span></td><td>${esc(cname[c.company_id] || "")}</td>
      <td>${(c.profiles || []).map((p) => `<a href="${safeUrl(p.url)}" target="_blank">${esc(p.network)}</a>`).join(" ")} ${esc(c.work_email || "")}</td>
      <td class="edit"><select onchange="patchContact(${js(c.id)},${js(c.pipeline.role_id || "")},{status:this.value})">${["found", "verified", "rejected", "contacted", "replied"].map((s) => `<option ${s === c.pipeline.status ? "selected" : ""}>${s}</option>`).join("")}</select></td>
      <td class="edit"><input value="${esc(c.pipeline.notes)}" onchange="patchContact(${js(c.id)},${js(c.pipeline.role_id || "")},{notes:this.value})"></td></tr>`).join("")}
    </table></div></div><div id="briefBox"></div>`;
  const filter = () => { const q = $("#rf_q").value.toLowerCase(), only = $("#rf_pass").checked; $$("#rolesTable tr[data-text]").forEach((tr) => tr.classList.toggle("hidden", (only && tr.dataset.pass !== "1") || (q && !tr.dataset.text.includes(q)))); };
  $("#rf_pass").onchange = filter; $("#rf_q").oninput = filter; filter();
  $("#ac_add").onclick = guard(async () => {
    const name = $("#ac_name").value.trim(); if (!name) return toast("Name required", true);
    const body = { name, domain: $("#ac_domain").value.trim() || null };
    if ($("#ac_ats").value && $("#ac_board").value.trim()) body.ats = { type: $("#ac_ats").value, board: $("#ac_board").value.trim() };
    await api(`/api/profiles/${S.pid}/companies`, { method: "POST", body }); toast("Company added"); renderPipeline();
  });
}
window.patchLink = guard(async (kind, id, patch) => { await api(`/api/profiles/${S.pid}/${kind}/${encodeURIComponent(id)}`, { method: "PATCH", body: patch }); toast("Saved"); });
window.patchContact = guard(async (id, roleId, patch) => { await api(`/api/profiles/${S.pid}/contacts/${encodeURIComponent(id)}`, { method: "PATCH", body: { ...patch, role_id: roleId || null } }); toast("Saved"); });
window.brief = guard(async (rid) => {
  const b = await api(`/api/profiles/${S.pid}/roles/${encodeURIComponent(rid)}/brief`);
  $("#briefBox").innerHTML = `<div class="card"><h2>Resume brief – ${esc(b.role.title)} @ ${esc(b.role.company)}</h2>
    <div class="row"><button class="btn sec" id="copyBrief">Copy JSON for your resume AI</button></div><pre class="log">${esc(JSON.stringify(b, null, 2))}</pre></div>`;
  $("#copyBrief").onclick = () => navigator.clipboard.writeText(JSON.stringify(b, null, 2)).then(() => toast("Copied"));
  $("#briefBox").scrollIntoView({ behavior: "smooth" });
});

// ----------------------------------------------------------------------------- runs
window.runFor = guard(async (kind, extra = {}) => {
  const run = await api(`/api/profiles/${S.pid}/runs`, { method: "POST", body: { kind, ...extra } });
  toast(`Started ${kind}`); show("runs"); watchRun(run.id);
});
async function renderRuns() {
  const el = $("#tab-runs"); if (needProfile(el)) return;
  const [runs, crit, conn] = await Promise.all([api(`/api/runs?profile_id=${S.pid}`), api(`/api/profiles/${S.pid}/criteria`), api("/api/admin/connections")]);
  const llm = Object.values(conn.models).some((m) => m.configured), search = conn.web_search.length > 0;
  const stoppedHtml = await stoppedRunsHtml();
  el.innerHTML = `${stoppedHtml}<div class="card"><h2>Start a search</h2>
    <div class="row"><label>Criteria<select id="run_crit"><option value="">all active</option>${crit.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("")}</select></label>
    <label>Limit<input id="run_limit" type="number" placeholder="default" style="width:90px"></label>
    <label>Companies<select id="run_scope"><option value="new">new + failed ones due for retry</option><option value="all">all companies (refresh every board)</option><option value="failed">only boards that could not be read</option></select></label>
    <label class="inline"><input type="checkbox" id="run_llm" checked> use AI</label></div>
    <div class="row">
      <button class="btn" onclick="startRun('autopilot')">Autopilot (companies → roles → contacts)</button>
      <button class="btn sec" onclick="startRun('find_companies')" ${search || llm ? "" : "disabled title='needs web search key or AI model'"}>Find companies</button>
      <button class="btn sec" onclick="startRun('find_roles')">Find roles</button>
      <button class="btn sec" onclick="startRun('find_contacts')" ${search ? "" : "disabled title='needs web search key'"}>Find HR contacts</button>
      <button class="btn sec" onclick="startRun('review_roles')" ${llm ? "" : "disabled title='needs a model for the review_role step'"}>Review top roles with AI</button>
      <button class="btn sec" onclick="startRun('recheck')">Recheck postings</button></div>
    <p class="muted">Find roles searches job APIs (Adzuna, Google Jobs, Remotive…) and the job boards of companies in your roster. ${search ? "" : "Add a web search key to discover companies and contacts. "}${llm ? "" : "Add an AI model for smarter queries and resume parsing."}</p></div>
    <div class="card"><h2>Guided mode – one small question at a time</h2>
      <p class="muted">For small or local models, any chatbot (copy the prompt, paste the reply), or answering yourself. JobFinder does the searching and checking; the AI only answers short questions like “1,3”.</p>
      <div class="row"><button class="btn" onclick="guidedNext()">Next question</button>
        <button class="btn sec" onclick="startRun('guided')" ${llm ? "" : "disabled title='needs a configured or running model'"}>Let my configured AI answer them</button></div>
      <div id="guidedBox"></div></div>
    <div class="grid2"><div class="card"><h2>Recent runs</h2><div class="scroll"><table><tr><th>When</th><th>Kind</th><th>Status</th><th>Result</th></tr>
    ${runs.map((r) => `<tr onclick="watchRun(${js(r.id)})" style="cursor:pointer"><td>${esc((r.created_at || "").replace("T", " ").slice(0, 16))}</td><td>${esc(r.kind)}</td>
      <td><span class="pill ${r.status === "done" ? "ok" : r.status === "failed" ? "bad" : "warn"}">${esc(r.status)}</span>${r.resumed_from ? '<br><span class="muted">resumed</span>' : ""}</td><td class="muted">${esc(summ(r.result))}${r.error ? `<br><span class="bad">${esc(r.error)}</span>` : ""}</td></tr>`).join("")}
    </table></div></div><div class="card"><h2>Run log <span id="runTitle" class="muted"></span></h2><div class="row"><button class="btn small danger hidden" id="runCancel">Cancel</button><button class="btn small hidden" id="runResume">Resume from where it stopped</button></div><div id="runLog" class="log">Select a run.</div></div></div>`;
}
function drawGuided(t, message = "", ok = true) {
  S.guided = t;
  const box = $("#guidedBox"); if (!box) return;
  box.innerHTML = `${message ? `<p class="${ok ? "ok" : "bad"}">${esc(message)}</p>` : ""}
    ${(t.done_automatically || []).length ? `<p class="muted">Done automatically: ${esc(t.done_automatically.join(" · "))}</p>` : ""}
    <p class="muted">${esc(t.kind)} · ${t.stats && t.stats.companies !== undefined ? `${t.stats.companies} companies · ${t.stats.roles} roles · ${t.stats.contacts} contacts` : ""}
      ${t.needs_user ? ' · <span class="warn">needs the person’s own details</span>' : ""}</p>
    <p><b>${esc(t.question)}</b></p>
    ${t.items.length ? `<pre class="log" style="background:#f4f7fb;color:var(--ink)">${esc(t.items.join("\n"))}</pre>` : ""}
    ${Object.keys(t.fields || {}).length ? `<ul>${Object.entries(t.fields).map(([k, v]) => `<li><code>${esc(k)}</code>: ${esc(v)}</li>`).join("")}</ul>` : ""}
    <p class="muted">Format: ${esc(t.answer_format)} · Example: <code>${esc(t.example.replace(/\n/g, " ⏎ "))}</code></p>
    <textarea id="guidedAnswer" style="width:100%" placeholder="${esc(t.example)}"></textarea>
    <div class="row"><button class="btn" id="gAnswer">Answer</button>${t.type === "form" ? "" : '<button class="btn sec" id="gSkip">Skip</button>'}
      <button class="btn sec" id="gCopy">Copy prompt for any chatbot</button></div>`;
  $("#gAnswer").onclick = guard(() => guidedAnswer($("#guidedAnswer").value));
  if ($("#gSkip")) $("#gSkip").onclick = guard(() => guidedAnswer("skip"));
  $("#gCopy").onclick = () => navigator.clipboard.writeText(t.prompt).then(() => toast("Prompt copied – paste the chatbot’s reply in the box"));
}
window.guidedNext = guard(async () => {
  $("#guidedBox").innerHTML = '<p class="muted">Working… (JobFinder may be reading job boards)</p>';
  drawGuided(await api(`/api/guided/next${S.pid ? "?profile_id=" + encodeURIComponent(S.pid) : ""}`));
});
async function guidedAnswer(answer) {
  if (!answer.trim()) return toast("Type an answer first", true);
  $("#guidedBox").insertAdjacentHTML("afterbegin", '<p class="muted">Working…</p>');
  const r = await api(`/api/guided/${encodeURIComponent(S.guided.id)}/answer`, { method: "POST", body: { answer } });
  if (r.next.profile_id && r.next.profile_id !== S.pid) { S.pid = r.next.profile_id; localStorage.setItem("jf.pid", S.pid); loadProfiles(); }
  drawGuided(r.next, r.message, r.ok);
}
const summ = (r) => !r ? "" : Object.entries(r).filter(([, v]) => typeof v === "number").map(([k, v]) => `${k}=${v}`).join(" ") + (r.per_criteria ? " " + JSON.stringify(r.per_criteria) : "") + ["find_companies", "find_roles", "find_contacts"].filter((k) => r[k]).map((k) => ` ${k}: ${summ(r[k])}`).join("");
window.startRun = guard(async (kind) => {
  const body = { kind, use_llm: $("#run_llm").checked };
  if ($("#run_crit").value) body.criteria_ids = [$("#run_crit").value];
  if ($("#run_limit").value) body.limit = +$("#run_limit").value;
  if ($("#run_scope")) body.scope = $("#run_scope").value;
  const run = await api(`/api/profiles/${S.pid}/runs`, { method: "POST", body });
  toast("Started " + kind); await renderRuns(); watchRun(run.id);
});
window.watchRun = (id) => {
  clearInterval(S.runPoll);
  const tick = guard(async () => {
    const r = await api(`/api/runs/${id}`);
    if (!$("#runLog")) return clearInterval(S.runPoll);
    $("#runTitle").textContent = `${r.kind} · ${r.status} · ${JSON.stringify(r.progress?.budget || {})}`;
    const cp = r.checkpoint || {};
    const where = ["queued", "running"].includes(r.status) && cp.at ? `\n\nNOW: ${cp.at}` : "";
    const stop = cp.stopped_at ? `\n\nSTOPPED AT: ${cp.stopped_at}\nWHY: ${cp.reason || ""}\n${cp.resume_hint || ""}${cp.resumed_by ? "\n(resumed as run " + cp.resumed_by + ")" : ""}` : "";
    $("#runLog").textContent = r.log.join("\n") + where + (r.error ? "\n\nERROR: " + r.error : "") + stop + (r.status === "done" ? "\n\nRESULT: " + JSON.stringify(r.result, null, 1) : "");
    const canResume = cp.resumable && !cp.resumed_by && !["queued", "running", "done"].includes(r.status);
    $("#runResume").classList.toggle("hidden", !canResume); $("#runResume").onclick = guard(async () => { await resumeRun(id); });
    $("#runLog").scrollTop = 1e9;
    const live = ["queued", "running"].includes(r.status);
    $("#runCancel").classList.toggle("hidden", !live); $("#runCancel").onclick = guard(async () => { await api(`/api/runs/${id}/cancel`, { method: "POST" }); });
    if (!live) { clearInterval(S.runPoll); loadProfiles(); }
  });
  tick(); S.runPoll = setInterval(tick, 2000);
};

// ----------------------------------------------------------------------------- excel
async function renderExcel() {
  const el = $("#tab-excel");
  el.innerHTML = `<div class="card"><h2>Excel review workbook</h2>
    <p>Sheets: Summary, Companies, Roles, Contacts, Criteria, Runs. <span style="background:var(--edit)">Yellow columns</span> (stage, exclude, notes, contact status) can be edited and imported back.</p>
    <div class="row"><label class="inline"><input type="checkbox" id="xl_all"> all profiles</label>
      <button class="btn" id="xl_dl">Download .xlsx</button><button class="btn sec" id="xl_save">Save to Documents/JobFinder Exports</button></div>
    <h3>Import your edits</h3><div class="row"><input type="file" id="xl_file" accept=".xlsx"><button class="btn sec" id="xl_imp">Import</button></div>
    <div id="xl_res" class="muted"></div></div>`;
  const q = () => ($("#xl_all").checked || !S.pid ? "" : `?profile_id=${encodeURIComponent(S.pid)}`);
  $("#xl_dl").onclick = guard(async () => {
    const r = await api("/api/export/excel" + q(), { raw: true }); if (!r.ok) throw new Error(await r.text());
    const a = document.createElement("a"); a.href = URL.createObjectURL(await r.blob()); a.download = `JobFinder-${new Date().toISOString().slice(0, 10)}.xlsx`; a.click();
  });
  $("#xl_save").onclick = guard(async () => { const r = await api("/api/export/excel/save", { method: "POST", body: { profile_id: $("#xl_all").checked || !S.pid ? null : [S.pid] } }); $("#xl_res").textContent = "Saved: " + r.path; });
  $("#xl_imp").onclick = guard(async () => {
    const f = $("#xl_file").files[0]; if (!f) return toast("Choose the .xlsx", true);
    const fd = new FormData(); fd.append("file", f);
    const r = await api("/api/import/excel", { method: "POST", body: fd }); $("#xl_res").textContent = "Imported: " + JSON.stringify(r);
  });
}

// ----------------------------------------------------------------------------- settings
async function renderSettings() {
  const [conn, env, sys, raw, steps] = await Promise.all([api("/api/admin/connections"), api("/api/admin/env"), api("/api/admin/system"), api("/api/admin/connections/raw"), api("/api/admin/steps")]);
  const groups = { "AI models": ["OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_SEARCH_MODEL", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "GEMINI_API_KEY", "GEMINI_MODEL", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "PERPLEXITY_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"], "Local models": ["OLLAMA_BASE_URL", "LOCAL_GEMMA_MODEL", "LOCAL_QWEN_CODER_MODEL", "LOCAL_QWEN_FLASH_MODEL", "OLLAMA_MODEL", "LMSTUDIO_MODEL", "LMSTUDIO_BASE_URL"], "Web search": ["TAVILY_API_KEY", "BRAVE_API_KEY", "SERPAPI_API_KEY"], "Job sources": ["ADZUNA_APP_ID", "ADZUNA_APP_KEY", "RAPIDAPI_KEY", "JOOBLE_API_KEY"] };
  const envBy = Object.fromEntries(env.map((e) => [e.key, e]));
  $("#tab-settings").innerHTML = `<div class="grid2">
    <div class="card"><h2>1. Choose how AI runs</h2>
      <div class="row"><select id="preset">${Object.entries(steps.presets).map(([p, d]) => `<option value="${esc(p)}" ${p === steps.preset ? "selected" : ""}>${esc(p)} – ${esc(d)}</option>`).join("")}</select><button class="btn sec" id="applyPreset">Apply to all steps</button></div>
      <p class="muted">A preset fills in every step below; fine-tune each step afterwards. Model names are optional (e.g. ANTHROPIC_MODEL, LOCAL_GEMMA_MODEL).<br>${esc(sys.local_model_advice)}<br>
      Ollama: ${sys.ollama.running ? `<span class="ok">running</span> (${esc(sys.ollama.models.join(", ") || "no models – run: ollama pull qwen2.5:7b")})` : `<span class="muted">not detected (optional – ollama.com)</span>`} · RAM ${sys.ram_gb ?? "?"} GB</p>
      <h3>Models</h3><table><tr><th>Name</th><th>Provider</th><th>Model</th><th>Ready</th></tr>${Object.entries(conn.models).map(([n, m]) => `<tr><td>${esc(n)}</td><td>${esc(m.provider)}</td><td>${esc(m.model)}</td><td>${m.configured ? '<span class="ok">✔</span>' : `<span class="muted">${["ollama", "lmstudio"].includes(m.provider) ? "not running" : "needs key"}</span>`}</td></tr>`).join("")}</table>
      <div class="row"><button class="btn" id="testConn">Test connections</button></div><div id="testRes"></div></div>
    <div class="card"><h2>2. API keys</h2><p class="muted">Stored only on this computer in <code>${esc(conn.env_file)}</code>. Leave blank to keep the current value.</p>
      ${Object.entries(groups).map(([g, keys]) => `<h3>${g}</h3><div class="grid">${keys.map((k) => `<label>${k} ${envBy[k]?.set ? '<span class="ok">✔ set</span>' : ""}<input data-env="${k}" type="${envBy[k]?.secret ? "password" : "text"}" placeholder="${esc(envBy[k]?.secret ? (envBy[k].set ? envBy[k].value : "") : envBy[k]?.value || "")}" autocomplete="off"></label>`).join("")}</div>`).join("")}
      <div class="row"><button class="btn" id="saveEnv">Save keys</button></div>
      <p class="muted">Free tiers: Tavily (1,000 searches/mo), Adzuna (free key), Brave Search, SerpApi (100/mo). Google Jobs via SerpApi/JSearch includes LinkedIn, Indeed and Monster listings.</p></div></div>
    <div class="card"><h2>Which AI model does each step</h2>
      <p class="muted">Saved in <code>${esc(steps.file)}</code> (edit it by hand too – changes apply immediately). Each step tries the models left to right; the first one that is ready answers. <span class="ok">Green</span> = ready, grey = needs a key / not running / not pulled.</p>
      ${steps.error ? `<p class="bad">${esc(steps.error)}</p>` : ""}
      <datalist id="modelNames">${Object.keys(steps.models).map((m) => `<option value="${esc(m)}">`).join("")}</datalist>
      <table id="stepsTable"><tr><th>Step</th><th>What it does · best local fit</th><th>Models (in order)</th><th>Now answering</th><th></th></tr>
      ${Object.entries(steps.steps).map(([name, s]) => `<tr data-step="${esc(name)}"><td><b>${esc(name)}</b><br><label class="inline"><input type="checkbox" class="st_on" ${s.enabled ? "checked" : ""}> AI on</label></td>
        <td style="max-width:420px">${esc(s.what)}<br><span class="muted">${esc(s.local)}</span></td>
        <td><input class="st_models" list="modelNames" style="width:100%;min-width:260px" value="${esc(s.models.join(", "))}">
          <div>${s.models.map((m) => `<span class="pill ${steps.models[m] && steps.models[m].ready ? "ok" : ""}">${esc(m)}</span>`).join("")}</div></td>
        <td>${s.active ? `<span class="pill ok">${esc(s.active)}</span>` : '<span class="muted">none ready</span>'}</td>
        <td><button class="btn small sec" onclick="testStep(${js(name)})">Test</button><div class="muted" id="st_res_${esc(name)}"></div></td></tr>`).join("")}</table>
      <div class="row"><button class="btn" id="saveSteps">Save steps</button></div>
      <h3>Local models found on this computer</h3>
      ${["ollama", "lmstudio"].map((prov) => `<p><b>${prov === "ollama" ? "Ollama" : "LM Studio"}</b>: ${(steps.detected_local[prov] || []).length ? steps.detected_local[prov].map((m) => `<span class="pill">${esc(m)}</span> <button class="btn small sec" onclick="addLocalModel(${js(prov)}, ${js(m)})">Add</button>`).join(" ") : '<span class="muted">none detected (start the app and load/pull models)</span>'}</p>`).join("")}
      <p class="muted">Added models get a short name you can type in any step above. Your local model names (Gemma 4, Qwen 3.8 Coder Next, Qwen 3.8 Flash Next) are wired through LOCAL_GEMMA_MODEL / LOCAL_QWEN_CODER_MODEL / LOCAL_QWEN_FLASH_MODEL – set them to the exact names Ollama shows.</p>
      <details><summary>Edit steps.yaml directly</summary><textarea id="rawSteps" class="code">${esc(steps.text)}</textarea>
        <div class="row"><button class="btn" id="saveRawSteps">Validate &amp; save steps.yaml</button></div></details></div>
    <div class="card"><h2>Sources</h2><table><tr><th>Source</th><th>Enabled</th><th>Ready</th><th>Key needed</th></tr>${Object.entries(conn.sources).map(([n, s]) => `<tr><td>${esc(n)}</td><td>${s.enabled ? "yes" : "no"}</td><td>${s.configured ? '<span class="ok">✔</span>' : '<span class="muted">—</span>'}</td><td>${s.keyless ? "no" : "yes"}</td></tr>`).join("")}</table></div>
    <div class="card"><h2>Use JobFinder from Codex, Claude, Copilot, OpenCode, Cursor (MCP)</h2>
      <p>Run once in a terminal inside the JobFinder folder (or use the <b>Connect AI agents</b> launcher):</p>
      <pre class="log">jobfinder mcp-install codex
jobfinder mcp-install claude-desktop
jobfinder mcp-install vscode
jobfinder mcp-install opencode
jobfinder mcp-install cursor</pre>
      <p class="muted">Then ask your agent: “Use jobfinder: get the workplan for profile ${esc(S.pid || "&lt;id&gt;")}, find 20 more companies in my criteria and tell me how many per profile.”</p></div>
    <div class="card"><h2>connections.yaml</h2><p class="muted">${esc(raw.path)} – edits here or in any editor are picked up automatically. Invalid files are rejected and the last good config keeps running.</p>
      ${conn.error ? `<p class="bad">${esc(conn.error)}</p>` : ""}<textarea id="rawConn" class="code">${esc(raw.text)}</textarea>
      <div class="row"><button class="btn" id="saveRaw">Validate &amp; save</button></div></div>
    <div class="card"><h2>System</h2><pre class="log">${esc(JSON.stringify(sys, null, 2))}</pre></div>`;
  $("#applyPreset").onclick = guard(async () => { if (!confirm("Set every step from this preset? (steps.yaml.bak keeps the old file)")) return; await api("/api/admin/preset", { method: "POST", body: { preset: $("#preset").value } }); toast("Preset applied"); renderSettings(); });
  $("#saveSteps").onclick = guard(async () => {
    const body = {};
    $$("#stepsTable tr[data-step]").forEach((tr) => { body[tr.dataset.step] = { models: lines($(".st_models", tr).value), enabled: $(".st_on", tr).checked }; });
    await api("/api/admin/steps", { method: "PUT", body: { steps: body } }); toast("Steps saved"); renderSettings();
  });
  $("#saveRawSteps").onclick = guard(async () => { await api("/api/admin/steps/raw", { method: "PUT", body: { text: $("#rawSteps").value } }); toast("steps.yaml saved"); renderSettings(); });
  $("#saveEnv").onclick = guard(async () => {
    const values = {}; $$("[data-env]").forEach((i) => { if (i.value.trim()) values[i.dataset.env] = i.value.trim(); });
    if (!Object.keys(values).length) return toast("Nothing to save");
    await api("/api/admin/env", { method: "PUT", body: values }); toast("Keys saved"); renderSettings();
  });
  $("#saveRaw").onclick = guard(async () => { await api("/api/admin/connections/raw", { method: "PUT", body: { text: $("#rawConn").value } }); toast("connections.yaml saved"); renderSettings(); });
  $("#testConn").onclick = guard(async () => {
    $("#testRes").innerHTML = '<p class="muted">Testing… (each model gets a tiny prompt)</p>';
    const r = await api("/api/admin/connections/test", { method: "POST" });
    $("#testRes").innerHTML = `<table>${r.models.map((m) => `<tr><td>${esc(m.model)}</td><td>${m.ok ? `<span class="ok">ok ${m.ms} ms</span>` : `<span class="muted">${esc(m.error)}</span>`}</td></tr>`).join("")}
      ${Object.entries(r.search).map(([n, s]) => `<tr><td>search: ${esc(n)}</td><td>${s.ok ? '<span class="ok">ok</span>' : `<span class="bad">${esc(s.error)}</span>`}</td></tr>`).join("")}
      ${Object.entries(r.sources).map(([n, s]) => `<tr><td>source: ${esc(n)}</td><td>${s.ok ? '<span class="ok">ok</span>' : `<span class="bad">${esc(s.error || "failed")}</span>`}</td></tr>`).join("")}</table>`;
  });
}

async function stoppedRunsHtml() {
  const runs = await api(`/api/runs-resumable${S.pid ? "?profile_id=" + encodeURIComponent(S.pid) : ""}`);
  if (!runs.length) return "";
  return `<div class="card" style="border-color:#f3d19e;background:#fffaf0"><h2>⚠ ${runs.length} search${runs.length > 1 ? "es" : ""} stopped before finishing</h2>
    ${runs.map((r) => `<div class="row" style="align-items:flex-start;border-top:1px solid var(--line);padding-top:8px">
      <div style="flex:1"><b>${esc(r.kind)}</b> <span class="pill warn">${esc(r.status)}</span> <span class="muted">${esc((r.started || "").replace("T", " ").slice(0, 16))}</span><br>
        Stopped at: <b>${esc(r.stopped_at || "?")}</b><br><span class="muted">Why: ${esc(r.reason || "")}</span><br>
        <span class="muted">${esc(r.resume_hint || "")}${r.skipped_steps ? " Skipped steps: " + esc(Object.keys(r.skipped_steps).join(", ")) : ""}</span></div>
      <button class="btn" onclick="resumeRun(${js(r.run_id)})">Resume</button>
      <button class="btn sec" onclick="watchRunTab(${js(r.run_id)})">Details</button>
      <button class="btn sec small" onclick="dismissRun(${js(r.run_id)})">Dismiss</button></div>`).join("")}</div>`;
}
window.resumeRun = guard(async (id) => {
  const run = await api(`/api/runs/${encodeURIComponent(id)}/resume`, { method: "POST" });
  toast("Resumed – continuing where it stopped"); await show("runs"); watchRun(run.id);
});
window.dismissRun = guard(async (id) => { await api(`/api/runs/${encodeURIComponent(id)}/dismiss`, { method: "POST" }); toast("Dismissed"); show(localStorage.getItem("jf.tab") || "home"); });
window.watchRunTab = guard(async (id) => { await show("runs"); watchRun(id); });
window.testStep = guard(async (step) => {
  const el = document.getElementById("st_res_" + step); el.textContent = "testing…";
  const r = await api("/api/admin/steps/test", { method: "POST", body: { step } });
  el.innerHTML = r.ok ? `<span class="ok">${esc(r.model || "ok")} · ${r.ms} ms</span>` : `<span class="bad">${esc(r.error)}</span>`;
});
window.addLocalModel = guard(async (provider, model) => {
  const suggested = model.toLowerCase().replace(/[:.\-\/]+/g, "_").replace(/[^a-z0-9_]/g, "").replace(/_latest$/, "").slice(0, 40);
  const name = prompt(`Short name for ${model} (used in the steps table):`, suggested); if (!name) return;
  const spec = { provider, model, api_base: provider === "ollama" ? "${OLLAMA_BASE_URL:-http://localhost:11434}" : "${LMSTUDIO_BASE_URL:-http://localhost:1234/v1}", timeout_s: 300 };
  await api("/api/admin/steps", { method: "PUT", body: { steps: {}, add_models: { [name]: spec } } });
  toast(`Added ${name}. Type it into any step and Save.`); renderSettings();
});
const accessPill = (f) => !f ? "" : f.ok && !f.partial ? "" : `<span class="pill ${f.ok ? "warn" : "bad"}" title="${esc((f.what || "") + " " + (f.url || "") + (f.next_retry_at ? " · retry after " + f.next_retry_at.replace("T", " ").slice(0, 16) : ""))}">${f.ok ? "partly read" : "couldn't read: " + esc(f.error || "error")}${!f.ok && f.failures > 1 ? ` (${f.failures}×)` : ""}</span>`;
const RENDER = { home: renderHome, profile: renderProfile, criteria: renderCriteria, skills: renderSkills, pipeline: renderPipeline, runs: renderRuns, excel: renderExcel, settings: renderSettings };
(async () => {
  await guard(loadProfiles)();
  const [tab, arg] = (location.hash.slice(1) || localStorage.getItem("jf.tab") || "home").split(":");
  await show(tab);
  if (tab === "criteria" && arg !== undefined) setTimeout(() => editCriteria(arg || null), 300);
})();
