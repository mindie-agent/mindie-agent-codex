"""Installation-owned native paths; no credentials or full environment snapshots."""
import json
import os
from pathlib import Path
import shutil
import stat

KEYS = {"CODEX_HOME", "MINDIE_CODEX_BIN"}


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
