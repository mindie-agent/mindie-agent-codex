#!/usr/bin/env python3
"""Codex plugin boundary. Hooks never start a service or persist retry work.

The Stop hook path is a no-op unless every capture precondition holds:
an active lease, community sharing enabled, and the event's native cwd inside
the authorized scope. The sharing gate runs BEFORE any capture claim or
payload forwarding: sharing off/missing means no capture row, no draft, and
no service/worker/model startup. The hook never parses transcripts, never
blocks the original task and never uses exit-2 continuation.
"""

import json
import os
from pathlib import Path
import re
import sys

from bounded_process import run
from session_gate import Sessions, config_path
import sharing
from update_lock import update_lock

MAX_HOOK_BYTES = 128 * 1024
MAX_SUMMARY = 32768
OPERATIONS = {
    "stop",
    "mcp",
    "status",
    "shutdown",
    "activate",
    "deactivate",
    "sharing-enable",
    "sharing-disable",
    "sharing-status",
}
IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


def _bounded_path(value, name):
    if not isinstance(value, str) or not 0 < len(value) <= 1024:
        raise ValueError(f"invalid {name}")
    if not os.path.isabs(value):
        raise ValueError(f"{name} must be absolute")
    return value


def hook_event():
    """Validate one bounded native Stop envelope; whitelist forwarding fields.

    A valid transcript event is never rejected for a missing final summary:
    transcript_path alone is enough. The transcript itself is never opened
    here; only its location and the bounded summary cross the boundary.
    """
    raw = sys.stdin.buffer.read(MAX_HOOK_BYTES + 1)
    if len(raw) > MAX_HOOK_BYTES:
        raise ValueError("hook input exceeds limit")
    event = json.loads(raw)
    if not isinstance(event, dict) or event.get("hook_event_name") != "Stop":
        raise ValueError("unexpected hook event")
    for key in ("session_id", "turn_id"):
        if not isinstance(event.get(key), str) or not IDENTITY.fullmatch(event[key]):
            raise ValueError("invalid hook identity")
    if event.get("stop_hook_active", False) is not False:
        raise ValueError("recursive Stop is not a capture")
    cwd = _bounded_path(event.get("cwd"), "cwd")
    transcript = event.get("transcript_path")
    if transcript is not None:
        transcript = _bounded_path(transcript, "transcript_path")
    summary = event.get("last_assistant_message")
    if summary is not None and (
        not isinstance(summary, str) or len(summary) > MAX_SUMMARY
    ):
        raise ValueError("invalid final summary")
    if isinstance(summary, str) and not summary.strip():
        summary = None
    if transcript is None and summary is None:
        raise ValueError("no transcript or summary to forward")
    forwarded = dict(
        hook_event_name="Stop",
        session_id=event["session_id"],
        turn_id=event["turn_id"],
        cwd=cwd,
    )
    if transcript is not None:
        forwarded["transcript_path"] = transcript
    if summary is not None:
        forwarded["last_assistant_message"] = summary
    return forwarded


def bind(lease):
    """Attach this lease's session to the domain store via one bounded call.

    Only runs when community sharing is enabled: activation prepares the
    admitted local collection endpoint, and a failed bind leaves the lease
    usable for read tools while marking capture unbound. It is never retried
    in the background, and sharing off never cold-starts collection here.
    """
    payload = dict(
        surface="knowledge",
        name="knowledge_attach",
        internal=True,
        arguments={},
        mindie_session_id=lease["mindie_session_id"],
        mindie_activation=lease["mindie_activation"],
    )
    try:
        config = json.loads(config_path().read_text())
        output = run(
            [config["python"], str(Path(__file__).with_name("runtime_call.py"))],
            json.dumps(payload),
            timeout=15,
        )
        result = json.loads(output)
        if isinstance(result, dict) and result.get("isError") is not True:
            return "bound"
        return "unbound:service-error"
    except Exception as exc:
        return f"unbound:{type(exc).__name__}"


def activate(operation):
    result = getattr(Sessions(), operation)()
    if operation != "activate":
        return result
    settings = sharing.read()
    if settings is not None and settings["enabled"]:
        result["capture"] = bind(result)
    else:
        # Sharing off/unconfigured: ordinary activation only. No cold start,
        # no bind, no collection preparation.
        result["capture"] = "disabled"
    return result


def stop():
    try:
        event = hook_event()
    except (ValueError, OSError, TypeError, RecursionError):
        print("{}")
        return
    try:
        sessions = Sessions()
        lease = sessions.check(event["session_id"])
        # Sharing gate BEFORE any capture claim or payload forwarding.
        if not sharing.capture_allowed(lease, event["cwd"]):
            print("{}")
            return
        if not sessions.claim(
            event["session_id"], "stop", event["turn_id"], lease["token"]
        ):
            print("{}")
            return
        event["mindie_activation"] = lease["token"]
        config = json.loads(config_path().read_text())
        command = [
            config["python"],
            "-m",
            "mindie_knowledge.loop.cli",
            "hook",
            "--config",
            config["engine_config"],
        ]
        with update_lock(sessions.config):
            sessions.check(event["session_id"], lease["token"])
            run(command, json.dumps(event), timeout=1.2, max_output=32768)
    except Exception:
        # The hook never propagates a failure into the original task.
        pass
    print("{}")


def sharing_operation(operation):
    if operation == "sharing-status":
        return sharing.status()
    if operation == "sharing-enable":
        settings = sharing.set_enabled(True)
        return dict(
            status="enabled",
            generation=settings["generation"],
            enabled_at=settings["enabled_at"],
            note="only newly authorized material is captured; no backfill",
        )
    settings = sharing.set_enabled(False)
    notify = sharing.cancel_notify()
    return dict(
        status="disabled",
        generation=settings["generation"],
        cancel_notify=notify,
        note="queued capture/organization in this scope is cancelled by the "
        "running service; drafts and published data are kept",
    )


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in OPERATIONS:
        print("Unsupported MindIE entry operation", file=sys.stderr)
        raise SystemExit(1)
    operation = sys.argv[1]
    if operation == "mcp":
        from mcp_gate import serve

        return serve("knowledge")
    if operation in {"activate", "deactivate"}:
        try:
            print(json.dumps(activate(operation)))
        except Exception as exc:
            print(
                f"MindIE session operation failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return
    if operation == "stop":
        stop()
        return
    if operation.startswith("sharing-"):
        try:
            print(json.dumps(sharing_operation(operation)))
        except Exception as exc:
            print(
                f"MindIE sharing operation failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return
    try:
        config = json.loads(config_path().read_text())
        control = [
            config["python"],
            str(Path(__file__).with_name("service_control.py")),
            operation,
        ]
        print(run(control, "", timeout=5, max_output=32768), end="")
    except Exception as exc:
        print(
            "MindIE Agent is not configured: "
            + type(exc).__name__
            + ". Run scripts/setup.py with the knowledge runtime interpreter.",
            file=sys.stderr,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
