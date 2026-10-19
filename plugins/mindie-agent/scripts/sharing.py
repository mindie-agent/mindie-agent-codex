"""Community sharing settings: separate from plugin activation, default OFF.

[Role]
Reads, validates, and updates the `mindie-community-config/1` JSON file and
consent authority shared between the Codex adapter and knowledge core.

[Invariants]
- Plugin activation never authorizes capture by itself; missing, malformed, or
  disabled community state fails closed for capture without blocking retrieval,
  feedback, or updates.
- Preserves sibling extension keys (`publishing`, `transaction`, `bot`,
  `transport`, `consent_config`, etc.) byte-identical across mutations.

[Timeouts & Locks]
- Uses `bounded_process.run` when delegating normalization to the configured
  runtime interpreter.

[Side-Effect Policy]
- Writes only to the designated MindIE community config and consent files via
  atomic temp-file replacement.
"""

import json
import math
import ntpath
import os
from pathlib import Path
import re
import secrets
import tempfile
import time

from bounded_process import run
from session_gate import config_path, runtime_env
from update_lock import update_lock

SCHEMA = "mindie-community-config/1"
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
    "Experience capture is not configured. Supply the missing public "
    "project/repository/account — scripts/setup.py configure "
    "--community-repository OWNER/REPO --community-project-root PATH "
    "--community-visibility public [--community-account NAME]. "
    "Contribution authorizes scoped knowledge PRs and sanitized product fault Issues together. "
    "Reuse existing user-approved values. Installation or task binding alone "
    "does not complete configuration."
)
NORMALIZE_SCRIPT = """
import json, sys
from mindie_knowledge.loop.settings import normalize, transition
try:
    request = json.loads(sys.stdin.read())
    value = (normalize(request['settings']) if request['previous'] is None
             else transition(request['previous'], request['settings']))
except ValueError as exc:
    print(json.dumps({"rejected": str(exc)[:240]}, ensure_ascii=False))
else:
    print(json.dumps({"ok": value}, ensure_ascii=False))
"""


class SharingError(ValueError):
    pass


def community_write_lock(config_file=None):
    """The one cross-process write lock for the profile community authority.

    Every enable/disable/configure write to this profile's
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
    from mindie_knowledge import consent_store

    shared = consent.shared_community_path_for(config_file or config_path())
    return consent_store._UpdateLock(shared.with_name(shared.name + ".lock"))


def configured_path(config_file=None, *, adapter=None):
    """The explicit profile authority; no legacy pointer fallback."""
    import consent

    config_file = Path(config_file or config_path())
    shared = consent.shared_community_path_for(config_file)
    config = adapter if adapter is not None else json.loads(config_file.read_text(encoding='utf-8'))
    value = config.get("community_config")
    if not isinstance(value, str) or not os.path.isabs(value) or Path(value) != shared:
        raise SharingError(
            "unsupported community configuration; finish installation with an explicit profile"
        )
    return shared






def canonical_root(value):
    if not isinstance(value, str) or not value:
        raise SharingError("community project roots must be path strings")
    if not os.path.isabs(value):
        raise SharingError("community project roots must be absolute: " + value[:80])
    if os.name == 'nt':
        value = ntpath.abspath(value)
        if not value.startswith('\\\\?\\'):
            value = '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value
    root = str(Path(value).resolve())
    if os.name == 'nt':
        # Comparison only: preserve extended paths at the actual I/O boundary.
        if root[:8].casefold() == '\\\\?\\unc\\':
            root = '\\\\' + root[8:]
        elif root.startswith('\\\\?\\'):
            root = root[4:]
        root = ntpath.normcase(root)
    return Path(root).as_posix()


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
        if not isinstance(key, str):
            raise SharingError("community extension key is invalid")
        result[key] = value
    return result


def normalize_with_runtime(settings, python, config_file=None, *, previous=None):
    """Definitive shared-core normalize through the committed interpreter.

    A document the core rejects raises a SharingError naming the rejection;
    a genuinely unavailable runtime fails with a distinct message — the two
    faults are never conflated.
    """
    if not isinstance(python, str) or not python:
        raise SharingError("adapter configuration has no runtime interpreter")
    try:
        request = json.dumps(dict(settings=settings, previous=previous))
        output = run(
            [python, "-c", NORMALIZE_SCRIPT],
            request,
            timeout=None,
            max_output=max(65536, len(request.encode('utf-8')) * 2),
            env=runtime_env(config_file) if config_file is not None else {
                key: value for key, value in os.environ.items() if key != "PYTHONPATH"
            },
        ).checked_stdout()
        value = json.loads(output)
    except Exception as exc:
        raise SharingError(
            f"core sharing normalize runtime failed: {type(exc).__name__}: {str(exc)[:200]}"
        )
    if not isinstance(value, dict):
        raise SharingError("core sharing normalize returned a non-object")
    if "rejected" in value:
        raise SharingError(
            "community settings rejected by core normalize: "
            + str(value["rejected"])[:200]
        )
    if not isinstance(value.get("ok"), dict):
        raise SharingError("core sharing normalize returned an invalid envelope")
    return value["ok"]


def read(config_file=None, *, adapter=None):
    """Cheap fail-closed view for capture precheck; no interpreter spawn."""
    try:
        raw = json.loads(configured_path(config_file, adapter=adapter).read_text(encoding='utf-8'))
        settings = validate(raw)
    except FileNotFoundError:
        return None
    return settings


def write(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".community-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def consent_allows(settings, config_file=None):
    """Require the explicit consent authority before capture or publication."""
    ref = settings.get("consent_config")
    if not isinstance(ref, str) or not os.path.isabs(ref):
        raise SharingError("unsupported capture configuration: explicit consent authority is required")
    from mindie_knowledge import consent_store

    view = consent_store.read(ref)
    if view["state"] != "ok":
        raise SharingError("consent authority is unavailable: " + view["state"])
    return view["choice"] == "contribute"


def capture_allowed(lease, cwd, config_file=None, *, adapter=None):
    """active lease AND community enabled AND lease project root in scope,
    plus the consent gate when the settings carry the consent authority.

    Scope is the lease's authorized activation-time project_root, never a
    caller-supplied cwd pointing into an allowed directory. Anything
    unreadable or incomplete fails closed for capture only.
    """
    settings = read(config_file, adapter=adapter)
    if settings is None or not settings["enabled"]:
        return False
    if consent_allows(settings, config_file) is False:
        return False
    if any(
        lease.get(key) is None
        for key in ("project_root", "root_session", "activated_at")
    ):
        raise SharingError("capture lease metadata is incomplete")
    try:
        root = Path(canonical_root(lease["project_root"]))
    except (OSError, ValueError) as exc:
        raise SharingError("capture lease scope is unreadable") from exc
    return any(
        root.is_relative_to(Path(allowed))
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
    """Offer missing setup only when no configuration or explicit choice exists."""
    import consent

    saved = saved if saved is not None else consent.load(config_file)
    if saved["state"] == "ok" and saved["choice"]:
        return None
    if saved["state"] in {"corrupt", "unreadable"}:
        return None  # a fault reported by status, never a fresh onboarding
    if consent.install_traces(config_file):
        return None
    try:
        json.loads(configured_path(config_file).read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return None  # damaged settings: a fault, not onboarding
    except (OSError, ValueError, SharingError):
        pass
    return dict(
        state="unconfigured",
        prompt=CHOICES,
        choices=[],
        required=["repository", "project_roots", "public_visibility"],
    )


def set_enabled(enable, config_file=None):
    """Flip the sharing switch atomically; keep the recorded scope/settings.

    The complete read-merge-replace runs inside the profile's community
    write lock: a concurrent toggle or configure can never
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
        adapter = json.loads(config_file.read_text(encoding='utf-8'))
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
        desired_choice = "contribute" if enable else "disabled"
        saved = consent.load(config_file)
        if (settings['enabled'] == bool(enable) and saved.get('choice') == desired_choice
                and saved.get("choice_revision")):
            return settings  # Already in the requested state: do not mutate.
        settings["enabled"] = bool(enable)
        # Keep the consent-authority pointer wired on this user-intent
        # boundary; the core validates this explicit authority.
        settings.setdefault(
            "consent_config", str(consent.consent_path_for(config_file))
        )
        normalized = normalize_with_runtime(settings, python, config_file, previous=parsed)
        if normalized != parsed:
            write(path, normalized)
    consent.record_choice(
        "contribute" if enable else "disabled", config_file
    )
    return normalized


def status(config_file=None, saved=None, *, adapter=None):
    """Read-only sharing status; initializes no service, model or database.

    One consent authority read per call (``saved`` may carry a caller's
    already-taken read); every branch reports the same ``first_use`` value.
    """
    import consent

    config_file = Path(config_file or config_path())
    saved = saved if saved is not None else consent.load(config_file)
    choice = adapter_choice(config_file, saved)
    unused = first_use(config_file, saved)
    if saved["state"] in {"corrupt", "unreadable"}:
        return dict(state="malformed", detail="saved setup state is damaged",
                    sharing_choice=choice, first_use=None)
    try:
        path = configured_path(config_file, adapter=adapter)
    except (OSError, ValueError) as exc:
        return dict(
            state="unconfigured",
            detail=str(exc)[:200],
            sharing_choice=choice,
            first_use=unused,
        )
    if not path.exists():
        return dict(
            state="disabled" if choice in {"read-only", "later", "disabled"} else "unconfigured",
            detail="no community settings recorded",
            sharing_choice=choice,
            first_use=unused,
        )
    try:
        settings = validate(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError) as exc:
        return dict(
            state="malformed",
            detail=str(exc)[:200],
            capture="fail-closed; retrieval and updates unaffected",
            sharing_choice=choice,
            first_use=unused,
        )
    return dict(
        state=("disabled" if choice in {"read-only", "later", "disabled"}
               else "enabled" if settings["enabled"] and consent_allows(settings, config_file) is not False
               else "disabled" if choice and settings.get("repository") and settings.get("project_roots")
               else "unconfigured"),
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
