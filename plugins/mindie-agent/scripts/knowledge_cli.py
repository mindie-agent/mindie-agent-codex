"""[Role] Private knowledge entry loaded only by the explicitly invoked Skill.

[Invariants]
- Neither this catalog nor its tool descriptions belongs in default MCP discovery.
- Identity comes from the native shell, never model-supplied session arguments.
- Catalog access and execution require the current task's manual grant.

[Timeouts & Locks]
- Calls reuse Gate's runtime lock, knowledge deadlines and owned child cleanup.
- Arguments are bounded to 1 MiB; describe only reads the existing lease/catalog.

[Side-Effect Policy]
- Describe never activates knowledge or starts a service. Calls retain the
  knowledge engine's domain, contribution and capture checks.
"""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from admission_ops import admission, native_session, read_config
from mcp_gate import Gate, InvalidToolArguments, call_failure
from runtime_call import KnowledgeInactive


def invoke(operation, arguments):
    try:
        session = native_session()
        if operation == "describe":
            config = read_config()
            if admission(config).knowledge_lease(session) is None:
                from admission_ops import config_path
                from manual_entry import explain_inactive_session
                raise KnowledgeInactive(explain_inactive_session(
                    config_path(), session, admission_path=config.get("admission_path")))
        gate = Gate("knowledge")
        if operation == "describe":
            return dict(tools=gate.tools)
    except Exception as exc:
        exc.request_not_started = True
        exc.request_stage = "admission"
        raise
    return gate.call(dict(id="knowledge-cli", params=dict(name=operation, arguments=arguments)),
                     cli_identity=(session, None))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) != 1:
            raise InvalidToolArguments("one knowledge operation or describe is required")
        arguments = {}
        if argv[0] != "describe":
            raw = sys.stdin.buffer.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise InvalidToolArguments("knowledge arguments exceed 1 MiB")
            try:
                arguments = json.loads(raw)
            except ValueError as exc:
                raise InvalidToolArguments("knowledge arguments must be valid JSON") from exc
        result = invoke(argv[0], arguments)
    except KnowledgeInactive as exc:
        explanation = str(exc)[:300]
        result = dict(
            content=[dict(
                type="text",
                text=f"Knowledge request rejected before execution: {explanation}. No automatic retry.",
            )],
            isError=True,
            structuredContent=dict(
                component="mindie-agent-codex",
                stage="authorization",
                code="knowledge_inactive",
                execution="not_started",
                automatic_retry=False,
                message=explanation,
            ),
        )
    except Exception as exc:
        result = call_failure(exc)
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return int(result.get("isError") is True)


if __name__ == "__main__":
    raise SystemExit(main())
