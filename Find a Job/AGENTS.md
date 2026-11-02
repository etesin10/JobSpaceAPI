# JobFinder – instructions for AI agents

You are in the JobFinder project: a local job-search app (profiles → criteria + skills → companies → roles → HR
contacts) with an HTTP API, an MCP server and a dashboard. These steps let you (Codex, OpenCode, Claude Code, Cursor,
Copilot, …) install it, connect to it and run the whole workflow for the user.

## 1. Install (once per computer, no admin rights)

Run from this folder. It installs uv + Python 3.12 + locked dependencies into the user's app-data folder and checks
the setup. Takes 1–3 minutes the first time.

| OS | Command |
|---|---|
| Windows | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/bootstrap.ps1 -Action doctor` |
| macOS / Linux | `bash scripts/bootstrap.sh doctor` |

The app's Python is then at:

- Windows: `%LOCALAPPDATA%\JobFinder\venv\Scripts\python.exe`
- macOS: `~/Library/Application Support/JobFinder/venv/bin/python`
- Linux: `~/.local/share/JobFinder/venv/bin/python`

Call it `JF_PY` below.

## 2. Find out how to connect

```
JF_PY -m jobfinder agent-info
```

It prints JSON with the MCP command, the exact install command for each agent app, the HTTP API URL and token, the
config file paths and what is configured (AI models, web search, profiles, runs waiting to be resumed).

## 3. Connect – choose one

**A. MCP (recommended).** Register JobFinder in your own client:

```
JF_PY -m jobfinder mcp-install codex          # or: opencode | claude-code | claude-desktop | vscode | cursor
JF_PY -m jobfinder mcp-install opencode --lite  # small/local model: only the guided one-question tools
```

It merges into the client's config (backup kept as `.bak`). **MCP clients load servers at startup**: tell the user to
restart the client (or start a new session), then the `jobfinder` tools appear. Claude Code alternative:
`claude mcp add jobfinder -- JF_PY -m jobfinder mcp`.

**B. HTTP API (works right now, no restart).** Start the server in the background and call it with the token:

```
JF_PY -m jobfinder serve            # http://127.0.0.1:8765, docs at /docs, schema at /openapi.json
curl -H "X-JobFinder-Token: <token from agent-info>" http://127.0.0.1:8765/api/profiles
```

**C. CLI only.** `JF_PY -m jobfinder run autopilot -p <profile_id>`, `… excel export`, `… profile import <bundle>`.

## 4. Do the workflow

Never invent personal details: ask the user for name, location, target jobs, salary, visa needs and their resume.

| Step | MCP tool | HTTP |
|---|---|---|
| Create profile | `create_profile` | `POST /api/profiles` |
| Skills from resume / list | `import_resume`, `set_skills` | `POST /api/profiles/{id}/skills/upload`, `PUT …/skills` |
| Criteria (ISO codes: `MX`, `MX-NLE`, `CAD`) | `save_criteria` (returns errors + planned queries) | `POST /api/profiles/{id}/criteria` |
| See what to do next | `get_workplan` → `next_actions`, `runs_to_resume` | `GET /api/profiles/{id}/workplan` |
| Let JobFinder search | `start_run` (find_companies, find_roles, review_roles, find_contacts, autopilot) + `get_run` | `POST /api/profiles/{id}/runs`, `GET /api/runs/{id}` |
| Add what you found yourself | `submit_workplan` (every new item needs a real evidence URL) | `POST /api/profiles/{id}/workplan` |
| Resume a stopped run | `list_runs_to_resume`, `resume_run` | `GET /api/runs-resumable`, `POST /api/runs/{id}/resume` |
| Counts / resume brief / Excel | `get_stats`, `role_brief`, `export_excel` | `GET …/stats`, `GET …/roles/{rid}/brief`, `GET /api/export/excel` |
| Small model: one question at a time | `next_task`, `answer_task` | `GET /api/guided/next`, `POST /api/guided/{id}/answer` |

Runs take minutes: poll `get_run` every ~20 s. Failures on single companies/postings are recorded and the loop goes on;
if a whole run stops it is saved with where/why and appears in `runs_to_resume`.

## 5. Keys and models

JobFinder works without keys (company job boards are keyless). For more, the user adds keys in the dashboard
(AI & Settings) or in the `.env` file listed by `agent-info`; never write keys into this repository.

- Which model does each step: `steps.yaml` (path in `agent-info`), hot-reloaded. Validate edits by reading
  `GET /api/admin/steps` (`error` field) after saving.
- Useful keys: an AI model (OpenAI / Anthropic / Gemini / DeepSeek, or a local Ollama model), a web search key
  (Tavily free tier), Adzuna (free job API).

## Development (changing the code)

```
uv sync && uv run pytest -q && uv run ruff check src tests
uv run jobfinder schemas --out schemas   # after changing models in src/jobfinder/models
```
