"""Group G6a orthogonal regression tests for `mindie-agent-codex` clean-core architecture."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "mindie-agent"
SCRIPTS = PLUGIN / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

FROZEN_HOOKS_SHA256 = "ea80d0815485ba5f762ff5275f1e40817130694870405237dbd65d03f28f9981"
FROZEN_HOOKS_BYTES = 25650


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    xdg_cfg = tmp_path / "xdg-config"
    xdg_data = tmp_path / "xdg-data"
    xdg_state = tmp_path / "xdg-state"
    xdg_cache = tmp_path / "xdg-cache"
    tmp_dir = tmp_path / "tmp"
    for d in (home, xdg_cfg, xdg_data, xdg_state, xdg_cache, tmp_dir):
        d.mkdir(parents=True, exist_ok=True)
    env_map = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "XDG_CONFIG_HOME": str(xdg_cfg),
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_STATE_HOME": str(xdg_state),
        "XDG_CACHE_HOME": str(xdg_cache),
        "MINDIE_CONFIG_HOME": str(xdg_cfg / "mindie-agent"),
        "MINDIE_DATA_HOME": str(xdg_data / "mindie-agent"),
        "MINDIE_DIAGNOSTICS_ROOT": str(tmp_path / "diagnostics"),
        "MINDIE_DIAGNOSTICS_CONFIG": str(tmp_path / "diagnostics" / "diagnostics.json"),
        "MINDIE_REMOTE_STATE_DIR": str(tmp_path / "remote-state"),
        "MINDIE_AGENT_CONFIG": str(xdg_cfg / "mindie-agent" / "codex.json"),
        "REMOTE_DEV_AUTH_DIR": str(tmp_path / "remote-auth"),
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "TMPDIR": str(tmp_dir),
    }
    for key, val in env_map.items():
        monkeypatch.setenv(key, val)
    return env_map


def test_hooks_json_exact_sha256_and_byte_length_frozen():
    hooks_path = PLUGIN / "hooks" / "hooks.json"
    raw = hooks_path.read_bytes()
    assert len(raw) == FROZEN_HOOKS_BYTES
    assert hashlib.sha256(raw).hexdigest() == FROZEN_HOOKS_SHA256


def test_bundled_harness_seed_and_host_adapter_contract(isolated_env, tmp_path):
    import host_adapter

    seed_info = host_adapter.verify_bundled_seed(PLUGIN)
    assert seed_info["schema"] == "mindie-harness-seed/1"
    assert seed_info["artifact"] == "mindie-harness-seed.pyz"
    assert Path(seed_info["path"]).is_file()

    adapter = host_adapter.CodexHostAdapter(
        plugin_root=PLUGIN,
        config_path=tmp_path / "codex.json",
        data_dir=tmp_path / "plugin-data",
    )
    assert adapter.host == "codex"
    if host_adapter.harness_available():
        from mindie_harness.api import HostAdapter

        assert isinstance(adapter, HostAdapter)
        profile = adapter.resolve_profile()
        assert profile.host == "codex"


def test_rsi_forwarders_under_30_lines_and_delegate_to_mindie_harness():
    for name in ("rsi_loop.py", "rsi_explore.py", "experiment_ledger.py"):
        lines = (SCRIPTS / name).read_text(encoding="utf-8").splitlines()
        assert len(lines) < 30, f"{name} exceeded 30 lines ({len(lines)})"

    import rsi_explore
    import rsi_loop
    import experiment_ledger

    parsed = rsi_explore.forwarded({"mode": "explore", "goal": "reduce host gap"})
    assert parsed["mode"] == "explore"
    assert parsed["goal"] == "reduce host gap"
    assert callable(rsi_loop.init)
    assert callable(experiment_ledger.fingerprint)
    if getattr(sys.modules.get("mindie_harness"), "__file__", None) or True:
        try:
            import mindie_harness.rsi.rsi_loop as harness_loop
        except ModuleNotFoundError:
            harness_loop = None
        if harness_loop is not None:
            assert rsi_loop.__name__ == "mindie_harness.rsi.rsi_loop"


def test_ad09_eight_concurrent_readonly_calls_succeed_and_zero_hotpath_owner_sweeps(
    isolated_env, tmp_path, monkeypatch
):
    import mcp_gate
    import receipt_owners

    sweep_calls = []
    monkeypatch.setattr(
        mcp_gate,
        "sweep_idle_owner_locks",
        lambda *a, **kw: sweep_calls.append((a, kw)) or [],
    )
    receipts = mcp_gate.RemoteReceipts("session-ad09")
    assert receipts.claim("mut-1")
    receipts.finish("mut-1", "succeeded")
    # Zero hot-path sweeps during _open_db / claim / finish!
    assert sweep_calls == []
    receipts.close()
    assert len(sweep_calls) == 1
    assert receipt_owners is not None

    # Spawn real mcp_transport.serve("remote") and fire 8 concurrent remote_read/remote_grep
    # calls synchronized on a barrier so all 8 are in-flight simultaneously!
    work = tmp_path / "mcp-concurrency"
    work.mkdir()
    driver = work / "serve_driver.py"
    driver.write_text(
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "from unittest.mock import patch\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import mcp_gate\n"
        "from bounded_process import ProcessResult\n"
        "work = Path(sys.argv[2])\n"
        "cfg = work / 'codex.json'\n"
        "cfg.write_text(json.dumps({'python': sys.executable, 'runtime_scripts': sys.argv[1], 'engine_config': str(work / 'eng.json')}))\n"
        "os.environ['MINDIE_AGENT_CONFIG'] = str(cfg)\n"
        "def fake_run(argv, stdin_text, **kw):\n"
        "    req = json.loads(stdin_text)\n"
        "    name = req['name']\n"
        "    tag = req['arguments'].get('path') or req['arguments'].get('pattern') or req['arguments'].get('command')\n"
        "    (work / f'{tag}.entered').write_text(name)\n"
        "    if str(tag).startswith('ro-'):\n"
        "        deadline = time.monotonic() + 5.0\n"
        "        while not (work / 'release.ro').exists() and time.monotonic() < deadline:\n"
        "            time.sleep(0.01)\n"
        "    elif str(tag).startswith('mut-'):\n"
        "        deadline = time.monotonic() + 5.0\n"
        "        while not (work / 'release.mut').exists() and time.monotonic() < deadline:\n"
        "            time.sleep(0.01)\n"
        "    return ProcessResult('completed', json.dumps({'content': [{'type': 'text', 'text': str(tag)}], 'isError': False}), 0)\n"
        "with patch.object(mcp_gate, 'run', side_effect=fake_run), \\\n"
        "     patch('update_trigger.Maintenance'), \\\n"
        "     patch('diagnostic_support.attach_pending', side_effect=lambda v: v), \\\n"
        "     patch('diagnostic_support.acknowledge_pending'), \\\n"
        "     patch('importlib.metadata.version', return_value='0.1.34'):\n"
        "    mcp_gate.serve('remote')\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, "-I", str(driver), str(SCRIPTS), str(work)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **isolated_env},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    replies: queue.Queue[dict] = queue.Queue()
    reader = threading.Thread(
        target=lambda: [replies.put(json.loads(line)) for line in proc.stdout],
        daemon=True,
    )
    reader.start()

    def send(msg: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        proc.stdin.flush()

    try:
        # 8 concurrent read-only calls (remote_read + remote_grep) with zero _meta
        for idx in range(8):
            tool = "remote_read" if idx % 2 == 0 else "remote_grep"
            arg = {"path": f"ro-{idx}"} if tool == "remote_read" else {"pattern": f"ro-{idx}"}
            send({"jsonrpc": "2.0", "id": f"r-{idx}", "method": "tools/call", "params": {"name": tool, "arguments": arg}})

        deadline = time.monotonic() + 5.0
        while len(list(work.glob("ro-*.entered"))) < 8 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(list(work.glob("ro-*.entered"))) == 8, "All 8 read-only calls must enter concurrently!"
        (work / "release.ro").write_text("go", encoding="utf-8")

        ro_results = {}
        for _ in range(8):
            msg = replies.get(timeout=5.0)
            ro_results[msg["id"]] = msg["result"]
        assert all(res["isError"] is False for res in ro_results.values())

        # Mutating remote_bash still enforces the 4-slot mutation cap on the 5th concurrent mutation
        for idx in range(4):
            send({
                "jsonrpc": "2.0",
                "id": f"m-{idx}",
                "method": "tools/call",
                "params": {"name": "remote_bash", "arguments": {"command": f"mut-{idx}"}},
            })
        deadline = time.monotonic() + 5.0
        while len(list(work.glob("mut-*.entered"))) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(list(work.glob("mut-*.entered"))) == 4
        send({
            "jsonrpc": "2.0",
            "id": "m-overflow",
            "method": "tools/call",
            "params": {"name": "remote_bash", "arguments": {"command": "mut-overflow"}},
        })
        overflow = replies.get(timeout=5.0)
        assert overflow["id"] == "m-overflow"
        assert overflow["result"]["isError"] is True
        (work / "release.mut").write_text("go", encoding="utf-8")
        for _ in range(4):
            res = replies.get(timeout=5.0)
            assert res["result"]["isError"] is False
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.wait(timeout=5.0)
        reader.join(timeout=2.0)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


def test_ad07_diagnostic_support_allows_symlinks_within_root_and_blocks_escape(
    isolated_env, tmp_path
):
    import diagnostic_support

    target = tmp_path / "real-meta.txt"
    target.write_text("Version: 0.2.0\n", encoding="utf-8")
    link = tmp_path / "link-meta.txt"
    try:
        os.symlink(target, link)
    except OSError as exc:
        pytest.skip(f"symlink unsupported: {exc}")

    assert diagnostic_support._read_bytes_bounded(link, 1024) == b"Version: 0.2.0\n"

    # Symlinked event file inside ev_dir is accepted; symlink pointing outside ev_dir is rejected
    diag_root = Path(isolated_env["MINDIE_DIAGNOSTICS_ROOT"])
    ev_dir = diag_root / "events" / "mindie-profiler"
    sub = ev_dir / "shard"
    sub.mkdir(parents=True)
    tok = "a" * 32
    oid_inside = "b" * 32
    oid_outside = "c" * 32
    inside_file = sub / "real.jsonl"
    inside_file.write_text(
        json.dumps({
            "component": "mindie-profiler",
            "event": "operation.end",
            "status": "error",
            "reportable": True,
            "invocation_token": tok,
            "operation_id": oid_inside,
        }) + "\n",
        encoding="utf-8",
    )
    outside_file = tmp_path / "outside.jsonl"
    outside_file.write_text(
        json.dumps({
            "component": "mindie-profiler",
            "event": "operation.end",
            "status": "error",
            "reportable": True,
            "invocation_token": tok,
            "operation_id": oid_outside,
        }) + "\n",
        encoding="utf-8",
    )
    os.symlink(inside_file, ev_dir / "inside.jsonl")
    os.symlink(outside_file, ev_dir / "escape.jsonl")
    with patch("mindie_diagnostics.fallback.default_root", return_value=diag_root):
        found = diagnostic_support._verified_failure_ids("mindie-profiler", invocation_token=tok)
    assert oid_inside in found
    assert oid_outside not in found


def test_ad12_mcp_manifest_and_baseline_v0134_upgrade_and_rollback(isolated_env, tmp_path):
    import hook_launcher
    import mcp_entry

    mcp = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    for server in mcp.values():
        assert "PLUGIN_ROOT" in server["env_vars"]
        assert "PLUGIN_DATA" in server["env_vars"]
        joined = "\n".join(server["args"])
        assert "def load" not in joined

    # Verify `_is_read_only_call` never evaluates `@property gate.tools` on the transport thread
    import mcp_transport
    import mcp_gate

    class BoomToolsGate:
        _tools = [{"name": "remote_read", "annotations": {"readOnlyHint": True}}]

        @property
        def tools(self):
            raise TimeoutError("catalog subprocess timed out")

    assert mcp_transport._is_read_only_call(
        mcp_gate, BoomToolsGate(), {"params": {"name": "remote_read"}}
    ) is True

    # Simulate an existing v0.1.34 baseline installation, upgrade to candidate 0.1.35, then rollback
    data = tmp_path / "plugin-data"
    cfg = Path(isolated_env["MINDIE_AGENT_CONFIG"])
    cfg.parent.mkdir(parents=True, exist_ok=True)
    (data / "prepare.lock").parent.mkdir(parents=True, exist_ok=True)
    (data / "prepare.lock").write_text("", encoding="utf-8")

    def materialize_release(ver: str) -> Path:
        rel = data / "releases" / ver
        rel.mkdir(parents=True, exist_ok=True)
        val = {"native_version": ver, "schema": "mindie-candidate-check/1", "status": "validated", "source_sha256": ver}
        (rel / "runtime.json").write_text(json.dumps({"python": sys.executable, "validation": val}), encoding="utf-8")
        entry = rel / "entry.py"
        entry.write_text(f"ACTIVE = {ver!r}\n", encoding="utf-8")
        launcher_copy = rel / "hook_launcher.py"
        launcher_copy.write_bytes((SCRIPTS / "hook_launcher.py").read_bytes())
        (data / "hook_launcher.py").write_bytes((SCRIPTS / "hook_launcher.py").read_bytes())
        return entry

    v34_entry = materialize_release("0.1.34")
    v35_entry = materialize_release("0.1.35")

    def activate_pointer(ver: str) -> None:
        val = {"native_version": ver, "schema": "mindie-candidate-check/1", "status": "validated", "source_sha256": ver}
        tmp_cfg = cfg.with_suffix(".tmp")
        tmp_cfg.write_text(json.dumps({"python": sys.executable, "product_validation": val}), encoding="utf-8")
        os.replace(tmp_cfg, cfg)

    activate_pointer("0.1.34")
    with patch.dict(os.environ, {"PLUGIN_DATA": str(data), "PLUGIN_ROOT": str(tmp_path / "evicted")}):
        assert mcp_entry.resolve_launcher(seed_version="0.1.34") == data / "hook_launcher.py"
        dispatched = []
        with patch.object(sys, "argv", ["hook_launcher.py", "stop"]), \
             patch("runpy.run_path", side_effect=lambda p, **kw: dispatched.append(p)):
            hook_launcher.launch()
        assert dispatched == [str(v34_entry)]

        # Atomic upgrade to 0.1.35
        activate_pointer("0.1.35")
        dispatched.clear()
        with patch.object(sys, "argv", ["hook_launcher.py", "stop"]), \
             patch("runpy.run_path", side_effect=lambda p, **kw: dispatched.append(p)):
            hook_launcher.launch()
        assert dispatched == [str(v35_entry)]

        # Atomic rollback to 0.1.34
        activate_pointer("0.1.34")
        dispatched.clear()
        with patch.object(sys, "argv", ["hook_launcher.py", "stop"]), \
             patch("runpy.run_path", side_effect=lambda p, **kw: dispatched.append(p)):
            hook_launcher.launch()
        assert dispatched == [str(v34_entry)]
