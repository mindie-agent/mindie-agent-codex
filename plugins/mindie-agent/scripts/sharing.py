"""Community sharing settings: separate from plugin activation, default OFF.

One shared local JSON file (schema mindie-community-config/1) is read by both
the adapter and the knowledge core. Plugin activation never authorizes
capture by itself; missing, malformed or disabled community state fails
closed for capture but never blocks retrieval, feedback or updates. Only
MindIE-owned files are ever written here; no unrelated plugin, scope or
global configuration is touched. Validated extension keys owned by sibling
components (publishing/transaction/bot/transport, private config_path, ...)
pass through every adapter mutation byte-identical.

Writes go through the core ``normalize`` via the configured interpreter so
this adapter does not keep a second copy of the idle/root ranges. The Stop
hook uses a cheap structural precheck only.
"""

import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import time

from bounded_process import run
from session_gate import config_path, generation_env
from update_lock import update_lock

SCHEMA = "mindie-community-config/1"
MAX_ROOTS = 64
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
CHOICES = (
    "Community sharing is unconfigured. Choose one (no default yes):\n"
    "1. Recommended: public community contribution for the current named "
    "project/repository/account — scripts/setup.py configure "
    "--community-repository OWNER/REPO --community-project-root PATH "
    "--community-visibility public [--community-account NAME]\n"
    "2. Read-only knowledge; no contribution — scripts/bridge.py "
    "sharing-choice read-only\n"
    "3. Configure later — scripts/bridge.py sharing-choice later"
)
NORMALIZE_SCRIPT = """
import json, sys
from mindie_knowledge.loop.settings import normalize
print(json.dumps(normalize(json.loads(sys.stdin.read())), ensure_ascii=False))
"""


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
    """Cheap structural check used by tests and as a pre-filter.

    Idle-second and repository ranges are owned by core ``normalize``;
    this does not copy those bounds.
    """
    if not isinstance(settings, dict) or settings.get("schema") != SCHEMA:
        raise SharingError("community settings schema mismatch")
    enabled = settings.get("enabled")
    if not isinstance(enabled, bool):
        raise SharingError("community enabled flag must be boolean")
    generation = settings.get("generation")
    if generation is not None and (
        not isinstance(generation, str) or (enabled and not generation)
    ):
        raise SharingError("community generation must be a nonempty opaque string")
    enabled_at = settings.get("enabled_at")
    if enabled_at is not None and not (
        isinstance(enabled_at, (int, float))
        and not isinstance(enabled_at, bool)
        and math.isfinite(enabled_at)
        and enabled_at > 0
    ):
        raise SharingError("community enabled_at must be finite Unix seconds or null")
    if enabled and enabled_at is None:
        raise SharingError("enabled community settings require enabled_at")
    repository = settings.get("repository")
    if repository is not None and (
        not isinstance(repository, str) or not REPOSITORY.fullmatch(repository)
    ):
        raise SharingError("community repository must be owner/repo")
    branch = settings.get("branch", "main")
    if not isinstance(branch, str) or not NAME.fullmatch(branch):
        raise SharingError("community branch is invalid")
    roots = settings.get("project_roots")
    if roots is None:
        roots = []
    if not isinstance(roots, list) or len(set(map(str, roots))) != len(roots):
        raise SharingError("community project_roots must be a unique list")
    if enabled and not roots:
        raise SharingError("community project_roots must be a non-empty unique list")
    if len(roots) > MAX_ROOTS:
        raise SharingError("community project_roots exceeds adapter bound")
    roots = [canonical_root(root) for root in roots]
    idle = settings.get("idle_seconds", 300)
    if idle is not None and (
        not isinstance(idle, int) or isinstance(idle, bool)
    ):
        raise SharingError("community idle_seconds must be an integer")
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


def normalize_with_runtime(settings, python, config_file=None):
    """Definitive shared-core normalize through the committed interpreter."""
    if not isinstance(python, str) or not python:
        raise SharingError("adapter configuration has no runtime interpreter")
    try:
        output = run(
            [python, "-c", NORMALIZE_SCRIPT],
            json.dumps(settings),
            timeout=10,
            max_output=65536,
            env=generation_env(config_file) if config_file is not None else {
                key: value for key, value in os.environ.items() if key != "PYTHONPATH"
            },
        )
        value = json.loads(output)
    except Exception as exc:
        raise SharingError(
            f"core sharing normalize failed: {type(exc).__name__}: {str(exc)[:200]}"
        )
    if not isinstance(value, dict):
        raise SharingError("core sharing normalize returned a non-object")
    return value


def read(config_file=None):
    """Cheap fail-closed view for capture precheck; no interpreter spawn."""
    try:
        raw = json.loads(configured_path(config_file).read_text())
        settings = validate(raw)
    except (OSError, ValueError):
        return None
    if not settings["enabled"]:
        return None
    return settings


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


def adapter_choice(config_file=None):
    try:
        value = json.loads(Path(config_file or config_path()).read_text()).get(
            "sharing_choice"
        )
    except (OSError, ValueError):
        return None
    if value in {"contribute", "read-only", "later"}:
        return value
    return None


def record_choice(choice, config_file=None):
    if choice not in {"contribute", "read-only", "later"}:
        raise SharingError("sharing choice must be contribute, read-only or later")
    config_file = Path(config_file or config_path())
    with update_lock(config_file):
        adapter = json.loads(config_file.read_text())
        adapter["sharing_choice"] = choice
        write(config_file, adapter)
        return choice


def first_use(config_file=None):
    """None once a choice exists; otherwise the three first-use options."""
    if adapter_choice(config_file) is not None:
        return None
    try:
        settings = json.loads(configured_path(config_file).read_text())
        if isinstance(settings, dict) and settings.get("schema") == SCHEMA:
            return None
    except (OSError, ValueError, SharingError):
        pass
    return dict(
        state="unconfigured",
        prompt=CHOICES,
        choices=["contribute", "read-only", "later"],
    )


def set_enabled(enable, config_file=None):
    """Flip the sharing switch atomically; keep the recorded scope/settings.

    Enabling sets a fresh enabled_at: material from a disabled period is
    never backfilled, and failed-attempt budgets survive. Disabling keeps
    drafts and published data untouched. The core normalizer is the range
    authority.
    """
    config_file = Path(config_file or config_path())
    with update_lock(config_file):
        path = configured_path(config_file)
        adapter = json.loads(config_file.read_text())
        python = adapter.get("python")
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            raise SharingError(
                "community sharing is not configured; run setup.py configure "
                "with --community-* to select repository, scope and visibility first"
            )
        settings = validate(raw)
        settings["enabled"] = bool(enable)
        settings["enabled_at"] = time.time() if enable else None
        settings["generation"] = secrets.token_hex(8)
        normalized = normalize_with_runtime(settings, python, config_file)
        write(path, normalized)
        if enable:
            adapter["sharing_choice"] = "contribute"
            write(config_file, adapter)
        return normalized


def status(config_file=None):
    """Read-only sharing status; initializes no service, model or database."""
    config_file = Path(config_file or config_path())
    choice = adapter_choice(config_file)
    unused = first_use(config_file)
    try:
        path = configured_path(config_file)
    except (OSError, ValueError) as exc:
        return dict(
            state="unconfigured",
            detail=str(exc)[:200],
            sharing_choice=choice,
            first_use=unused,
        )
    if not path.exists():
        return dict(
            state="off",
            detail="no community settings recorded",
            sharing_choice=choice,
            first_use=unused,
        )
    try:
        settings = validate(json.loads(path.read_text()))
    except (OSError, ValueError) as exc:
        return dict(
            state="malformed",
            detail=str(exc)[:200],
            capture="fail-closed; retrieval and updates unaffected",
            sharing_choice=choice,
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
        sharing_choice=choice,
        note="capture only processes material authorized after enabled_at; "
        "no disabled-period backfill",
    )
