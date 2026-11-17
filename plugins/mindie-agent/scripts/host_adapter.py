"""Codex thin host adapter backed by `mindie-harness` (`API_VERSION = 1`).

[Role]
Implements `CodexHostAdapter` (`mindie_harness.api.HostAdapter`) and verifies
the bundled `assets/{mindie-harness-seed.pyz, seed-lock.json}` bootstrap seed,
bridging shared `mindie_harness` lock, atomic-write, and storage primitives with
a zero-dependency stdlib fallback during cold `uv run --no-project` startup.

[Invariants]
- Top-level import uses pure Python stdlib only so cold hook/MCP entrypoints
  can import `host_adapter` before the locked runtime venv exists.
- When `mindie_harness` is installed in the runtime venv, `CodexHostAdapter`
  satisfies `isinstance(adapter, mindie_harness.api.HostAdapter)`.
- Bundled seed verification checks exact `schema`, `size_bytes`, and `sha256`
  against `seed-lock.json` without executing untrusted code.

[Timeouts & Locks]
- Pure path/hash resolution on import and `verify_bundled_seed()`; tool dispatch
  and file locking delegate to `mcp_gate.Gate` and `mindie_harness.locks`.

[Side-Effect Policy]
- Importing and seed verification are strictly read-only.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
from typing import Any

SEED_SCHEMA = "mindie-harness-seed/1"


def plugin_root_dir(source: str | Path | None = None) -> Path:
    """Locate the `plugins/mindie-agent` package root from scripts or repo root."""
    if source is not None:
        candidate = Path(source).expanduser().resolve()
        nested = candidate / "plugins" / "mindie-agent"
        return nested if nested.is_dir() else candidate
    return Path(__file__).resolve().parents[1]


def verify_bundled_seed(source: str | Path | None = None) -> dict[str, Any]:
    """Verify `assets/mindie-harness-seed.pyz` against `assets/seed-lock.json`."""
    root = plugin_root_dir(source)
    assets = root / "assets"
    lock_path = assets / "seed-lock.json"
    if not lock_path.is_file():
        raise ValueError(f"missing bundled harness seed lock: {lock_path}")
    raw_lock = lock_path.read_bytes()
    if len(raw_lock) > 16384:
        raise ValueError("seed-lock.json exceeds metadata byte bound")
    lock = json.loads(raw_lock.decode("utf-8"))
    if not isinstance(lock, dict) or lock.get("schema") != SEED_SCHEMA:
        raise ValueError("unsupported bundled harness seed schema")
    artifact = lock.get("artifact")
    expected_sha = lock.get("sha256")
    expected_size = lock.get("size_bytes")
    if (
        not isinstance(artifact, str)
        or "/" in artifact
        or "\\" in artifact
        or not isinstance(expected_sha, str)
        or len(expected_sha) != 64
        or type(expected_size) is not int
        or expected_size <= 0
    ):
        raise ValueError("invalid seed-lock.json fields")
    seed_path = assets / artifact
    if not seed_path.is_file():
        raise ValueError(f"missing bundled harness seed archive: {seed_path}")
    payload = seed_path.read_bytes()
    if len(payload) != expected_size:
        raise ValueError("bundled harness seed size mismatch")
    actual_sha = hashlib.sha256(payload).hexdigest()
    if actual_sha != expected_sha:
        raise ValueError("bundled harness seed sha256 mismatch")
    return dict(lock, path=str(seed_path))


def harness_available() -> bool:
    """Return True when `mindie_harness` (`API_VERSION == 1`) is installed."""
    try:
        import mindie_harness.api as api
        return getattr(api, "API_VERSION", None) == 1
    except ModuleNotFoundError:
        return False


class CodexHostAdapter:
    """Thin Codex `HostAdapter` delegating runtime & MCP plumbing to `mindie_harness`."""

    host: str = "codex"

    def __init__(
        self,
        *,
        plugin_root: str | Path | None = None,
        config_path: str | Path | None = None,
        data_dir: str | Path | None = None,
    ) -> None:
        self._plugin_root = plugin_root_dir(plugin_root)
        self._config_path = Path(config_path).expanduser().resolve() if config_path else None
        self._data_dir = Path(data_dir).expanduser().resolve() if data_dir else None

    def resolve_profile(self, config_path: str | Path | None = None):
        target_cfg = config_path or self._config_path or os.environ.get("MINDIE_AGENT_CONFIG")
        target_data = self._data_dir or os.environ.get("PLUGIN_DATA")
        from mindie_harness.paths import ProfileRef

        return ProfileRef.resolve(
            self.host,
            plugin_root=self._plugin_root,
            config_path=target_cfg,
            data_dir=target_data,
        )

    def extract_session_identity(
        self,
        request: Mapping[str, Any],
        env: Mapping[str, str],
    ) -> str | None:
        from mcp_identity import optional_native_identity

        try:
            session = optional_native_identity(dict(request), default_session="")
            if session:
                return session
        except ValueError:
            return None
        thread_id = env.get("CODEX_THREAD_ID", "").strip()
        return thread_id or None

    def mcp_tools(self, surface: str, profile: Any = None) -> list[dict[str, Any]]:
        import mcp_gate

        normalized = "remote" if surface in ("remote", "remote-dev") else (
            "components" if surface in ("tools", "components") else surface
        )
        return list(mcp_gate.Gate(normalized).tools)

    def dispatch_tool(
        self,
        ctx: Any,
        arguments: dict[str, Any],
        profile: Any = None,
    ) -> dict[str, Any]:
        import mcp_gate

        surface = getattr(ctx, "surface", "components")
        normalized = "remote" if surface in ("remote", "remote-dev") else (
            "components" if surface in ("tools", "components") else surface
        )
        gate = mcp_gate.Gate(normalized)
        try:
            request = {
                "jsonrpc": "2.0",
                "id": getattr(ctx, "request_id", 1),
                "method": "tools/call",
                "params": {
                    "name": getattr(ctx, "tool_name", ""),
                    "arguments": dict(arguments),
                },
            }
            session_id = getattr(ctx, "session_id", None)
            cli_id = (session_id, None) if isinstance(session_id, str) and session_id else None
            return gate.call(
                request,
                cancel=getattr(ctx, "cancel_event", None),
                cli_identity=cli_id,
            )
        finally:
            gate.close()


__all__ = [
    "CodexHostAdapter",
    "SEED_SCHEMA",
    "harness_available",
    "plugin_root_dir",
    "verify_bundled_seed",
]
