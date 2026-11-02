"""Command line: `jobfinder start` (dashboard + API), `jobfinder mcp` (agent tools), and maintenance commands."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path

import typer

from jobfinder import __version__
from jobfinder.settings import DEFAULT_PORT, get_workspace, set_workspace

app = typer.Typer(add_completion=False, no_args_is_help=True, help="JobFinder – AI-assisted job search.")
excel_app = typer.Typer(help="Excel review workbook.")
profile_app = typer.Typer(help="Profile bundles (profile + criteria + skills) for sharing/moving.")
app.add_typer(excel_app, name="excel")
app.add_typer(profile_app, name="profile")


@app.callback()
def _root(workspace: Path | None = typer.Option(None, "--workspace", "-w", envvar="JOBFINDER_HOME",  # noqa: UP007
                                                   help="Workspace folder (default: per-user app data).")) -> None:
    if workspace:
        set_workspace(workspace)


def _app():  # type: ignore[no-untyped-def]
    from jobfinder.core import App

    return App()


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) != 0


def _running_instance(host: str, port: int) -> bool:
    import httpx

    try:
        r = httpx.get(f"http://{host}:{port}/api/health", timeout=1.5)
        return r.status_code == 200 and r.json().get("app") == "jobfinder"
    except Exception:
        return False


@app.command()
def start(port: int = typer.Option(0, help="Port (default from connections.yaml, 8765)."),
          host: str = typer.Option("", help="Bind address (default 127.0.0.1)."),
          browser: bool = typer.Option(True, "--browser/--no-browser", help="Open the dashboard.")) -> None:
    """Start the API + dashboard (what the Start JobFinder launcher runs)."""
    import uvicorn

    from jobfinder.api import create_app
    from jobfinder.core import App

    a = App(watch_config=True)
    if os.environ.get("JOBFINDER_NO_BROWSER") == "1":
        browser = False
    host = host or a.conn.server.host or "127.0.0.1"
    port = port or a.conn.server.port or DEFAULT_PORT
    url_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    if _running_instance(url_host, port):
        typer.echo(f"JobFinder already running at http://{url_host}:{port}")
        if browser:
            webbrowser.open(f"http://{url_host}:{port}/")
        return
    original = port
    while not _port_free(url_host, port):
        port += 1
        if port > original + 20:
            typer.echo("No free port found", err=True)
            raise typer.Exit(1)
    url = f"http://{url_host}:{port}/"
    a.ws.server_file.write_text(json.dumps({"url": url, "port": port, "started": datetime.now().isoformat()}), encoding="utf-8")
    typer.echo("")
    typer.echo(f"  JobFinder {__version__} running at {url}")
    typer.echo(f"  Workspace: {a.ws.root}")
    typer.echo("  Keep this window open. Close it (or press Ctrl+C) to stop.")
    typer.echo("")
    if browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(a), host=host, port=port, log_level="warning")


@app.command()
def serve(port: int = 0, host: str = "") -> None:
    """Start the API without opening a browser."""
    start(port=port, host=host, browser=False)


@app.command()
def mcp(lite: bool = typer.Option(False, "--lite", help="Only the guided tools (best for small/local models).")) -> None:
    """Run the MCP server on stdio (agent clients launch this)."""
    from jobfinder.mcp.server import run_stdio

    run_stdio(lite=lite)


@app.command("mcp-install")
def mcp_install(client: str = typer.Argument(..., help="codex | claude-desktop | claude-code | vscode | opencode | cursor"),
                lite: bool = typer.Option(False, "--lite", help="Register the guided-only server for small models."),
                dry_run: bool = typer.Option(False, "--dry-run", help="Only print what would be written.")) -> None:
    """Register JobFinder as an MCP server in an agent client's config."""
    from jobfinder.mcp.install import install

    res = install(client, dry_run=dry_run, lite=lite)
    if dry_run:
        typer.echo(f"# {res['path']}\n{res['would_write']}")
    else:
        typer.echo(f"Registered JobFinder in {res['path']} (backup saved as .bak). Restart {client}.")


@app.command("agent-info")
def agent_info() -> None:
    """Everything an AI agent needs to connect, as JSON: MCP command, HTTP API URL + token, what is configured."""
    import json as _json

    from jobfinder.llm import is_configured
    from jobfinder.mcp.install import CLIENTS, config_path, server_command
    from jobfinder.services import websearch

    a = _app()
    cmd, args, env = server_command()
    _, lite_args, _ = server_command(lite=True)
    server = {}
    if a.ws.server_file.exists():
        try:
            server = _json.loads(a.ws.server_file.read_text(encoding="utf-8"))
        except ValueError:
            server = {}
    url = (server.get("url") or f"http://127.0.0.1:{a.conn.server.port}/").rstrip("/")
    typer.echo(_json.dumps({
        "version": __version__,
        "mcp": {"command": cmd, "args": args, "env": env, "lite_args": lite_args,
                "install": {c: f'"{cmd}" -m jobfinder mcp-install {c}' for c in CLIENTS},
                "config_files": {c: str(config_path(c)) for c in CLIENTS},
                "note": "MCP clients load servers at startup: restart the client after installing."},
        "http": {"base_url": url, "running": _running_instance(url.split("//")[1].split(":")[0],
                                                               int(url.rsplit(":", 1)[1])),
                 "start": f'"{cmd}" -m jobfinder serve', "token_header": "X-JobFinder-Token",
                 "token": a.ws.api_token(), "docs": url + "/docs", "openapi": url + "/openapi.json"},
        "workspace": str(a.ws.root),
        "config_files": {"connections": str(a.ws.connections_file), "steps": str(a.ws.steps_file),
                         "env": str(a.ws.env_file)},
        "ready": {"ai_models": [n for n, m in a.conn.llm.models.items() if is_configured(m)],
                  "web_search": websearch.available(a.conn),
                  "profiles": [p.id for p in a.store.list_profiles()],
                  "runs_to_resume": [r.id for r in a.runner.resumable()]},
    }, indent=2))


@app.command()
def doctor() -> None:
    """Check workspace, configuration, models, search and network."""
    from jobfinder.llm import is_configured
    from jobfinder.services import websearch

    a = _app()
    ok = typer.style("ok", fg="green")
    warn = typer.style("!!", fg="yellow")
    typer.echo(f"JobFinder {__version__} | Python {sys.version.split()[0]} | {sys.executable}")
    typer.echo(f"workspace   {a.ws.root}")
    typer.echo(f"config      {a.ws.connections_file} [{ok if not a.config.last_error else warn + ' ' + a.config.last_error}]")
    typer.echo(f"database    {a.ws.db_path} (schema v{a.store.schema_version()})")
    typer.echo(f"exports     {a.exports_dir()}")
    models = [n for n, m in a.conn.llm.models.items() if is_configured(m)]
    typer.echo(f"models      {', '.join(models) or warn + ' none configured (add a key in Settings or run Ollama)'}")
    typer.echo(f"web search  {', '.join(websearch.available(a.conn)) or warn + ' none (company/contact discovery limited)'}")
    from jobfinder.sources import SOURCES

    srcs = [n for n, s in SOURCES.items() if s.configured(a.conn.sources.get(n).model_dump() if a.conn.sources.get(n) else None)]
    typer.echo(f"sources     {', '.join(srcs)}")
    http = a.http()
    try:
        r = http.get("https://boards-api.greenhouse.io/v1/boards/gitlab", use_cache=False)
        typer.echo(f"internet    {ok if r.status_code == 200 else warn} (HTTP {r.status_code})")
    except Exception as e:
        typer.echo(f"internet    {warn} {type(e).__name__}: {e}")
    finally:
        http.close()


@app.command()
def run(kind: str = typer.Argument(..., help="find_roles | find_companies | find_contacts | review_roles | recheck | "
                                              "autopilot | guided"),
        profile: str = typer.Option(..., "--profile", "-p"),
        criteria: list[str] = typer.Option([], "--criteria", "-c"),
        company: list[str] = typer.Option([], "--company"),
        scope: str = typer.Option("new", help="find_roles companies: new | all | failed"),
        limit: int | None = typer.Option(None),  # noqa: UP007
        no_llm: bool = typer.Option(False, "--no-llm")) -> None:
    """Run a discovery job in the foreground and print the result."""
    from jobfinder.models import RunRequest

    a = _app()
    req = RunRequest(kind=kind, criteria_ids=criteria, company_ids=company, limit=limit, use_llm=not no_llm,  # type: ignore[arg-type]
                     scope=scope)  # type: ignore[arg-type]
    r = a.runner.run_sync(profile, req)
    for line in r.log[-40:]:
        typer.echo(line)
    typer.echo(json.dumps({"status": r.status, "error": r.error, "result": r.result}, indent=2, default=str)[:6000])
    typer.echo(json.dumps(a.store.stats(profile), indent=2))


@app.command()
def schemas(out: Path = typer.Option(Path("schemas"), help="Output folder")) -> None:
    """Export JSON Schemas of every contract."""
    from jobfinder.models import SCHEMA_EXPORTS

    out.mkdir(parents=True, exist_ok=True)
    for name, model in SCHEMA_EXPORTS.items():
        (out / f"{name}.schema.json").write_text(json.dumps(model.model_json_schema(by_alias=True), indent=2) + "\n",
                                                 encoding="utf-8")
    typer.echo(f"wrote {len(SCHEMA_EXPORTS)} schemas to {out.resolve()}")


@excel_app.command("export")
def excel_export(profile: list[str] = typer.Option([], "--profile", "-p"),
                 out: Path | None = typer.Option(None)) -> None:  # noqa: UP007
    from jobfinder.services import excel

    typer.echo(str(excel.export(_app(), profile or None, out)))


@excel_app.command("import")
def excel_import(file: Path) -> None:
    from jobfinder.services import excel

    typer.echo(json.dumps(excel.import_workbook(_app(), file), indent=2))


@profile_app.command("export")
def profile_export(profile_id: str, out: Path | None = typer.Option(None)) -> None:
    """Write profile + criteria + skills as one JSON bundle."""
    from jobfinder.services.profiles import export_bundle

    out = out or Path(f"{profile_id}.profile.json")
    out.write_text(json.dumps(export_bundle(_app(), profile_id), indent=2, ensure_ascii=False), encoding="utf-8")
    typer.echo(str(out.resolve()))


@profile_app.command("import")
def profile_import(file: Path, new_id: str | None = typer.Option(None, "--id")) -> None:
    """Load a profile bundle (see examples/)."""
    from jobfinder.services.profiles import import_bundle

    typer.echo(json.dumps(import_bundle(_app(), json.loads(file.read_text(encoding="utf-8")), new_id), indent=2))


@app.command()
def backup(out: Path | None = typer.Option(None, help="Zip file to create")) -> None:  # noqa: UP007
    """Zip the workspace (config, .env, database) to move it to another computer."""
    import sqlite3

    ws = get_workspace().ensure()
    out = out or Path.home() / f"JobFinder-backup-{datetime.now().strftime('%Y%m%d-%H%M')}.zip"
    snap = ws.state_dir / "backup.db"
    src = sqlite3.connect(ws.db_path)
    dst = sqlite3.connect(snap)
    src.backup(dst)
    dst.close()
    src.close()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(ws.connections_file, "config/connections.yaml")
        if ws.env_file.exists():
            z.write(ws.env_file, ".env")
        z.write(snap, "data/jobfinder.db")
    snap.unlink(missing_ok=True)
    typer.echo(f"{out}\nContains your API keys (.env): keep it private.")


@app.command()
def restore(zip_file: Path, force: bool = typer.Option(False, "--force")) -> None:
    """Restore a backup zip into the workspace (stop the app first)."""
    ws = get_workspace()
    if ws.db_path.exists() and not force:
        raise typer.BadParameter("workspace already has data; use --force to overwrite (a copy is kept)")
    if ws.db_path.exists():
        ws.db_path.rename(ws.db_path.with_suffix(f".db.{int(time.time())}.old"))
        for ext in ("-wal", "-shm"):
            Path(str(ws.db_path) + ext).unlink(missing_ok=True)
    with zipfile.ZipFile(zip_file) as z:
        for name in z.namelist():
            if name not in ("config/connections.yaml", ".env", "data/jobfinder.db"):
                continue
            target = ws.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(name))
    typer.echo(f"restored into {ws.root}")


@app.command()
def paths() -> None:
    """Show where things live."""
    ws = get_workspace().ensure()
    for k in ("root", "connections_file", "env_file", "db_path", "logs_dir", "token_file"):
        typer.echo(f"{k:17} {getattr(ws, k)}")


@app.command()
def token() -> None:
    """Print the local API token (for scripts calling the HTTP API)."""
    typer.echo(get_workspace().ensure().api_token())


@app.command()
def version() -> None:
    typer.echo(__version__)


if __name__ == "__main__":
    app()
