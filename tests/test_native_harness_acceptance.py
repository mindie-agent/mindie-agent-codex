"""Opt-in, model-free acceptance against a real Codex binary and isolated profile."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/mindie-agent"
SCRIPTS = PLUGIN / "scripts"
sys.path.insert(0, str(SCRIPTS))
from native_hooks import NativeHooks, _own_handler


def invoke(argv, *, env, timeout=60):
    result = subprocess.run(argv, env=env, capture_output=True, text=True, encoding="utf-8",
                            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _native() -> str:
    value = os.environ.get("MINDIE_TEST_CODEX_BIN")
    if not value:
        pytest.skip("set MINDIE_TEST_CODEX_BIN for native acceptance")
    return value


def test_native_plugin_hooks_and_config_cas(monkeypatch, tmp_path):
    native = _native()
    monkeypatch.setenv("MINDIE_CODEX_BIN", native)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    env = dict(os.environ)
    invoke([native, "plugin", "marketplace", "add", str(ROOT), "--json"], env=env)
    installed = invoke([native, "plugin", "add", "mindie-agent@mindie-agent", "--json"], env=env)
    source = Path(installed["installedPath"])
    with NativeHooks(native, env) as api:
        hooks = api.hooks()
        assert {hook["eventName"] for hook in hooks} == {"sessionStart", "userPromptSubmit", "stop", "interrupt"}
        assert len(hooks) == 5
        assert all(_own_handler(hook, source) for hook in hooks)
        hook = next(row for row in hooks if row["eventName"] == "stop")
        _, layer, target = api.user_config()
        api.call("config/value/write", dict(
            keyPath="hooks.state." + json.dumps(hook["key"]) + ".trusted_hash",
            value=hook["currentHash"], mergeStrategy="replace",
            expectedVersion=layer["version"], filePath=str(target)))
        assert api.write_trust(hook, hook["currentHash"])
        _, layer, target = api.user_config()
        api.call("config/value/write", dict(
            keyPath="hooks.state." + json.dumps(hook["key"]) + ".enabled",
            value=False, mergeStrategy="replace", expectedVersion=layer["version"],
            filePath=str(target)))
        assert not api.write_trust(hook, hook["currentHash"])
        assert not Path(env["MINDIE_AGENT_CONFIG"]).exists()


def test_native_locked_runtime_and_mcp_discovery(monkeypatch, tmp_path):
    native = _native()
    monkeypatch.setenv("MINDIE_CODEX_BIN", native)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    env = dict(os.environ)
    invoke([native, "plugin", "marketplace", "add", str(ROOT), "--json"], env=env)
    installed = invoke([native, "plugin", "add", "mindie-agent@mindie-agent", "--json"], env=env)
    source = Path(installed["installedPath"])
    data = Path(env["CODEX_HOME"]) / "plugins/data/mindie-agent-mindie-agent"
    code = ("import sys,json;from pathlib import Path;sys.path.insert(0,str(Path(sys.argv[1])/'scripts'));"
            "from bootstrap import ensure_runtime;p,s,r=ensure_runtime(sys.argv[1],sys.argv[2]);"
            "print(json.dumps({'python':str(p),'scripts':str(s),'release':str(r)}))")
    runtime = invoke([sys.executable, "-I", "-X", "utf8", "-B", "-c", code, str(source), str(data)],
                     env=env, timeout=930)
    invoke([runtime["python"], "-I", "-X", "utf8", str(Path(runtime["scripts"]) / "setup.py"),
            "install", "--knowledge-python", runtime["python"],
            "--config", env["MINDIE_AGENT_CONFIG"], "--root", str(tmp_path / "knowledge"),
            "--no-public-feed"], env=env, timeout=180)
    with NativeHooks(native, env) as api:
        hook = next(row for row in api.hooks() if row["eventName"] == "sessionStart")
        _, layer, target = api.user_config()
        api.call("config/value/write", dict(
            keyPath="hooks.state." + json.dumps(hook["key"]) + ".trusted_hash",
            value=hook["currentHash"], mergeStrategy="replace",
            expectedVersion=layer["version"], filePath=str(target)))
        thread = api.call("thread/start", dict(cwd=str(tmp_path), ephemeral=True, approvalPolicy="never"))["thread"]["id"]
        rows = api.call("mcpServerStatus/list", dict(threadId=thread))["data"]
        remote = next(row for row in rows if "mindie-remote-dev" in row["name"])
        tools = next(row for row in rows if "mindie-tools" in row["name"])
        expected = ("knowledge_query", "knowledge_explain", "knowledge_feedback",
                    "knowledge_distill", "knowledge_sync")
        assert "remote_probe" in remote["tools"] and "remote_exec_command" in remote["tools"]
        assert "remote_BashOutput" not in remote["tools"] and "remote_KillShell" not in remote["tools"]
        assert not any(name.startswith("knowledge_") for name in remote["tools"])
        assert {"mindie_doctor", "diagnostics_status", "npu_top"} <= set(tools["tools"])
        assert all(name in tools["tools"] for name in expected)
        doctor_res = api.call(
            "mcpServer/tool/call",
            dict(threadId=thread, server=tools["name"], tool="mindie_doctor", arguments={"probe_remote": False}),
            timeout=60,
        )
        assert doctor_res.get("isError") is False and doctor_res.get("content")
        kq_res = api.call(
            "mcpServer/tool/call",
            dict(threadId=thread, server=tools["name"], tool="knowledge_query", arguments={"query": "aclgraph", "domain": "vllm-ascend"}),
            timeout=90,
        )
        assert kq_res.get("isError") is False and kq_res.get("content")
