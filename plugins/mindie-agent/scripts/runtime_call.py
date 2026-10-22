"""One authorized call in this fixed release runtime; no request replay.

[Role]
Executes a single authorized knowledge or remote-dev tool invocation inside the
fixed installed runtime environment.

[Invariants]
- Admission belongs to the shared core; each request uses the exact installed
  configuration without script or runtime reselection.
- An explicit public domain or canonical `mindie://DOMAIN/...` reference routes
  to one existing configured engine; a batch `refs` list routes by its first
  canonical entry and never merges authorities. A missing/failed target route
  never falls back to the default; a no-domain call keeps the exact default path.
- The target engine's own admission store owns capture/distill leases and its
  own sharing configuration owns publishability. No authority is copied across
  domains.
- Connection and deadline failures are classified as transient unavailability,
  never as authorization pauses.

[Timeouts & Locks]
- Bounded RPC and helper deadlines; C6 distillation uses the shared summary
  worker budget plus ledger/cleanup reserve, not the short query deadline.

[Side-Effect Policy]
- Attaches structured diagnostic references on failure; never silently replays
  failed tool calls.
"""

import json
import os
from pathlib import Path
import sys
import subprocess


def _transient_unavailable(exc):
    """Connection and deadline failures are not an authorization pause."""
    return isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__name__ in {
        "URLError", "TimeoutExpired",
    }

from diagnostic_support import attach, failure, reference
from rsi_explore import attach as attach_exploration
from rsi_explore import forwarded as forwarded_exploration
from session_gate import IDENTITY, config_path, runtime_env, runtime_scripts


def knowledge_names():
    catalog = json.loads(Path(__file__).with_name("mcp_catalog.json").read_text(encoding='utf-8'))
    return {tool["name"] for tool in catalog["knowledge"]}


def distill_rpc_timeout():
    """The shared C6 budget: two scanners, identity, worker and SQLite reserve."""
    try:
        from mindie_knowledge.loop import summary_budget
        return summary_budget.distill_rpc_timeout()
    except (ImportError, AttributeError) as exc:
        raise ValueError("installed knowledge runtime does not provide C6 distillation") from exc


def distill_helper_timeout():
    """Leave process start, serialization and cleanup outside the inner RPC."""
    return distill_rpc_timeout() + 30.0


class LeaseRequired(ValueError):
    """The current native session has no active lease in the selected engine."""


def _active_lease_token(config, session):
    """Resolve the existing target-engine lease; never mint a caller token."""
    lease = _admission(config).active_lease(session)
    if lease is None:
        raise LeaseRequired(
            "knowledge distill requires an active task lease in the selected domain"
        )
    return lease["token"]


def _admission(config):
    from mindie_knowledge.loop.activation import Admission

    if isinstance(config, (str, os.PathLike)):
        # Routing already validated this selected JSON object; admission only
        # needs the target engine's own configured path, not a second config
        # interpretation. Service selection performs the full validation.
        config = json.loads(Path(config).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("selected engine configuration is not a JSON object")
    path = config.get("admission_path")
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError("selected engine configuration lacks an absolute admission_path")
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
    config = json.loads(config_path().read_text(encoding='utf-8'))
    args, name = payload["arguments"], payload["name"]
    if name in {"knowledge_query", "knowledge_explain"}:
        args = forwarded_exploration(args)
    token = payload.get("mindie_activation")
    session = payload.get("mindie_session_id")
    if (not isinstance(session, str) or not IDENTITY.fullmatch(session)
            or token is None and payload.get('native_session_verified') is not True):
        raise ValueError("MindIE runtime identity mismatch")
    if payload["surface"] != "knowledge":
        raise ValueError("unknown plugin surface")
    internal = payload.get("internal") is True
    if name in knowledge_names() and not internal:
        pass
    elif name == "knowledge_attach" and internal:
        # Internal activation-time bind only; never a front-stage tool.
        pass
    else:
        raise ValueError("unknown knowledge tool")
    from service_client import (
        connect,
        explicit_route,
        route_ref as batch_route_ref,
        select_engine,
    )
    from mindie_knowledge.loop.transport import RequestRejected, rpc

    # Select exactly one existing engine before any public data read. A
    # no-domain call returns the configured default unchanged (old-runtime
    # compatible); an explicit domain or URI must select its own engine.
    # Batch explain has no singular `ref`; route by its first canonical
    # mindie:// entry and reject a batch that spans multiple authorities.
    route_domain = args.get("domain")
    route_ref = args.get("ref")
    if route_ref is None:
        route_ref = batch_route_ref(refs=args.get("refs"))
    routed = explicit_route(route_domain, route_ref)
    selected = select_engine(
        config["engine_config"], domain=route_domain, ref=route_ref,
        domain_configs=config.get("domain_configs"),
    )
    # C6 capture admission needs the existing target-engine lease token, not a
    # fabricated `_session_verified` flag. A target with an independent
    # admission store must already hold this session's lease; a deliberately
    # shared admission_path remains supported configuration.
    lease_config = selected if routed else config
    if name == "knowledge_distill" and token is None:
        try:
            token = _active_lease_token(lease_config, session)
        except LeaseRequired:
            if routed:
                raise
            # Preserve the installed no-domain call's historical disposition.
            raise ValueError(
                "knowledge distill requires an active task lease"
            ) from None
    # The internal activation token resolves the owning lease again against
    # the selected engine; it must agree with the gate-bound session, and no
    # caller-supplied identity is ever trusted on its own.
    lease = resolve_lease(lease_config, token) if token else None
    if lease is not None and lease['session'] != session:
        raise ValueError("MindIE runtime identity mismatch")
    if name == "knowledge_attach":
        # Sharing off must not cold-start collection through this path.
        import sharing

        if not sharing.capture_allowed(lease, None):
            raise ValueError("knowledge attach requires enabled community sharing")
    connection = connect(selected)
    if name == "knowledge_attach":
        # Admission is owned by the existing lease; startup needs no
        # second attach protocol or duplicate session registry.
        value = rpc(connection, "status")
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
            # With a resolved activation token the service checks the capture
            # lease; without one the verified host flag is the read/feedback
            # path. Never send both, because C6 requires exactly inputs/task_state.
            identity = dict(_session_id=session)
            if token is None:
                identity["_session_verified"] = True
            else:
                identity["_activation"] = token
            value = rpc(
                connection,
                name.removeprefix("knowledge_"),
                dict(args, **identity),
                timeout=distill_rpc_timeout() if name == "knowledge_distill" else None,
            )
            is_error = False
            if name == "knowledge_distill":
                from mindie_knowledge.graded.distill import result_view
                value, is_error = result_view(value)
            elif name in {"knowledge_query", "knowledge_explain"}:
                value = attach_exploration(value, args)
            succeeded = not is_error
            result = dict(
                content=[dict(type="text", text=json.dumps(value, ensure_ascii=False))],
                structuredContent=value,
                isError=is_error,
            )
        except RequestRejected as exc:
            if name not in {"knowledge_query", "knowledge_explain"}:
                raise
            code = getattr(exc, "error_code", None)
            if code == "material_corrupt":
                raise  # Corrupt required bytes are an operational failure.
            # A rejected read never started execution and is not an uncertain
            # mutation. Keep the service's bounded validation reason so the
            # caller can understand a bad ref. The explicit not_started
            # disposition is caller feedback, not a runtime failure: it must
            # not consume the failure circuit. isError stays True.
            neutral = True
            message = f"Knowledge read rejected: {str(exc)[:240]}. No corpus change; no automatic retry."
            allowed_codes = {"reference_invalid", "withdrawn", "removed_or_superseded",
                             "continuation_invalid", "continuation_expired"}
            result = dict(content=[dict(type="text", text=message)],
                          structuredContent=dict(code=code if code in allowed_codes else "read_rejected",
                                                 execution="not_started",
                                                 message=message,
                                                 automatic_retry=False),
                          isError=True)
            read_ref = getattr(exc, "read_ref", None)
            if isinstance(read_ref, str) and read_ref.startswith("mindie://") and len(read_ref) <= 1024:
                result["structuredContent"]["read_ref"] = read_ref
        return result
    finally:
        pending = sys.exc_info()[1]
        if pending is not None and _transient_unavailable(pending):
            neutral = True
        if token is not None and not neutral:
            try:
                finish_outcome(lease_config, session, token, succeeded)
            except Exception as exc:
                # The operation already ran. Expose the bookkeeping fault
                # without telling the caller to repeat a successful mutation.
                if pending is None:
                    result["accounting"] = dict(status="failed", error_type=type(exc).__name__,
                                                execution="completed", automatic_retry=False)
                    result["content"].append(dict(type="text", text=
                        "Knowledge operation completed; outcome accounting failed. Do not repeat the operation."))
                else:
                    pending.add_note("MindIE outcome accounting also failed: " + type(exc).__name__)


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
    catalog = json.loads(Path(__file__).with_name("mcp_catalog.json").read_text(encoding='utf-8'))
    if name not in {tool["name"] for tool in catalog["remote"]}:
        raise ValueError("unknown remote tool")
    os.environ["REMOTE_DEV_STATE_DIR"] = str(remote_state_dir() / "runtime" / session)
    os.environ["REMOTE_DEV_SESSION_ID"] = "codex-task-" + session
    from remote_dev.mcp.tools import call_tool
    from remote_dev.core.rpc_transport import close_connections
    from remote_dev.mcp.server import tool_text

    args = dict(payload["arguments"])
    shaped = None
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
        shaped = attach(shaped, diagnostic) if diagnostic else shaped
        return shaped
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
            "local_state", "stream_read", "stream_write",
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
        shaped = attach(shaped, diagnostic) if diagnostic else shaped
        return shaped
    finally:
        original = sys.exc_info()[1]
        try:
            close_connections()
        except Exception as cleanup_error:
            if shaped is not None:
                warning = dict(status="failed", stage="close_connections",
                               error_type=type(cleanup_error).__name__,
                               operation_outcome="preserved", automatic_retry=False)
                shaped["cleanup"] = warning
                structured = shaped.get("structuredContent")
                if isinstance(structured, dict):
                    structured["cleanup_warning"] = dict(warning)
                # The primary call owns the business result. A secondary
                # connection-cleanup fault is reported without rewriting a
                # succeeded remote call into an error.
                shaped["content"].append(dict(type="text", text=
                    "Remote operation result preserved; local connection cleanup failed. Do not repeat the operation."))
            elif original is not None:
                original.add_note("Connection cleanup also failed: " + type(cleanup_error).__name__)
            else:
                raise



if __name__ == "__main__":
    stage = "request"
    execution = "not_started"
    try:
        stage = "request"
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw)
        stage = "operation"
        execution = "outcome_unconfirmed"
        sys.stdout.buffer.write((json.dumps(call(payload), ensure_ascii=False) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
    except Exception as exc:
        try:
            from mindie_knowledge.loop.transport import FeedUnavailableError
        except ImportError:
            # A missing runtime dependency must retain the original failure.
            feed_unavailable = False
        else:
            feed_unavailable = isinstance(exc, FeedUnavailableError)
        try:
            from service_client import DomainRouteError
        except ImportError:
            domain_route_error = False
        else:
            domain_route_error = isinstance(exc, DomainRouteError)
        lease_required = isinstance(exc, LeaseRequired)
        routing_error = lease_required or domain_route_error
        if feed_unavailable:
            stage, execution = "query-source", "not_started"
        elif routing_error:
            stage, execution = "routing", "not_started"
        message = (
            "Knowledge query unavailable: a configured feed has no verified installed snapshot. "
            "The query did not run; inspect the feed sync result. No automatic retry."
            if feed_unavailable else
            f"Knowledge request rejected before execution: {str(exc)[:300]}. No automatic retry."
            if routing_error else
            f"MindIE {stage}: {type(exc).__name__}; execution={execution}; no automatic retry."
        )
        shaped = dict(
            content=[
                dict(
                    type="text",
                    text=message,
                )
            ],
            isError=True,
            structuredContent=dict(component="mindie-agent-codex", stage=stage,
                                   execution=execution, automatic_retry=False),
        )
        if feed_unavailable:
            shaped["structuredContent"]["code"] = "feed_unavailable"
        elif routing_error:
            shaped["structuredContent"]["code"] = (
                "lease_required" if lease_required else "domain_routing_rejected"
            )
            shaped["structuredContent"]["message"] = str(exc)[:300]
        diagnostic = reference({"diagnostic": getattr(exc, "mindie_diagnostic", None)})
        # RequestRejected derives ValueError. MaintenanceCancelled may derive
        # RuntimeError; match the class name statically so discovery does not
        # import core. ValueError, OSError/TimeoutError, and cancellation stay
        # expected and are not recorded here.
        internal = (ImportError, AttributeError, KeyError, TypeError, RuntimeError)
        if (
            diagnostic is None
            and not feed_unavailable
            and isinstance(exc, internal)
            and type(exc).__name__ != "MaintenanceCancelled"
        ):
            diagnostic = failure(
                "runtime.dispatch", "dispatch", "internal", exception=exc
            )
        if diagnostic:
            shaped = attach(shaped, diagnostic)
        print(json.dumps(shaped))
