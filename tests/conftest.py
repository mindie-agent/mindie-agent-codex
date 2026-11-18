"""Isolated, fast host-environment fixtures for the thin-plugin contract suite."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def pytest_configure(config):
    config.addinivalue_line("markers", "live_npu: standing A2/A3 integration")


@pytest.fixture(autouse=True)
def isolate_host_environment(monkeypatch, tmp_path, request):
    host = tmp_path / "host"
    home = host / "home"
    directories = {
        "HOME": home, "USERPROFILE": home, "APPDATA": host / "appdata",
        "LOCALAPPDATA": host / "localappdata", "XDG_CONFIG_HOME": host / "xdg-config",
        "XDG_DATA_HOME": host / "xdg-data", "XDG_STATE_HOME": host / "xdg-state",
        "MINDIE_CONFIG_HOME": host / "mindie-config", "MINDIE_DATA_HOME": host / "mindie-data",
        "MINDIE_DIAGNOSTICS_ROOT": host / "diagnostics",
                "CODEX_HOME": host / "codex-home", "PLUGIN_DATA": host / "plugin-data",
        "REMOTE_DEV_AUTH_DIR": host / "remote-auth", "UV_CACHE_DIR": host / "uv-cache",
    }
    for name, path in directories.items():
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(host / "mindie-config" / "mindie-agent" / "codex.json"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_CONFIG", str(host / "diagnostics-config.json"))
    temporary = tempfile.TemporaryDirectory(prefix="mindie-thin-")
    request.addfinalizer(temporary.cleanup)
    monkeypatch.setenv("TMPDIR", temporary.name)
    monkeypatch.setenv("TEMP", temporary.name)
    monkeypatch.setenv("TMP", temporary.name)
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.delenv("MINDIE_CODEX_BIN", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    yield home
