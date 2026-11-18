"""Keep standing-host integration selectable outside the unit CI runner."""
import os
from pathlib import Path
import shutil
import tempfile

import pytest


def _link_or_copy(src, dst):
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


@pytest.fixture(scope="session")
def _session_gitleaks_seed(tmp_path_factory):
    """Session-local read-only copy of verified Gitleaks cache to avoid repeated GitHub downloads."""
    seed = tmp_path_factory.mktemp("gitleaks-seed") / "gitleaks"
    real_cache = Path(os.path.expanduser("~")) / ".cache" / "mindie-agent" / "gitleaks"
    if real_cache.is_dir():
        shutil.copytree(real_cache, seed)
    return seed


def pytest_configure(config):
    config.addinivalue_line("markers", "live_npu: standing A2/A3 integration")


def pytest_collection_modifyitems(items):
    for item in items:
        if item.path.name == "test_live_npu_acceptance.py":
            item.add_marker(pytest.mark.live_npu)


@pytest.fixture(autouse=True)
def isolate_host_environment(monkeypatch, tmp_path, request, _session_gitleaks_seed):
    """Give every test its own host home and XDG/MindIE state roots."""
    host = tmp_path / "host"
    home = host / "home"
    # Resolve the real home before redirecting HOME/USERPROFILE.
    real_home = Path(os.path.expanduser("~"))
    monkeypatch.delenv("CODEX_APP_RUNTIME_CLI_PATH", raising=False)
    short_tmp_parent = "/tmp" if os.name == "posix" and Path("/tmp").is_dir() else None
    short_tmp = tempfile.TemporaryDirectory(prefix="ma-t-", dir=short_tmp_parent)
    request.addfinalizer(short_tmp.cleanup)
    short_tmp_path = Path(short_tmp.name)
    directories = {
        "HOME": home,
        "USERPROFILE": home,
        "APPDATA": host / "appdata",
        "LOCALAPPDATA": host / "localappdata",
        "XDG_CONFIG_HOME": host / "xdg-config",
        "XDG_DATA_HOME": host / "xdg-data",
        "XDG_STATE_HOME": host / "xdg-state",
        "XDG_CACHE_HOME": host / "xdg-cache",
        "UV_CACHE_DIR": host / "uv-cache",
        "REMOTE_DEV_AUTH_DIR": host / "remote-auth",
        "CODEX_HOME": host / "codex-home",
        "MINDIE_DIAGNOSTICS_ROOT": host / "diagnostics",
        "MINDIE_CONFIG_HOME": host / "mindie-config",
        "MINDIE_DATA_HOME": host / "mindie-data",
        "MINDIE_REMOTE_STATE_DIR": host / "remote",
        "TMPDIR": short_tmp_path,
        "TEMP": short_tmp_path,
        "TMP": short_tmp_path,
    }
    for name, path in directories.items():
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(path))
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(host / "mindie-agent.json"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_CONFIG", str(host / "diagnostics.json"))
    import sys as _sys
    if "session_gate" in _sys.modules:
        monkeypatch.setattr(_sys.modules["session_gate"], "_explicit_config", None, raising=False)
    # Native hook commands use `uv run --python 3.13`; keep uv's managed
    # interpreter directory reachable while HOME itself stays isolated.
    uv_python = (os.environ.get("UV_PYTHON_INSTALL_DIR")
                 or str(real_home / ".local/share/uv/python"))
    if Path(uv_python).is_dir():
        monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", uv_python)
    if _session_gitleaks_seed.is_dir():
        target_gl = home / ".cache" / "mindie-agent" / "gitleaks"
        if not target_gl.exists():
            target_gl.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(_session_gitleaks_seed, target_gl, copy_function=_link_or_copy)
    if request.node.get_closest_marker("live_npu"):
        real_ssh = real_home / ".ssh"
        if real_ssh.exists() and not (home / ".ssh").exists():
            (home / ".ssh").symlink_to(real_ssh, target_is_directory=True)
