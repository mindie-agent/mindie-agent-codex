"""Build-time schema snapshot; discovery must not import the business runtime.

Gate rejects undeclared argument keys, so the snapshot must advertise exactly
that strict contract. Upstream remote-dev permits additional properties for
consumer-registered endpoint resolvers; this plugin never registers one, so
the generator drops the resolver boilerplate and pins additionalProperties to
false instead of asking Gate to ignore unknown keys.

The knowledge surface is the adapter-owned contract (query/explain/optional
feedback); it is defined here, not imported from the knowledge package, so
static discovery never depends on the installed runtime version. Internal
attach/status/capture are not front-stage tools and are never advertised.
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

# Truthful MCP annotations: query/explain are pure local reads; feedback is
# an idempotent write and is never labeled read-only. Remote tools mutate a
# remote environment and are annotated accordingly.
READ_ONLY = dict(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
FEEDBACK_WRITE = dict(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
REMOTE_MUTATION = dict(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)

STRING = {"type": "string"}


def schema(properties, required):
    return dict(
        type="object",
        properties=properties,
        required=required,
        additionalProperties=False,
    )


# Ordinary business surface per the sharing contract: on-demand search, read
# by reference, and optional feedback. No knowledge_use, no judging, no
# sync-to-authority frontend. Querying is never a prerequisite for capture.
KNOWLEDGE_TOOLS = [
    dict(
        name="knowledge_query",
        description=(
            "Search the selected domain's published knowledge and experience. "
            "Each advisory hit has a short pinned ref, title, summary and known software versions."
        ),
        inputSchema=schema(
            dict(query=STRING, limit={"type": "integer", "minimum": 1, "maximum": 20}),
            ["query"],
        ),
        annotations=READ_ONLY,
    ),
    dict(
        name="knowledge_explain",
        description=(
            "Read a result's detailed case and cited evidence using its exact short ref. "
            "Normally omit offset and limit; when supplied they count characters, not lines. "
            "A withdrawn result is explicitly marked as historical."
        ),
        inputSchema=schema(
            dict(
                ref={"type": "string", "description": "Copy the pinned ref returned by knowledge_query exactly."},
                offset={"type": "integer", "minimum": 0, "description": "Character offset; normally omit."},
                limit={"type": "integer", "minimum": 1, "maximum": 65536, "description": "Maximum characters, not lines; normally omit to read the detailed case."},
            ),
            ["ref"],
        ),
        annotations=READ_ONLY,
    ),
    dict(
        name="knowledge_feedback",
        description=(
            "Optional: record one current up/down vote on a reference revision "
            "with an optional one-line reason. Never required; silence is not a vote."
        ),
        inputSchema=schema(
            dict(
                ref=STRING,
                rating={"type": "string", "enum": ["up", "down"]},
                reason={"type": "string", "maxLength": 1000},
            ),
            ["ref", "rating"],
        ),
        annotations=FEEDBACK_WRITE,
    ),
]


def catalog():
    from remote_dev.mcp.tools import (
        ALIASES,
        PARAM_ALIASES,
        TOOL_SCHEMAS,
        list_tools,
        selector_fields,
    )

    result = dict(
        knowledge=copy.deepcopy(KNOWLEDGE_TOOLS),
        remote=[copy.deepcopy(t) for t in list_tools() if t["name"] in REMOTE_TOOLS],
    )
    for tool in result["remote"]:
        schema_ = tool["inputSchema"]
        # Preserve call-supported keys the advertised schema may omit.
        canonical = ALIASES[tool["name"]]
        call_properties = TOOL_SCHEMAS[canonical]["properties"]
        if tool["name"] == "remote_job_stop" and "force" in call_properties:
            schema_["properties"].setdefault("force", dict(type="boolean"))
        if tool["name"] in TRANSFER_TOOLS:
            schema_["properties"].update(
                timeout_ms=dict(type="integer"), timeout=dict(type="integer")
            )
        # Every advertised key must be one call_tool would actually accept.
        allowed = (
            set(call_properties)
            | set(PARAM_ALIASES.get(canonical, {}))
            | set(selector_fields())
            | {"timeout", "timeout_ms"}
        )
        extra = set(schema_["properties"]) - allowed
        if extra:
            raise ValueError(
                f"{tool['name']} advertises keys call_tool rejects: {sorted(extra)}"
            )
    for tool in result["remote"]:
        tool["annotations"] = REMOTE_MUTATION
    for tool in result["knowledge"]:
        tool["description"] = (
            "Requires manual MindIE activation in this session; the host "
            "binds task identity per call. " + tool["description"]
        )
    for tool in result["remote"]:
        tool["description"] = (
            "General remote tool for every native Codex task; no MindIE "
            "activation needed. The host binds native task identity per call; "
            "one task cannot reach another task's remote jobs. "
            + tool["description"]
        )
    for tools in result.values():
        for tool in tools:
            schema_ = tool["inputSchema"]
            schema_["additionalProperties"] = False
            if schema_.get("description"):
                schema_["description"] = schema_["description"].replace(
                    RESOLVER_BOILERPLATE, ""
                )
    return result


if __name__ == "__main__":
    Path(__file__).with_name("mcp_catalog.json").write_text(
        json.dumps(catalog(), ensure_ascii=False, indent=2) + "\n"
    )
