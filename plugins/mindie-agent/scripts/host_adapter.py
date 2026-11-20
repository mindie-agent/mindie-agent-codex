"""Codex thin host adapter and turn-scoped interrupt coordinator backed by `mindie-harness`.
[Role] Expose `CodexHostAdapter`, `extract_turn_scope`, `plugin_root_dir`, `verify_bundled_seed`, and `harness_available`; delegate MCP routing to `ManagedMCPAdapter` while scoping `mindie_interrupt` to `thread_id`/`turn_id`.
[Invariants] Stdlib-only top-level import; satisfies `HostAdapter`; `(thread A, turn 2)` never cancels `(thread A, None)` or `(thread B, turn 2)`, and every request ID is cleaned up in `finally:` around `router.call_tool` without evicting live entries.
[Timeouts & Locks] Pure path/hash resolution on import; `_scope_lock` guards active in-flight scopes and `_FramedWriter` serializes outbound stdio frames.
[Side-Effect Policy] Read-only seed verification; `serve_mcp` bridges stdio pipes cleanly.
"""
from __future__ import annotations
from collections.abc import Mapping
import hashlib, json, os, sys, threading
from pathlib import Path
from typing import Any
SEED_SCHEMA = "mindie-harness-seed/1"
def _clean(v: Any) -> str | None: return v.strip() if isinstance(v, str) and v.strip() else None
def plugin_root_dir(source: str | Path | None = None) -> Path:
    cand = Path(source).expanduser().resolve() if source is not None else Path(__file__).resolve().parents[1]
    return cand / "plugins/mindie-agent" if source is not None and (cand / "plugins/mindie-agent").is_dir() else cand
def verify_bundled_seed(source: str | Path | None = None) -> dict[str, Any]:
    assets = plugin_root_dir(source) / "assets"; lock_path = assets / "seed-lock.json"
    if not lock_path.is_file(): raise ValueError(f"missing bundled harness seed lock: {lock_path}")
    raw = lock_path.read_bytes()
    if len(raw) > 16384: raise ValueError("seed-lock.json exceeds metadata byte bound")
    lock = json.loads(raw.decode("utf-8"))
    if not isinstance(lock, dict) or lock.get("schema") != SEED_SCHEMA: raise ValueError("unsupported bundled harness seed schema")
    artifact, sha, size = lock.get("artifact"), lock.get("sha256"), lock.get("size_bytes")
    if not isinstance(artifact, str) or "/" in artifact or "\\" in artifact or not isinstance(sha, str) or len(sha) != 64 or type(size) is not int or size <= 0:
        raise ValueError("invalid seed-lock.json fields")
    seed_path = assets / artifact
    if not seed_path.is_file(): raise ValueError(f"missing bundled harness seed archive: {seed_path}")
    payload = seed_path.read_bytes()
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != sha: raise ValueError("bundled harness seed integrity mismatch")
    return dict(lock, path=str(seed_path))
def harness_available() -> bool:
    try: import mindie_harness.api as api
    except ModuleNotFoundError: return False
    return getattr(api, "API_VERSION", None) == 1
def extract_turn_scope(request: Mapping[str, Any], env: Mapping[str, str] | None = None) -> tuple[str | None, str | None]:
    params = request.get("params") if isinstance(request, Mapping) else None
    meta = params.get("_meta") if isinstance(params, Mapping) and isinstance(params.get("_meta"), Mapping) else {}
    raw_turn = meta.get("x-codex-turn-metadata")
    if isinstance(raw_turn, str):
        try: raw_turn = json.loads(raw_turn)
        except ValueError: raw_turn = {}
    tmap = raw_turn if isinstance(raw_turn, Mapping) else {}
    m_th = _clean(tmap.get("thread_id")) or _clean(meta.get("threadId")) or _clean(meta.get("session_id")) or _clean(meta.get("sessionId")) or _clean(meta.get("conversationId")) or _clean(tmap.get("session_id"))
    m_tu = _clean(tmap.get("turn_id")) or _clean(meta.get("turnId")) or _clean(meta.get("turn_id"))
    env_th = next((v for k in ("CODEX_THREAD_ID", "MINDIE_SESSION_ID", "REMOTE_DEV_SESSION_ID") if isinstance(env, Mapping) and (v := _clean(env.get(k)))), None)
    if isinstance(params, Mapping) and params.get("name") == "mindie_interrupt":
        args = params.get("arguments") if isinstance(params.get("arguments"), Mapping) else {}
        a_th, a_tu = _clean(args.get("thread_id")) or _clean(args.get("session_id")), _clean(args.get("turn_id"))
        if (a_th and m_th and a_th != m_th) or (a_tu and m_tu and a_tu != m_tu): return None, "__contradictory__"
        eff_th, eff_tu = a_th or m_th, a_tu or m_tu
        return (eff_th or (env_th if eff_tu is not None else None)), eff_tu
    return (m_th or env_th), m_tu
def _scope_matches(req_scope: tuple[str | None, str | None], target: tuple[str | None, str | None]) -> bool:
    r_th, r_tu = req_scope; i_th, i_tu = target
    if i_tu == "__contradictory__" or (i_th is None and i_tu is None) or r_th != i_th: return False
    return r_tu == i_tu if i_tu is not None else True
class _FramedWriter:
    def __init__(self, target) -> None: self._target, self._lock, self._local = target, threading.Lock(), threading.local()
    def write(self, data: bytes) -> int:
        if (buf := getattr(self._local, "buf", None)) is None: buf = self._local.buf = bytearray()
        buf.extend(data); return len(data)
    def flush(self) -> None:
        if buf := getattr(self._local, "buf", None):
            payload = bytes(buf); buf.clear()
            with self._lock: self._target.write(payload); self._target.flush()
class CodexHostAdapter:
    host = "codex"
    def __init__(self, *, plugin_root: str | Path | None = None, config_path: str | Path | None = None, data_dir: str | Path | None = None) -> None:
        self._plugin_root, self._config_path = plugin_root_dir(plugin_root), (Path(config_path).expanduser().resolve() if config_path else None)
        self._data_dir, self._scope_lock, self._scopes = (Path(data_dir).expanduser().resolve() if data_dir else None), threading.Lock(), {}
    def _managed(self):
        from mindie_harness.mcp.gateway import ManagedMCPAdapter
        return ManagedMCPAdapter(self.host, plugin_root=self._plugin_root, config_path=self._config_path, data_dir=self._data_dir)
    def resolve_profile(self, config_path: str | Path | None = None): return self._managed().resolve_profile(config_path or self._config_path or os.environ.get("MINDIE_AGENT_CONFIG"))
    def extract_session_identity(self, request: Mapping[str, Any], env: Mapping[str, str]) -> str | None: return extract_turn_scope(request, env)[0] or self._managed().extract_session_identity(request, env)
    def mcp_tools(self, surface: str, profile: Any = None) -> list[dict[str, Any]]: return self._managed().mcp_tools(surface, profile or self.resolve_profile())
    def dispatch_tool(self, ctx: Any, arguments: dict[str, Any], profile: Any = None) -> dict[str, Any]:
        return self._managed().dispatch_tool(ctx, dict(arguments), profile or self.resolve_profile())
    def serve_mcp(self, router: Any, *, server_name: str, server_version: str, stdin: Any = None, stdout: Any = None) -> int:
        from mindie_harness.mcp.server import MCPStdioServer, read_mcp_message, write_mcp_message
        from mindie_harness.paths import HarnessError
        in_s, out_s = (sys.stdin.buffer if stdin is None else stdin), _FramedWriter(sys.stdout.buffer if stdout is None else stdout)
        r_fd, w_fd = os.pipe(); pipe_in, pipe_out = os.fdopen(r_fd, "rb"), os.fdopen(w_fd, "wb")
        orig_call = router.call_tool
        def _tracked_call(message: Mapping[str, Any], *, cancel: Any = None, timeout: float | None = None) -> dict[str, Any]:
            try: return orig_call(message, cancel=cancel, timeout=timeout)
            finally:
                with self._scope_lock: self._scopes.pop(message.get("id"), None)
        router.call_tool = _tracked_call
        def _pump() -> None:
            framing = "jsonl"
            try:
                while True:
                    try: msg, framing = read_mcp_message(in_s)
                    except (ValueError, UnicodeDecodeError, HarnessError): write_mcp_message(out_s, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error / invalid MCP request"}}, framing); continue
                    except EOFError: break
                    if msg is None: break
                    req_id = msg.get("id")
                    valid_id = isinstance(req_id, (str, int)) and not isinstance(req_id, bool) and (not isinstance(req_id, str) or len(req_id) <= 256)
                    if msg.get("method") == "tools/call" and valid_id:
                        params = msg.get("params") or {}
                        if isinstance(params, Mapping) and params.get("name") == "mindie_interrupt" and (target := extract_turn_scope(msg, os.environ)) != (None, None):
                            with self._scope_lock: matched = [rid for rid, sc in list(self._scopes.items()) if _scope_matches(sc, target)]
                            for rid in matched: write_mcp_message(pipe_out, {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": rid}}, framing)
                            write_mcp_message(out_s, {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps({"interrupted": matched})}], "isError": False}}, framing); continue
                        if not (isinstance(params, Mapping) and params.get("name") == "mindie_interrupt"):
                            with self._scope_lock:
                                if req_id not in self._scopes: self._scopes[req_id] = extract_turn_scope(msg, os.environ)
                    write_mcp_message(pipe_out, msg, framing)
            except TimeoutError: raise
            except OSError: pass
            finally:
                try: pipe_out.close()
                except OSError: pass
        pump = threading.Thread(target=_pump, name="mindie-codex-mcp-in", daemon=True); pump.start()
        try: return MCPStdioServer(router, server_name=server_name, server_version=server_version).serve(stdin=pipe_in, stdout=out_s)
        finally:
            router.call_tool = orig_call
            with self._scope_lock: self._scopes.clear()
            try: pipe_in.close()
            except OSError: pass
            pump.join(timeout=2.0)
__all__ = ["CodexHostAdapter", "SEED_SCHEMA", "extract_turn_scope", "harness_available", "plugin_root_dir", "verify_bundled_seed"]
