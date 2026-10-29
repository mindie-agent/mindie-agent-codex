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
from mcp_gate import Gate, call_failure
from runtime_call import KnowledgeInactive


def invoke(operation, arguments):
    session = native_session()
    if operation == "describe" and admission(read_config()).knowledge_lease(session) is None:
        raise KnowledgeInactive("Knowledge requires manual /mindie-agent activation in this task")
    gate = Gate("knowledge")
    if operation == "describe":
        return dict(tools=gate.tools)
    return gate.call(dict(id="knowledge-cli", params=dict(name=operation, arguments=arguments)),
                     cli_identity=(session, None))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) != 1:
            raise ValueError("one knowledge operation or describe is required")
        arguments = {}
        if argv[0] != "describe":
            raw = sys.stdin.buffer.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("knowledge arguments exceed 1 MiB")
            arguments = json.loads(raw)
        result = invoke(argv[0], arguments)
    except Exception as exc:
        result = call_failure(exc)
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return int(result.get("isError") is True)


if __name__ == "__main__":
    raise SystemExit(main())
