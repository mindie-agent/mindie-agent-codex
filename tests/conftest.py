"""Keep standing-host integration selectable outside the unit CI runner."""
import os
from pathlib import Path

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "live_npu: standing A2/A3 integration")


def pytest_collection_modifyitems(items):
    for item in items:
        if item.path.name == "test_live_npu_acceptance.py":
            item.add_marker(pytest.mark.live_npu)


@pytest.fixture(autouse=True)
def isolate_host_environment(monkeypatch, tmp_path, request):
    """Give every test its own host home and XDG/MindIE state roots."""
    host = tmp_path / "host"
    home = host / "home"
    # Resolve the real home before redirecting HOME/USERPROFILE.
    real_home = Path(os.path.expanduser("~"))
    directories = {
        "HOME": home,
        "USERPROFILE": home,
        "XDG_CONFIG_HOME": host / "xdg-config",
        "XDG_DATA_HOME": host / "xdg-data",
        "XDG_STATE_HOME": host / "xdg-state",
        "CODEX_HOME": host / "codex-home",
        "MINDIE_DIAGNOSTICS_ROOT": host / "diagnostics",
        "MINDIE_REMOTE_STATE_DIR": host / "remote",
    }
    for name, path in directories.items():
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(host / "mindie-agent.json"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_CONFIG", str(host / "diagnostics.json"))
    # Native hook commands use `uv run --python 3.13`; keep uv's managed
    # interpreter directory reachable while HOME itself stays isolated.
    uv_python = (os.environ.get("UV_PYTHON_INSTALL_DIR")
                 or str(real_home / ".local/share/uv/python"))
    if Path(uv_python).is_dir():
        monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", uv_python)
    if request.node.get_closest_marker("live_npu"):
        real_ssh = real_home / ".ssh"
        if real_ssh.exists() and not (home / ".ssh").exists():
            (home / ".ssh").symlink_to(real_ssh, target_is_directory=True)
