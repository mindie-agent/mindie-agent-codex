"""Pre-0.1.38 (`v0.1.34`–`v0.1.37 -> v0.1.40`) live-session and self-upgrade handoff tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
_NO_WIN = 0x08000000 if os.name == "nt" else 0


def test_in_flight_v0137_shims_harness_update_runtime_call_and_admission_ops(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg" / "codex.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"python": sys.executable, "runtime_scripts": str(SCRIPTS)}), encoding="utf-8")
    env = dict(os.environ, MINDIE_AGENT_CONFIG=str(cfg), MINDIE_CONFIG_HOME=str(cfg.parent),
               MINDIE_DATA_HOME=str(tmp_path / "data"), _MINDIE_UV_FLAGS="--offline")

    # 1) admission_ops.py responds ok=True
    adm = subprocess.run(
        [sys.executable, str(SCRIPTS / "admission_ops.py"), "claim_bind", str(cfg)],
        input="{}", env=env, capture_output=True, text=True, timeout=30, creationflags=_NO_WIN,
    )
    assert adm.returncode == 0, adm.stderr
    assert json.loads(adm.stdout)["ok"] is True

    # 2) runtime_call.py serves catalog discovery for live_catalog.py
    cat = subprocess.run(
        [sys.executable, str(SCRIPTS / "runtime_call.py")],
        input=json.dumps({"surface": "catalog", "catalog_surface": "remote"}),
        env=env, capture_output=True, text=True, timeout=30, creationflags=_NO_WIN,
    )
    assert cat.returncode == 0, cat.stderr
    tools = json.loads(cat.stdout)
    assert isinstance(tools, list) and any(item["name"] == "remote_exec_command" for item in tools)

    # 2b) runtime_call.py executes one-shot tools/call via MCPRouter.call_tool
    call = subprocess.run(
        [sys.executable, str(SCRIPTS / "runtime_call.py")],
        input=json.dumps({"surface": "components", "name": "mindie_status", "arguments": {}, "mindie_session_id": "sess-123"}),
        env=env, capture_output=True, text=True, timeout=30, creationflags=_NO_WIN,
    )
    assert call.returncode == 0, call.stderr
    call_res = json.loads(call.stdout)
    assert call_res["isError"] is False and isinstance(call_res.get("content"), list)

    # 2c) runtime_call.py remote surface preserves legacy mindie-remote-dev/runtime/<session> and allows remote_write_stdin(session_id=...)
    rem_env = dict(env, MINDIE_REMOTE_STATE_DIR=str(tmp_path / "legacy-remote-root"))
    stdin_call = subprocess.run(
        [sys.executable, str(SCRIPTS / "runtime_call.py")],
        input=json.dumps({
            "surface": "remote",
            "name": "remote_write_stdin",
            "arguments": {"session_id": "missing-pty-1", "chars": "pwd\n"},
            "remote_session_id": "thread-abc",
            "helper_timeout_seconds": 30.0,
        }),
        env=rem_env, capture_output=True, text=True, timeout=30, creationflags=_NO_WIN,
    )
    assert stdin_call.returncode == 0, stdin_call.stderr
    stdin_res = json.loads(stdin_call.stdout)
    assert "forged_identity_argument" not in json.dumps(stdin_res)
    assert "identity override" not in json.dumps(stdin_res).lower()

    # 2d) Resident entry.py remote-mcp accepts required session_id in remote_write_stdin over stdio MCP
    mcp_stdin = subprocess.run(
        [sys.executable, str(SCRIPTS / "entry.py"), "remote-mcp"],
        input=json.dumps({
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "remote_write_stdin", "arguments": {"session_id": 1, "chars": "ls\n"}},
        }) + "\n",
        env=env, capture_output=True, text=True, timeout=30, creationflags=_NO_WIN,
    )
    assert mcp_stdin.returncode == 0, mcp_stdin.stderr
    assert "schema_missing_field" not in mcp_stdin.stdout
    assert "forged_identity_argument" not in mcp_stdin.stdout

    # 3) harness_update.py --config <cfg> exits 0 with JSON dict and no status_receipt_error_type
    upd = subprocess.run(
        [sys.executable, str(SCRIPTS / "harness_update.py"), "--config", str(cfg)],
        env=env, capture_output=True, text=True, timeout=30, creationflags=_NO_WIN,
    )
    assert upd.returncode == 0, upd.stderr
    outcome = json.loads(upd.stdout)
    assert isinstance(outcome, dict) and "status_receipt_error_type" not in outcome


def test_release_activation_refuses_uncertain_retirement(tmp_path: Path, monkeypatch) -> None:
    import entry
    import mindie_knowledge.loop.lifecycle as lifecycle
    import release_activation

    cfg = tmp_path / "codex.json"
    eng = tmp_path / "codex.engine.json"
    rel = tmp_path / "releases/0.1.40"
    rel.mkdir(parents=True)
    kroot = tmp_path / "kroot"
    (kroot / "vllm-ascend").mkdir(parents=True, exist_ok=True)
    eng.write_text(json.dumps({
        "root": str(kroot),
        "domain": "vllm-ascend",
        "capture_mode": "public-transcript",
        "transcript_adapter": str(SCRIPTS / "codex_transcript.py"),
        "redactor_executable": str((tmp_path / "gitleaks").resolve()),
        "admission_path": str((tmp_path / "codex.admission.sqlite3").resolve()),
        "community_config": str((tmp_path / "mindie-community.json").resolve()),
    }), encoding="utf-8")
    current = {
        "python": "/old/python",
        "runtime_scripts": "/old/scripts",
        "engine_config": str(eng),
        "product_validation": {"native_version": "0.1.37"},
        "component_commands": {},
    }
    cfg.write_text(json.dumps(current), encoding="utf-8")
    (rel / "runtime.json").write_text(json.dumps({
        "python": sys.executable,
        "source": str(ROOT / "plugins/mindie-agent"),
        "validation": {"schema": "mindie-candidate-check/1", "status": "validated", "native_version": "0.1.40"},
    }), encoding="utf-8")
    monkeypatch.setattr(lifecycle, "retire_service", lambda *a, **kw: {"idle": True, "cleanup_failed": True})
    import io
    attempt = io.BytesIO(json.dumps({
        "schema": "mindie-activation-attempt/1",
        "expected_binding": release_activation.runtime_binding(current),
    }).encode("utf-8"))
    res = release_activation.main(["release_activation.py", str(cfg), str(rel), "--attempt"], stdin=attempt)
    assert res["status"] == "incomplete"
    assert entry._read(cfg)["python"] == "/old/python"
