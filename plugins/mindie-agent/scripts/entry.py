"""Thin runtime dispatcher delegating to `mindie_harness` (`API_VERSION = 1`).
[Role] Dispatch `session-start`, `user-prompt-submit`, `stop`, the three MCP roles, `status`, `doctor`, and `configure`; delegate 9-domain knowledge binding and Stop capture to `mindie_harness`.
[Invariants] Cold `session-start` writes only `pending-update-check.json` without SQLite; `stop` checks consent+scope before SQLite and sets `turn` vs `notification` identity honestly; MCP stdio stays clean.
[Timeouts & Locks] Migration holds `.codex.json.update.lock` for 15s (`already_locked=True`); Stop capture is bounded to 6s.
[Side-Effect Policy] First MCP use writes MindIE-owned config; `stop` writes at most one authorized capture row.
"""
from __future__ import annotations
import json, os, re, sys, time
from pathlib import Path
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path: sys.path.insert(0, str(SCRIPTS))
MCP_ROLES = {"remote-mcp": "remote", "tools-mcp": "tools", "knowledge-mcp": "knowledge"}
VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\Z")
def _current_version(source: Path) -> str:
    try: value = json.loads((Path(source) / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))["version"]
    except TimeoutError: raise
    except (OSError, ValueError, KeyError, TypeError): return "0.1.40"
    return value if isinstance(value, str) and VERSION.fullmatch(value) else "0.1.40"
def _read(path: Path) -> dict:
    target = Path(path)
    if not target.exists(): return {}
    value = json.loads(target.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict): raise ValueError(f"expected JSON object in {target}")
    return value
def parse_args(argv: list[str]):
    values, release, source = list(argv), None, None
    for name in ("--release", "--source"):
        if name in values:
            idx = values.index(name); target = Path(values[idx + 1]).expanduser().resolve()
            if name == "--release": release = target
            else: source = target
            del values[idx:idx + 2]
    migrate = values[:1] == ["migrate"]
    if migrate: values.pop(0)
    return release, source, migrate, (values.pop(0) if values else ""), values
def _profile(source: Path):
    from host_adapter import CodexHostAdapter
    adapter = CodexHostAdapter(plugin_root=source or SCRIPTS.parent, config_path=os.environ.get("MINDIE_AGENT_CONFIG"))
    return adapter, adapter.resolve_profile()
def _rebind_unlocked(profile, *, source: Path, release: Path, version: str, current: dict | None = None, retire_previous: bool = True) -> dict:
    from mindie_harness.atomic import atomic_write_json
    from mindie_harness.component_commands import rebind_component_commands
    from mindie_harness.runtime.knowledge_binding import ensure_knowledge_binding
    config_path = Path(profile.config_path); before = dict(_read(config_path) if current is None else current)
    res = ensure_knowledge_binding(profile, plugin_root=source, python_executable=sys.executable, install_redactor=False, retire_previous=retire_previous, already_locked=True)
    ret_st = (res.get("retirement") or {}).get("status") if isinstance(res, dict) else None
    if not isinstance(res, dict) or res.get("status") != "configured" or ret_st in {"deferred", "incomplete"}:
        st = ret_st if ret_st in {"deferred", "incomplete"} else str((res or {}).get("status") or "failed")
        return {"status": st, "reason": str((res or {}).get("reason") or ("service_busy" if st == "deferred" else "old_retirement_unconfirmed"))}
    adapter = {**before, **(res.get("binding_fields") or {})}; validation = dict(adapter.get("product_validation") or {})
    validation.update(schema="mindie-candidate-check/1", status="validated", native_version=version)
    adapter.update(host="codex", python=sys.executable, runtime_scripts=str(SCRIPTS), source_root=str(source), component_release=str(release), product_validation=validation, updated_at=time.time())
    rebind_component_commands(adapter, sys.executable); atomic_write_json(config_path, adapter); return adapter
def _rebind(profile, *, source: Path, release: Path, version: str) -> dict:
    from mindie_harness.locks import FileLock
    cfg = Path(profile.config_path)
    with FileLock(cfg.with_name(f".{cfg.name}.update.lock"), exclusive=True, timeout=15.0):
        return _rebind_unlocked(profile, source=source, release=release, version=version)
def _prepare(release: Path | None, source: Path | None, migrate: bool):
    source = source or SCRIPTS.parent; version = _current_version(source); _handle, profile = _profile(source)
    config_path = Path(profile.config_path); adapter = _read(config_path); old_ep = adapter.get("engine_config")
    default_rel = release or Path(os.environ.get("PLUGIN_DATA") or profile.data_dir) / "releases" / version
    if not config_path.is_file() or not (isinstance(old_ep, str) and Path(old_ep).is_file()):
        from setup import ensure_initialized
        adapter, _ = ensure_initialized(profile, python=Path(sys.executable), scripts_dir=SCRIPTS, release_dir=default_rel)
    native = (adapter.get("product_validation") or {}).get("native_version")
    if migrate or native != version or adapter.get("python") != sys.executable or not isinstance(adapter.get("domain_configs"), dict) or len(adapter["domain_configs"]) < 9:
        approved = None
        if os.environ.get("MINDIE_CODEX_BIN") and os.environ.get("CODEX_HOME"):
            try:
                from native_hooks import preserve, snapshot
                prev = Path(adapter["source_root"]) if isinstance(adapter.get("source_root"), str) else source
                approved = snapshot(os.environ["MINDIE_CODEX_BIN"], dict(os.environ), prev if (prev / "hooks/hooks.json").is_file() else source)
            except TimeoutError: raise
            except Exception: approved = None
        res = _rebind(profile, source=source, release=release or Path(adapter.get("component_release") or default_rel), version=version)
        if isinstance(res, dict) and "engine_config" in res: adapter = res
        if approved:
            try: preserve(os.environ["MINDIE_CODEX_BIN"], dict(os.environ), source, approved)
            except TimeoutError: raise
            except Exception: pass
    ep = adapter.get("engine_config")
    return adapter, (_read(Path(ep)) if isinstance(ep, str) and Path(ep).is_file() else {}), profile
def _stop(profile, source: Path | None = None) -> int:
    from mindie_harness.runtime.knowledge_binding import run_stop_capture
    raw = sys.stdin.buffer.read(1048577)
    try: event = json.loads(raw.decode("utf-8-sig")) if raw.strip() else {}
    except ValueError: print("{}"); return 0
    if not isinstance(event, dict) or event.get("stop_hook_active", False) is not False: print("{}"); return 0
    session = event.get("session_id") or event.get("thread_id") or os.environ.get("CODEX_THREAD_ID")
    cwd = event["cwd"] if "cwd" in event else (event.get("workspace") or event.get("project_root"))
    turn_id = next((v.strip() for k in ("turn_id", "turnId") if isinstance(v := event.get(k), str) and v.strip()), None)
    event_id = next((v.strip() for k in ("event_id", "eventId") if isinstance(v := event.get(k), str) and v.strip()), None)
    payload = dict(event, hook_event_name=event.get("hook_event_name") or "Stop", harness="codex")
    if isinstance(session, str) and session: payload["session_id"] = session
    if isinstance(cwd, str) and cwd: payload["cwd"] = cwd
    if turn_id and not event_id: payload["turn_id"], payload["identity_kind"] = turn_id, "turn"
    elif event_id and not turn_id: payload["event_id"], payload["identity_kind"] = event_id, "notification"; payload.pop("last_assistant_message", None)
    run_stop_capture(profile, payload, plugin_root=source or SCRIPTS.parent, timeout_sec=6.0); print("{}"); return 0
def main(argv: list[str] | None = None) -> int:
    release, source, migrate, role, rest = parse_args(list(sys.argv[1:] if argv is None else argv)); root = source or SCRIPTS.parent
    if role == "user-prompt-submit": print("{}"); return 0
    if role == "session-start":
        from mindie_harness.runtime.activation import write_pending_update_check
        write_pending_update_check(_profile(root)[1], reason="session_start"); print("{}"); return 0
    if role == "stop":
        profile = _profile(root)[1]
        if not Path(profile.config_path).is_file(): print("{}"); return 0
        try: return _stop(profile, root)
        except TimeoutError: raise
        except Exception: print("{}"); return 0
    adapter, _engine, profile = _prepare(release, root, migrate)
    if role in MCP_ROLES:
        from mindie_harness.mcp.router import MCPRouter
        cadapter, surface = _profile(root)[0], MCP_ROLES[role]
        os.environ.setdefault("REMOTE_DEV_STATE_DIR", str(profile.state_dir / "remote")); os.environ.setdefault("REMOTE_DEV_HARNESS_PROFILE", "codex")
        router = MCPRouter(profile, surface=surface, tools=cadapter.mcp_tools(surface, profile), handler=lambda ctx, args: cadapter.dispatch_tool(ctx, args, profile), session_extractor=cadapter.extract_session_identity)
        return cadapter.serve_mcp(router, server_name=f"mindie-{surface}", server_version=_current_version(root))
    if role in {"status", "doctor", "configure"}:
        import setup
        return setup.main([{"status": "sharing", "doctor": "doctor", "configure": "configure"}[role], *([] if role != "status" else ["--status"]), *rest])
    print("{}"); return 0
if __name__ == "__main__": raise SystemExit(main())
