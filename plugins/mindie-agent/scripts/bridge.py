#!/usr/bin/env python3
"""Codex plugin boundary. Hooks never start a service or persist retry work."""

import json
from pathlib import Path
import re
import sys

from bounded_process import run
from session_gate import Sessions, config_path
from update_lock import update_lock

MAX_HOOK_BYTES = 128 * 1024
OPERATIONS = {
    "session-start",
    "stop",
    "mcp",
    "status",
    "shutdown",
    "activate",
    "deactivate",
}


def hook_event(operation):
    raw = sys.stdin.buffer.read(MAX_HOOK_BYTES + 1)
    if len(raw) > MAX_HOOK_BYTES:
        raise ValueError("hook input exceeds limit")
    event = json.loads(raw)
    expected = "SessionStart" if operation == "session-start" else "Stop"
    if not isinstance(event, dict) or event.get("hook_event_name") != expected:
        raise ValueError("unexpected hook event")
    for key in (
        ("session_id",) if operation == "session-start" else ("session_id", "turn_id")
    ):
        if not isinstance(event.get(key), str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", event[key]
        ):
            raise ValueError("invalid hook identity")
    if operation == "stop":
        summary = event.get("last_assistant_message")
        if event.get("stop_hook_active", False) is not False:
            raise ValueError("recursive Stop is not a capture")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 32768:
            raise ValueError("invalid final summary")
        # Forward only capture fields; never propagate transcripts or other context.
        return {
            key: event[key]
            for key in (
                "hook_event_name",
                "session_id",
                "turn_id",
                "last_assistant_message",
            )
        }
    return event


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
            print(json.dumps(getattr(Sessions(), operation)()))
        except Exception as exc:
            print(
                f"MindIE session operation failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return
    if operation in {"session-start", "stop"}:
        try:
            event = hook_event(operation)
        except (ValueError, OSError, TypeError, RecursionError):
            print("{}")
            return
    if operation == "session-start":
        # Old loaded tasks may still invoke this command. No context injection,
        # activation, subprocess or service start, even when a config exists.
        print("{}")
        return
    try:
        if operation == "stop":
            sessions = Sessions()
            lease = sessions.check(event["session_id"])
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
            "hook"
            if operation == "stop"
            else "stop"
            if operation == "shutdown"
            else operation,
            "--config",
            config["engine_config"],
        ]
        if operation == "stop":
            with update_lock(sessions.config):
                sessions.check(event["session_id"], lease["token"])
                run(command, json.dumps(event), timeout=1.2, max_output=32768)
            print("{}")
        else:
            control = [
                config["python"],
                str(Path(__file__).with_name("service_control.py")),
                operation,
            ]
            print(run(control, "", timeout=5, max_output=32768), end="")
    except Exception as exc:
        if operation == "stop":
            print("{}")
        else:
            print(
                "MindIE Agent is not configured: "
                + type(exc).__name__
                + ". Run scripts/setup.py with the knowledge runtime interpreter.",
                file=sys.stderr,
            )
            raise SystemExit(1)


if __name__ == "__main__":
    main()
