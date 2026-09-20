#!/usr/bin/env python3
"""Codex plugin boundary. Hooks never start a service or persist retry work.

The Stop hook path is a no-op unless every capture precondition holds:
an active lease, community sharing enabled, and the event's native cwd inside
the authorized scope. The sharing gate runs BEFORE any capture claim or
payload forwarding: sharing off/missing means no capture row, no draft, and
no service/worker/model startup. The hook never parses transcripts, never
blocks the original task and never uses exit-2 continuation.

Explicit operator entries also cover unified offline status, service
shutdown and the deterministic core contribution-recovery operations
(contribution-inspect/-reconcile/-retry/-compact); none of them starts a
service, a model or an uncertain write.
"""

import json
import os
from pathlib import Path
import re
import sys
import threading
import time

from bounded_process import run
from session_gate import Sessions, config_path, generation_env, runtime_scripts
import sharing
from update_lock import update_lock

MAX_HOOK_BYTES = 128 * 1024
MAX_SUMMARY = 32768
OPERATIONS = {
    "stop",
    "mcp",
    "status",
    "init",
    "shutdown",
    "activate",
    "deactivate",
    "config",
    "sharing-enable",
    "sharing-disable",
    "sharing-status",
    "sharing-choice",
}
# Deterministic core recovery surface (documented exact names; each takes one
# existing contribution batch id and never reruns organizer/model work).
CONTRIBUTION_OPERATIONS = {
    "contribution-inspect",
    "contribution-reconcile",
    "contribution-retry",
    "contribution-compact",
}
IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
BATCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
# Native Stop budget is 2s. The whole hook (stdin + helper) stays under
# HOOK_BUDGET so print/exit still fit before the host kills this process.
HOOK_BUDGET = 1.5


def _bounded_path(value, name):
    if not isinstance(value, str) or not 0 < len(value) <= 1024:
        raise ValueError(f"invalid {name}")
    if not os.path.isabs(value):
        raise ValueError(f"{name} must be absolute")
    return value


def _read_hook_stdin(limit, timeout):
    """Deadline-bounded raw fd read; never buffered I/O (shutdown can hang).

    Stops at EOF, the byte cap, the deadline, or the first complete JSON
    value so a held-open pipe cannot consume the helper's remaining time.
    Windows native select is sockets-only; a daemon os.read thread is the
    portable bound (code-only on Windows; not natively verified).
    """
    remaining = timeout
    if remaining <= 0:
        raise TimeoutError("hook stdin deadline exceeded")
    buf = bytearray()
    lock = threading.Lock()
    finished = threading.Event()

    def reader():
        try:
            fd = sys.stdin.fileno()
            while True:
                with lock:
                    if len(buf) > limit:
                        return
                    room = limit + 1 - len(buf)
                try:
                    chunk = os.read(fd, min(8192, room))
                except (OSError, ValueError):
                    return
                if not chunk:
                    return
                with lock:
                    buf.extend(chunk)
                    if len(buf) > limit:
                        return
                    try:
                        json.loads(bytes(buf))
                    except ValueError:
                        continue
                    return
        finally:
            finished.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    finished.wait(timeout=max(0.0, remaining))
    if not finished.is_set():
        raise TimeoutError("hook stdin deadline exceeded")
    with lock:
        return bytes(buf)


def hook_event(raw):
    """Validate one bounded native Stop envelope; whitelist forwarding fields.

    A valid transcript event is never rejected for a missing final summary:
    transcript_path alone is enough. The transcript itself is never opened
    here; only its location and the bounded summary cross the boundary.
    """
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
        config_file = config_path()
        with update_lock(config_file):
            config = json.loads(config_file.read_text())
            output = run(
                [
                    config["python"],
                    str(Path(runtime_scripts(config)) / "runtime_call.py"),
                ],
                json.dumps(payload),
                timeout=15,
                env=generation_env(config_file),
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


def unconfigured_status():
    """Stdlib-only offline first-use payload. Creates no files or services."""
    return dict(
        configured=False,
        sharing=dict(state="unconfigured"),
        first_use=dict(
            state="unconfigured",
            prompt=sharing.CHOICES,
            choices=["contribute", "read-only", "later"],
        ),
        next=(
            "Run scripts/setup.py install --knowledge-python PYTHON "
            "(headless leaves sharing off). Then choose: recommended "
            "public contribution via setup.py configure "
            "--community-repository OWNER/REPO --community-project-root PATH "
            "--community-visibility public; or scripts/bridge.py "
            "sharing-choice read-only|later. Do not edit JSON or reinstall."
        ),
        recovery=[],
        service=dict(state="not-running"),
    )


def stop():
    deadline = time.monotonic() + HOOK_BUDGET
    try:
        # Cheap default-off before stdin: no helper, no lock, no lease DB.
        if sharing.read() is None:
            print("{}")
            return
        event = hook_event(
            _read_hook_stdin(MAX_HOOK_BYTES, deadline - time.monotonic())
        )
    except (ValueError, OSError, TypeError, RecursionError, TimeoutError):
        print("{}")
        return
    try:
        # Remaining helper time under the same whole-hook deadline.
        remaining = deadline - time.monotonic()
        if remaining > 0:
            Sessions(op_timeout=remaining)._op(
                "stop_capture", {"event": event, "session": event["session_id"]}
            )
    except Exception:
        # The hook never propagates a failure into the original task.
        pass
    print("{}")


def sharing_operation(operation, extra=None):
    if operation == "sharing-status":
        return sharing.status()
    if operation == "sharing-choice":
        if extra not in {"read-only", "later"}:
            raise ValueError(
                "sharing-choice is read-only or later; contribution uses "
                "setup.py configure / bridge.py config"
            )
        choice = sharing.record_choice(extra)
        return dict(
            status="recorded",
            sharing_choice=choice,
            sharing="off",
            note="knowledge retrieval stays available; no capture until "
            "an explicit later configure",
        )
    if operation == "sharing-enable":
        settings = sharing.set_enabled(True)
        return dict(
            status="enabled",
            generation=settings["generation"],
            enabled_at=settings["enabled_at"],
            note="only newly authorized material is captured; no backfill",
        )
    settings = sharing.set_enabled(False)
    return dict(
        status="disabled",
        generation=settings["generation"],
        cancel="the running service rereads this generation on its bounded "
        "idle tick and cancels matching queued capture/organization/outbound "
        "work (core-owned); drafts and published data are kept",
    )


def contribution(operation, batch_id):
    """Explicit operator recovery for one existing contribution batch.

    Thin wrapper over the deterministic core CLI operations; it starts no
    service or model, never rebuilds a payload and never replays failed work.
    """
    config_file = config_path()
    with update_lock(config_file):
        config = json.loads(config_file.read_text())
        output = run(
            [
                config["python"],
                "-m",
                "mindie_knowledge.loop.cli",
                operation,
                "--config",
                config["engine_config"],
                "--batch",
                batch_id,
            ],
            "",
            timeout=130,
            max_output=65536,
            env=generation_env(config_file),
        )
    return json.loads(output) if output.strip() else dict(status="no-output")


def configure(argv):
    """Post-install sharing configuration; never refuses an existing engine."""
    config_file = config_path()
    with update_lock(config_file):
        config = json.loads(config_file.read_text())
        output = run(
            [
                config["python"],
                str(Path(runtime_scripts(config)) / "setup.py"),
                "configure",
                "--config",
                str(config_file),
                *argv,
            ],
            "",
            timeout=30,
            max_output=65536,
            env=generation_env(config_file),
        )
    return json.loads(output) if output.strip() else dict(status="configured")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in OPERATIONS | CONTRIBUTION_OPERATIONS:
        print("Unsupported MindIE entry operation", file=sys.stderr)
        raise SystemExit(1)
    operation = sys.argv[1]
    if operation == "config":
        try:
            print(json.dumps(configure(sys.argv[2:])))
        except Exception as exc:
            print(
                f"MindIE config failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return
    if operation == "sharing-choice":
        if len(sys.argv) != 3:
            print("sharing-choice requires read-only or later", file=sys.stderr)
            raise SystemExit(1)
        try:
            print(json.dumps(sharing_operation(operation, sys.argv[2])))
        except Exception as exc:
            print(
                f"MindIE sharing operation failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return
    if operation in CONTRIBUTION_OPERATIONS:
        if len(sys.argv) != 3:
            print("contribution operations require --batch id as argv", file=sys.stderr)
            raise SystemExit(1)
        batch_id = sys.argv[2]
        if not BATCH.fullmatch(batch_id):
            print("Invalid contribution batch id", file=sys.stderr)
            raise SystemExit(1)
        try:
            print(json.dumps(contribution(operation, batch_id)))
        except Exception as exc:
            print(
                f"MindIE contribution recovery failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return
    if len(sys.argv) != 2:
        print("Unsupported MindIE entry operation", file=sys.stderr)
        raise SystemExit(1)
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
    if operation in {"init", "status"}:
        config_file = config_path()
        if not config_file.is_file():
            print(json.dumps(unconfigured_status()))
            return
        try:
            with update_lock(config_file):
                config = json.loads(config_file.read_text())
                control = [
                    config["python"],
                    str(Path(runtime_scripts(config)) / "service_control.py"),
                    "status",
                ]
                print(
                    run(
                        control,
                        "",
                        timeout=5,
                        max_output=32768,
                        env=generation_env(config_file),
                    ),
                    end="",
                )
        except Exception:
            print(json.dumps(unconfigured_status()))
        return
    try:
        config_file = config_path()
        with update_lock(config_file):
            config = json.loads(config_file.read_text())
            control = [
                config["python"],
                str(Path(runtime_scripts(config)) / "service_control.py"),
                operation,
            ]
            print(
                run(
                    control,
                    "",
                    timeout=5,
                    max_output=32768,
                    env=generation_env(config_file),
                ),
                end="",
            )
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
