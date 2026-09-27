"""Community sharing settings: separate from plugin activation, default OFF.

One shared local JSON file (schema mindie-community-config/1) is read by both
the adapter and the knowledge core. Plugin activation never authorizes
capture by itself; missing, malformed or disabled community state fails
closed for capture but never blocks retrieval, feedback or updates. Only
MindIE-owned files are ever written here; no unrelated plugin, scope or
global configuration is touched. Validated extension keys owned by sibling
components (publishing/transaction/bot/transport, private config_path, ...)
pass through every adapter mutation byte-identical; the framework-owned
``consent_config`` extension points the core gate at the profile consent
authority and is wired at install/upgrade/entry boundaries.

Reads follow the adapter-config pointer exactly — no implicit multi-file
rewrite and no fallback to a second authority. Convergence on the
profile-shared path and one-time legacy adoption happen only at explicit
boundaries (``migrate_community_path`` from setup/upgrade/entry attach).

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


def community_write_lock(config_file=None):
    """The one cross-process write lock for the profile community authority.

    Every enable/disable/configure/migration write to this profile's
    community settings serializes here, with the complete
    read-merge-replace inside. The lock key is the authority's sibling
    ``mindie-community.json.lock`` — the same key all adapters and the core
    settings writer use for this profile — implemented with the shared
    consent store's bounded file lock (no forked protocol). The generation
    update lock keeps protecting the running version; it never substitutes
    for this data write lock. Converges to core's ``settings.write*`` API
    when kimi-core publishes it (same key, same mechanism).
    """
    import consent
    import consent_store

    shared = consent.shared_community_path_for(config_file or config_path())
    return consent_store._UpdateLock(shared.with_name(shared.name + ".lock"))


def configured_path(config_file=None):
    """The designated community settings path.

    The profile-shared conventional path is the live authority whenever it
    exists — a legacy adapter-config pointer only locates a not-yet-migrated
    file. Pure read: no adoption copy, no config rewrite, and never a
    fallback to a second file when the authority is unreadable or damaged.
    Convergence of the pointer itself happens only at the explicit
    install/upgrade/entry boundaries (``migrate_community_path``).
    """
    import consent

    config_file = Path(config_file or config_path())
    shared = consent.shared_community_path_for(config_file)
    if shared.exists():
        return shared
    config = json.loads(config_file.read_text())
    value = config.get("community_config")
    if not isinstance(value, str) or not os.path.isabs(value):
        raise SharingError(
            "adapter configuration lacks an absolute community_config pointer"
        )
    return Path(value)


def _migrate_community_locked(config_file):
    """The resolve → adopt → repoint → wire sequence; the caller MUST hold
    ``community_write_lock(config_file)`` — one context for the whole
    boundary, so a toggle or configure can never interleave mid-sequence."""
    import consent

    shared = consent.shared_community_path_for(config_file)
    authority = str(consent.consent_path_for(config_file))
    result = dict(status="current", path=str(shared), detail=None)
    adapter = json.loads(config_file.read_text())
    pointer = adapter.get("community_config")
    pointer = (
        Path(pointer)
        if isinstance(pointer, str) and os.path.isabs(pointer)
        else None
    )
    if pointer != shared:
        if (
            pointer is not None
            and pointer.exists()
            and not shared.exists()
        ):
            shared.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(
                dir=shared.parent, prefix=".community-"
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(pointer.read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(name, 0o600)
                os.replace(name, shared)
            finally:
                Path(name).unlink(missing_ok=True)
            result.update(status="adopted", adopted_from=str(pointer))
        elif pointer is not None and pointer.exists():
            if pointer.read_bytes() != shared.read_bytes():
                result["detail"] = (
                    "legacy community settings differ from the shared "
                    "authority; the shared file wins unchanged and the "
                    f"legacy file is kept as evidence: {pointer}"
                )
        if result["status"] == "current":
            result["status"] = "repointed"
        adapter["community_config"] = str(shared)
        write(config_file, adapter)
        engine_value = adapter.get("engine_config")
        if isinstance(engine_value, str) and os.path.isabs(engine_value):
            engine_path = Path(engine_value)
            try:
                engine = json.loads(engine_path.read_text())
            except (OSError, ValueError):
                engine = None
            if (
                isinstance(engine, dict)
                and engine.get("community_config") != str(shared)
            ):
                engine["community_config"] = str(shared)
                write(engine_path, engine)
    if shared.exists():
        # The consent wiring stamp may only touch a document that passes the
        # existing structural validator: a damaged, foreign-schema or
        # otherwise non-conforming document is preserved byte-identical and
        # reported, never implicitly repaired or stamped (the same contract
        # core's update_extensions enforces; the explicit managed configure
        # remains the only repair path for malformed values).
        try:
            settings = validate(json.loads(shared.read_text()))
        except ValueError:
            settings = None
        if settings is None:
            if result["detail"] is None:
                result["detail"] = (
                    "shared community settings are damaged or non-conforming; "
                    "bytes preserved and the consent wiring stamp skipped — "
                    "the fault surfaces via status; repair is an explicit "
                    "managed configure or manual removal"
                )
        elif (
            not isinstance(settings.get("consent_config"), str)
            or not os.path.isabs(settings["consent_config"])
            or settings["consent_config"] != authority
        ):
            raw = json.loads(shared.read_text())
            raw["consent_config"] = authority
            write(shared, raw)
    return result


def migrate_community_path(config_file=None):
    """Explicit boundary convergence on the profile-shared community path.

    Called at install/upgrade/entry-attach boundaries only, never from
    status/load. A legacy adapter-specific file is adopted exactly once
    (atomic copy; the legacy file is kept as evidence) and the adapter and
    engine configurations are repointed so adapter, engine and worker read
    the same designated authority. When both files exist, the shared
    authority always wins as-is — scopes are never merged into a larger
    public range — and a content difference is reported. The consent
    authority pointer (``consent_config`` extension) is wired into the
    settings here so the core gate reads the same profile document.
    """
    import consent

    config_file = Path(config_file or config_path())
    shared = consent.shared_community_path_for(config_file)
    authority = str(consent.consent_path_for(config_file))
    # Steady-state fast path without the lock: pointer already converged and
    # the consent wiring already present.
    try:
        current = json.loads(config_file.read_text())
        if current.get("community_config") == str(shared):
            try:
                present = json.loads(shared.read_text())
            except (OSError, ValueError):
                present = None
            if present is None or (
                isinstance(present, dict)
                and present.get("consent_config") == authority
            ):
                return dict(status="current", path=str(shared), detail=None)
    except (OSError, ValueError):
        pass
    with community_write_lock(config_file):
        return _migrate_community_locked(config_file)


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
    consent_config = settings.get("consent_config")
    if consent_config is not None and not (
        isinstance(consent_config, str) and os.path.isabs(consent_config)
    ):
        raise SharingError("community consent_config must be an absolute path")
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


def consent_allows(settings, config_file=None):
    """The consent_config extension gate on the adapter's own write path.

    ``None`` — the legacy format without the field keeps its previous read
    compatibility (the community enabled flag alone decides). When the field
    is present, the named profile consent authority must hold a saved
    ``contribute`` choice: missing, unreadable, corrupt, or any other saved
    choice stops the capture write path — the same rule the core gate
    applies at the engine/model/outbound boundaries. The field grants no
    permission by itself; explicit enabled=false still wins first.
    """
    ref = settings.get("consent_config")
    if ref is None:
        return None
    if not isinstance(ref, str) or not os.path.isabs(ref):
        return False
    import consent_store

    view = consent_store.read(ref)
    return view["state"] == "ok" and view["choice"] == "contribute"


def capture_allowed(lease, cwd, config_file=None):
    """active lease AND community enabled AND lease project root in scope,
    plus the consent gate when the settings carry the consent authority.

    Scope is the lease's authorized activation-time project_root, never a
    caller-supplied cwd pointing into an allowed directory. Anything
    unreadable or incomplete fails closed for capture only.
    """
    settings = read(config_file)
    if settings is None or not settings["enabled"]:
        return False
    if consent_allows(settings, config_file) is False:
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


def adapter_choice(config_file=None, saved=None):
    """The persistent install-level choice from the shared consent document.

    ``saved`` may carry an already-taken consent read so one status pass
    loads the authority exactly once.
    """
    import consent

    saved = saved if saved is not None else consent.load(config_file)
    if saved["state"] == "ok" and saved["choice"] in consent.CHOICES:
        return saved["choice"]
    return None


def record_choice(choice, config_file=None):
    if choice not in {"contribute", "read-only", "later"}:
        raise SharingError("sharing choice must be contribute, read-only or later")
    import consent

    return consent.record_choice(choice, config_file)


def first_use(config_file=None, saved=None):
    """None once a choice exists, saved state is damaged, or any install
    trace shows this is an existing installation pending its one-time
    boundary migration; otherwise the three first-use options. Installer
    default-off is unchosen: the one-time setup is presented exactly until
    a choice is recorded."""
    import consent

    saved = saved if saved is not None else consent.load(config_file)
    if saved["state"] == "ok" and saved["choice"]:
        return None
    if saved["state"] in {"corrupt", "unreadable"}:
        return None  # a fault reported by status, never a fresh onboarding
    if consent.install_traces(config_file):
        return None  # existing installation; the entry boundary migrates it
    try:
        json.loads(configured_path(config_file).read_text())
    except json.JSONDecodeError:
        return None  # damaged settings: a fault, not onboarding
    except (OSError, ValueError, SharingError):
        pass
    return dict(
        state="unconfigured",
        prompt=CHOICES,
        choices=["contribute", "read-only", "later"],
    )


def set_enabled(enable, config_file=None):
    """Flip the sharing switch atomically; keep the recorded scope/settings.

    The complete read-merge-replace runs inside the profile's community
    write lock: a concurrent toggle, configure or migration can never
    overwrite this user's committed choice with a stale read. The current
    authority is resolved and re-read inside the lock. Enabling refreshes
    enabled_at only on an off->on edge (the shared core write semantics):
    material from a disabled period is never backfilled, a redundant enable
    manufactures no capture gap, and failed-attempt budgets survive.
    Disabling keeps drafts and published data untouched. The core
    normalizer is the range authority.
    """
    config_file = Path(config_file or config_path())
    with update_lock(config_file):
        adapter = json.loads(config_file.read_text())
        python = adapter.get("python")
    import consent

    with community_write_lock(config_file):
        path = configured_path(config_file)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise SharingError(
                "community sharing is not configured; run setup.py configure "
                "with --community-* to select repository, scope and visibility first"
            ) from None
        except OSError:
            raise SharingError(
                "community settings file is unreadable; inspect and repair "
                "the designated file explicitly — no fallback authority is used"
            ) from None
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise SharingError(
                "community settings file is damaged; its bytes are preserved. "
                "Inspect the file and remove it explicitly before configuring "
                "again — a damaged authority is never silently replaced"
            ) from None
        settings = validate(parsed)
        was_enabled = settings["enabled"]
        settings["enabled"] = bool(enable)
        # Match the shared core write() semantics: enabled_at refreshes only
        # on an off->on edge (a redundant enable must not manufacture a
        # capture gap); disabling clears it. Generation always refreshes —
        # it is the core-observed cancellation signal.
        settings["enabled_at"] = (
            time.time()
            if enable and not was_enabled
            else settings["enabled_at"] if enable else None
        )
        settings["generation"] = secrets.token_hex(8)
        # Keep the consent-authority pointer wired on this user-intent
        # boundary; an explicit value pointing elsewhere is left for the
        # migration boundary to correct.
        settings.setdefault(
            "consent_config", str(consent.consent_path_for(config_file))
        )
        normalized = normalize_with_runtime(settings, python, config_file)
        write(path, normalized)
    consent.record_choice(
        "contribute" if enable else "disabled", config_file
    )
    return normalized


def status(config_file=None, saved=None):
    """Read-only sharing status; initializes no service, model or database.

    One consent authority read per call (``saved`` may carry a caller's
    already-taken read); every branch reports the same ``first_use`` value.
    """
    import consent

    config_file = Path(config_file or config_path())
    saved = saved if saved is not None else consent.load(config_file)
    choice = adapter_choice(config_file, saved)
    unused = first_use(config_file, saved)
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
            first_use=unused,
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
        first_use=unused,
        note="capture only processes material authorized after enabled_at; "
        "no disabled-period backfill",
    )
