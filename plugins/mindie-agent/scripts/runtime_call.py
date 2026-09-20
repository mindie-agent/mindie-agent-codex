"""One authorized call in the configured interpreter; no request replay."""

import json
import os
from pathlib import Path
import sys

from mcp_gate import remote_state_dir
from session_gate import IDENTITY, Sessions, config_path


def knowledge_names():
    catalog = json.loads(Path(__file__).with_name("mcp_catalog.json").read_text())
    return {tool["name"] for tool in catalog["knowledge"]}


def call(payload):
    if payload["surface"] == "remote":
        return remote(payload)
    # The internal activation token resolves the owning lease again inside the
    # runtime; it must agree with the gate-bound session, and no
    # caller-supplied identity is ever trusted on its own.
    lease = Sessions().resolve(payload["mindie_activation"])
    session = lease["session"]
    if session != payload.get("mindie_session_id"):
        raise ValueError("MindIE runtime identity mismatch")
    config = json.loads(config_path().read_text())
    args, name = payload["arguments"], payload["name"]
    if payload["surface"] != "knowledge":
        raise ValueError("unknown plugin surface")
    from mindie_knowledge.loop.cli import ensure_service
    from mindie_knowledge.loop.transport import rpc

    if name in knowledge_names():
        pass
    elif name == "knowledge_attach" and payload.get("internal") is True:
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
    value = rpc(
        connection,
        name.removeprefix("knowledge_"),
        dict(args, _session_id=session, _activation=payload["mindie_activation"]),
        timeout=5,
    )
    return dict(
        content=[dict(type="text", text=json.dumps(value, ensure_ascii=False))],
        structuredContent=value,
        isError=False,
    )


def remote(payload):
    """General remote dispatch: gate-bound native task identity, no lease.

    The gate derived remote_session_id from verified host metadata; it is
    revalidated here and reaches remote-dev's REMOTE_DEV_SESSION_ID so one
    task cannot operate on another task's jobs. State lives in the
    independent remote state dir; the knowledge engine config/root and any
    activation bearer are never read on this path.
    """
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
        return dict(
            content=[dict(type="text", text=tool_text(value))],
            structuredContent=result,
            isError=is_error,
        )
    finally:
        close_connections()


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError("call exceeds limit")
        print(json.dumps(call(json.loads(raw)), ensure_ascii=False))
    except Exception as exc:
        print(
            json.dumps(
                dict(
                    content=[
                        dict(
                            type="text",
                            text=f"MindIE {type(exc).__name__}; no retry. Outcome may be unknown.",
                        )
                    ],
                    isError=True,
                )
            )
        )
