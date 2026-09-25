"""Stable launcher. The committed generation supplies the updater.

Scheduler invocation stays `launcher.py <settings>` and runs check.
Management operations are only check, status, disable, and uninstall
(--purge only with uninstall). Anything else is rejected before exec.
"""

import json
import os
from pathlib import Path
import sys


def _fail(message):
    sys.stderr.write(message + "\n")
    raise SystemExit(2)


def _operation(argv):
    if len(argv) < 2 or not argv[1]:
        _fail(
            "usage: update_launcher.py <settings> "
            "[check|status|disable|uninstall [--purge]]"
        )
    extra = tuple(argv[2:])
    allowed = {
        (): ("check",),
        ("check",): ("check",),
        ("status",): ("status",),
        ("disable",): ("disable",),
        ("uninstall",): ("uninstall",),
        ("uninstall", "--purge"): ("uninstall", "--purge"),
    }
    if extra not in allowed:
        _fail("unsupported launcher operation")
    return allowed[extra]


def _current_updater(settings_path):
    try:
        settings = json.loads(settings_path.read_text())
        if not isinstance(settings, dict) or not isinstance(settings.get("root"), str):
            raise ValueError("settings")
        root = Path(settings["root"])
        state = json.loads((root / "state.json").read_text())
        if not isinstance(state, dict):
            raise ValueError("state")
        current = state.get("current")
        if not isinstance(current, dict):
            raise ValueError("current")
        plugin = current.get("plugin")
        if not isinstance(plugin, str) or not plugin or not Path(plugin).is_absolute():
            raise ValueError("plugin")
        entry = (Path(plugin) / "scripts" / "auto_update.py").resolve()
        entry.relative_to((root / "generations").resolve())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        _fail("invalid current generation")
    if not entry.is_file():
        _fail("invalid current generation")
    return entry


def main(argv):
    operation = _operation(argv)
    settings_path = Path(argv[1]).expanduser()
    try:
        settings_path = settings_path.resolve()
    except OSError:
        _fail("cannot read updater settings")
    if not settings_path.is_file():
        _fail("cannot read updater settings")
    entry = _current_updater(settings_path)
    os.execv(
        sys.executable,
        [sys.executable, str(entry), "--settings", str(settings_path), *operation],
    )


if __name__ == "__main__":
    main(sys.argv)
