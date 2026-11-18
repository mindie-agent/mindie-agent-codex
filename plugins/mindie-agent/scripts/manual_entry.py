"""[Role] Passive UserPromptSubmit hook handler and session status receipt helper.

[Invariants]
- All prompts pass through passively without requiring prompt prefixes or gestures.
- Cold prompts never read configuration or initialize SQLite state.

[Timeouts & Locks]
- Input is bounded at 32 MiB; receipt writes use atomic os.replace().

[Side-Effect Policy]
- Passive UserPromptSubmit mutates no Admission lease and starts no service,
  corpus, model, transcript or contribution worker.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time

from session_gate import IDENTITY

ENTRY_STATUS_SCHEMA = "mindie-manual-entry/2"
_MAX_SESSION_RECEIPTS = 16
_UNSCOPED_FAILURE_TTL = 300.0
_STAGE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{16}\Z")


def entry_status_path(config_file):
    return Path(config_file).expanduser().absolute().with_suffix(".entry-status.json")


def _session_digest(session):
    if not isinstance(session, str) or not IDENTITY.fullmatch(session):
        return None
    return hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]


def _valid_entry(item):
    return (
        isinstance(item, dict)
        and item.get("status") in {"authorized", "deactivated", "failed"}
        and type(item.get("updated_at")) in (int, float)
        and math.isfinite(item["updated_at"])
    )


def _load_status_store(target):
    try:
        if not target.is_file():
            return {"schema": ENTRY_STATUS_SCHEMA, "sessions": {}}
        with target.open("rb") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            return {"schema": ENTRY_STATUS_SCHEMA, "sessions": {}}
        value = json.loads(raw.decode("utf-8"))
    except TimeoutError:
        raise
    except (OSError, ValueError, TypeError):
        return {"schema": ENTRY_STATUS_SCHEMA, "sessions": {}}
    if not isinstance(value, dict) or value.get("schema") != ENTRY_STATUS_SCHEMA:
        return {"schema": ENTRY_STATUS_SCHEMA, "sessions": {}}
    raw_sessions = value.get("sessions")
    sessions = {}
    if isinstance(raw_sessions, dict):
        for key, item in raw_sessions.items():
            if isinstance(key, str) and _DIGEST.fullmatch(key) and _valid_entry(item):
                sessions[key] = item
    store = {"schema": ENTRY_STATUS_SCHEMA, "sessions": sessions}
    unscoped = value.get("unscoped_failure")
    if _valid_entry(unscoped) and unscoped.get("status") == "failed":
        store["unscoped_failure"] = unscoped
    return store


def record_entry_status(config_file, *, stage, status, session_id=None, error_type=None):
    """Persist a session-keyed status receipt when the config directory exists."""
    if status not in {"authorized", "deactivated", "failed"}:
        return
    target = entry_status_path(config_file)
    if not target.parent.is_dir():
        if status in {"authorized", "deactivated"}:
            raise FileNotFoundError("entry status receipt directory does not exist")
        return
    safe_stage = stage if isinstance(stage, str) and _STAGE.fullmatch(stage) else "entry"
    safe_error = (
        error_type
        if isinstance(error_type, str) and _ERROR_TYPE.fullmatch(error_type)
        else None
    )
    entry = dict(
        updated_at=time.time(),
        stage=safe_stage,
        status=status,
        error_type=safe_error,
    )
    store = _load_status_store(target)
    digest = _session_digest(session_id)
    if digest is not None:
        sessions = dict(store["sessions"])
        sessions[digest] = entry
        if len(sessions) > _MAX_SESSION_RECEIPTS:
            ordered = sorted(sessions.items(), key=lambda pair: pair[1]["updated_at"])
            sessions = dict(ordered[-_MAX_SESSION_RECEIPTS:])
        store["sessions"] = sessions
    elif status == "failed":
        store["unscoped_failure"] = entry
    else:
        return
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(store, stream, sort_keys=True)
            stream.flush()
        os.replace(temporary, target)
    except TimeoutError:
        raise
    except OSError:
        raise
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def read_entry_status(config_file, session_id=None):
    store = _load_status_store(entry_status_path(config_file))
    if session_id is None:
        return store if (store["sessions"] or "unscoped_failure" in store) else None
    digest = _session_digest(session_id)
    if digest is not None and digest in store["sessions"]:
        return store["sessions"][digest]
    unscoped = store.get("unscoped_failure")
    if unscoped is not None and 0 <= time.time() - unscoped["updated_at"] <= _UNSCOPED_FAILURE_TTL:
        return dict(unscoped, unscoped=True)
    return None


def prompt_failure_response(stage, error_type):
    safe_stage = stage if isinstance(stage, str) and _STAGE.fullmatch(stage) else "entry"
    safe_error = (
        error_type
        if isinstance(error_type, str) and _ERROR_TYPE.fullmatch(error_type)
        else "RuntimeError"
    )
    return dict(
        systemMessage=(
            f"MindIE prompt hook failed at {safe_stage} ({safe_error}); "
            "inspect local status/permissions and continue with available tools."
        ),
        hookSpecificOutput=dict(
            hookEventName="UserPromptSubmit",
            additionalContext=(
                f"MindIE UserPromptSubmit failed at stage '{safe_stage}' ({safe_error}); "
                "inspect local status/permissions if needed and continue independent task work."
            ),
        ),
    )


def _unwritable_state_hint(config_file, admission_path=None):
    if os.name == "nt":
        return False
    for candidate in (admission_path, config_file):
        if not candidate:
            continue
        current = Path(candidate).expanduser().absolute()
        if current.exists():
            return not os.access(current, os.W_OK)
        for parent in current.parents:
            if parent.exists():
                return not os.access(parent, os.W_OK)
    return False


def explain_inactive_session(config_file, session, *, admission_path=None):
    """Return a stage-specific Inactive message (<260 chars) without leaking paths or IDs."""
    receipt = read_entry_status(config_file, session_id=session) if config_file is not None else None
    if receipt is not None:
        status = receipt.get("status")
        stage = receipt.get("stage")
        stage = stage if isinstance(stage, str) and _STAGE.fullmatch(stage) else "entry"
        error_type = receipt.get("error_type")
        error_type = (
            error_type
            if isinstance(error_type, str) and _ERROR_TYPE.fullmatch(error_type)
            else "RuntimeError"
        )
        if status == "failed":
            return (
                f"Knowledge is unavailable in this session: hook failed at "
                f"{stage} ({error_type}); inspect status/permissions and continue with native tools"
            )
        if status == "deactivated":
            return (
                "Knowledge was explicitly deactivated for this session; "
                "continue with native or deterministic tools"
            )
    if config_file is not None and _unwritable_state_hint(config_file, admission_path=admission_path):
        return (
            "Knowledge state is unwritable (admission_unwritable: sandbox or "
            "permissions blocked state writes); continue with native/deterministic tools"
        )
    return "Knowledge is inactive for this session; continue with native/deterministic tools"


def requested(event):
    """UserPromptSubmit is always a fast passive pass-through."""
    return False


def read_event(stream):
    raw = stream.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("native prompt hook exceeds its byte budget")
    text = raw.decode("utf-8-sig") if isinstance(raw, (bytes, bytearray)) else str(raw).lstrip("\ufeff")
    return json.loads(text)


def action(event):
    return ""


def authorize(adapter, event):
    return {}
