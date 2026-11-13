"""[Role] Bind an explicit native UserPromptSubmit gesture to its session.

[Invariants]
- Ordinary prompts return no knowledge context and never read configuration.
- Only the native hook's session/cwd can establish the manual entry grant.
- A later turn keeps the grant; new sessions and subagents never inherit it.

[Timeouts & Locks]
- Input is bounded at 32 MiB; writes use Admission's WAL/5-second lock budget.

[Side-Effect Policy]
- Explicit entry mutates only the existing Admission lease; no knowledge
  service, corpus, model, transcript or contribution worker is started here.
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

ENTRY_STATUS_SCHEMA = "mindie-manual-entry/1"
_STAGE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
# Match explicit start-of-prompt /mindie-agent, $mindie-agent, and Codex
# Composer's linked skill/plugin pill forms (plus optional leading UTF-8 BOM).
GESTURE = re.compile(
    r"^[\s\ufeff]*(?:"
    r"/mindie-agent"
    r"|\$mindie-agent(?::mindie-agent)?"
    r"|\[\$mindie-agent(?::mindie-agent)?\]\([^\r\n)]+\)"
    r"|\[@mindie-agent\]\(plugin://mindie-agent@[^\r\n)]+\)"
    r")(?=\s|$)"
)


def entry_status_path(config_file):
    return Path(config_file).expanduser().absolute().with_suffix(".entry-status.json")


def _session_digest(session):
    if not isinstance(session, str) or not IDENTITY.fullmatch(session):
        return None
    return hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]


def record_entry_status(config_file, *, stage, status, session_id=None, error_type=None):
    """Persist a sanitized manual-entry receipt when the config directory exists."""
    if status not in {"authorized", "deactivated", "failed"}:
        return
    target = entry_status_path(config_file)
    if not target.parent.is_dir():
        return
    safe_stage = stage if isinstance(stage, str) and _STAGE.fullmatch(stage) else "entry"
    safe_error = (
        error_type
        if isinstance(error_type, str) and _ERROR_TYPE.fullmatch(error_type)
        else None
    )
    payload = dict(
        schema=ENTRY_STATUS_SCHEMA,
        updated_at=time.time(),
        stage=safe_stage,
        status=status,
        session_sha256=_session_digest(session_id),
        error_type=safe_error,
    )
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
        os.replace(temporary, target)
    except TimeoutError:
        raise
    except OSError:
        # Read-only sandboxes cannot persist receipts; keep the primary hook outcome intact.
        return
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def read_entry_status(config_file):
    target = entry_status_path(config_file)
    try:
        if not target.is_file():
            return None
        with target.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            return None
        value = json.loads(raw.decode("utf-8"))
    except TimeoutError:
        raise
    except (OSError, ValueError, TypeError):
        return None
    if (not isinstance(value, dict)
            or value.get("schema") != ENTRY_STATUS_SCHEMA
            or value.get("status") not in {"authorized", "deactivated", "failed"}
            or type(value.get("updated_at")) not in (int, float)
            or not math.isfinite(value["updated_at"])):
        return None
    return value


def prompt_failure_response(stage, error_type):
    safe_stage = stage if isinstance(stage, str) and _STAGE.fullmatch(stage) else "entry"
    safe_error = (
        error_type
        if isinstance(error_type, str) and _ERROR_TYPE.fullmatch(error_type)
        else "RuntimeError"
    )
    return dict(
        systemMessage=(
            f"MindIE session authorization failed at {safe_stage} ({safe_error}); "
            "inspect local status/permissions and do not repeat /mindie-agent in a loop."
        ),
        hookSpecificOutput=dict(
            hookEventName="UserPromptSubmit",
            additionalContext=(
                f"MindIE UserPromptSubmit failed at stage '{safe_stage}' ({safe_error}) "
                "before session authorization completed. Do NOT ask the user to resend "
                "/mindie-agent in a loop; inspect local status/permissions if needed and "
                "continue independent task work with native or deterministic tools."
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
    digest = _session_digest(session)
    receipt = read_entry_status(config_file) if config_file is not None else None
    if receipt is not None:
        status = receipt.get("status")
        recorded_session = receipt.get("session_sha256")
        stage = receipt.get("stage")
        stage = stage if isinstance(stage, str) and _STAGE.fullmatch(stage) else "entry"
        error_type = receipt.get("error_type")
        error_type = (
            error_type
            if isinstance(error_type, str) and _ERROR_TYPE.fullmatch(error_type)
            else "RuntimeError"
        )
        if status == "failed" and (recorded_session is None or recorded_session == digest):
            return (
                f"Knowledge is not authorized in this session: the last /mindie-agent hook "
                f"failed at {stage} ({error_type}); inspect status/permissions, do not repeat "
                "/mindie-agent in a loop, and continue with native/deterministic tools"
            )
        if status == "deactivated" and recorded_session == digest:
            return (
                "Knowledge is not authorized in this session: /mindie-agent was deactivated; "
                "send /mindie-agent only to re-enable, or continue with native/deterministic tools"
            )
        if status == "authorized" and recorded_session and digest and recorded_session != digest:
            return (
                "Knowledge is not authorized in this session (session_mismatch: /mindie-agent "
                "authorized another CODEX_THREAD_ID); do not repeat /mindie-agent in a loop; "
                "use the top-level session or continue with native/deterministic tools"
            )
    if config_file is not None and _unwritable_state_hint(config_file, admission_path=admission_path):
        return (
            "Knowledge is not authorized in this session (admission_unwritable: sandbox or "
            "permissions blocked /mindie-agent state writes); do not repeat /mindie-agent in "
            "a loop; continue with native/deterministic tools"
        )
    return (
        "Knowledge is not authorized in this session (no /mindie-agent hook receipt; send "
        "/mindie-agent at prompt start, or if already sent in this turn, UserPromptSubmit did "
        "not run—do not resend in a loop; continue with native/deterministic tools)"
    )


def requested(event):
    return (isinstance(event, dict)
            and event.get("hook_event_name") == "UserPromptSubmit"
            and not event.get("agent_id")
            and isinstance(event.get("prompt"), str)
            and GESTURE.match(event["prompt"]) is not None)


def read_event(stream):
    raw = stream.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("native prompt hook exceeds its byte budget")
    return json.loads(raw)


def action(event):
    return event["prompt"][GESTURE.match(event["prompt"]).end():].strip()


def authorize(adapter, event):
    if not requested(event):
        return {}
    session, scope = event.get("session_id"), event.get("cwd")
    if not isinstance(session, str) or not IDENTITY.fullmatch(session):
        raise ValueError("native prompt hook has no valid session identity")
    if not isinstance(scope, str) or not Path(scope).is_absolute():
        raise ValueError("native prompt hook has no absolute project scope")
    from mindie_knowledge.loop.activation import Admission

    if action(event) == "deactivate":
        from mindie_knowledge.loop.domain_routes import deactivate_registered_domains
        result = deactivate_registered_domains(
            adapter["engine_config"], domain_configs=adapter.get("domain_configs"),
            session=session,
        )
        if result["status"] != "inactive":
            return dict(
                systemMessage="MindIE session deactivation is incomplete; inspect the per-domain result.",
                hookSpecificOutput=dict(hookEventName="UserPromptSubmit", additionalContext=
                    "The user requested session deactivation. Preserve completed revocations and inspect "
                    "the remaining domains; do not claim global completion: " + json.dumps(result)),
            )
        context = "MindIE knowledge is deactivated for this session. Remote-dev remains available."
    else:
        Admission(adapter["admission_path"]).activate(
            session, project_root=str(Path(scope).resolve()), root_session=session,
        )
        context = ("The user explicitly enabled MindIE knowledge for this session. "
                   "Follow the installed mindie-agent skill for this task. Later turns keep this grant; "
                   "background capture additionally requires saved contribution consent and project scope.")
        plugin_root = os.environ.get("PLUGIN_ROOT")
        if plugin_root:
            context += " Read the entry instructions at " + str(
                Path(plugin_root) / "skills/mindie-agent/SKILL.md") + "."
    return dict(hookSpecificOutput=dict(hookEventName="UserPromptSubmit", additionalContext=context))
