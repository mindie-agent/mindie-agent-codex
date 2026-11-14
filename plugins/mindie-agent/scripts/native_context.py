"""Installation-owned native paths; no credentials or full environment snapshots.

[Role]
Captures and validates explicit native Codex paths (`CODEX_HOME`, `MINDIE_CODEX_BIN`), proxy/TLS transport environment variables (`TRANSPORT_ENV`), and resolves the `codex` binary (`codex_executable`).

[Invariants]
- Never snapshots credentials or full `os.environ`; only `CODEX_HOME` and `MINDIE_CODEX_BIN` are persisted in `native_context`.

[Timeouts & Locks]
- Synchronous filesystem checks; zero subprocesses or locks.

[Side-Effect Policy]
- Purely read-only over environment variables and executable paths.
"""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct

KEYS = {"CODEX_HOME", "MINDIE_CODEX_BIN"}
_WIN_SUFFIXES = frozenset({".exe", ".cmd", ".bat", ".ps1", ".com"})
_WSL_DRVFS_RE = re.compile(r"^/mnt/[a-zA-Z](?:/|$)")
_REAL_WHICH = shutil.which


def in_wsl() -> bool:
    """Return True when POSIX Python is running inside a WSL2 Linux environment."""
    if os.name != "posix":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists()
    except TimeoutError:
        raise
    except OSError:
        return False


def _has_pe_header(path: Path) -> bool:
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size < 64:
            return False
        head = os.read(fd, 64)
        if len(head) < 64 or head[:2] != b"MZ":
            return False
        pe_off = struct.unpack_from("<I", head, 0x3C)[0]
        if pe_off < 64 or pe_off + 4 > info.st_size or pe_off > 16 * 1024 * 1024:
            return False
        os.lseek(fd, pe_off, os.SEEK_SET)
        return os.read(fd, 4) == b"PE\x00\x00"
    except TimeoutError:
        raise
    except OSError:
        return False
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


_WINAPPS_STUB_NAMES = frozenset({"python", "python.exe", "python3", "python3.exe", "bash", "bash.exe"})


def _is_windowsapps_stub(candidate) -> bool:
    if not candidate or os.name != "nt":
        return False
    norm = os.fspath(candidate).replace("/", "\\").lower()
    if "\\microsoft\\windowsapps\\" not in norm:
        return False
    if norm.rsplit("\\", 1)[-1] in _WINAPPS_STUB_NAMES:
        return True
    try:
        return os.stat(candidate).st_size == 0
    except TimeoutError:
        raise
    except OSError:
        return True


def is_windows_host_binary(candidate) -> bool:
    """Reject Windows PE/.exe/DrvFS shims when discovering POSIX/WSL host CLIs."""
    if not candidate or os.name != "posix":
        return False
    raw = os.fspath(candidate)
    if Path(raw).suffix.lower() in _WIN_SUFFIXES:
        return True
    norm = os.path.normpath(raw)
    if in_wsl() and _WSL_DRVFS_RE.match(norm):
        return True
    return _has_pe_header(Path(raw))


def which_host_binary(name: str, *, path: str | None = None, which_fn=None) -> str | None:
    """Resolve a host CLI without WSL2 `/mnt/<drive>` shims or WindowsApps alias stubs."""
    lookup = which_fn or shutil.which
    found = lookup(name) if path is None else lookup(name, path=path)
    if found is None:
        return None
    rejected = _is_windowsapps_stub if os.name == "nt" else (is_windows_host_binary if os.name == "posix" else None)
    if rejected is None or not rejected(found):
        return found
    search = os.environ.get("PATH", "") if path is None else path
    wsl = os.name == "posix" and in_wsl()
    for entry in search.split(os.pathsep):
        if not entry:
            continue
        if wsl and _WSL_DRVFS_RE.match(os.path.normpath(entry)):
            continue
        cand = _REAL_WHICH(name, path=entry)
        if cand and not rejected(cand):
            return cand
    return None
TRANSPORT_ENV = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "CODEX_CA_CERTIFICATE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "GIT_SSL_CAPATH",
    "SSL_CLIENT_CERT", "UV_CONFIG_FILE", "UV_PYTHON_INSTALL_MIRROR", "UV_PYPY_INSTALL_MIRROR",
    "UV_SYSTEM_CERTS", "UV_NATIVE_TLS", "UV_INDEX", "UV_DEFAULT_INDEX",
    "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL",
    "PIP_CERT", "PIP_CONFIG_FILE", "GH_CONFIG_DIR", "GH_HOST", "GH_TOKEN", "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
    "MINDIE_GIT_BIN", "GCM_INTERACTIVE",
)


def _desktop_replacement(configured):
    """Desktop replaces its versioned bin/<build_id>/codex(.exe) directory on update."""
    path = Path(configured)
    if path.is_file():
        return str(path)
    try:
        candidates = [entry / path.name for entry in path.parent.parent.iterdir()
                      if entry.is_dir() and (entry / path.name).is_file()]
        if candidates:
            return str(max(candidates, key=lambda item: item.stat().st_mtime))
    except TimeoutError:
        raise
    except OSError:
        return None
    return None


def environment(adapter):
    value = adapter.get("native_context", {})
    if (not isinstance(value, dict) or set(value) - KEYS
            or any(not isinstance(path, str) or not Path(path).is_absolute()
                   for path in value.values())):
        raise ValueError("invalid installation native context")
    result = dict(value)
    running = os.environ.get("CODEX_APP_RUNTIME_CLI_PATH")
    if (running and Path(running).is_absolute() and Path(running).is_file()
            and not is_windows_host_binary(running)):
        # Desktop owns this path and replaces its versioned binary directory
        # during updates. Keep the selected profile, but use its running host.
        result["MINDIE_CODEX_BIN"] = running
    elif "MINDIE_CODEX_BIN" in result:
        replacement = _desktop_replacement(result["MINDIE_CODEX_BIN"])
        if replacement is not None:
            result["MINDIE_CODEX_BIN"] = replacement
    return result


def capture(*, home=None, binary=None):
    result = {"CODEX_HOME": str(Path(home or os.environ.get("CODEX_HOME")
                                    or Path.home() / ".codex").expanduser().absolute())}
    app_cli = os.environ.get("CODEX_APP_RUNTIME_CLI_PATH")
    if app_cli and is_windows_host_binary(app_cli):
        app_cli = None
    binary = (binary or os.environ.get("MINDIE_CODEX_BIN")
              or app_cli or which_host_binary("codex", which_fn=shutil.which))
    if binary:
        expanded = Path(binary).expanduser()
        if not expanded.is_absolute() and not os.path.dirname(os.fspath(binary)):
            resolved = which_host_binary(str(expanded), which_fn=shutil.which)
            if not resolved:
                raise ValueError("selected native executable is unavailable")
            expanded = Path(resolved)
        result["MINDIE_CODEX_BIN"] = str(expanded.absolute())
    return result


def service_launch_context(config, adapter):
    """Bind installed paths and allowed names, without persisting secret values."""
    from diagnostic_support import environment_context
    from summary_provider import environment_keys

    native = environment(adapter)
    paths = dict(native, **environment_context(adapter, config))
    paths["MINDIE_AGENT_CONFIG"] = str(Path(config).expanduser().absolute())
    names = sorted(set((*TRANSPORT_ENV, *environment_keys(native.get("CODEX_HOME")))))
    # The selected runtime validates this declaration during prepare-store,
    # before installation publishes it. Setup itself remains stdlib-only.
    return dict(paths=paths, environment=names)


def read(config):
    try:
        fd = os.open(config, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return {}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError("invalid installation native configuration")
        raw = os.read(fd, 65537)
        if len(raw) > 65536:
            raise ValueError("invalid installation native configuration")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("invalid installation native configuration")
        return environment(value)
    finally:
        os.close(fd)
