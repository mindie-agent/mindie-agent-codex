"""Codex thin host adapter backed by `mindie-harness` (`API_VERSION = 1`).
[Role] Expose `CodexHostAdapter`, `plugin_root_dir`, `verify_bundled_seed`, and `harness_available`; delegate protocol members lazily to `ManagedMCPAdapter`.
[Invariants] Stdlib-only import; the adapter satisfies `HostAdapter` when installed.
[Timeouts & Locks] Pure path/hash resolution; no locks or subprocesses.
[Side-Effect Policy] Read-only import and seed verification.
"""
from __future__ import annotations
from collections.abc import Mapping
import hashlib, json, os
from pathlib import Path
from typing import Any
SEED_SCHEMA = "mindie-harness-seed/1"
def plugin_root_dir(source: str | Path | None = None) -> Path:
    candidate = Path(source).expanduser().resolve() if source is not None else Path(__file__).resolve().parents[1]
    nested = candidate / "plugins/mindie-agent"
    return nested if source is not None and nested.is_dir() else candidate
def verify_bundled_seed(source: str | Path | None = None) -> dict[str, Any]:
    assets = plugin_root_dir(source) / "assets"; lock_path = assets / "seed-lock.json"
    if not lock_path.is_file():
        raise ValueError(f"missing bundled harness seed lock: {lock_path}")
    raw = lock_path.read_bytes()
    if len(raw) > 16384:
        raise ValueError("seed-lock.json exceeds metadata byte bound")
    lock = json.loads(raw.decode("utf-8"))
    if not isinstance(lock, dict) or lock.get("schema") != SEED_SCHEMA:
        raise ValueError("unsupported bundled harness seed schema")
    artifact, sha, size = lock.get("artifact"), lock.get("sha256"), lock.get("size_bytes")
    if not isinstance(artifact, str) or "/" in artifact or "\\" in artifact or not isinstance(sha, str) or len(sha) != 64 or type(size) is not int or size <= 0:
        raise ValueError("invalid seed-lock.json fields")
    seed_path = assets / artifact
    if not seed_path.is_file():
        raise ValueError(f"missing bundled harness seed archive: {seed_path}")
    payload = seed_path.read_bytes()
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != sha:
        raise ValueError("bundled harness seed integrity mismatch")
    return dict(lock, path=str(seed_path))
def harness_available() -> bool:
    try:
        import mindie_harness.api as api
    except ModuleNotFoundError:
        return False
    return getattr(api, "API_VERSION", None) == 1
class CodexHostAdapter:
    """Thin Codex `HostAdapter` delegating to the shared managed gateway."""
    host = "codex"
    def __init__(self, *, plugin_root: str | Path | None = None, config_path: str | Path | None = None, data_dir: str | Path | None = None) -> None:
        self._plugin_root = plugin_root_dir(plugin_root); self._config_path = Path(config_path).expanduser().resolve() if config_path else None; self._data_dir = Path(data_dir).expanduser().resolve() if data_dir else None
    def _managed(self):
        from mindie_harness.mcp.gateway import ManagedMCPAdapter
        return ManagedMCPAdapter(self.host, plugin_root=self._plugin_root, config_path=self._config_path, data_dir=self._data_dir)
    def resolve_profile(self, config_path: str | Path | None = None):
        return self._managed().resolve_profile(config_path or self._config_path or os.environ.get("MINDIE_AGENT_CONFIG"))
    def extract_session_identity(self, request: Mapping[str, Any], env: Mapping[str, str]) -> str | None:
        return self._managed().extract_session_identity(request, env)
    def mcp_tools(self, surface: str, profile: Any = None) -> list[dict[str, Any]]:
        return self._managed().mcp_tools(surface, profile or self.resolve_profile())
    def dispatch_tool(self, ctx: Any, arguments: dict[str, Any], profile: Any = None) -> dict[str, Any]:
        return self._managed().dispatch_tool(ctx, dict(arguments), profile or self.resolve_profile())
__all__ = ["CodexHostAdapter", "SEED_SCHEMA", "harness_available", "plugin_root_dir", "verify_bundled_seed"]
