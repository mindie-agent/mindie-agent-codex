"""Thin-plugin contract: frozen hooks, LOC ceiling, seed, locator, release metadata."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/mindie-agent"
SCRIPTS = PLUGIN / "scripts"
HOOKS_SHA256 = "ea80d0815485ba5f762ff5275f1e40817130694870405237dbd65d03f28f9981"


def test_hooks_manifest_byte_frozen():
    payload = (PLUGIN / "hooks/hooks.json").read_bytes()
    assert len(payload) == 25650
    assert hashlib.sha256(payload).hexdigest() == HOOKS_SHA256


def test_scripts_line_ceiling():
    files = list(SCRIPTS.glob("*.py")) + [PLUGIN / "__init__.py"]
    files += [path for path in PLUGIN.rglob("*") if path.suffix in {".js", ".mjs", ".cjs"}
              and "tests" not in path.parts]
    lines = sum(len(path.read_text(encoding="utf-8").splitlines()) for path in files)
    assert lines <= 1200, f"thin plugin scripts exceeded the ceiling: {lines}"


def test_bundled_seed_and_host_adapter():
    import mindie_harness.api as api
    from host_adapter import CodexHostAdapter, SEED_SCHEMA, verify_bundled_seed

    info = verify_bundled_seed(PLUGIN)
    assert info["schema"] == SEED_SCHEMA
    assert Path(info["path"]).is_file()
    assert isinstance(CodexHostAdapter(plugin_root=PLUGIN), api.HostAdapter)


def test_mcp_locator_outside_plugin(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "with_runtime.py").write_text("raise RuntimeError('workspace import hijack')\n", encoding="utf-8", newline="\n")
    plugin_root = tmp_path / "empty-plugin"
    plugin_root.mkdir()
    plugin_data = tmp_path / "data"
    plugin_data.mkdir()
    launcher = plugin_data / "hook_launcher.py"
    launcher.write_text((SCRIPTS / "hook_launcher.py").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    servers = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    remote = servers["mindie-remote-dev"]
    assert servers["mindie-remote-dev"]["env"]["REMOTE_DEV_HARNESS_PROFILE"] == "codex"
    args = list(remote["args"])
    inline = args[args.index("-c") + 1]
    seed = args[args.index("--seed") + 1]
    assert seed == "0.1.38"
    env = dict(os.environ, PLUGIN_ROOT=str(plugin_root), PLUGIN_DATA=str(plugin_data),
               CODEX_HOME=str(tmp_path / "codex-home"), MINDIE_AGENT_CONFIG=str(tmp_path / "missing.json"),
               PYTHONPATH="")
    result = subprocess.run([sys.executable, "-B", "-c", inline, "--seed", seed, "user-prompt-submit"],
                            cwd=outside, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "{}\n"


def test_release_metadata_cli():
    result = subprocess.run([sys.executable, str(SCRIPTS / "setup.py"), "release-metadata",
                             "--source", str(PLUGIN), "--tests", str(ROOT / "tests"),
                             "--revision", "a" * 40], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["schema"] == "mindie-plugin-release/1"
    assert value["version"] == "0.1.38"
    assert value["source"] == {"repository": "mindie-agent/mindie-agent-codex", "sha": "a" * 40}
    assert value["compatibility"]["harness"] == "codex"
