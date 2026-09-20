"""Community sharing settings: separate from plugin activation, default OFF.

One shared local JSON file (schema mindie-community-config/1) is read by both
the adapter and the knowledge core. Plugin activation never authorizes
capture by itself; missing, malformed or disabled community state fails
closed for capture but never blocks retrieval, feedback or updates. Only
MindIE-owned files are ever written here; no unrelated plugin, scope or
global configuration is touched. Validated extension keys owned by sibling
components (publishing/transaction/bot/transport, private config_path, ...)
pass through every adapter mutation byte-identical.
"""

import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import time

from session_gate import config_path
from update_lock import update_lock

SCHEMA = "mindie-community-config/1"
MAX_ROOTS = 64
# Shared bounds, frozen across adapter/core/community: generation is a
# nonempty opaque string, idle 30..86400 seconds.
MIN_IDLE, MAX_IDLE = 30, 86400
MAX_EXTENSION_BYTES = 4096
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]{1,128}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")
CORE_KEYS = {
    "schema",
    "enabled",
    "generation",
    "enabled_at",
    "repository",
    "branch",
    "project_roots",
    "idle_seconds",
}


class SharingError(ValueError):
    pass


def configured_path(config_file=None):
    """Absolute community settings path recorded in the adapter config."""
    config_file = Path(config_file or config_path())
    config = json.loads(config_file.read_text())
    value = config.get("community_config")
    if not isinstance(value, str) or not os.path.isabs(value):
        raise SharingError(
            "adapter configuration lacks an absolute community_config pointer"
        )
    return Path(value)


def canonical_root(value):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise SharingError("community project roots must be path strings")
    if not os.path.isabs(value):
        raise SharingError("community project roots must be absolute: " + value[:80])
    return Path(value).resolve().as_posix()


def validate(settings):
    """Normalize one settings document or raise SharingError.

    Core fields are strictly validated; unknown extension keys owned by
    sibling components are preserved verbatim (bounded), never dropped or
    reinterpreted by an adapter toggle.
    """
    if not isinstance(settings, dict) or settings.get("schema") != SCHEMA:
        raise SharingError("community settings schema mismatch")
    enabled = settings.get("enabled")
    if not isinstance(enabled, bool):
        raise SharingError("community enabled flag must be boolean")
    generation = settings.get("generation")
    if not isinstance(generation, str) or not generation or len(generation) > 64:
        raise SharingError("community generation must be a nonempty opaque string")
    enabled_at = settings.get("enabled_at")
    if enabled_at is not None and not (
        isinstance(enabled_at, (int, float))
        and not isinstance(enabled_at, bool)
        and math.isfinite(enabled_at)
        and enabled_at > 0
    ):
        raise SharingError("community enabled_at must be finite Unix seconds or null")
    repository = settings.get("repository")
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise SharingError("community repository must be owner/repo")
    branch = settings.get("branch", "main")
    if not isinstance(branch, str) or not NAME.fullmatch(branch):
        raise SharingError("community branch is invalid")
    roots = settings.get("project_roots")
    if (
        not isinstance(roots, list)
        or not 0 < len(roots) <= MAX_ROOTS
        or len(set(roots)) != len(roots)
    ):
        raise SharingError("community project_roots must be a non-empty unique list")
    roots = [canonical_root(root) for root in roots]
    idle = settings.get("idle_seconds", 300)
    if not isinstance(idle, int) or isinstance(idle, bool) or not MIN_IDLE <= idle <= MAX_IDLE:
        raise SharingError("community idle_seconds out of range")
    if enabled and enabled_at is None:
        raise SharingError("enabled community settings require enabled_at")
    result = dict(
        schema=SCHEMA,
        enabled=enabled,
        generation=generation,
        enabled_at=enabled_at,
        repository=repository,
        branch=branch,
        project_roots=roots,
        idle_seconds=idle,
    )
    for key, value in settings.items():
        if key in CORE_KEYS or value is None:
            continue
        if not isinstance(key, str) or len(key) > 64:
            raise SharingError("community extension key is invalid")
        if len(json.dumps(value, ensure_ascii=False)) > MAX_EXTENSION_BYTES:
            raise SharingError("community extension value exceeds limit: " + key)
        result[key] = value
    return result


def read(config_file=None):
    """Validated settings, or None when unconfigured/malformed (fail closed)."""
    try:
        return validate(json.loads(configured_path(config_file).read_text()))
    except (OSError, ValueError):
        return None


def write(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".community-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def capture_allowed(lease, cwd, config_file=None):
    """active lease AND community enabled AND lease project root in scope.

    Scope is the lease's authorized activation-time project_root, never a
    caller-supplied cwd pointing into an allowed directory. Anything
    unreadable or incomplete fails closed for capture only.
    """
    settings = read(config_file)
    if settings is None or not settings["enabled"]:
        return False
    if any(
        lease.get(key) is None
        for key in ("project_root", "root_session", "activated_at")
    ):
        return False
    try:
        root = Path(lease["project_root"]).resolve().as_posix()
    except (OSError, ValueError):
        return False
    return any(
        root == allowed or root.startswith(allowed + "/")
        for allowed in settings["project_roots"]
    )


def set_enabled(enable, config_file=None):
    """Flip the sharing switch atomically; keep the recorded scope/settings.

    Enabling sets a fresh enabled_at: material from a disabled period is
    never backfilled, and failed-attempt budgets survive. Disabling keeps
    drafts and published data untouched.
    """
    config_file = Path(config_file or config_path())
    with update_lock(config_file):
        path = configured_path(config_file)
        settings = read(config_file)
        if settings is None:
            raise SharingError(
                "community sharing is not configured; run setup.py with --community-* "
                "to select repository, scope and visibility first"
            )
        settings["enabled"] = bool(enable)
        settings["enabled_at"] = time.time() if enable else None
        settings["generation"] = secrets.token_hex(8)
        write(path, settings)
        return settings


def status(config_file=None):
    """Read-only sharing status; initializes no service, model or database."""
    config_file = Path(config_file or config_path())
    try:
        path = configured_path(config_file)
    except (OSError, ValueError) as exc:
        return dict(state="unconfigured", detail=str(exc)[:200])
    if not path.exists():
        return dict(state="off", detail="no community settings recorded")
    try:
        settings = validate(json.loads(path.read_text()))
    except (OSError, ValueError) as exc:
        return dict(
            state="malformed",
            detail=str(exc)[:200],
            capture="fail-closed; retrieval and updates unaffected",
        )
    return dict(
        state="enabled" if settings["enabled"] else "disabled",
        path=str(path),
        generation=settings["generation"],
        enabled_at=settings["enabled_at"],
        repository=settings["repository"],
        branch=settings["branch"],
        project_roots=settings["project_roots"],
        visibility=settings.get("visibility"),
        note="capture only processes material authorized after enabled_at; "
        "no disabled-period backfill",
    )
