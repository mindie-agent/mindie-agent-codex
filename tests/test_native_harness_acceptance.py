"""Opt-in, model-free acceptance against a real Codex binary and isolated profile.

Set MINDIE_TEST_CODEX_BIN to an absolute native executable. conftest isolates
HOME, CODEX_HOME and every MindIE state root; no real user trust is changed.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
from native_hooks import NativeHooks, _own_handler
from release_metadata import require_harness


def invoke(argv, *, env, timeout=60):
    result = subprocess.run(argv, env=env, capture_output=True, text=True,
                            encoding="utf-8", timeout=timeout)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(not os.environ.get("MINDIE_TEST_CODEX_BIN"),
                    reason="set MINDIE_TEST_CODEX_BIN for native acceptance")
def test_native_plugin_hooks_and_config_cas(monkeypatch, tmp_path):
    native = os.environ["MINDIE_TEST_CODEX_BIN"]
    monkeypatch.setenv("MINDIE_CODEX_BIN", native)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    env = dict(os.environ)
    version = require_harness(SCRIPTS.parent)
    invoke([native, "plugin", "marketplace", "add", str(ROOT), "--json"], env=env)
    installed = invoke([native, "plugin", "add", "mindie-agent@mindie-agent", "--json"], env=env)
    source = Path(installed["installedPath"])
    with NativeHooks(native, env) as api:
        hooks = api.hooks()
        assert {h["eventName"] for h in hooks} == {
            "sessionStart", "userPromptSubmit", "stop", "interrupt"}
        assert len(hooks) == 5
        assert all(_own_handler(h, source) for h in hooks)
        assert all(h["trustStatus"] == "untrusted" for h in hooks)
        interrupts = [h for h in hooks if h["eventName"] == "interrupt"]
        assert {h["server"] for h in interrupts} == {"mindie-remote-dev", "mindie-tools"}
        assert all(h["tool"] == "mindie_interrupt" for h in interrupts)
        hook = next(h for h in hooks if h["eventName"] == "stop")
        _, layer, target = api.user_config()
        # Seed one explicit approval in the disposable test profile, then
        # exercise the production updater's exact approval-preservation path.
        api.call("config/value/write", dict(
            keyPath="hooks.state." + json.dumps(hook["key"]) + ".trusted_hash",
            value=hook["currentHash"], mergeStrategy="replace",
            expectedVersion=layer["version"], filePath=str(target)))
        assert api.write_trust(hook, hook["currentHash"])
        assert next(h for h in api.hooks() if h["key"] == hook["key"])["trustStatus"] == "trusted"
        _, layer, target = api.user_config()
        api.call("config/value/write", dict(
            keyPath="hooks.state." + json.dumps(hook["key"]) + ".enabled",
            value=False, mergeStrategy="replace", expectedVersion=layer["version"],
            filePath=str(target)))
        assert not api.write_trust(hook, hook["currentHash"])
        # Discovery and config CAS alone must never initialize MindIE capture.
        assert not Path(env["MINDIE_AGENT_CONFIG"]).exists()
    print("native acceptance:", version, "plugin + five hooks + trust CAS + explicit disable")


@pytest.mark.skipif(not os.environ.get("MINDIE_TEST_CODEX_BIN"),
                    reason="set MINDIE_TEST_CODEX_BIN for native acceptance")
def test_native_locked_runtime_and_mcp_discovery(monkeypatch, tmp_path):
    native = os.environ["MINDIE_TEST_CODEX_BIN"]
    monkeypatch.setenv("MINDIE_CODEX_BIN", native)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    env = dict(os.environ)
    invoke([native, "plugin", "marketplace", "add", str(ROOT), "--json"], env=env)
    installed = invoke([native, "plugin", "add", "mindie-agent@mindie-agent", "--json"], env=env)
    source = Path(installed["installedPath"])
    data = Path(env["CODEX_HOME"]) / "plugins/data/mindie-agent-mindie-agent"
    code = (
        "import sys,json;from pathlib import Path;"
        "sys.path.insert(0,str(Path(sys.argv[1])/'scripts'));"
        "from bootstrap import ensure_runtime;"
        "p,s,r=ensure_runtime(sys.argv[1],sys.argv[2]);"
        "print(json.dumps({'python':str(p),'scripts':str(s),'release':str(r)}))")
    runtime = invoke([sys.executable, "-I", "-B", "-c", code, str(source), str(data)],
                     env=env, timeout=900)
    invoke([runtime["python"], "-I", str(Path(runtime["scripts"]) / "setup.py"),
            "install", "--knowledge-python", runtime["python"],
            "--config", env["MINDIE_AGENT_CONFIG"], "--root", str(tmp_path / "knowledge"),
            "--no-public-feed"], env=env, timeout=120)
    with NativeHooks(native, env) as api:
        hook = next(h for h in api.hooks() if h["eventName"] == "sessionStart")
        _, layer, target = api.user_config()
        api.call("config/value/write", dict(
            keyPath="hooks.state." + json.dumps(hook["key"]) + ".trusted_hash",
            value=hook["currentHash"], mergeStrategy="replace",
            expectedVersion=layer["version"], filePath=str(target)))
        thread = api.call("thread/start", dict(cwd=str(tmp_path), ephemeral=True,
                                               approvalPolicy="never"))["thread"]["id"]
        # Starting a native thread supplies its real identity to SessionStart;
        # no model turn, knowledge activation or remote workload is requested.
        status = api.call("mcpServerStatus/list", dict(threadId=thread))
        rows = status["data"]
        remote = next(r for r in rows if "mindie-remote-dev" in r["name"])
        components = next(r for r in rows if "mindie-tools" in r["name"])
        assert remote["tools"]
        assert "diagnostics_status" in components["tools"]
        assert not any("knowledge_" in name for r in rows for name in r["tools"])
        print("native MCP discovery:", len(remote["tools"]), "remote tools,",
              len(components["tools"]), "component tools; thread", thread)
