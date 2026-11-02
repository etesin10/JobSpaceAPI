"""Register the JobFinder MCP server with agent clients (Codex, Claude Desktop/Code, VS Code, OpenCode, Cursor)."""

from __future__ import annotations

import json
import os
import platform
import re
import sys
from pathlib import Path
from typing import Any

CLIENTS = ("codex", "claude-desktop", "claude-code", "vscode", "opencode", "cursor")


def server_command(lite: bool = False) -> tuple[str, list[str], dict[str, str]]:
    """The venv's python runs the server directly: no uv / PATH needed when the client launches it."""
    env = {}
    if os.environ.get("JOBFINDER_HOME"):
        env["JOBFINDER_HOME"] = os.environ["JOBFINDER_HOME"]
    return sys.executable, ["-m", "jobfinder", "mcp", *(["--lite"] if lite else [])], env


def _home() -> Path:
    return Path.home()


def config_path(client: str) -> Path:
    sysname = platform.system()
    appdata = Path(os.environ.get("APPDATA", _home() / "AppData" / "Roaming"))
    if client == "codex":
        return Path(os.environ.get("CODEX_HOME", _home() / ".codex")) / "config.toml"
    if client == "claude-desktop":
        if sysname == "Windows":
            return appdata / "Claude" / "claude_desktop_config.json"
        if sysname == "Darwin":
            return _home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        return _home() / ".config" / "Claude" / "claude_desktop_config.json"
    if client == "claude-code":
        return _home() / ".claude.json"
    if client == "vscode":
        if sysname == "Windows":
            return appdata / "Code" / "User" / "mcp.json"
        if sysname == "Darwin":
            return _home() / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
        return _home() / ".config" / "Code" / "User" / "mcp.json"
    if client == "opencode":
        return _home() / ".config" / "opencode" / "opencode.json"
    if client == "cursor":
        return _home() / ".cursor" / "mcp.json"
    raise ValueError(f"unknown client {client}; choose {CLIENTS}")


def snippet(client: str, lite: bool = False) -> tuple[Path, str]:
    cmd, args, env = server_command(lite)
    path = config_path(client)
    if client == "codex":
        def q(s: str) -> str:
            return json.dumps(s)

        lines = ["[mcp_servers.jobfinder]", f"command = {q(cmd)}", f"args = [{', '.join(q(a) for a in args)}]",
                 "startup_timeout_sec = 60", "tool_timeout_sec = 600"]
        if env:
            lines.append("env = { " + ", ".join(f"{k} = {q(v)}" for k, v in env.items()) + " }")
        return path, "\n".join(lines) + "\n"
    if client == "vscode":
        return path, json.dumps({"servers": {"jobfinder": {"type": "stdio", "command": cmd, "args": args, "env": env}}}, indent=2)
    if client == "opencode":
        return path, json.dumps({"$schema": "https://opencode.ai/config.json",
                                 "mcp": {"jobfinder": {"type": "local", "command": [cmd, *args], "enabled": True,
                                                       "environment": env, "timeout": 600000}}}, indent=2)
    return path, json.dumps({"mcpServers": {"jobfinder": {"command": cmd, "args": args, "env": env}}}, indent=2)


def install(client: str, dry_run: bool = False, lite: bool = False) -> dict[str, Any]:
    path, text = snippet(client, lite)
    if dry_run:
        return {"client": client, "path": str(path), "would_write": text}
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_suffix(path.suffix + ".bak").write_bytes(path.read_bytes())
    if client == "codex":
        cur = path.read_text(encoding="utf-8") if path.exists() else ""
        cur = re.sub(r"(?ms)^\[mcp_servers\.jobfinder\]\n.*?(?=^\[|\Z)", "", cur).rstrip()
        path.write_text((cur + "\n\n" if cur else "") + text, encoding="utf-8")
    else:
        cur: dict[str, Any] = {}
        if path.exists() and path.read_text(encoding="utf-8").strip():
            try:
                cur = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path} is not plain JSON (comments?). Add this manually:\n{text}") from e
        new = json.loads(text)
        for top, val in new.items():
            if isinstance(val, dict):
                cur.setdefault(top, {})
                cur[top].update(val)
            else:
                cur.setdefault(top, val)
        path.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    return {"client": client, "path": str(path), "written": True}
