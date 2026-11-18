"""Launcher fast paths, native migration, MCP surfaces, and sharing delegation."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/mindie-agent"
SCRIPTS = PLUGIN / "scripts"


def _config() -> Path:
    return Path(os.environ["MINDIE_AGENT_CONFIG"])


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8", newline="\n")


def _sharing(root: str, *, enabled: bool = True, choice: str = "local-only") -> None:
    config = _config()
    consent = config.parent / "mindie-consent.json"
    community = config.parent / "mindie-community.json"
    _write_json(consent, {"schema": "mindie-consent/1", "choice": choice, "enabled": enabled,
                          "project_roots": [root]})
    _write_json(community, {"schema": "mindie-community-config/1", "enabled": enabled,
                            "project_roots": [root], "consent_config": str(consent)})


def _stdin(monkeypatch, payload: dict | None = None):
    class Stream:
        buffer = io.BytesIO(json.dumps(payload).encode() if payload is not None else b"")

    monkeypatch.setattr(sys, "stdin", Stream())


def _fast(role: str) -> tuple[str, float]:
    import hook_launcher

    output = io.StringIO()
    started = time.perf_counter()
    with redirect_stdout(output):
        code = hook_launcher.fast_path(role, name="mindie-agent", marketplace="mindie-agent",
                                       config=_config(), data=Path(os.environ["PLUGIN_DATA"]))
    elapsed = (time.perf_counter() - started) * 1000
    assert code == 0
    return output.getvalue(), elapsed


def test_stop_fast_paths_without_sqlite(monkeypatch, tmp_path, isolate_host_environment):
    _stdin(monkeypatch)
    assert _fast("stop")[0] == "{}\n"
    _write_json(_config(), {})
    _sharing("/work/app", enabled=False)
    assert _fast("stop")[0] == "{}\n"
    _sharing("/work/app")
    _stdin(monkeypatch, {"cwd": "/work/app-other", "project_root": "/work/app"})
    output, elapsed = _fast("stop")
    assert output == "{}\n" and elapsed < 30
    assert not list(Path(isolate_host_environment).rglob("*.sqlite3"))


def test_user_prompt_and_session_start_fast_paths(monkeypatch, isolate_host_environment):
    class Forbidden:
        @property
        def buffer(self):
            raise AssertionError("user-prompt-submit must not read stdin")

    monkeypatch.setattr(sys, "stdin", Forbidden())
    output, elapsed = _fast("user-prompt-submit")
    assert output == "{}\n" and elapsed < 15
    _write_json(_config(), {"python": sys.executable})
    output, elapsed = _fast("session-start")
    assert output == "{}\n" and elapsed < 15
    marker = Path(os.environ["MINDIE_DATA_HOME"]) / "state/codex/pending-update-check.json"
    assert json.loads(marker.read_text(encoding="utf-8"))["reason"] == "session_start"
    assert not list(Path(isolate_host_environment).rglob("*.sqlite3"))


def test_migrate_rebinds_components(monkeypatch):
    import entry

    config = _config()
    engine = config.with_name(config.stem + ".engine.json")
    _write_json(engine, {"transcript_adapter": "/old/codex_transcript.py"})
    _write_json(config, {
        "python": sys.executable, "runtime_scripts": "/old/scripts", "component_release": "/old/release",
        "engine_config": str(engine), "admission_path": str(config.parent / "codex.admission.sqlite3"),
        "product_validation": {"schema": "mindie-candidate-check/1", "status": "validated",
                               "native_version": "0.1.23"},
        "component_commands": {"sim": ["/old/python", "-m", "mindie_sim"],
                               "profiler": ["/old/python", "-m", "mindie_profiler"]},
    })
    adapter, _engine, _profile = entry._prepare(None, PLUGIN, True)
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["runtime_scripts"] == str(SCRIPTS)
    assert saved["product_validation"]["native_version"] == "0.1.38"
    assert saved["python"] == sys.executable
    for name in ("sim", "profiler"):
        assert saved["component_commands"][name][0] == sys.executable
    assert adapter["source_root"] == str(PLUGIN)


def test_mcp_surface_separation():
    from host_adapter import CodexHostAdapter

    handle = CodexHostAdapter(plugin_root=PLUGIN)
    profile = handle.resolve_profile()
    remote = {tool["name"] for tool in handle.mcp_tools("remote", profile)}
    tools = {tool["name"] for tool in handle.mcp_tools("tools", profile)}
    assert {"remote_probe", "remote_exec_command"} <= remote
    assert not {"remote_BashOutput", "remote_KillShell"} & remote
    assert not any(name.startswith("knowledge_") for name in remote)
    assert {"mindie_doctor", "diagnostics_status"} <= tools
    assert {"knowledge_query", "knowledge_explain", "knowledge_feedback",
            "knowledge_distill", "knowledge_sync"} <= tools


def test_mindie_interrupt_cancels_inflight_request(tmp_path):
    from mindie_harness.mcp.router import MCPRouter
    from mindie_harness.mcp.server import MCPStdioServer
    from mindie_harness.paths import ProfileRef

    profile = ProfileRef.resolve("codex", plugin_root=PLUGIN, data_dir=tmp_path / "data")
    started = threading.Event()

    def handler(ctx, arguments):
        started.set()
        for _ in range(400):
            if ctx.cancel.is_set():
                return {"content": [{"type": "text", "text": "cancelled"}], "isError": False}
            time.sleep(0.005)
        return {"content": [{"type": "text", "text": "timeout"}], "isError": True}

    tools = [{"name": "slow", "description": "slow", "annotations": {"readOnlyHint": True},
              "inputSchema": {"type": "object", "properties": {}}}]
    router = MCPRouter(profile, surface="tools", tools=tools, handler=handler)
    server = MCPStdioServer(router)
    read_fd, write_fd = os.pipe()
    output = io.BytesIO()

    def writer():
        with os.fdopen(write_fd, "wb") as stream:
            stream.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode() + b"\n")
            stream.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                     "params": {"name": "slow", "arguments": {}}}).encode() + b"\n")
            started.wait(5)
            stream.write(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                     "params": {"name": "mindie_interrupt", "arguments": {}}}).encode() + b"\n")

    thread = threading.Thread(target=writer); thread.start()
    with os.fdopen(read_fd, "rb") as stdin:
        server.serve(stdin=stdin, stdout=output)
    thread.join(5)
    responses = [json.loads(line) for line in output.getvalue().decode().splitlines() if line.strip()]
    slow = next(item for item in responses if item.get("id") == 2)
    assert "cancelled" in slow["result"]["content"][0]["text"]


def test_sharing_and_doctor_cli(tmp_path, capsys, monkeypatch):
    import setup

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for args in (["sharing", "--local-only", "--project-root", str(workspace)],
                 ["sharing", "--status"], ["sharing", "--enable", "--project-root", str(workspace)],
                 ["sharing", "--disable"]):
        assert setup.main(args) == 0
        json.loads(capsys.readouterr().out)
    captured = {}

    def fake_run_host(argv):
        captured["argv"] = list(argv)
        return 0

    import mindie_harness.api
    monkeypatch.setattr(mindie_harness.api, "run_host", fake_run_host)
    assert setup._doctor(["--probe-remote", "--cwd", str(workspace), "--timeout", "9"]) == 0
    assert captured["argv"] == ["--host", "codex", "doctor", "--probe-remote",
                                "--cwd", str(workspace), "--timeout", "9"]
    with pytest.raises(SystemExit):
        setup.main(["sharing", "--enable", "--disable"])


def test_isolated_entry_subprocess_and_corrupt_config_guard(tmp_path):
    import subprocess
    import entry

    proc = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPTS / "entry.py"), "user-prompt-submit"],
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0 and proc.stdout == "{}\n"
    corrupt = _config()
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{corrupt-json", encoding="utf-8")
    (corrupt.parent / "codex.engine.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        entry._prepare(None, PLUGIN, False)
    assert corrupt.read_text(encoding="utf-8") == "{corrupt-json"


def test_legacy_release_activation_cas_and_recovery(tmp_path):
    import subprocess
    import entry
    import release_activation

    config = _config()
    legacy_engine = tmp_path / "legacy-release/profile-deadbeef.engine.json"
    _write_json(legacy_engine, {
        "root": str(tmp_path / "knowledge"),
        "domain": "vllm-ascend",
        "admission_path": str(config.parent / "codex.admission.sqlite3"),
        "community_config": str(config.parent / "mindie-community.json"),
        "transcript_adapter": "/old/codex_transcript.py",
    })
    base_cfg = {
        "python": "/old/python",
        "runtime_scripts": "/old/scripts",
        "component_release": str(tmp_path / "legacy-release"),
        "engine_config": str(legacy_engine),
        "domain_configs": {"vllm-ascend": str(legacy_engine)},
        "product_validation": {"schema": "mindie-candidate-check/1", "status": "validated", "native_version": "0.1.23"},
        "component_commands": {},
    }
    expected_stale = release_activation.runtime_binding(base_cfg)
    mutated = dict(base_cfg, component_commands={
        "sim": ["/ext/python", "-I", "-X", "utf8", "-m", "mindie_sim"],
        "profiler": ["/ext/python", "-I", "-X", "utf8", "-m", "mindie_profiler"],
    })
    _write_json(config, mutated)
    candidate_release = tmp_path / "releases/0.1.38"
    _write_json(candidate_release / "runtime.json", {
        "key": "k",
        "python": sys.executable,
        "source": str(PLUGIN),
        "validation": {"schema": "mindie-candidate-check/1", "status": "validated", "native_version": "0.1.38"},
    })
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPTS / "release_activation.py"), str(config), str(candidate_release), "--attempt"],
        input=json.dumps({"schema": "mindie-activation-attempt/1", "expected_binding": expected_stale}),
        capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {
        "execution": "not_started",
        "reason": "installation_changed",
        "status": "deferred",
    }
    assert json.loads(config.read_text(encoding="utf-8")) == mutated
    assert not (config.parent / "codex.engine.json").exists()

    # Subsequent MCP startup migration preserves legacy engine root and rebinds external commands
    entry._prepare(candidate_release, PLUGIN, True)
    recovered = json.loads(config.read_text(encoding="utf-8"))
    recovered_engine = json.loads((config.parent / "codex.engine.json").read_text(encoding="utf-8"))
    assert recovered["product_validation"]["native_version"] == "0.1.38"
    assert recovered["component_commands"]["sim"] == [sys.executable, "-I", "-X", "utf8", "-m", "mindie_sim"]
    assert recovered["component_commands"]["profiler"] == [sys.executable, "-I", "-X", "utf8", "-m", "mindie_profiler"]
    assert recovered_engine["root"] == str(tmp_path / "knowledge")

    # Matching expected_binding activates directly
    _write_json(config, mutated)
    proc_ok = subprocess.run(
        [sys.executable, "-I", str(SCRIPTS / "release_activation.py"), str(config), str(candidate_release), "--attempt"],
        input=json.dumps({"schema": "mindie-activation-attempt/1", "expected_binding": release_activation.runtime_binding(mutated)}),
        capture_output=True, text=True, timeout=20,
    )
    assert proc_ok.returncode == 0
    assert json.loads(proc_ok.stdout)["status"] == "active"
    activated = json.loads(config.read_text(encoding="utf-8"))
    assert activated["python"] == sys.executable
    assert activated["component_commands"]["sim"][0] == sys.executable
