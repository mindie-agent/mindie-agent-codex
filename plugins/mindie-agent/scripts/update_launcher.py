"""Stable launchd entry; the committed generation supplies the updater itself."""

import json
import os
from pathlib import Path
import sys

settings_path = Path(sys.argv[1])
root = Path(json.loads(settings_path.read_text())["root"])
current = json.loads((root / "state.json").read_text())["current"]
entry = Path(current["plugin"]) / "scripts/auto_update.py"
entry.resolve().relative_to((root / "generations").resolve())
os.execv(
    sys.executable,
    [sys.executable, str(entry), "--settings", str(settings_path), "check"],
)
