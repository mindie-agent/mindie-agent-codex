"""Select the prepared native entry for Codex hooks and MCP startup.
[Role] Implement the frozen `hooks.json` fast paths and the `.mcp.json` seed-aware selector.
[Invariants] `user-prompt-submit` never reads stdin; cold and warm `session-start` write only `pending-update-check.json` without `uv sync` or SQLite; disabled/out-of-scope `stop` never imports `mindie_harness` or creates SQLite.
[Timeouts & Locks] No locks or third-party imports; selected entries own deadlines.
[Side-Effect Policy] `session-start` writes one atomic pending-check marker in `ProfileRef.state_dir`.
"""
from __future__ import annotations
import json, os, re, runpy, subprocess, sys, tempfile, time
from pathlib import Path
MCP_ROLES, VERSION, _NO_WIN = ("remote-mcp", "tools-mcp", "knowledge-mcp"), re.compile(r"[0-9A-Za-z][0-9A-Za-z.+!-]{0,79}"), (0x08000000 if os.name == "nt" else 0)
def _config_path() -> Path:
    if explicit := os.environ.get("MINDIE_AGENT_CONFIG"): return Path(explicit).expanduser().resolve()
    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") if os.name == "nt" else Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return (Path(os.environ.get("MINDIE_CONFIG_HOME") or base / "mindie-agent").expanduser() / "codex.json").resolve()
def _data_dir(name: str, marketplace: str) -> Path:
    return Path(os.environ["PLUGIN_DATA"]).expanduser() if os.environ.get("PLUGIN_DATA") else Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "plugins/data" / f"{name}-{marketplace}"
def _state_dir() -> Path:
    if root := os.environ.get("MINDIE_DATA_HOME"): return Path(os.path.abspath(os.path.expanduser(root))) / "state/codex"
    raw_home = (os.environ.get("USERPROFILE") or os.environ.get("HOME")) if os.name == "nt" else (os.environ.get("HOME") or os.environ.get("USERPROFILE"))
    home = Path(raw_home).expanduser() if raw_home else Path.home()
    base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData/Local") if os.name == "nt" else Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    return Path(os.path.abspath(os.path.expanduser(str(base / "mindie-agent")))) / "state/codex"
def read_json(path: Path) -> dict | None:
    try: raw = path.read_bytes(); value = json.loads(raw.decode("utf-8-sig")) if len(raw) <= 65536 else None
    except TimeoutError: raise
    except (OSError, ValueError): return None
    return value if isinstance(value, dict) else None
def parse_argv(argv):
    name, marketplace, seed, idx = "mindie-agent", "mindie-agent", None, 0
    while idx < len(argv):
        tok = argv[idx]
        if tok in {"--name", "--marketplace", "--seed"} and idx + 1 < len(argv):
            if tok == "--name": name = argv[idx + 1]
            elif tok == "--marketplace": marketplace = argv[idx + 1]
            else: seed = argv[idx + 1]
            idx += 2
        else: return name, marketplace, seed, list(argv[idx + 1:] if tok == "--" else argv[idx:])
    return name, marketplace, seed, []
def _version(value):
    return tuple(int(p) if p.isdigit() else 0 for p in value.split(".")[:3]) if isinstance(value, str) and VERSION.fullmatch(value) else None
def _roots(adapter: dict, config: Path) -> list[str]:
    cpath = Path(adapter["community_config"]) if isinstance(adapter.get("community_config"), str) and os.path.isabs(adapter["community_config"]) else config.parent / "mindie-community.json"
    community, consent_path = read_json(cpath), config.parent / "mindie-consent.json"
    for src in (community, adapter):
        if isinstance((raw := (src or {}).get("consent_config")), str) and os.path.isabs(raw): consent_path = Path(raw); break
    consent = read_json(consent_path); allowed = (consent or {}).get("choice") in {"contribute", "local-only"} or ("choice" not in (consent or {}) and (consent or {}).get("enabled") is True)
    src = (community if community.get("enabled") is True and allowed else None) if community is not None else (consent if consent is not None and allowed else None)
    values = (src or {}).get("project_roots")
    return [os.path.normcase(os.path.abspath(v)) for v in values] if isinstance(values, list) and values and all(isinstance(v, str) and os.path.isabs(v) for v in values) else []
def _in_scope(raw: bytes, roots: list[str]) -> bool:
    try: event = json.loads(raw.decode("utf-8-sig")) if 0 < len(raw) <= 1048576 and raw.strip() else None
    except ValueError: return False
    if not isinstance(event, dict): return False
    keys = ("cwd",) if isinstance(event.get("cwd"), str) and os.path.isabs(event["cwd"]) else ("cwd", "workspace", "project_root")
    for key in keys:
        if not (isinstance(val := event.get(key), str) and os.path.isabs(val)): continue
        cand = os.path.normcase(os.path.abspath(val))
        for root in roots:
            try:
                if os.path.splitdrive(cand)[0] == os.path.splitdrive(root)[0] and os.path.commonpath([cand, root]) == root: return True
            except ValueError: continue
    return False
def _active_python(adapter: dict) -> Path | None: return Path(raw) if isinstance(raw := adapter.get("python"), str) and Path(raw).is_file() else None
def _active_entry(adapter: dict, data: Path) -> tuple[Path, Path] | None:
    ver = (adapter.get("product_validation") or {}).get("native_version")
    for root in (adapter.get("runtime_scripts"), adapter.get("component_release"), str(data / "releases" / ver) if isinstance(ver, str) else None):
        if isinstance(root, str) and os.path.isabs(root) and (entry := Path(root) / "entry.py").is_file(): return entry, entry.parent
    return None
def _source(home: Path, marketplace: str, name: str, seed: str | None) -> Path:
    if explicit := os.environ.get("PLUGIN_ROOT"): return Path(explicit)
    cached = home / "plugins/cache" / marketplace / name / (seed or "unprepared")
    return cached if cached.is_dir() else Path(__file__).resolve().parents[1]
def _write_pending() -> None:
    state = _state_dir()
    try: state.mkdir(mode=0o700, parents=True, exist_ok=True); fd, raw = tempfile.mkstemp(prefix=".pending.", dir=state)
    except TimeoutError: raise
    except OSError: return
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as s: json.dump({"schema": "mindie-pending-update/1", "host": "codex", "reason": "session_start", "requested_at": time.time()}, s); s.write("\n")
        os.replace(tmp, state / "pending-update-check.json")
    except TimeoutError: raise
    except OSError: return
    finally: tmp.unlink(missing_ok=True)
def _emit() -> int: sys.stdout.write("{}\n"); sys.stdout.flush(); return 0
def _execute(python: Path, argv: list[str]) -> int:
    if os.name != "nt": os.execv(str(python), [str(python), *argv])
    try: sys.stdin.fileno(); sys.stdout.fileno(); sys.stderr.fileno(); io_kw = {"stdin": sys.stdin, "stdout": sys.stdout, "stderr": sys.stderr}
    except Exception: io_kw = {}
    return subprocess.run([str(python), *argv], **io_kw, creationflags=_NO_WIN).returncode
def fast_path(role: str, *, name: str, marketplace: str, config: Path, data: Path) -> int | None:
    if role == "user-prompt-submit": return _emit()
    if role == "session-start": _write_pending(); return _emit()
    if role == "stop":
        adapter = read_json(config)
        if not isinstance(adapter, dict) or not (roots := _roots(adapter, config)): return _emit()
        raw = sys.stdin.buffer.read(1048577)
        if not _in_scope(raw, roots) or (python := _active_python(adapter)) is None or (selected := _active_entry(adapter, data)) is None: return _emit()
        try: subprocess.run([str(python), "-I", "-X", "utf8", str(selected[0]), "--release", str(selected[1]), "stop"], input=raw, capture_output=True, timeout=8.0, creationflags=_NO_WIN)
        except subprocess.TimeoutExpired: pass
        return _emit()
    return None
def _selection(*, name: str, marketplace: str, seed: str | None, args: list[str]):
    home, data, config = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"), _data_dir(name, marketplace), _config_path()
    adapter, source = read_json(config), _source(home, marketplace, name, seed)
    if isinstance(adapter, dict) and _active_python(adapter) is not None:
        installed, target = _version((adapter.get("product_validation") or {}).get("native_version")), _version(seed)
        if seed and args[:1] and args[0] in MCP_ROLES and installed is not None and target is not None and target > installed and (source / "scripts/bootstrap.py").is_file():
            return source / "scripts/bootstrap.py", [str(data), "--source", str(source), "migrate", *args], None
        if (selected := _active_entry(adapter, data)) is not None: return selected[0], args, _active_python(adapter)
    if (bootstrap := source / "scripts/bootstrap.py").is_file(): return bootstrap, [str(data), "--source", str(source), *args], None
    if not (source / "pyproject.toml").is_file() and (seed_archive := source / "assets/mindie-harness-seed.pyz").is_file():
        return seed_archive, ["--host", "codex", "--plugin-root", str(source), "--config", str(config), *args], Path(sys.executable)
    raise RuntimeError("MindIE runtime is unavailable; install the plugin before starting a session")
def select_entry(*, name="mindie-agent", marketplace="mindie-agent", seed=None, args=None): return _selection(name=name, marketplace=marketplace, seed=seed, args=list(args or []))[:2]
def launch(argv=None) -> int:
    name, marketplace, seed, args = parse_argv(sys.argv[1:] if argv is None else argv)
    if args and (code := fast_path(args[0], name=name, marketplace=marketplace, config=_config_path(), data=_data_dir(name, marketplace))) is not None: return code
    entry, forwarded, python = _selection(name=name, marketplace=marketplace, seed=seed, args=args)
    if python is not None: return _execute(python, ["-I", "-X", "utf8", str(entry), *forwarded])
    sys.argv = [str(entry), *forwarded]; runpy.run_path(str(entry), run_name="__main__"); return 0
if __name__ == "__main__": raise SystemExit(launch())
