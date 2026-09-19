"""One authorized call in the configured interpreter; no request replay."""

import json
import os
from pathlib import Path
import sys

from session_gate import Sessions, config_path


def call(payload):
    session = payload["mindie_session_id"]
    Sessions().check(session, payload["mindie_activation"])
    config = json.loads(config_path().read_text())
    args, name = payload["arguments"], payload["name"]
    if payload["surface"] == "knowledge":
        from mindie_knowledge.loop.cli import ensure_service
        from mindie_knowledge.loop.transport import rpc

        if name not in {"knowledge_attach", "knowledge_query", "knowledge_explain", "knowledge_use"}:
            raise ValueError("unknown knowledge tool")
        connection = ensure_service(config["engine_config"])
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
    if payload["surface"] != "remote":
        raise ValueError("unknown plugin surface")
    catalog = json.loads(Path(__file__).with_name("mcp_catalog.json").read_text())
    if name not in {tool["name"] for tool in catalog["remote"]}:
        raise ValueError("unknown remote tool")
    engine = json.loads(Path(config["engine_config"]).read_text())
    os.environ["REMOTE_DEV_STATE_DIR"] = str(Path(engine["root"]) / "remote-dev")
    os.environ["REMOTE_DEV_SESSION_ID"] = "mindie-" + session
    from remote_dev.mcp.tools import call_tool
    from remote_dev.core.rpc_transport import close_connections
    from remote_dev.mcp.server import tool_text

    args = dict(args)
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
        # isError is RPC/policy level. A completed remote command with a
        # non-zero exit_code is still a successful observation (outcome
        # "failed" + state/status + exit_code). Blocked paths and uncertain
        # launches are not.
        is_error = (
            not isinstance(result, dict)
            or outcome is None
            or outcome == "blocked"
            or uncertain
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
