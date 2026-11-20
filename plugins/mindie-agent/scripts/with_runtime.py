"""Execute a script with the configured MindIE runtime interpreter.
[Role] Resolve `MINDIE_AGENT_CONFIG`, select its configured `"python"` (or worktree `.venv` / `sys.executable`), strip `PYTHONPATH`, and `execv` the target on POSIX.
[Invariants] Every Windows spawn passes `CREATE_NO_WINDOW`; missing harness is tolerated.
[Timeouts & Locks] None; the target script owns its deadlines.
[Side-Effect Policy] Read-only configuration lookup before executing the target.
"""
from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
def _config() -> Path:
    if explicit := os.environ.get("MINDIE_AGENT_CONFIG"): return Path(explicit).expanduser()
    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") if os.name == "nt" else Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return Path(os.environ.get("MINDIE_CONFIG_HOME") or base / "mindie-agent").expanduser() / "codex.json"
def selected_python() -> str:
    try: value = json.loads(_config().read_bytes().decode("utf-8-sig"))
    except TimeoutError: raise
    except (OSError, ValueError): value = None
    python = value.get("python") if isinstance(value, dict) else None
    if isinstance(python, str) and Path(python).is_file(): return python
    local = Path(__file__).resolve().parents[3] / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(local) if local.is_file() else sys.executable
def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args: print("usage: with_runtime.py <script.py> [args...]", file=sys.stderr); return 2
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}; python = selected_python(); command = [python, "-I", "-X", "utf8", *args]
    if os.name != "nt": os.execve(python, command, env)
    return subprocess.run(command, env=env, creationflags=CREATE_NO_WINDOW).returncode
if __name__ == "__main__": raise SystemExit(main())
