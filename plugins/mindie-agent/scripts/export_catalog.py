"""Build-time schema snapshot; discovery must not import the business runtime.

[Role]
Generates `mcp_catalog.json` containing the static `knowledge` (`knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`) and `remote` tool schemas for `mcp_gate.py`.

[Invariants]
- Enforces `additionalProperties: false` on all exported tool input schemas so `mcp_gate.py` can reject undeclared arguments before runtime dispatch.
- Never advertises internal operations (`knowledge_attach`) or retired tools (`knowledge_use`, `knowledge_judge`).

[Timeouts & Locks]
- Build-time helper; no runtime locks or timeouts.

[Side-Effect Policy]
- Importing is side-effect free; `__main__` writes `mcp_catalog.json`.
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

# Preserve deadlines declared by each upstream tool. Streaming artifact
# transfers also accept the common timeout aliases, so advertise those
# caller controls without adding undeclared keys elsewhere (e.g. remote_write).
TRANSFER_TOOLS = {"remote_artifact_pull", "remote_artifact_push"}

# Truthful MCP annotations: query/explain are pure local reads; feedback is
# an idempotent write and distill invokes one bounded organizer. Neither is
# labeled read-only. Remote tools mutate a remote environment.
READ_ONLY = dict(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
FEEDBACK_WRITE = dict(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
DISTILL_WRITE = dict(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
REMOTE_MUTATION = dict(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)
REMOTE_READ = dict(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
REMOTE_READ_TOOLS = {"remote_read", "remote_grep", "remote_job_status", "remote_job_tail"}

STRING = {"type": "string"}
DOMAIN_PATTERN = "^[a-z][a-z0-9-]{0,63}$"


def domain_property():
    return {"type": "string", "pattern": DOMAIN_PATTERN,
            "description": "One configured canonical knowledge domain; omit to use the installed default."}


DISTILL_KINDS = [
    "experiment_ledger", "run_manifest", "authorized_transcript",
    "ecosystem_repo", "hardware_spec", "ablation_receipt",
]
DISTILL_OUTCOMES = [
    "observed", "verified", "compile_failed", "infra_failed",
    "measurement_invalid", "in_flight",
]
DISTILL_TASK_STATES = ["in_flight", "verified_milestone", "completed"]


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
            "Search graded rules and task material in the selected domain. Each match identifies its source, "
            "has a readable ref, and returns feedback_ref for the exact observed revision. "
            "Pass known task conditions to filter graded rules; unknown conditions are excluded by default. "
            "For related task blocks, pass related_next as continuation without query. "
            "A changed corpus rejects the continuation. Reference material does not establish applicability."
        ),
        inputSchema=schema(
            dict(
                query=STRING,
                limit={"type": "integer", "minimum": 1, "maximum": 20},
                conditions={"type": "object", "description": "Observed task facts used to check rule applicability."},
                continuation=STRING,
                domain_path={"type": "string",
                             "description": "Preferred graded domain_path prefix; cross-directory fallback remains available."},
                token_budget={"type": "integer", "minimum": 1, "maximum": 16000,
                              "default": 800,
                              "description": "Body excerpt and inherited-rule budget in estimated tokens."},
                include_unknown={"type": "boolean", "default": False,
                                 "description": "Include units whose declared conditions are unknown; unknown is not evidence of applicability."},
                domain=domain_property(),
            ),
            [],
        ),
        annotations=READ_ONLY,
    ),
    dict(
        name="knowledge_explain",
        description=(
            "Read a returned graded-unit, task or block ref. A graded-unit ref returns its exact current revision "
            "and conditions; a task ref returns navigation and first_block_ref; a block ref returns its immutable "
            "body and adjacent block refs. Replaced graded revisions and withdrawn material are unavailable. "
            "Use the returned ref to read and feedback_ref to vote."
        ),
        inputSchema=schema(
            dict(
                ref={"type": "string", "description": "Copy the graded-unit, task or block ref returned by query/explain exactly."},
            ),
            ["ref"],
        ),
        annotations=READ_ONLY,
    ),
    dict(
        name="knowledge_feedback",
        description=(
            "Optional: copy feedback_ref from query/explain into ref to record an up/down vote on that observed revision "
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
    dict(
        name="knowledge_distill",
        description=(
            "Explicitly distill caller-selected, already-authorized evidence into graded knowledge. "
            "task_state is supplied by the caller; ordinary Stop never selects completion. "
            "The host binds native task identity and the active lease. "
            "May invoke the configured native organizer once and apply a returned C6 decision locally; "
            "no GitHub write."
        ),
        inputSchema=schema(
            dict(
                inputs={
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "kind": {"type": "string", "enum": DISTILL_KINDS},
                            "outcome": {"type": "string", "enum": DISTILL_OUTCOMES},
                            "source_locator": STRING,
                            "content": STRING,
                            "evidence_refs": {"type": "array", "items": STRING},
                            "repetitions": {"type": "integer", "minimum": 1},
                        },
                        "required": ["kind", "outcome", "source_locator", "content", "evidence_refs"],
                    },
                },
                task_state={"type": "string", "enum": DISTILL_TASK_STATES},
                domain=domain_property(),
            ),
            ["inputs", "task_state"],
        ),
        annotations=DISTILL_WRITE,
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
        tool["annotations"] = REMOTE_READ if tool["name"] in REMOTE_READ_TOOLS else REMOTE_MUTATION
    for tool in result["knowledge"]:
        tool["description"] = (
            "The host binds native task identity per call. " + tool["description"]
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
        json.dumps(catalog(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
