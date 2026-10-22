#!/usr/bin/env python3
"""Codex plugin boundary.

[Role]
Unified operator and Hook CLI boundary (`stop`, `mcp`, `status`, `init`, `shutdown`, `activate`, `deactivate`, `config`, `sharing-*`, `reporting-*`, `history-import`, `contribution-*`).

[Invariants]
- `stop` is a no-op returning `{}` unless community sharing is enabled and consent allows; it never parses transcripts directly or blocks the user task.
- When `activate` or `sharing-enable` succeeds in creating/retaining a read-only knowledge lease but capture binding (`knowledge_attach`) fails, it honestly returns `status="degraded"` rather than masking the failure as `"active"`.
- `offline_status()` and `init` never cold-start a service or modify state.

[Timeouts & Locks]
- Acquires shared `update_lock(config_file)` around helper invocations (`30.0s` for `bind` and `service_control.py status`, `180.0s` for `setup.py configure`, `60.0s` for `contribution-*`, and `300.0s` for an explicit `history-import`).

[Side-Effect Policy]
- Read-only for `status`/`init`/`sharing-status`/`reporting-status`; contribution changes use `config`/`sharing-enable`/`sharing-disable`, and retired independent reporting mutators only return guidance.
"""

import json
import os
from pathlib import Path
import re
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bounded_process import run
from session_gate import (
    Inactive,
    Sessions,
    bind_explicit_config,
    config_path,
    runtime_env,
    runtime_scripts,
)
import sharing
from update_lock import update_lock
from bridge_commands import (
    configure,
    contribute,
    contribute_Now,
    contribute_args,
    contribute_command,
    contribute_result,
    contribution,
    doctor,
    history_import,
    offline_status,
    reporting_disable,
    reporting_enable,
    reporting_operation,
    reporting_status,
    sharing_disable,
    sharing_enable,
    sharing_operation,
    sharing_status,
    unconfigured_status,
    _activate_reporting,
    _adapter_python,
    _refresh_capture,
    _reporting_choice,
    _reporting_unavailable,
    _status_failure,
    _with_reporting,
)

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
    "history-import",
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
DOMAIN = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
BATCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
# Includes JSON escaping of the native final-answer copy; only references
# are forwarded. This byte cap bounds memory without imposing a run deadline.
MAX_HOOK_BYTES = 32 * 1024 * 1024


def _bounded_path(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid {name}")
    if not os.path.isabs(value):
        raise ValueError(f"{name} must be absolute")
    return value


_HOOK_TOKEN = re.compile(rb'["{}\[\]]')


def _read_hook_stdin(*, max_bytes=None):
    """Frame one byte-bounded event; wait for the caller while it owns us."""
    max_bytes = MAX_HOOK_BYTES if max_bytes is None else max_bytes
    buf = bytearray()
    finished = threading.Event()
    errors = []
    owner = os.getppid()

    def reader():
        try:
            fd = sys.stdin.fileno()
            depth = 0
            quoted = escaped = False
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise ValueError('hook event exceeds its byte bound')
                pos = int(escaped)
                escaped = False
                while pos < len(chunk):
                    if quoted:
                        quote = chunk.find(b'"', pos)
                        if quote < 0:
                            if chunk.endswith(b'\\'):
                                tail = len(chunk) - max(pos, len(chunk.rstrip(b'\\')))
                                escaped = bool(tail % 2)
                            break
                        before = quote
                        while before > pos and chunk[before - 1] == 92:
                            before -= 1
                        if (quote - before) % 2 == 0:
                            quoted = False
                        pos = quote + 1
                    else:
                        match = _HOOK_TOKEN.search(chunk, pos)
                        if match is None:
                            break
                        pos = match.end()
                        token = match.group()
                        if token == b'"':
                            quoted = True
                        elif token in (b'{', b'['):
                            depth += 1
                        else:
                            depth -= 1
                            if depth <= 0:
                                return
        except TimeoutError:
            raise
        except (OSError, ValueError) as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    while not finished.wait(0.1):
        if os.name == 'posix' and os.getppid() != owner:
            raise ConnectionError('native hook owner exited')
    if errors:
        raise errors[0]
    return bytes(buf)


def hook_event(raw):
    """Validate native identity and forward only the transcript reference.

    A valid transcript event is never rejected for a missing final summary:
    transcript_path alone is enough. The transcript itself is never opened
    here. The optional final-answer copy has no role in transcript capture.
    """
    event = json.loads(raw)
    if not isinstance(event, dict) or event.get("hook_event_name") != "Stop":
        raise ValueError("unexpected hook event")
    for key in ("session_id", "turn_id"):
        if not isinstance(event.get(key), str) or not IDENTITY.fullmatch(event[key]):
            raise ValueError("invalid hook identity")
    if event.get("stop_hook_active", False) is not False:
        raise ValueError("recursive Stop is not a capture")
    cwd = _bounded_path(event.get("cwd"), "cwd")
    transcript = _bounded_path(event.get("transcript_path"), "transcript_path")
    forwarded = dict(
        hook_event_name="Stop",
        identity_kind="turn",
        session_id=event["session_id"],
        turn_id=event["turn_id"],
        cwd=cwd,
    )
    forwarded["transcript_path"] = transcript
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
            config = json.loads(config_file.read_text(encoding='utf-8'))
            output = run(
                [
                    config["python"],
                    str(Path(runtime_scripts(config)) / "runtime_call.py"),
                ],
                json.dumps(payload),
                timeout=30.0,
                env=runtime_env(config_file),
                allow_service=True,
            ).checked_stdout()
        result = json.loads(output)
        if isinstance(result, dict) and result.get("isError") is not True:
            return "bound"
        return "unbound:service-error"
    except Exception as exc:
        return f"unbound:{type(exc).__name__}"




def _runtime_identity():
    """The actually running plugin runtime, for verifiable binding proof.

    ``scripts`` is the resolved directory of this running bridge.py; ``build``
    carries the installed runtime's revision/version stamp when present.
    A test profile can check these against the selected candidate instead of
    trusting whichever stale cache copy a host or model happened to open.
    """
    scripts = Path(__file__).resolve().parent
    build = None
    stamp = scripts / "diagnostic-build.json"
    try:
        data = json.loads(stamp.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            build = {
                key: data[key]
                for key in ("revision", "version")
                if isinstance(data.get(key), str)
            } or None
    except TimeoutError:
        raise
    except (OSError, ValueError):
        build = None
    return scripts, build


def activate(operation, *, domain=None):
    sessions = Sessions()
    if operation == "activate" and domain is not None:
        # Target-domain activation returns its own lease and domain facts. It
        # must not enter the default capture preparation with a target token.
        result = sessions.activate(domain=domain)
    else:
        result = getattr(sessions, operation)()
    if operation != "activate":
        return result
    scripts, build = _runtime_identity()
    result["scripts"] = str(scripts)
    if build:
        result["build"] = build
    if domain is not None:
        return result
    return _prepare_capture(result)


def _prepare_capture(result):
    """Report the effective loop state for this already verified binding."""
    try:
        settings = sharing.read()
        view = sharing.status()
        if (
            settings is not None
            and settings["enabled"]
            and sharing.consent_allows(settings) is not False
        ):
            if sharing.capture_allowed(dict(result, root_session=result["mindie_session_id"]), result.get("project_root")):
                result["capture"] = bind(result)
            else:
                result["capture"] = "out-of-scope"
        else:
            # Sharing off/unconfigured or consent-blocked: ordinary activation
            # only. No cold start, no bind, no collection preparation.
            result["capture"] = "disabled"
    except TimeoutError:
        raise
    except (OSError, ValueError) as exc:
        # Binding may already exist. Preserve its receipt
        # while reporting that required capture preparation did not complete.
        return dict(result, status="degraded", capture="unavailable", experience="unavailable",
                    error=dict(stage="capture-configuration", type=type(exc).__name__),
                    warning=f"Capture configuration could not be prepared ({type(exc).__name__}); read-only knowledge lease remains active.",
                    next="Inspect the existing capture configuration; do not activate again to repair it.")
    result["sharing"] = view
    result["experience"] = (
        "capture-ready" if result["capture"] == "bound"
        else "out-of-scope" if result["capture"] == "out-of-scope"
        else "unavailable" if result["capture"].startswith("unbound:")
        else "disabled" if view["state"] == "disabled"
        else "unavailable" if view["state"] == "malformed"
        else "needs-configuration"
    )
    if result["experience"] == "needs-configuration":
        result["next"] = sharing.CHOICES
    elif result["experience"] == "out-of-scope":
        result["next"] = "This task is outside the configured project scope; capture is not active."
    elif result["experience"] == "unavailable":
        result["status"] = "degraded"
        reason = (
            result["capture"].removeprefix("unbound:")
            if result["capture"].startswith("unbound:")
            else view["state"]
        )
        result["warning"] = (
            f"Capture binding unavailable ({reason}); read-only knowledge lease remains active."
        )
        result.setdefault(
            "error",
            dict(
                stage="capture-bind" if result["capture"].startswith("unbound:") else "capture-configuration",
                reason=reason,
            ),
        )
        result["next"] = "Capture could not be prepared; inspect status. Task binding alone does not establish capture readiness."
    return result


def _record_stop(stage, category, exc=None):
    """Local diagnostic only. No transcript, token, or exception text."""
    try:
        from diagnostic_support import failure

        failure(
            "capture.stop", stage=stage, category=category,
            exception=exc, reportable=False,
        )
    except Exception:
        pass


def _observe_stop(result):
    if not isinstance(result, dict):
        _record_stop("handoff", "internal")
        return False
    stage = result.get("stage")
    if stage == "accepted-local" and (result.get("wake") == "failed"
                                      or result.get("reason") == "worker-dead"):
        _record_stop("handoff", "consumer_unavailable")
        return False
    if stage in {"inert", "organized", "no-new-material", "no-shareable-material",
                 "cancelled", "discarded", "processing", "accepted-runtime",
                 "apply-pending", "accepted-local"}:
        return True
    if stage not in {"unavailable", "rejected", "failed"}:
        _record_stop("handoff", "internal")
        return False
    reason = result.get("reason")
    if not isinstance(reason, str) or not reason.replace("-", "").replace("_", "").isalnum():
        reason = "handoff"
    if not reason[:1].isalpha():
        reason = "handoff"
    _record_stop(stage, reason)
    return False


def stop(*, adapter=None, hook_input=None):
    try:
        if adapter is None:
            from product_contract import read_json
            adapter = read_json(config_path())[0]
        if not isinstance(adapter, dict):
            raise ValueError("invalid Stop binding")
        settings = sharing.read(adapter=adapter)
        # Missing setup is an Agent diagnostic; an explicit disable is inert.
        # Neither branch reads stdin, starts a helper or creates a capture row.
        if settings is None:
            disabled = sharing.status(adapter=adapter).get("state") == "disabled"
            if not disabled:
                _record_stop("configuration", "missing_configuration")
            print("{}")
            return 0 if disabled else 1
        if not settings["enabled"] or sharing.consent_allows(settings) is False:
            print("{}")
            return 0
    except TimeoutError:
        raise
    except (OSError, ValueError) as exc:
        _record_stop("configuration", "unavailable", exc)
        print("{}")
        return 1
    try:
        event = hook_event(_read_hook_stdin() if hook_input is None else hook_input)
        # This host delivers no native thread identity to the hook process
        # (verified on codex-cli 0.153.4: the hook env carries CODEX_HOME
        # only). When a host does supply CODEX_THREAD_ID, a disagreement with
        # the event's session is a genuine anomaly — fail closed before any
        # capture. The transcript-artifact ownership check in the helper is
        # the second, always-available layer.
        native = os.environ.get("CODEX_THREAD_ID")
        if native and native != event["session_id"]:
            _record_stop("envelope", "identity_mismatch")
            print("{}")
            return 1
    except ValueError as exc:
        if str(exc) in {"unexpected hook event", "recursive Stop is not a capture"}:
            print("{}")
            return 0
        _record_stop("envelope", "invalid_envelope", exc)
        print("{}")
        return 1
    except TimeoutError as exc:
        _record_stop("envelope", "timeout", exc)
        print("{}")
        return 1
    except (OSError, TypeError, RecursionError) as exc:
        _record_stop("envelope", "invalid_envelope", exc)
        print("{}")
        return 1
    failed = False
    try:
        result = Sessions(stop_adapter=adapter)._op(
            "stop_capture", {"event": event, "session": event["session_id"]}
        )
        failed = not _observe_stop(result)
    except Inactive as exc:
        detail = exc.failure or {"stage": "helper", "code": "unavailable"}
        _record_stop(detail["stage"], detail["code"], exc)
        failed = True
    except Exception as exc:
        # The hook never propagates a failure into the original task.
        _record_stop("helper", "unavailable", exc)
        failed = True
    print("{}")
    # The wrapper returns the neutral hook response; internal failures remain
    # in the local machine diagnostic channel for a natural capability call.
    return 1 if failed else 0


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
    if argv == ["stop", "--snapshot"]:
        try:
            # The raw event and 64 KiB adapter are already bounded. Re-encoding
            # adds escaping/spacing plus the fixed envelope keys.
            envelope = json.loads(_read_hook_stdin(max_bytes=2 * (MAX_HOOK_BYTES + 65536) + 64))
            if (not isinstance(envelope, dict) or set(envelope) != {"adapter", "hook_input"}
                    or not isinstance(envelope["hook_input"], str)):
                raise ValueError("invalid internal Stop envelope")
            raise SystemExit(stop(adapter=envelope["adapter"], hook_input=envelope["hook_input"]))
        except TimeoutError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            _record_stop("envelope", "invalid_snapshot", exc)
            print("{}")
            raise SystemExit(1)
    if not argv or argv[0] not in OPERATIONS | CONTRIBUTION_OPERATIONS:
        print("Unsupported MindIE entry operation", file=sys.stderr)
        raise SystemExit(1)
    operation = argv[0]
    if operation == "history-import":
        raise SystemExit(history_import(argv[1:]))
    if operation == "config":
        try:
            result = configure(argv[1:])
            print(json.dumps(result))
            if result.get("status") in {"degraded", "failed", "unavailable", "error"}:
                raise SystemExit(1)
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
    domain = None
    if operation == "activate":
        if len(argv) == 3 and argv[1] == "--domain":
            domain = argv[2]
            if DOMAIN.fullmatch(domain) is None:
                print("Invalid MindIE domain", file=sys.stderr)
                raise SystemExit(1)
        elif len(argv) != 1:
            print("Unsupported MindIE entry operation", file=sys.stderr)
            raise SystemExit(1)
    elif len(argv) != 1:
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
            result = activate(operation, domain=domain)
            print(json.dumps(result))
            if result.get("status") in {"degraded", "partial"}:
                raise SystemExit(1)
        except Exception as exc:
            print(
                f"MindIE session operation failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return
    if operation == "stop":
        raise SystemExit(stop())
    if operation.startswith("sharing-"):
        try:
            result = sharing_operation(operation)
            print(json.dumps(result))
            if (result.get("status") in {"degraded", "failed", "unavailable", "error"}
                    or result.get("state") in {"malformed", "unavailable"}):
                raise SystemExit(1)
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
            config = json.loads(config_file.read_text(encoding='utf-8'))
            control = [
                config["python"],
                str(Path(runtime_scripts(config)) / "service_control.py"),
                operation,
            ]
            print(
                run(
                    control,
                    "",
                    timeout=60.0,
                    max_output=32768,
                    env=runtime_env(config_file),
                ).checked_stdout(),
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
