"""Thin runtime dispatcher delegating to `mindie_harness` (`API_VERSION = 1`).
[Role] Dispatch `session-start`, `user-prompt-submit`, `stop`, the three MCP roles, `status`, `doctor`, and `configure`; initialize and migrate adapter/engine config.
[Invariants] `stop` never initializes, migrates, or opens SQLite before consent+scope verification; MCP stdio stays clean; explicit hook disables survive.
[Timeouts & Locks] Migration holds `.update.lock` for 15s; Stop capture is bounded to 6s.
[Side-Effect Policy] First use writes only MindIE-owned config; `stop` writes one authorized capture row.
"""
from __future__ import annotations
import json, os, re, sys, tempfile, time
from pathlib import Path
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
MCP_ROLES = {"remote-mcp": "remote", "tools-mcp": "tools", "knowledge-mcp": "knowledge"}
VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\Z")
def _current_version(source: Path) -> str:
    try: value = json.loads((Path(source) / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))["version"]
    except TimeoutError: raise
    except (OSError, ValueError, KeyError, TypeError): return "0.1.39"
    return value if isinstance(value, str) and VERSION.fullmatch(value) else "0.1.39"
def _read(path: Path) -> dict:
    target = Path(path)
    if not target.exists(): return {}
    value = json.loads(target.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict): raise ValueError(f"expected JSON object in {target}")
    return value
def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent); temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, sort_keys=True); stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
def parse_args(argv: list[str]):
    values = list(argv); release = source = None
    for name in ("--release", "--source"):
        if name in values:
            index = values.index(name); target = Path(values[index + 1]).expanduser().resolve()
            if name == "--release": release = target
            else: source = target
            del values[index:index + 2]
    migrate = bool(values[:1] == ["migrate"])
    if migrate: values.pop(0)
    return release, source, migrate, (values.pop(0) if values else ""), values
def _profile(source: Path):
    from host_adapter import CodexHostAdapter
    adapter = CodexHostAdapter(plugin_root=source or SCRIPTS.parent, config_path=os.environ.get("MINDIE_AGENT_CONFIG"), data_dir=os.environ.get("PLUGIN_DATA"))
    return adapter, adapter.resolve_profile()
def _rebind_unlocked(config_path: Path, engine_path: Path, *, source: Path, release: Path, version: str, current: dict | None = None) -> dict:
    from mindie_harness.component_commands import rebind_component_commands
    adapter = dict(_read(config_path) if current is None else current); old_ep = adapter.get("engine_config")
    engine = _read(engine_path) or (_read(Path(old_ep)) if isinstance(old_ep, str) and Path(old_ep).is_file() else {})
    validation = dict(adapter.get("product_validation") or {}); validation.update(schema="mindie-candidate-check/1", status="validated", native_version=version)
    adapter.update(python=sys.executable, runtime_scripts=str(SCRIPTS), source_root=str(source), component_release=str(release), product_validation=validation, engine_config=str(engine_path), updated_at=time.time())
    rebind_component_commands(adapter, sys.executable); engine.update(transcript_adapter=str(SCRIPTS / "codex_transcript.py"), product_validation=validation)
    _atomic(engine_path, engine); _atomic(config_path, adapter); return adapter
def _rebind(config_path: Path, engine_path: Path, *, source: Path, release: Path, version: str) -> dict:
    from mindie_harness.locks import FileLock
    lock = FileLock(config_path.with_suffix(".update.lock"), exclusive=True, timeout=15.0); lock.acquire()
    try:
        return _rebind_unlocked(config_path, engine_path, source=source, release=release, version=version)
    finally:
        lock.release()
def _prepare(release: Path | None, source: Path | None, migrate: bool):
    source = source or SCRIPTS.parent; version = _current_version(source); _handle, profile = _profile(source)
    config_path = Path(profile.config_path); engine_path = config_path.with_name(config_path.stem + ".engine.json")
    adapter = _read(config_path); old_ep = adapter.get("engine_config")
    if not config_path.is_file() or not (engine_path.is_file() or (isinstance(old_ep, str) and Path(old_ep).is_file())):
        from setup import ensure_initialized
        ensure_initialized(profile, python=Path(sys.executable), scripts_dir=SCRIPTS, release_dir=release or Path(profile.data_dir) / "releases" / version)
        adapter = _read(config_path)
    native = (adapter.get("product_validation") or {}).get("native_version")
    if migrate or native != version or adapter.get("python") != sys.executable:
        approved = None
        if os.environ.get("MINDIE_CODEX_BIN") and os.environ.get("CODEX_HOME"):
            try:
                from native_hooks import preserve, snapshot
                prev = Path(adapter["source_root"]) if isinstance(adapter.get("source_root"), str) else source
                approved = snapshot(os.environ["MINDIE_CODEX_BIN"], dict(os.environ), prev if (prev / "hooks/hooks.json").is_file() else source)
            except TimeoutError: raise
            except Exception: approved = None
        adapter = _rebind(config_path, engine_path, source=source, release=release or Path(adapter.get("component_release") or SCRIPTS.parent), version=version)
        if approved:
            try: preserve(os.environ["MINDIE_CODEX_BIN"], dict(os.environ), source, approved)
            except TimeoutError: raise
            except Exception: pass
    return adapter, _read(engine_path), profile
def _stop(profile, adapter: dict) -> int:
    raw = sys.stdin.buffer.read(1048577)
    try: event = json.loads(raw.decode("utf-8-sig")) if raw.strip() else {}
    except ValueError: print("{}"); return 0
    if not isinstance(event, dict): print("{}"); return 0
    cwd = event["cwd"] if "cwd" in event else (event.get("workspace") or event.get("project_root"))
    from mindie_harness.sharing import sharing_status
    status = sharing_status(profile, cwd=cwd)
    if status.get("tier") not in {"contribute", "local-only"} or not status.get("cwd_in_scope"):
        print("{}"); return 0
    engine_path = Path(adapter.get("engine_config") or Path(profile.config_path).with_name(Path(profile.config_path).stem + ".engine.json"))
    if not engine_path.is_file(): print("{}"); return 0
    from mindie_knowledge.loop.handoff import prepare_schema
    prepare_schema(engine_path)
    session = event.get("session_id") or event.get("thread_id") or os.environ.get("CODEX_THREAD_ID")
    if not isinstance(session, str) or not session: print("{}"); return 0
    admission_path = adapter.get("admission_path")
    if isinstance(admission_path, str) and os.path.isabs(admission_path):
        from mindie_knowledge.loop.activation import Admission
        admission = Admission(admission_path)
        if admission.inspect(session).get("status") == "inactive": print("{}"); return 0
        lease = admission.active_lease(session)
        if lease is None and isinstance(cwd, str) and os.path.isabs(cwd):
            admission.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            lease = admission.activate(session, project_root=str(Path(cwd).resolve()), root_session=session, not_before=0.0)
        if isinstance(lease, dict) and isinstance(lease.get("token"), str):
            event["mindie_activation"] = lease["token"]
    event["session_id"] = session
    from mindie_knowledge.loop.cli import capture_hook
    capture_hook(engine_path, event, timeout=6.0); print("{}"); return 0
def main(argv: list[str] | None = None) -> int:
    release, source, migrate, role, rest = parse_args(list(sys.argv[1:] if argv is None else argv))
    if role == "user-prompt-submit": print("{}"); return 0
    if role == "stop":
        _handle, profile = _profile(source or SCRIPTS.parent); config_path = Path(profile.config_path)
        if not config_path.is_file(): print("{}"); return 0
        try: return _stop(profile, _read(config_path))
        except TimeoutError: raise
        except Exception: print("{}"); return 0
    adapter, _engine, profile = _prepare(release, source, migrate)
    if role == "session-start":
        from mindie_harness.runtime.activation import write_pending_update_check
        write_pending_update_check(profile, reason="session_start"); print("{}"); return 0
    if role in MCP_ROLES:
        from host_adapter import CodexHostAdapter
        from mindie_harness.mcp.router import MCPRouter
        from mindie_harness.mcp.server import MCPStdioServer
        surface = MCP_ROLES[role]
        cadapter = CodexHostAdapter(plugin_root=source or SCRIPTS.parent, config_path=os.environ.get("MINDIE_AGENT_CONFIG"), data_dir=os.environ.get("PLUGIN_DATA"))
        class _CodexRouter(MCPRouter):
            def _validate_and_clean_args(self, name: str, raw_args):
                schema = ((self._tool_map.get(name) or {}).get("inputSchema") or {})
                if not (isinstance(raw_args, dict) and "session_id" in raw_args and "session_id" in (schema.get("properties") or {})):
                    return super()._validate_and_clean_args(name, raw_args)
                from mindie_harness.errors import HarnessError
                from mindie_harness.mcp.router import CORRELATION_STRIP_KEYS, FORGED_IDENTITY_KEYS
                if (FORGED_IDENTITY_KEYS - {"session_id"}).intersection(raw_args.keys()):
                    raise HarnessError("Model-supplied identity override arguments are forbidden", stage="tool_schema", reason="forged_identity_argument", reportable=False)
                args = {k: v for k, v in raw_args.items() if k not in CORRELATION_STRIP_KEYS}
                if missing := [r for r in (schema.get("required") or ()) if r not in args]:
                    raise HarnessError(f"Missing required argument `{missing[0]}` for {name}", stage="tool_schema", reason="schema_missing_field", reportable=False, details={"field": missing[0]})
                if isinstance(schema.get("properties"), dict) and schema.get("additionalProperties") is False and (set(args) - set(schema["properties"])):
                    raise HarnessError(f"Undeclared arguments supplied to {name}", stage="tool_schema", reason="schema_unknown_field", reportable=False)
                return args
        os.environ.setdefault("REMOTE_DEV_STATE_DIR", str(profile.state_dir / "remote"))
        os.environ.setdefault("REMOTE_DEV_HARNESS_PROFILE", "codex")
        router = _CodexRouter(profile, surface=surface, tools=cadapter.mcp_tools(surface, profile),
                              handler=lambda ctx, args: cadapter.dispatch_tool(ctx, args, profile),
                              session_extractor=cadapter.extract_session_identity)
        return MCPStdioServer(router, server_name=f"mindie-{surface}", server_version=_current_version(source or SCRIPTS.parent)).serve()
    if role in {"status", "doctor", "configure"}:
        import setup
        return setup.main([{"status": "sharing", "doctor": "doctor", "configure": "configure"}[role], *([] if role != "status" else ["--status"]), *rest])
    print("{}"); return 0
if __name__ == "__main__":
    raise SystemExit(main())
