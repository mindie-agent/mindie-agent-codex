"""Direct file-based MCP locator forwarding to `hook_launcher.py`.
[Role] Resolve the plugin-root, durable, seeded, cache, or configured launcher and forward `--seed` plus the MCP role.
[Invariants] Pure stdlib; zero substrate, knowledge, or device imports.
[Timeouts & Locks] File-existence checks only.
[Side-Effect Policy] Strictly read-only locator before `runpy` handoff.
"""
from __future__ import annotations
import json, os, runpy, sys
from pathlib import Path
def _config_path() -> Path:
    if explicit := os.environ.get("MINDIE_AGENT_CONFIG"): return Path(explicit).expanduser()
    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") if os.name == "nt" else Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return Path(os.environ.get("MINDIE_CONFIG_HOME") or base / "mindie-agent").expanduser() / "codex.json"
def resolve_launcher(*, seed_version: str | None = None, name: str = "mindie-agent", marketplace: str = "mindie-agent") -> Path:
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    data = Path(os.environ.get("PLUGIN_DATA") or home / "plugins/data" / f"{name}-{marketplace}")
    candidates = ([Path(os.environ["PLUGIN_ROOT"]) / "scripts/hook_launcher.py"] if os.environ.get("PLUGIN_ROOT") else []) + [data / "hook_launcher.py"]
    candidates += ([data / "releases" / seed_version / "hook_launcher.py"] if seed_version else []) + [home / "plugins/cache" / marketplace / name / (seed_version or "unprepared") / "scripts/hook_launcher.py"]
    if found := next((candidate for candidate in candidates if candidate.is_file()), None):
        return found
    try: raw = _config_path().read_bytes(); parsed = json.loads(raw.decode("utf-8-sig")) if len(raw) <= 65536 else None
    except TimeoutError: raise
    except (OSError, ValueError): parsed = None
    if isinstance(parsed, dict):
        release, version = parsed.get("component_release"), (parsed.get("product_validation") or {}).get("native_version")
        for candidate in (Path(release) / "hook_launcher.py" if isinstance(release, str) else None, data / "releases" / version / "hook_launcher.py" if isinstance(version, str) else None):
            if candidate is not None and candidate.is_file(): return candidate
    raise RuntimeError("MindIE launcher is unavailable")
def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    launcher = resolve_launcher(seed_version=next((args[index + 1] for index, token in enumerate(args[:-1]) if token == "--seed"), None))
    sys.argv = [str(launcher), *args]; runpy.run_path(str(launcher), run_name="__main__")
if __name__ == "__main__":
    main()
