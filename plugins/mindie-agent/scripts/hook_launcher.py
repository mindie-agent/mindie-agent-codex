"""Select the prepared native entry for Codex hooks and MCP startup.
[Role] Implement the frozen `hooks.json` fast paths and the `.mcp.json` seed-aware selector.
[Invariants] `user-prompt-submit` never reads stdin; disabled/out-of-scope `stop` never reads stdin, imports `mindie_harness`, or creates SQLite; hooks never migrate.
[Timeouts & Locks] No locks or third-party imports; selected entries own deadlines.
[Side-Effect Policy] `session-start` writes one atomic pending-check marker.
"""
from __future__ import annotations
import json, os, re, runpy, subprocess, sys, tempfile, time
from pathlib import Path
MCP_ROLES = ("remote-mcp", "tools-mcp", "knowledge-mcp")
VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+!-]{0,79}")
_NO_WIN = 0x08000000 if os.name == "nt" else 0
def _config_path() -> Path:
    if explicit := os.environ.get("MINDIE_AGENT_CONFIG"):
        return Path(explicit).expanduser().resolve()
    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") if os.name == "nt" else Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return (Path(os.environ.get("MINDIE_CONFIG_HOME") or base / "mindie-agent").expanduser() / "codex.json").resolve()
def _data_dir(name: str, marketplace: str) -> Path:
    return Path(os.environ["PLUGIN_DATA"]).expanduser() if os.environ.get("PLUGIN_DATA") else Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "plugins/data" / f"{name}-{marketplace}"
def _state_dir() -> Path:
    root = os.environ.get("MINDIE_DATA_HOME")
    if not root:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") if os.name == "nt" else Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        root = str(base / "mindie-agent")
    return Path(root).expanduser() / "state/codex"
def read_json(path: Path) -> dict | None:
    try:
        raw = path.read_bytes(); value = json.loads(raw.decode("utf-8-sig")) if len(raw) <= 65536 else None
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None
def parse_argv(argv):
    name, marketplace, seed, index = "mindie-agent", "mindie-agent", None, 0
    while index < len(argv):
        token = argv[index]
        if token in {"--name", "--marketplace", "--seed"} and index + 1 < len(argv):
            if token == "--name": name = argv[index + 1]
            elif token == "--marketplace": marketplace = argv[index + 1]
            else: seed = argv[index + 1]
            index += 2
        elif token == "--":
            return name, marketplace, seed, list(argv[index + 1:])
        else:
            return name, marketplace, seed, list(argv[index:])
    return name, marketplace, seed, []
def _version(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value): return None
    return tuple(int(part) if part.isdigit() else 0 for part in value.split(".")[:3])
def _roots(adapter: dict, config: Path) -> list[str]:
    community_path = Path(adapter["community_config"]) if isinstance(adapter.get("community_config"), str) and os.path.isabs(adapter["community_config"]) else config.parent / "mindie-community.json"
    community, consent_path = read_json(community_path), config.parent / "mindie-consent.json"
    for source in (community, adapter):
        if isinstance((raw := (source or {}).get("consent_config")), str) and os.path.isabs(raw):
            consent_path = Path(raw); break
    consent = read_json(consent_path); allowed = (consent or {}).get("choice") in {"contribute", "local-only"} or ("choice" not in (consent or {}) and (consent or {}).get("enabled") is True)
    if community is not None:
        if community.get("enabled") is not True or not allowed:
            return []
        source = community
    elif consent is not None and allowed:
        source = consent
    else:
        return []
    values = source.get("project_roots")
    if not isinstance(values, list) or not values or not all(isinstance(value, str) and os.path.isabs(value) for value in values):
        return []
    return [os.path.normcase(os.path.abspath(value)) for value in values]
def _in_scope(raw: bytes, roots: list[str]) -> bool:
    try:
        event = json.loads(raw.decode("utf-8-sig")) if 0 < len(raw) <= 1048576 and raw.strip() else None
    except ValueError:
        return False
    if not isinstance(event, dict):
        return False
    keys = ("cwd",) if isinstance(event.get("cwd"), str) and os.path.isabs(event["cwd"]) else ("cwd", "workspace", "project_root")
    for key in keys:
        value = event.get(key)
        if not isinstance(value, str) or not os.path.isabs(value):
            continue
        candidate = os.path.normcase(os.path.abspath(value))
        for root in roots:
            try:
                if os.path.splitdrive(candidate)[0] == os.path.splitdrive(root)[0] and os.path.commonpath([candidate, root]) == root:
                    return True
            except ValueError:
                continue
    return False
def _active_python(adapter: dict) -> Path | None:
    raw = adapter.get("python")
    return Path(raw) if isinstance(raw, str) and Path(raw).is_file() else None
def _active_entry(adapter: dict, data: Path) -> tuple[Path, Path] | None:
    version = (adapter.get("product_validation") or {}).get("native_version")
    for root in (adapter.get("runtime_scripts"), adapter.get("component_release"), str(data / "releases" / version) if isinstance(version, str) else None):
        if isinstance(root, str) and os.path.isabs(root) and (entry := Path(root) / "entry.py").is_file():
            return entry, entry.parent
    return None
def _source(home: Path, marketplace: str, name: str, seed: str | None) -> Path:
    if explicit := os.environ.get("PLUGIN_ROOT"): return Path(explicit)
    cached = home / "plugins/cache" / marketplace / name / (seed or "unprepared")
    return cached if cached.is_dir() else Path(__file__).resolve().parents[1]
def _write_pending() -> None:
    state = _state_dir()
    try:
        state.mkdir(mode=0o700, parents=True, exist_ok=True); descriptor, raw = tempfile.mkstemp(prefix=".pending.", dir=state)
    except TimeoutError:
        raise
    except OSError:
        return
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump({"schema": "mindie-pending-update/1", "host": "codex", "reason": "session_start", "requested_at": time.time()}, stream); stream.write("\n")
        os.replace(temporary, state / "pending-update-check.json")
    except TimeoutError:
        raise
    except OSError:
        return
    finally:
        temporary.unlink(missing_ok=True)
def _emit() -> int: sys.stdout.write("{}\n"); sys.stdout.flush(); return 0
def _execute(python: Path, argv: list[str]) -> int:
    if os.name != "nt": os.execv(str(python), [str(python), *argv])
    try: sys.stdin.fileno(); sys.stdout.fileno(); sys.stderr.fileno(); io_kw = {"stdin": sys.stdin, "stdout": sys.stdout, "stderr": sys.stderr}
    except Exception: io_kw = {}
    return subprocess.run([str(python), *argv], **io_kw, creationflags=_NO_WIN).returncode
def fast_path(role: str, *, name: str, marketplace: str, config: Path, data: Path) -> int | None:
    if role == "user-prompt-submit":
        return _emit()
    adapter = read_json(config)
    if role == "stop":
        if not isinstance(adapter, dict) or not (roots := _roots(adapter, config)):
            return _emit()
        raw = sys.stdin.buffer.read(1048577)
        if not _in_scope(raw, roots) or (python := _active_python(adapter)) is None or (selected := _active_entry(adapter, data)) is None:
            return _emit()
        entry, release = selected
        try:
            subprocess.run([str(python), "-I", "-X", "utf8", str(entry), "--release", str(release), "stop"], input=raw, capture_output=True, timeout=8.0, creationflags=_NO_WIN)
        except subprocess.TimeoutExpired:
            pass
        return _emit()
    if role == "session-start" and isinstance(adapter, dict) and _active_python(adapter) is not None:
        _write_pending(); return _emit()
    return None
def _selection(*, name: str, marketplace: str, seed: str | None, args: list[str]):
    home, data, config = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"), _data_dir(name, marketplace), _config_path()
    adapter, source = read_json(config), _source(home, marketplace, name, seed)
    if isinstance(adapter, dict) and _active_python(adapter) is not None:
        migration = False
        if seed and args[:1] and args[0] in MCP_ROLES:
            installed, target = _version((adapter.get("product_validation") or {}).get("native_version")), _version(seed)
            migration = installed is not None and target is not None and target > installed
        if migration and (source / "scripts/bootstrap.py").is_file():
            return source / "scripts/bootstrap.py", [str(data), "--source", str(source), "migrate", *args], None
        if (selected := _active_entry(adapter, data)) is not None:
            return selected[0], args, _active_python(adapter)
    bootstrap = source / "scripts/bootstrap.py"
    if bootstrap.is_file():
        return bootstrap, [str(data), "--source", str(source), *args], None
    seed_archive = source / "assets/mindie-harness-seed.pyz"
    if not (source / "pyproject.toml").is_file() and seed_archive.is_file():
        return seed_archive, ["--host", "codex", "--plugin-root", str(source), "--config", str(config), *args], Path(sys.executable)
    raise RuntimeError("MindIE runtime is unavailable; install the plugin before starting a session")
def select_entry(*, name="mindie-agent", marketplace="mindie-agent", seed=None, args=None):
    entry, forwarded, _python = _selection(name=name, marketplace=marketplace, seed=seed, args=list(args or []))
    return entry, forwarded
def launch(argv=None) -> int:
    name, marketplace, seed, args = parse_argv(sys.argv[1:] if argv is None else argv)
    if args and (code := fast_path(args[0], name=name, marketplace=marketplace, config=_config_path(), data=_data_dir(name, marketplace))) is not None:
        return code
    entry, forwarded, python = _selection(name=name, marketplace=marketplace, seed=seed, args=args)
    if python is not None:
        return _execute(python, ["-I", "-X", "utf8", str(entry), *forwarded])
    sys.argv = [str(entry), *forwarded]; runpy.run_path(str(entry), run_name="__main__"); return 0
if __name__ == "__main__":
    raise SystemExit(launch())
