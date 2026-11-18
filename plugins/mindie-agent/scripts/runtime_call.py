"""Legacy pre-0.1.38 `runtime_call.py` MCP helper shim for in-flight `v0.1.37` sessions.
[Role] Serve catalog discovery (`surface=="catalog"`), legacy `remote` job-store calls (`mindie-remote-dev/runtime/<session>`), and `knowledge`/`components` calls spawned by open `v0.1.37` servers after `codex.json` activates `0.1.39+`.
[Invariants] Preserves `v0.1.37` per-session `REMOTE_DEV_STATE_DIR` and `remote_write_stdin(session_id=...)` tool arguments without triggering forged-identity guards.
[Timeouts & Locks] Delegates per-call budgets to `compute_tool_deadline` and `MCPRouter.call_tool`.
[Side-Effect Policy] Emits one JSON list (catalog) or one MCP tool result object on stdout.
"""
from __future__ import annotations
import json, os, re, sys, threading
from pathlib import Path
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path: sys.path.insert(0, str(SCRIPTS))
_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
def _legacy_remote_root() -> Path:
    if explicit := os.environ.get("MINDIE_REMOTE_STATE_DIR"):
        return Path(explicit).expanduser().absolute()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base).expanduser().absolute() / "mindie-remote-dev"
def main() -> int:
    from host_adapter import CodexHostAdapter
    from mindie_harness.mcp.gateway_dispatch import dispatch_remote_tool
    from mindie_harness.mcp.router import MCPRouter, RequestContext, compute_tool_deadline
    req = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
    adapter = CodexHostAdapter()
    profile = adapter.resolve_profile()
    if req.get("surface") == "catalog":
        print(json.dumps(adapter.mcp_tools(str(req.get("catalog_surface") or "all"), profile), ensure_ascii=False))
        return 0
    surface, name = str(req.get("surface") or "all"), str(req.get("name") or "")
    raw_args = req.get("arguments")
    arguments = dict(raw_args) if isinstance(raw_args, dict) else {}
    if surface == "remote":
        r_sess = req.get("remote_session_id")
        if not isinstance(r_sess, str) or not _ID_RE.fullmatch(r_sess):
            raise ValueError("MindIE remote runtime identity mismatch")
        state_dir = str(_legacy_remote_root() / "runtime" / r_sess)
        sess_id = f"codex-task-{r_sess}"
        outer = req.get("helper_timeout_seconds")
        explicit_to = float(outer) if isinstance(outer, (int, float)) and not isinstance(outer, bool) and outer > 0 else None
        deadline = compute_tool_deadline("remote", name, arguments, explicit_timeout=explicit_to)
        env = {**os.environ, "REMOTE_DEV_STATE_DIR": state_dir, "REMOTE_DEV_SESSION_ID": sess_id, "REMOTE_DEV_HARNESS_PROFILE": "codex"}
        ctx = RequestContext(request_id=1, surface="remote", tool_name=name, session_id=sess_id, cwd=None, env=env, deadline=deadline, cancel=threading.Event())
        print(json.dumps(dispatch_remote_tool(ctx, arguments, profile, host="codex"), ensure_ascii=False))
        return 0
    os.environ.setdefault("REMOTE_DEV_STATE_DIR", str(profile.state_dir / "remote"))
    os.environ.setdefault("REMOTE_DEV_HARNESS_PROFILE", "codex")
    router = MCPRouter(profile, surface=surface, tools=adapter.mcp_tools(surface, profile),
                       handler=lambda ctx, args: adapter.dispatch_tool(ctx, args, profile),
                       session_extractor=adapter.extract_session_identity)
    meta = {"session_id": req["mindie_session_id"]} if isinstance(req.get("mindie_session_id"), str) and req["mindie_session_id"] else {}
    timeout = float(req["timeout"]) if isinstance(req.get("timeout"), (int, float)) and not isinstance(req.get("timeout"), bool) and req["timeout"] > 0 else None
    result = router.call_tool({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": arguments, "_meta": meta}}, timeout=timeout)
    print(json.dumps(result, ensure_ascii=False))
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
