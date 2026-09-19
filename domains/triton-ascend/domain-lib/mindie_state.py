"""Local run-state for domain tools, rooted at an explicit directory.

Default root is ./.mindie relative to the caller's working directory (the
user's business repository), never a workspace-managed hidden directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4


class SessionStateError(RuntimeError):
    pass


def state_root(explicit: str | None = None) -> Path:
    root = explicit or os.environ.get("MINDIE_DOMAIN_STATE_DIR") or "./.mindie"
    path = Path(root).expanduser().absolute()
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_run_token(value: str, fallback: str | None = None) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    if not token:
        if fallback is None:
            raise SessionStateError("run token must contain a safe character")
        token = re.sub(r"[^A-Za-z0-9._-]+", "-", fallback).strip("-.") or "run"
    return token[:80]


def allocate_run_dir(base, token: str | None = None, *, root: str | None = None) -> Path:
    """Allocate a collision-safe run directory.

    ``base`` is either a run-kind name (placed under the state root) or an
    explicit directory path (created and used directly as the parent).
    """
    if isinstance(base, Path) or "/" in str(base):
        parent = Path(base)
    else:
        parent = state_root(root) / "runs" / safe_run_token(str(base))
    parent.mkdir(parents=True, exist_ok=True)
    name = token or f"{int(time.time())}-{uuid4().hex[:8]}"
    path = parent / safe_run_token(str(name))
    path.mkdir(parents=True, exist_ok=False)
    return path


def benchmark_dir(*, root: str | None = None) -> Path:
    path = state_root(root) / "benchmarks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_serving_state(*, root: str | None = None) -> dict:
    path = state_root(root) / "serving.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_serving_state(value: dict, *, root: str | None = None) -> None:
    path = state_root(root) / "serving.json"
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
