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
import shutil
import stat

KEYS = {"CODEX_HOME", "MINDIE_CODEX_BIN"}
TRANSPORT_ENV = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "CODEX_CA_CERTIFICATE",
)


def environment(adapter):
    value = adapter.get("native_context", {})
    if (not isinstance(value, dict) or set(value) - KEYS
            or any(not isinstance(path, str) or not Path(path).is_absolute()
                   for path in value.values())):
        raise ValueError("invalid installation native context")
    return dict(value)


def capture(*, home=None, binary=None):
    result = {"CODEX_HOME": str(Path(home or os.environ.get("CODEX_HOME")
                                    or Path.home() / ".codex").expanduser().absolute())}
    binary = binary or os.environ.get("MINDIE_CODEX_BIN") or shutil.which("codex")
    if binary:
        expanded = Path(binary).expanduser()
        if not expanded.is_absolute() and not os.path.dirname(os.fspath(binary)):
            resolved = shutil.which(str(expanded))
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
