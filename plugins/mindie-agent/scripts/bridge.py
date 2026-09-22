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
import stat
import sys
import threading
import time

from bounded_process import run
from session_gate import (
    Sessions,
    bind_explicit_config,
    config_path,
    generation_env,
    runtime_scripts,
)
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
    "reporting-status",
    "reporting-enable",
    "reporting-disable",
    "reporting-ensure",
    "reporting-maintain",
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
        diagnostics=_reporting_choice(),
    )


def _status_failure(state, stage, exc, config_file, selected=None):
    """Local operator diagnostic; never print helper stderr or config values."""
    python = selected["python"] if selected else sys.executable
    scripts = Path(runtime_scripts(selected)) if selected else Path(__file__).parent
    commands = {
        "status": [python, str(scripts / "bridge.py"), "--config", str(config_file), "status"],
        "check_config_json": [
            sys.executable, "-c",
            "import json,pathlib,sys; json.loads(pathlib.Path(sys.argv[1]).read_text()); print('JSON syntax valid')",
            str(config_file),
        ],
    }
    recovery = {
        "invalid_config": "Inspect the named config and correct its JSON or required absolute runtime paths, then run status. Existing state has not been reinitialized.",
        "update_busy": "An update holds the generation lock. Let that operation finish, then explicitly run status; installation is not missing.",
        "helper_failed": "Inspect the selected runtime and the reported helper stage, then explicitly run status. No recovery action was started.",
    }
    if selected:
        commands["check_runtime"] = [
            python, "-c",
            "import mindie_knowledge,remote_dev; print('runtime imports available')",
        ]
    result = dict(
        status=state,
        configured=None,
        config=str(config_file),
        first_use=None,
        sharing=dict(state="unknown"),
        service=dict(state="unknown"),
        error=dict(stage=stage, type=type(exc).__name__),
        commands=commands,
        recovery=[recovery[state]],
        next="Use the listed status/check commands. Native shell/SSH or separately configured remote-dev remain available without knowledge activation.",
    )
    # Config and lock contention are expected. Only a failed status helper
    # is recorded; a bad response is a protocol failure.
    if state == "helper_failed" and stage in {"helper_run", "helper_response"}:
        from diagnostic_support import attach, failure

        category = "helper_protocol" if stage == "helper_response" else "helper_failed"
        updated = attach(
            result,
            failure("status", stage, category, exception=exc),
        )
        if isinstance(updated, dict):
            result = updated
    return result


def offline_status():
    """Distinguish missing installation from unreadable or busy existing state."""
    deadline = time.monotonic() + 5
    config_file = config_path()
    selected = None
    stage = "config_stat"
    try:
        try:
            config_file.stat()
        except FileNotFoundError:
            return unconfigured_status(), 0
        stage = "update_lock"
        with update_lock(config_file):
            stage = "config_read"
            # A corrupt config path may be a FIFO/device. Do not wait for a
            # writer before the bounded helper is even started.
            fd = os.open(config_file, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("adapter config must be a regular file")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    raw = stream.read(64 * 1024 + 1)
                if len(raw) > 64 * 1024:
                    raise ValueError("adapter config exceeds bound")
                config = json.loads(raw)
            finally:
                os.close(fd)
            stage = "config_validate"
            if not isinstance(config, dict):
                raise ValueError("adapter config must be an object")
            for key in ("python", "engine_config"):
                _bounded_path(config.get(key), key)
            if "runtime_scripts" in config:
                _bounded_path(config["runtime_scripts"], "runtime_scripts")
            selected = config
            stage = "helper_run"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("status budget exhausted")
            output = run(
                [config["python"], str(Path(runtime_scripts(config)) / "service_control.py"), "status"],
                "", timeout=remaining, max_output=32768, env=generation_env(config_file),
            )
            stage = "helper_response"
            payload = json.loads(output)
            if not isinstance(payload, dict):
                raise ValueError("status response must be an object")
            return payload, 0
    except Exception as exc:
        if stage == "update_lock" and isinstance(exc, BlockingIOError):
            state = "update_busy"
        elif stage.startswith("config") or stage == "update_lock":
            state = "invalid_config"
        else:
            state = "helper_failed"
        return _status_failure(state, stage, exc, config_file, selected), 1


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


def _reporting_choice():
    """Optional, independent reporting recommendation. Never installs."""
    from diagnostic_support import reporting_hint, reporting_status

    view = reporting_status()
    result = dict(reporting=view)
    if view.get("status") == "not_configured":
        result["choice"] = reporting_hint()
    return result


def _reporting_unavailable(stage, exc):
    print(json.dumps(dict(
        status="unavailable",
        error=dict(type=type(exc).__name__, stage=stage),
    )))
    raise SystemExit(1)


def _adapter_python(config_file):
    """Read the selected interpreter under the generation lock, then release it."""
    with update_lock(config_file):
        fd = os.open(config_file, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("adapter config must be a regular file")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(64 * 1024 + 1)
        finally:
            os.close(fd)
        if len(raw) > 64 * 1024:
            raise ValueError("adapter config exceeds bound")
        config = json.loads(raw)
        if not isinstance(config, dict):
            raise ValueError("adapter config must be an object")
        python = _bounded_path(config.get("python"), "python")
        scripts = runtime_scripts(config)
        if "runtime_scripts" in config:
            _bounded_path(config["runtime_scripts"], "runtime_scripts")
    return python, scripts


def _print_reporting_json(output, stage):
    try:
        payload = json.loads(output)
    except ValueError as exc:
        _reporting_unavailable(stage, exc)
    if not isinstance(payload, dict):
        _reporting_unavailable(stage, ValueError("reporting response must be an object"))
    print(json.dumps(payload))
    if payload.get("status") in {"unavailable", "failed", "degraded", "configuration_unavailable", "error"}:
        raise SystemExit(1)


def reporting_operation(operation):
    """Explicit reporting ops. Enable does not spawn ensure; Stop never calls this."""
    config_file = config_path()
    verb = operation.split("-", 1)[1]
    if verb == "status":
        stage = "config_read"
        try:
            try:
                config_file.stat()
            except FileNotFoundError:
                print(json.dumps(_reporting_choice()["reporting"]))
                return
            python, _scripts = _adapter_python(config_file)
        except BlockingIOError as exc:
            _reporting_unavailable("update_lock", exc)
        except Exception:
            # Unconfigured or malformed adapter: local shim only, no install.
            print(json.dumps(_reporting_choice()["reporting"]))
            return
        stage = "helper_run"
        try:
            output = run(
                [python, "-m", "mindie_diagnostics.cli", "reporting", "status"],
                "",
                timeout=3,
                max_output=65536,
                allowed_returncodes=(0, 1),
                env=generation_env(config_file),
            )
            _print_reporting_json(output, "helper_response")
        except SystemExit:
            raise
        except Exception as exc:
            _reporting_unavailable(stage, exc)
        return
    if verb in {"enable", "disable"}:
        stage = "config_read"
        try:
            python, scripts = _adapter_python(config_file)
            stage = "helper_run"
            output = run(
                [
                    python,
                    "-c",
                    "import json,sys; sys.path.insert(0, sys.argv[2]); "
                    "from diagnostic_support import configure_reporting; "
                    "print(json.dumps(configure_reporting("
                    "sys.argv[1]=='true', sys.executable)))",
                    "true" if verb == "enable" else "false",
                    str(Path(scripts)),
                ],
                "",
                timeout=3,
                max_output=65536,
                allowed_returncodes=(0, 1),
                env=generation_env(config_file),
            )
            _print_reporting_json(output, "helper_response")
        except SystemExit:
            raise
        except Exception as exc:
            _reporting_unavailable(stage, exc)
        return
    timeout = 60 if verb == "ensure" else 5
    stage = "config_read"
    try:
        python, _scripts = _adapter_python(config_file)
        stage = "helper_run"
        output = run(
            [python, "-m", "mindie_diagnostics.cli", "reporting", verb],
            "",
            timeout=timeout,
            max_output=65536,
            allowed_returncodes=(0, 1),
            env=generation_env(config_file),
        )
        _print_reporting_json(output, "helper_response")
    except SystemExit:
        raise
    except Exception as exc:
        _reporting_unavailable(stage, exc)


def _optional_config_prefix(argv):
    """Accept `--config PATH` before the operation; leave host identity alone.

    Sets the process-local explicit override and MINDIE_AGENT_CONFIG for
    child dispatch. Default invocation without the prefix is unchanged.
    Native task identity is not set here.
    """
    if len(argv) >= 2 and argv[0] == "--config":
        value = argv[1]
        if not isinstance(value, str) or not os.path.isabs(value):
            print("MindIE --config requires an absolute path", file=sys.stderr)
            raise SystemExit(1)
        bind_explicit_config(value)
        return argv[2:]
    return argv


def main():
    argv = _optional_config_prefix(sys.argv[1:])
    if not argv or argv[0] not in OPERATIONS | CONTRIBUTION_OPERATIONS:
        print("Unsupported MindIE entry operation", file=sys.stderr)
        raise SystemExit(1)
    operation = argv[0]
    if operation == "config":
        try:
            print(json.dumps(configure(argv[1:])))
        except Exception as exc:
            print(
                f"MindIE config failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return
    if operation == "sharing-choice":
        if len(argv) != 2:
            print("sharing-choice requires read-only or later", file=sys.stderr)
            raise SystemExit(1)
        try:
            print(json.dumps(sharing_operation(operation, argv[1])))
        except Exception as exc:
            print(
                f"MindIE sharing operation failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return
    if operation in CONTRIBUTION_OPERATIONS:
        if len(argv) != 2:
            print("contribution operations require --batch id as argv", file=sys.stderr)
            raise SystemExit(1)
        batch_id = argv[1]
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
    if len(argv) != 1:
        print("Unsupported MindIE entry operation", file=sys.stderr)
        raise SystemExit(1)
    if operation.startswith("reporting-"):
        reporting_operation(operation)
        return
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
        payload, code = offline_status()
        print(json.dumps(payload))
        if code:
            raise SystemExit(code)
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
