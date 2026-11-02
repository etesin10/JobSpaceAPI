# JobFinder

AI-assisted job search that runs on any Windows, Mac or Linux computer with one double-click.
It keeps **profiles → criteria + skills → companies → roles → HR contacts** as JSON documents, finds
open positions through legal sources (company job boards, job APIs, Google Jobs), scores every role
against your criteria, and feeds everything into an **Excel workbook** for review. Any AI – OpenAI,
DeepSeek, Azure, Claude, Gemini, or a free local model (Ollama / LM Studio) – can power it, and agent apps
(Codex, Claude, Copilot, OpenCode, Cursor) can drive it through MCP.

---

## 1. Start it (one click)

| Computer | Do this |
|---|---|
| **Windows** | Double-click **`Start JobFinder.cmd`** |
| **Mac** | Double-click **`Start JobFinder.command`**. The first time, macOS may block it: right-click → **Open** → **Open**. |
| **Linux** | `./start.sh` |

The first run takes 1–3 minutes: it installs [uv](https://docs.astral.sh/uv/) for your user (no admin
rights), which downloads Python 3.12 and the exact library versions in `uv.lock`. Then the dashboard opens
at <http://127.0.0.1:8765>. Keep the black window open while you use it; close it to stop.

Nothing is installed system-wide. Everything lives in one per-user folder:

| | Windows | Mac | Linux |
|---|---|---|---|
| App data | `%LOCALAPPDATA%\JobFinder` | `~/Library/Application Support/JobFinder` | `~/.local/share/JobFinder` |

Inside it: `workspace/` (your config, `.env` keys, database, logs), `venv/` (Python + libraries), `bin/uv`.
The workspace deliberately stays **out of OneDrive/iCloud**, which corrupt live databases.
Excel exports go to `Documents/JobFinder Exports`.

Other launchers: **`Update JobFinder`** (pulls the latest version and reinstalls libraries) and
**`Connect AI agents`** (registers JobFinder in Codex / Claude / VS Code / OpenCode / Cursor).

## 2. First-time setup (dashboard)

1. **AI & Settings** → pick a preset (`cloud-openai`, `cloud-claude`, `cloud-gemini`, `cloud-deepseek`, `cloud-azure`, `local`, `hybrid`) and paste the keys you have.
   Any single provider is enough: the preset only sets which one is tried first, and every other configured provider (or a running local model) is a fallback. Model names can be changed with `OPENAI_MODEL`, `ANTHROPIC_MODEL`, `GEMINI_MODEL`, `DEEPSEEK_MODEL`, `OLLAMA_MODEL`.
   - No AI key? Everything still works with deterministic matching; AI adds smarter queries, resume parsing and company discovery.
   - Free/cheap keys: **Tavily** (web search, 1,000/mo free), **Adzuna** (job API, free), **SerpApi** (Google Jobs – includes LinkedIn/Indeed/Monster listings, 100/mo free).
   - Local AI: install [Ollama](https://ollama.com), run `ollama pull qwen2.5:7b`, choose the `local` or `hybrid` preset.
2. **Home** → create a profile (or import one of the bundles in `examples/`).
3. **Skills** → upload a resume (PDF/DOCX/TXT) or a skills CSV (`skill,level,years,category`).
4. **Criteria** → one criteria per role family. Start from an occupation template.
5. **Search runs** → **Autopilot** (companies → roles → contacts) or run each step.
6. **Companies & Roles** → set stage (applied, interview…), exclude, notes, open a **Brief** for resume tailoring.
7. **Excel** → download the workbook; edit the yellow columns; import it back.

## 3. How it is organised

```
Profile ─┬─ Criteria (1..n)   user-owned constraints/targets/rules + AI-owned search_strategy (versioned)
         ├─ Skills            from resume / CSV / manual, normalised to catalog ids
         └─ Pipeline (per profile, private)
              ├─ Company link   status, excluded, notes, criteria_ids ─┐
              ├─ Role link      score, match, stage, notes            ├─ point into the shared catalog
              └─ Contact link   status, notes, role                   ┘
Catalog (shared facts, deduplicated): companies (by domain), roles (by posting URL / source id), contacts (by profile URL)
```

**Why a shared catalog but private pipelines?** A Mexico profile only ever *sees* companies and roles linked to
it, so results never blur. Filtering happens on the **role's location**, not the company's headquarters (a
Canadian company hiring in Monterrey is valid for Mexico; its Toronto roles are not). Facts are stored once,
so a company researched for one profile isn't paid for twice. Deleting a profile removes its pipeline and any
contact no other profile uses.

### Criteria contract (no free text where it matters)

| Field | Standard |
|---|---|
| Country / region | ISO 3166-1 alpha-2 / ISO 3166-2 (`MX`, `MX-NLE`, `CA-ON`) – validated |
| Currency | ISO 4217 (`MXN`, `CAD`) + period (`hour`…`year`), compared annualised |
| Languages | ISO 639-1 + CEFR (`en:B2`) |
| Occupations | ISCO-08 / NOC-2021 / SOC-2018 codes → multilingual titles (`data/occupations.yaml`) |
| Skills | Catalog ids with English/Spanish/French aliases (`data/skills_catalog.yaml`), `custom:<slug>` otherwise |
| Sponsorship | `CA_LMIA`, `US_H1B`, `US_TN`, `UK_SKILLED_WORKER`, `MX_WORK_VISA`, … |
| Remote | `remote_eligibility`: countries or blocs `LATAM`, `NA`, `EU`, `EMEA`, `APAC`, `GLOBAL` |

`rules` is a small boolean DSL: `must` (all required), `should` (weighted score), `must_not` (any rejects).
Operators: `eq ne in nin any all none contains not_contains prefix regex gte lte between exists within_km`.
Score = weighted mix of skills coverage, should-rules and title similarity – every score comes with
`matched_skills`, `missing_skills`, `failed` and `warnings`, so it is explainable.
JSON Schemas for every contract are in [`schemas/`](schemas) and at `GET /api/schemas/{name}`.

### The workplan (template the AI loops over)

`GET /api/profiles/{id}/workplan` returns criteria, skills, companies → roles → contacts and a prioritised
`next_actions` list (`find_companies`, `find_roles`, `enrich_role`, `find_contacts`, `tune_strategy`) with a budget.
Any AI (or you) sends results back with `POST` in the same shape – see
[`examples/workplan_submission.example.json`](examples/workplan_submission.example.json). Items merge by natural
key, so re-submitting is safe. New items need an **evidence URL that the server checks**; AI-invented companies,
postings and emails are rejected. Hand edits (exclude, notes, stage) go through the same endpoint or Excel.

## 4. Sources

| Kind | Sources | Key |
|---|---|---|
| Company job boards (employer-published) | Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Workday, schema.org JobPosting on career sites | none |
| Job APIs | Adzuna, Jooble, Remotive, Arbeitnow | Adzuna/Jooble free keys |
| Aggregators (LinkedIn, Indeed, Monster listings via Google Jobs) | SerpApi Google Jobs, JSearch (RapidAPI) | paid/free tier |
| Web search (company & contact discovery) | Tavily, Brave, SerpApi | one of them |

JobFinder does **not** scrape LinkedIn, Indeed or Monster directly (their terms forbid it). Career sites are
fetched politely: robots.txt respected, 1 request/second per site, 12 h cache.
Contacts are limited to public business information (name, title, public profile URL, published business
email/phone) with the URL where each was found; nothing is ever sent automatically.

### When a site can't be read

Loops over companies never stop on one failure. Each attempt is recorded:

- **Company** (`fetch` on the company, shown as a red badge, an *access* column in Excel): HTTP status, reason
  (`HTTP 403 (access blocked)`, `timed out`, `blocked by robots.txt`, board not found …), attempt count and the
  next automatic retry (30 min, 1 h, 2 h … up to 24 h). The company's status becomes `error`.
- **Posting** (`fetch` on the role): a posting that can't be opened keeps its place in the pipeline with the reason;
  404/410 closes it. Partial reads (a later page failed) are marked *partly read* and never close roles.
- **Search runs → Companies**: `new` (default: new companies + failed ones whose retry time has come), `all`
  (re-read every board in the roster) or `failed` (retry only the ones that failed). CLI: `--scope`, MCP: `scope`.
- The workplan tells agents exactly what couldn't be read, so ChatGPT/Claude can open those pages themselves.

### When a whole run stops: checkpoints and resume

Every run saves its progress as it goes (which searches, companies, postings, contacts are finished, and what it is
working on now). If it stops before the end, nothing is lost and it is marked **resumable** with *where* and *why*:

| Why it stopped | Status |
|---|---|
| Budget used up (pages, searches, AI tokens, minutes) | `interrupted` |
| No internet (checked against several sites before blaming any company) | `interrupted` – no company is marked failed |
| No AI model answered (key missing, Ollama stopped, provider down) | `interrupted` |
| You pressed Cancel | `cancelled` |
| The app was closed, crashed or the computer slept | `interrupted` (detected on next start) |
| An unexpected error | `failed` |

Where you see it, and how to continue:

- **Dashboard** (Home and Search runs): a "search stopped before finishing" card → **Resume**, Details, Dismiss.
- **AI agents**: `get_workplan` lists `runs_to_resume` and puts a `resume_run` action first; MCP tools
  `list_runs_to_resume` and `resume_run(run_id)`. Small models in guided mode are asked "RESUME or SKIP".
- **HTTP**: `GET /api/runs-resumable`, `POST /api/runs/{id}/resume`, `POST /api/runs/{id}/dismiss`.

A resume starts a new run with the same settings that skips everything already finished (the item that was in
progress is redone). In autopilot, finished steps are skipped and a step that was skipped (e.g. no search key yet) is
tried again.

## 5. Plug in any AI

### A different model for each step (`steps.yaml`)

`workspace/config/steps.yaml` says which model does each step. Edit it by hand (changes apply instantly; an invalid
file is rejected and the last good one keeps running) or in **AI & Settings → Which AI model does each step**, where
you can also apply a preset, test a step, and add any model Ollama / LM Studio is serving with one click.
Each step lists models in order; the first one that is *ready* answers (cloud: key set; local: server running **and**
model pulled), the rest are fallbacks. Per step you can also set `enabled: false` (no AI, built-in rules only),
`temperature`, `max_tokens`, `timeout_s`.

```yaml
models:                       # local models (add as many as you like)
  gemma:      {provider: ollama, model: "${LOCAL_GEMMA_MODEL:-gemma4}"}
  qwen_coder: {provider: ollama, model: "${LOCAL_QWEN_CODER_MODEL:-qwen3.8-coder-next}"}
  qwen_flash: {provider: ollama, model: "${LOCAL_QWEN_FLASH_MODEL:-qwen3.8-flash-next}"}
steps:
  extract_skills: {models: [gemma, qwen_flash, openai_mini], temperature: 0.0}
  review_role:    {models: [qwen_coder, gemma, claude]}
  find_companies: {models: [openai_mini, qwen_coder]}
  web_search:     {models: [openai_search, gemini_search, perplexity]}
  guided:         {models: [qwen_flash]}
```

| Step | What it does | Suggested local model |
|---|---|---|
| `extract_skills` | read a resume and list skills | **Gemma 4** (multilingual reading) · Qwen Flash fallback |
| `plan_queries` | write searches in the local language | **Qwen Flash** or Gemma 4 |
| `find_companies` | pick employers from search results / name employers | **Qwen Coder**; a cloud model when naming from memory |
| `extract_jobs` | read a careers page without structured data | **Qwen Coder** (messy page → clean JSON) |
| `review_role` | fit, required skills, gaps, summary for the resume | **Qwen Coder** or Gemma 4; cloud for the most nuance |
| `find_contacts` | keep recruiters / HR from profile results | **Qwen Flash** |
| `guided` | answer guided-mode questions | **Qwen Flash** |
| `web_search` | models that search the web themselves | none – local models can't browse: `openai_search`, `gemini_search` (Google grounding) or `perplexity` |

Presets: `auto` (cloud first), `hybrid` (local reads/reviews, cloud discovers and searches), `local-first`,
`local-only`, `cloud-openai/claude/gemini/deepseek/azure`. Put the exact names from `ollama list` in
`LOCAL_GEMMA_MODEL`, `LOCAL_QWEN_CODER_MODEL`, `LOCAL_QWEN_FLASH_MODEL` (AI & Settings → keys).

**Cloud AI as web search:** add `web_search` models and an OpenAI / Gemini / Perplexity key; the `ai` provider is then
used like Tavily/Brave/SerpApi. Only URLs the provider actually cites are kept.

### Keys and providers (`connections.yaml`)

Edit `connections.yaml` (Settings → connections.yaml, or any editor; changes apply
instantly, invalid files are rejected and the last good config keeps running) to define cloud models and sources:

```yaml
llm:
  models:
    local:    {provider: ollama,   model: qwen2.5:7b}
    deepseek: {provider: deepseek, model: deepseek-chat, api_key: "${DEEPSEEK_API_KEY}"}
    groq:     {provider: openai_compatible, model: llama-3.3-70b, api_base: "https://api.groq.com/openai/v1", api_key: "${GROQ_KEY}"}
  routes:
    extract_skills: [local, deepseek]
    find_companies: [deepseek, local]
```

**From agent apps (MCP)** – run `Connect AI agents` or `jobfinder mcp-install codex|claude-desktop|claude-code|vscode|opencode|cursor`.
Tools (23): setup – `create_profile`, `update_profile`, `delete_profile`, `import_resume`, `set_skills`,
`occupation_templates`, `save_criteria`, `delete_criteria`, `add_company`; search loop – `list_profiles`,
`get_workplan`, `submit_workplan`, `get_criteria`, `update_search_strategy`, `start_run`, `get_run`, `web_search`,
`fetch_page`; results – `get_stats`, `role_brief`, `update_pipeline`, `export_excel`, `get_schema`.

#### Example: OpenCode with ChatGPT or Claude

1. Install [OpenCode](https://opencode.ai) and sign in with your model account or API key (`opencode auth login`).
   Those credentials stay in OpenCode – JobFinder never sees them and needs no AI key of its own in this mode.
2. Double-click **`Connect AI agents`** and pick `opencode` (writes `~/.config/opencode/opencode.json`, keeps your
   other settings, backs up the old file, and raises the tool timeout to 10 minutes for long searches).
3. Start OpenCode and ask, for example:
   > Use the jobfinder tools. Create a profile for María López in Monterrey (MX-NLE), Spanish native, English C1.
   > Import my resume from C:\Users\me\Documents\cv.pdf. Create criteria for data engineer roles in Monterrey,
   > hybrid or onsite, at least 55,000 MXN/month. Then find 20 companies, their matching roles and recruiters,
   > and tell me how many companies, roles and contacts the profile has. Export the Excel.

   The agent then creates the profile, criteria and skills, discovers companies with its own web search (or
   JobFinder's `web_search` if you added a Tavily/Brave/SerpApi key), reads job boards with `start_run`, adds
   recruiters it finds, and reports counts. Everything new needs a real, reachable source URL – invented companies,
   postings and emails are rejected.

What depends on which side:

| Step | Agent (OpenCode + ChatGPT/Claude) | JobFinder keys needed |
|---|---|---|
| Profile, criteria, skills, resume | ✔ via tools | none |
| Find companies | agent's own web search, **or** `start_run find_companies` | none if the agent searches; otherwise a web-search key |
| Find roles on company job boards | `start_run find_roles` | none (Greenhouse, Lever, Ashby, Workday… are keyless) |
| Find roles via job APIs / Google Jobs | `start_run find_roles` | Adzuna / SerpApi / JSearch keys |
| Find HR contacts | agent's own web search, **or** `start_run find_contacts` | none if the agent searches; otherwise a web-search key |
| Counts, briefs, Excel | ✔ via tools | none |

JobFinder (and therefore the dashboard) must be on the same computer as OpenCode; the agent starts the MCP server
itself, so the dashboard doesn't need to be open. If OpenCode can't search the web with your model, add a Tavily key
in JobFinder (free tier) so the agent can use the `web_search` tool.

**Any script** – the HTTP API is documented at <http://127.0.0.1:8765/docs>. Send the token from
`jobfinder token` as header `X-JobFinder-Token`.

## 6. Moving and sharing

- **Same person, new computer**: `jobfinder backup` → copy the zip → `jobfinder restore file.zip` (contains your `.env` keys – keep it private).
- **Different person**: give them the folder (or zip). Each computer has its own workspace and keys.
  Share profiles as bundles (Home → *Download bundle* / *Import profile bundle*).
- **Separate searches on one computer**: set `JOBFINDER_HOME` (or `jobfinder -w <folder> start`) to use another workspace.

## 7. Command line

```
jobfinder start | serve | doctor | paths | token | version
jobfinder run find_roles -p <profile> [--criteria <id>] [--limit N]
jobfinder excel export [-p <profile>] | excel import <file.xlsx>
jobfinder profile export <id> | profile import <bundle.json>
jobfinder mcp | mcp-install <client> [--dry-run]
jobfinder schemas --out schemas | backup | restore <zip>
```
(Inside the app folder: `uv run jobfinder …`, or the venv's `python -m jobfinder …`.)

## 8. Development

```
uv sync                 # creates .venv with dev tools
uv run pytest -q
uv run ruff check src tests
uv run jobfinder schemas --out schemas   # after changing models
```

Layout: `src/jobfinder/{models,services,sources,llm,mcp,api,web}`, data files in `src/jobfinder/data`,
launchers in the root, bootstrap scripts in `scripts/`.

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| Window closes / error on first start | See `bootstrap.log` in the app data folder. Usually no internet or a proxy. |
| Corporate network / TLS inspection | Handled: uv and the app use the OS certificate store. |
| Slow connections on some networks | Handled: broken IPv6 is detected and IPv4 is preferred. |
| “Excel file is open” | Export writes a timestamped copy instead. |
| Mac: “cannot be opened because it is from an unidentified developer” | Right-click → Open, once. |
| Port 8765 busy | The next free port is used automatically. |
| Logs | `workspace/logs/jobfinder.log`; each run's log is in **Search runs**. |
