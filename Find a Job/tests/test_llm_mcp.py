import asyncio
import json
from types import SimpleNamespace

import pytest

from jobfinder.config import ModelSpec
from jobfinder.llm import LLM, Budget, BudgetExceeded, LLMUnavailable, litellm_kwargs, parse_json
from jobfinder.llm import router as router_mod


def test_parse_json_variants():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('<think>hmm</think>Sure: {"a": [1,2]} done') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        parse_json("no json here")


def test_litellm_mapping():
    assert litellm_kwargs(ModelSpec(provider="ollama", model="qwen2.5:7b"))["model"] == "ollama_chat/qwen2.5:7b"
    lm = litellm_kwargs(ModelSpec(provider="lmstudio", model="m"))
    assert lm["model"] == "openai/m" and lm["api_base"].endswith("/v1")
    assert litellm_kwargs(ModelSpec(provider="deepseek", model="deepseek-chat", api_key="k"))["model"] == "deepseek/deepseek-chat"
    az = litellm_kwargs(ModelSpec(provider="azure", model="dep", api_key="k", api_base="https://x", api_version="v"))
    assert az["model"] == "azure/dep" and az["api_version"] == "v"


def test_routing_fallback_and_budget(app, monkeypatch):
    app.config.set_env_values({"OPENAI_API_KEY": "sk-test", "DEEPSEEK_API_KEY": "ds-test"})
    calls = []

    def fake_completion(messages, **kw):
        calls.append(kw["model"])
        if kw["model"].startswith("openai/"):
            raise RuntimeError("rate limited")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
                               usage=SimpleNamespace(total_tokens=42))

    monkeypatch.setattr(router_mod, "_lib", lambda: SimpleNamespace(completion=fake_completion))
    llm = LLM(lambda: app.conn)
    b = Budget(max_tokens=50)
    data, model = llm.json("plan_queries", "sys", "user", budget=b)
    assert data == {"ok": True} and model == "deepseek" and calls[0].startswith("openai/")
    assert b.tokens == 42
    b.charge(tokens=10)
    with pytest.raises(BudgetExceeded):
        llm.json("plan_queries", "sys", "user", budget=b)


@pytest.mark.parametrize("env,expected_model", [
    ({"ANTHROPIC_API_KEY": "sk-ant"}, "anthropic/claude-sonnet-4-5"),
    ({"ANTHROPIC_API_KEY": "sk-ant", "ANTHROPIC_MODEL": "claude-opus-4-1"}, "anthropic/claude-opus-4-1"),
    ({"GEMINI_API_KEY": "g"}, "gemini/gemini-2.5-flash"),
    ({"OPENAI_API_KEY": "sk"}, "openai/gpt-4.1-mini"),
    ({"DEEPSEEK_API_KEY": "d"}, "deepseek/deepseek-chat"),
    ({"LOCAL": "1"}, "ollama_chat/qwen2.5:7b"),
])
def test_any_single_vendor_works(app, monkeypatch, env, expected_model):
    """With the default preset, whichever single provider is configured is used (vendor-agnostic)."""
    from jobfinder.llm import router as r

    monkeypatch.setattr(r, "_local_models", lambda base: {"qwen2.5:7b"} if "LOCAL" in env else None)
    app.config.set_env_values({k: v for k, v in env.items() if k != "LOCAL"})
    seen = []

    def fake(messages, **kw):
        seen.append(kw["model"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": 1}'))], usage=None)

    monkeypatch.setattr(r, "_lib", lambda: SimpleNamespace(completion=fake))
    for task in ("extract_skills", "plan_queries", "find_companies"):
        LLM(lambda: app.conn).json(task, "s", "u")
    assert set(seen) == {expected_model}


def test_presets_cover_vendors():
    from jobfinder.config import PRESETS, default_connections_text, parse_connections

    assert {"cloud-openai", "cloud-claude", "cloud-gemini", "cloud-deepseek", "cloud-azure", "local", "hybrid"} <= set(PRESETS)
    conn = parse_connections(default_connections_text("cloud-claude"), {})
    assert conn.llm.routes["find_companies"][0] == "claude" and "local" in conn.llm.routes["find_companies"]
    assert parse_connections(default_connections_text("local"), {}).llm.routes["default"] == ["local", "lmstudio"]


def test_no_models(app):
    with pytest.raises(LLMUnavailable):
        LLM(lambda: app.conn).chat("default", [{"role": "user", "content": "x"}])


def test_mcp_tools(app, profile, mx_criteria):
    from jobfinder.mcp.server import build_server

    srv = build_server(app)
    tools = {t.name for t in asyncio.run(srv.list_tools())}
    assert {"get_workplan", "submit_workplan", "web_search", "start_run", "get_stats", "export_excel"} <= tools
    res = asyncio.run(srv.call_tool("get_workplan", {"profile_id": profile.id}))
    content = getattr(res, "content", None) or (res[0] if isinstance(res, tuple) else res)
    payload = json.loads(content[0].text)
    assert payload["profile"]["id"] == profile.id and payload["criteria"][0]["id"] == mx_criteria.id


def test_mcp_setup_tools_and_errors(app):
    from jobfinder.mcp.server import build_server

    srv = build_server(app)

    def call(tool, **args):
        try:
            res = asyncio.run(srv.call_tool(tool, args))
        except Exception as e:  # in-process call surfaces ToolError; over stdio it becomes isError
            return str(e), True
        content = getattr(res, "content", None) or (res[0] if isinstance(res, tuple) else res)
        is_err = bool(getattr(res, "isError", False) or getattr(res, "is_error", False))
        text = content[0].text
        return (text, True) if is_err else (json.loads(text), False)

    p, _ = call("create_profile", name="Lucía Gómez", home_country="MX", home_region="MX-JAL", languages=["es:native"])
    assert p["id"] == "lucia-gomez" and p["languages"][0]["level"] == "native"
    sk, _ = call("set_skills", profile_id=p["id"], skills=["Excel avanzado", {"name": "dispensación", "level": 4}])
    assert sk["total"] == 2 and any("pharmacy-dispensing" in s for s in sk["skills"])
    bad, err = call("save_criteria", profile_id=p["id"], criteria={"name": "x", "targets": {"titles": ["a"]},
                                                                   "constraints": {"locations": [{"country": "MX", "region": "CA-ON"}]}})
    assert not err and not bad["saved"] and "not inside country" in bad["errors"][0]
    ok, _ = call("save_criteria", profile_id=p["id"], criteria={"name": "Farmacia GDL", "targets": {"titles": ["auxiliar de farmacia"]},
                                                                "constraints": {"locations": [{"country": "MX", "region": "MX-JAL", "city": "Guadalajara"}]}})
    assert ok["saved"] and ok["role_queries"]
    msg, err = call("delete_profile", profile_id=p["id"])
    assert err and "confirm=true" in msg  # readable reason, not a generic crash
    msg, err = call("get_workplan", profile_id="nope")
    assert err and "Not found" in msg
    msg, err = call("web_search", query="x")
    assert err and "TAVILY_API_KEY" in msg


def test_mcp_install_snippets(tmp_path, monkeypatch):
    from jobfinder.mcp import install

    monkeypatch.setattr(install, "_home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('model = "x"\n\n[mcp_servers.jobfinder]\ncommand = "old"\n', encoding="utf-8")
    install.install("codex")
    txt = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert txt.count("[mcp_servers.jobfinder]") == 1 and 'model = "x"' in txt and '"old"' not in txt
    install.install("vscode")
    install.install("vscode")
    p = install.config_path("vscode")
    assert json.loads(p.read_text(encoding="utf-8"))["servers"]["jobfinder"]["args"] == ["-m", "jobfinder", "mcp"]
    oc = install.config_path("opencode")
    oc.parent.mkdir(parents=True)
    oc.write_text('{"$schema": "https://opencode.ai/config.json", "model": "openai/gpt-5", "mcp": {"other": {"type": "local"}}}',
                  encoding="utf-8")
    install.install("opencode")
    cfg = json.loads(oc.read_text(encoding="utf-8"))
    assert cfg["model"] == "openai/gpt-5" and "other" in cfg["mcp"]
    assert cfg["mcp"]["jobfinder"]["timeout"] == 600000 and cfg["mcp"]["jobfinder"]["command"][1:] == ["-m", "jobfinder", "mcp"]
