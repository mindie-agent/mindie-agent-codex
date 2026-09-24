"""One authorized call in the configured interpreter; no request replay.

This module always executes inside one committed runtime generation: when an
older loaded entrypoint spawns it with a stale interpreter or from a stale
path, it re-execs the generation-recorded copy under the generation's
interpreter before touching the payload. Admission checks use the shared
core store directly (this process is the generation), and the knowledge
call's outcome accounting (failure circuit, with the read-rejection
exemption) is recorded here where the trusted disposition is known.
"""

import json
import os
from pathlib import Path
import sys


def _transient_unavailable(exc):
    """Connection and deadline failures are not an authorization pause."""
    return isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__name__ in {
        "URLError", "TimeoutExpired",
    }

from diagnostic_support import attach, failure, reference
from session_gate import IDENTITY, config_path, generation_env, runtime_scripts


def knowledge_names():
    catalog = json.loads(Path(__file__).with_name("mcp_catalog.json").read_text())
    return {tool["name"] for tool in catalog["knowledge"]}


def _admission(config):
    from mindie_knowledge.loop.activation import Admission

    path = config.get("admission_path")
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError("adapter configuration lacks an absolute admission_path")
    return Admission(path)


def resolve_lease(config, token):
    """The internal activation token resolves to its owning valid lease."""
    lease = _admission(config).resolve(token)
    if lease is None:
        raise ValueError("MindIE activation does not match an active lease")
    return lease


def finish_outcome(config, session, token, succeeded):
    _admission(config).finish(session, token, succeeded)


def call(payload):
    if payload["surface"] == "remote":
        return remote(payload)
    config = json.loads(config_path().read_text())
    # The internal activation token resolves the owning lease again inside the
    # runtime; it must agree with the gate-bound session, and no
    # caller-supplied identity is ever trusted on its own.
    token = payload["mindie_activation"]
    lease = resolve_lease(config, token)
    session = lease["session"]
    if session != payload.get("mindie_session_id"):
        raise ValueError("MindIE runtime identity mismatch")
    args, name = payload["arguments"], payload["name"]
    if payload["surface"] != "knowledge":
        raise ValueError("unknown plugin surface")
    from mindie_knowledge.loop.cli import ensure_service
    from mindie_knowledge.loop.transport import RequestRejected, rpc

    internal = payload.get("internal") is True
    if name in knowledge_names() and not internal:
        pass
    elif name == "knowledge_attach" and internal:
        # Internal activation-time bind only; never a front-stage tool.
        # Sharing off must not cold-start collection through this path.
        import sharing

        if not sharing.capture_allowed(lease, None):
            raise ValueError("knowledge attach requires enabled community sharing")
    else:
        raise ValueError("unknown knowledge tool")
    connection = ensure_service(config["engine_config"])
    if name == "knowledge_attach":
        # Admission is owned by the existing lease; startup needs no
        # second attach protocol or duplicate session registry.
        value = rpc(connection, "status", timeout=5)
        return dict(
            content=[dict(type="text", text=json.dumps(value, ensure_ascii=False))],
            structuredContent=value,
            isError=False,
        )
    # Never reconnect and resubmit a request with an uncertain outcome.
    succeeded = False
    neutral = False
    try:
        try:
            value = rpc(
                connection,
                name.removeprefix("knowledge_"),
                dict(args, _session_id=session, _activation=token),
                timeout=5,
            )
            result = dict(
                content=[dict(type="text", text=json.dumps(value, ensure_ascii=False))],
                structuredContent=value,
                isError=False,
            )
            succeeded = True
        except RequestRejected as exc:
            if name not in {"knowledge_query", "knowledge_explain"}:
                raise
            # A rejected read never started execution and is not an uncertain
            # mutation. Keep the service's bounded validation reason so the
            # caller can understand a bad ref. The explicit not_started
            # disposition is caller feedback, not a runtime failure: it must
            # not consume the failure circuit. isError stays True.
            neutral = True
            message = f"Knowledge read rejected: {str(exc)[:240]}. No corpus change; no automatic retry."
            result = dict(content=[dict(type="text", text=message)],
                          structuredContent=dict(code="read_rejected",
                                                 execution="not_started",
                                                 message=message,
                                                 automatic_retry=False),
                          isError=True)
        return result
    finally:
        pending = sys.exc_info()[1]
        if pending is not None and _transient_unavailable(pending):
            neutral = True
        if not neutral:
            try:
                finish_outcome(config, session, token, succeeded)
            except Exception:
                pass


def remote(payload):
    """General remote dispatch: gate-bound native task identity, no lease.

    The gate derived remote_session_id from verified host metadata; it is
    revalidated here and reaches remote-dev's REMOTE_DEV_SESSION_ID so one
    task cannot operate on another task's jobs. State lives in the
    independent remote state dir; the knowledge engine config/root and any
    activation bearer are never read on this path.
    """
    from mcp_gate import remote_state_dir

    session = payload.get("remote_session_id")
    if not isinstance(session, str) or not IDENTITY.fullmatch(session):
        raise ValueError("MindIE remote runtime identity mismatch")
    name = payload["name"]
    catalog = json.loads(Path(__file__).with_name("mcp_catalog.json").read_text())
    if name not in {tool["name"] for tool in catalog["remote"]}:
        raise ValueError("unknown remote tool")
    os.environ["REMOTE_DEV_STATE_DIR"] = str(remote_state_dir() / "runtime" / session)
    os.environ["REMOTE_DEV_SESSION_ID"] = "codex-task-" + session
    from remote_dev.mcp.tools import call_tool
    from remote_dev.core.rpc_transport import close_connections
    from remote_dev.mcp.server import tool_text

    args = dict(payload["arguments"])
    args["connect_timeout_ms"] = min(args.get("connect_timeout_ms", 10000), 10000)
    if "yield_time_ms" in args:
        args["yield_time_ms"] = min(args["yield_time_ms"], 30000)
    try:
        value = call_tool(name, args)
        result = value.get("result", {}) if isinstance(value, dict) else {}
        # Job authorization stays inside remote-dev's task-local store. The
        # model controls jobs by opaque job_id and must not receive IPC tokens.
        if isinstance(result, dict) and isinstance(result.get("job"), dict):
            result = dict(result, job={k: v for k, v in result["job"].items()
                                      if k != "authorization"})
        details = result.get("error_details") if isinstance(result, dict) else None
        uncertain = isinstance(result, dict) and (
            result.get("status") == "submission_uncertain"
            or (isinstance(details, dict) and details.get("submission_state") == "uncertain")
        )
        outcome = result.get("outcome") if isinstance(result, dict) else None
        # Nonzero command exits are valid observations only when the command
        # actually completed. A failed start/status/artifact RPC still trips the
        # failure circuit; its outcome must not be disguised as successful I/O.
        completed_command = (
            name in {"remote_bash", "remote_job_stdin"}
            and result.get("state") in {"succeeded", "failed", "timeout", "cancelled"}
            and isinstance(result.get("exit_code"), int)
        )
        is_error = (
            not isinstance(result, dict)
            or uncertain
            or (outcome not in {"success", "cancelled"} and not completed_command)
        )
        shaped = dict(
            content=[dict(type="text", text=tool_text(value))],
            structuredContent=result,
            isError=is_error,
        )
        # Trust only a diagnostic the shared remote component already attached.
        diagnostic = reference(value) or reference(result)
        return attach(shaped, diagnostic) if diagnostic else shaped
    except Exception as exc:
        # Preserve transport certainty inside the selected runtime. Exception
        # messages and arbitrary remote attributes are not public diagnostics.
        from remote_dev.core.errors import error_details
        from remote_dev.core.job_ops import JOB_ID_RE

        raw = error_details(exc)
        categories = {
            "internal", "caller", "validation", "permission", "remote_execution",
            "cancelled", "connection_capacity", "connection_timeout",
            "connection_unavailable", "rpc_disconnected", "rpc_send", "rpc_timeout",
            "command_exit", "command_protocol", "command_timeout", "command_cancelled",
            "remote_worker", "worker_capacity",
        }
        category = raw.get("category")
        category = category if isinstance(category, str) and category in categories else "internal"
        delivery = raw.get("submission_state")
        delivery = delivery if delivery in ("not_sent", "acknowledged", "uncertain") else "unknown"
        details = dict(category=category, submission_state=delivery,
                       retryable=raw.get("retryable") is True)
        result = dict(outcome="failed", error_details=details, automatic_retry=False)
        job = args.get("job_id")
        if isinstance(job, str) and JOB_ID_RE.fullmatch(job):
            result["job_id"] = job
        message = f"Remote {category}; submission={delivery}. No automatic retry."
        if "job_id" in result:
            message += f" Original job_id={job}; inspect this job before another submission."
        elif delivery != "not_sent":
            message += " Inspect the original operation before another submission; its outcome may be unknown."
        shaped = dict(content=[dict(type="text", text=message)],
                      structuredContent=result, isError=True)
        # In-process reference set by shared remote-dev; do not record expected
        # caller, network, permission, nonzero, timeout, or cancel outcomes.
        diagnostic = reference({"diagnostic": getattr(exc, "mindie_diagnostic", None)})
        return attach(shaped, diagnostic) if diagnostic else shaped
    finally:
        close_connections()


def redispatch():
    """Route this call through the one committed generation under its lock.

    The adapter config records (python, runtime_scripts) atomically at every
    install. An old loaded wrapper that still points at its own cached copy
    re-execs the recorded generation's copy with the recorded interpreter
    instead of mixing an old script with a new interpreter or library.
    """
    try:
        config = json.loads(config_path().read_text())
    except (OSError, ValueError):
        return  # Unconfigured: run in place and fail closed on the call itself.
    python, scripts = config.get("python"), config.get("runtime_scripts")
    if not isinstance(python, str) or not isinstance(scripts, str):
        return  # Pre-update setup layout: this copy IS the committed runtime.
    try:
        target = Path(scripts) / "runtime_call.py"
        same_script = target.is_file() and target.resolve() == Path(__file__).resolve()
        same_python = Path(python).resolve() == Path(sys.executable).resolve()
        if same_script and same_python:
            return
        if not target.is_file():
            return
        os.execve(python, [python, str(target)], generation_env())
    except OSError:
        return  # A stale generation record fails closed in this process.


if __name__ == "__main__":
    redispatch()
    try:
        raw = sys.stdin.buffer.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError("call exceeds limit")
        print(json.dumps(call(json.loads(raw)), ensure_ascii=False))
    except Exception as exc:
        shaped = dict(
            content=[
                dict(
                    type="text",
                    text=f"MindIE {type(exc).__name__}; no retry. Outcome may be unknown.",
                )
            ],
            isError=True,
        )
        diagnostic = reference({"diagnostic": getattr(exc, "mindie_diagnostic", None)})
        # RequestRejected derives ValueError. MaintenanceCancelled may derive
        # RuntimeError; match the class name statically so discovery does not
        # import core. ValueError, OSError/TimeoutError, and cancellation stay
        # expected and are not recorded here.
        internal = (ImportError, AttributeError, KeyError, TypeError, RuntimeError)
        if (
            diagnostic is None
            and isinstance(exc, internal)
            and type(exc).__name__ != "MaintenanceCancelled"
        ):
            diagnostic = failure(
                "runtime.dispatch", "dispatch", "internal", exception=exc
            )
        if diagnostic:
            shaped = attach(shaped, diagnostic)
        print(json.dumps(shaped))
