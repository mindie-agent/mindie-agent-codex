"""Build-time schema snapshot; discovery must not import the business runtime.

Gate rejects undeclared argument keys, so the snapshot must advertise exactly
that strict contract. Upstream remote-dev permits additional properties for
consumer-registered endpoint resolvers; this plugin never registers one, so
the generator drops the resolver boilerplate and pins additionalProperties to
false instead of asking Gate to ignore unknown keys.
"""

import copy
import json
from pathlib import Path

REMOTE_TOOLS = {
    "remote_bash",
    "remote_read",
    "remote_write",
    "remote_grep",
    "remote_apply_patch",
    "remote_job_status",
    "remote_job_tail",
    "remote_job_stdin",
    "remote_job_stop",
    "remote_artifact_pull",
    "remote_artifact_push",
}

RESOLVER_BOILERPLATE = (
    "A consumer-registered resolver may claim additional keys; "
    "remote-dev does not interpret session, profile, or binding identifiers itself. "
)

# call_tool accepts a common timeout/timeout_ms on every operation (it bounds
# the 120-second default). Advertise it only on streaming artifact transfers,
# where a long pull/push genuinely needs caller control; every other tool
# keeps failing closed on an undeclared timeout key (e.g. remote_write).
TRANSFER_TOOLS = {"remote_artifact_pull", "remote_artifact_push"}

MINDIE_KEYS = {
    "mindie_session_id": "Native Codex session ID from manual activation; not a remote job ID",
    "mindie_activation": "Session capability from manual activation; never copy from another session",
}


def catalog():
    from mindie_knowledge.loop.cli import TOOLS
    from remote_dev.mcp.tools import ALIASES, PARAM_ALIASES, TOOL_SCHEMAS, list_tools, selector_fields

    result = dict(
        knowledge=copy.deepcopy(TOOLS),
        remote=[copy.deepcopy(t) for t in list_tools() if t["name"] in REMOTE_TOOLS],
    )
    for tool in result["remote"]:
        schema = tool["inputSchema"]
        # Preserve call-supported keys the advertised schema may omit.
        canonical = ALIASES[tool["name"]]
        call_properties = TOOL_SCHEMAS[canonical]["properties"]
        if tool["name"] == "remote_job_stop" and "force" in call_properties:
            schema["properties"].setdefault("force", dict(type="boolean"))
        if tool["name"] in TRANSFER_TOOLS:
            schema["properties"].update(
                timeout_ms=dict(type="integer"), timeout=dict(type="integer")
            )
        # Every advertised key must be one call_tool would actually accept.
        allowed = (
            set(call_properties)
            | set(PARAM_ALIASES.get(canonical, {}))
            | set(selector_fields())
            | {"timeout", "timeout_ms"}
            | set(MINDIE_KEYS)
        )
        extra = set(schema["properties"]) - allowed
        if extra:
            raise ValueError(
                f"{tool['name']} advertises keys call_tool rejects: {sorted(extra)}"
            )
    for tools in result.values():
        for tool in tools:
            tool["description"] = (
                "Requires manual MindIE activation in this session. "
                + tool["description"]
            )
            schema = tool["inputSchema"]
            schema["additionalProperties"] = False
            if schema.get("description"):
                schema["description"] = schema["description"].replace(
                    RESOLVER_BOILERPLATE, ""
                )
            for key, text in MINDIE_KEYS.items():
                schema["properties"][key] = dict(type="string", description=text)
                schema.setdefault("required", []).append(key)
    return result


if __name__ == "__main__":
    Path(__file__).with_name("mcp_catalog.json").write_text(
        json.dumps(catalog(), ensure_ascii=False, indent=2) + "\n"
    )
