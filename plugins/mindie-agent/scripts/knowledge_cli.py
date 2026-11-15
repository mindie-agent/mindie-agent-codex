"""[Role] Private knowledge entry loaded only by the explicitly invoked Skill.

[Invariants]
- Neither this catalog nor its tool descriptions belongs in default MCP discovery.
- Identity comes from the native shell, never model-supplied session arguments.
- Catalog access and execution require the current task's manual grant.
- Input is exactly one byte source: binary stdin or one UTF-8 `--input-file`.
  A selected file is authoritative; an unreadable file never falls back to stdin.

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
from mcp_gate import Gate, InputUnavailableError, InvalidToolArguments, call_failure
from runtime_call import KnowledgeInactive

INPUT_LIMIT = 1024 * 1024


def _read_arguments(argv):
    """Map the declared CLI grammar to one bounded, exclusive byte source."""
    if len(argv) == 1 and argv[0] == "describe":
        return None
    if not argv or argv[0] == "describe":
        raise InvalidToolArguments(
            "one knowledge operation or describe is required",
            stage="cli_input", reason="argv_invalid")
    if len(argv) == 1:
        raw = sys.stdin.buffer.read(INPUT_LIMIT + 1)
    elif len(argv) == 3 and argv[1] == "--input-file" and bool(argv[2]):
        try:
            target = Path(argv[2])
            if not target.is_file():
                raise InputUnavailableError("knowledge input file is unavailable")
            with target.open("rb") as stream:
                raw = stream.read(INPUT_LIMIT + 1)
        except TimeoutError:
            raise
        except OSError:
            raise InputUnavailableError("knowledge input file is unavailable") from None
    else:
        raise InvalidToolArguments(
            "one knowledge operation with optional --input-file is required",
            stage="cli_input", reason="argv_invalid")
    if len(raw) > INPUT_LIMIT:
        raise InvalidToolArguments(
            "knowledge arguments exceed 1 MiB",
            stage="cli_input", reason="input_too_large")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InvalidToolArguments(
            "knowledge arguments must be UTF-8 JSON",
            stage="cli_input", reason="input_encoding") from None
    if not text or not text.strip():
        raise InvalidToolArguments(
            "knowledge arguments are empty",
            stage="cli_input", reason="input_empty")
    try:
        arguments = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidToolArguments(
            "knowledge arguments must be valid JSON",
            stage="cli_input", reason="input_json",
            line=exc.lineno, column=exc.colno) from None
    except ValueError:
        raise InvalidToolArguments(
            "knowledge arguments must be valid JSON",
            stage="cli_input", reason="input_json") from None
    if type(arguments) is not dict:
        raise InvalidToolArguments(
            "knowledge arguments must be a JSON object",
            stage="cli_input", reason="input_not_object")
    return arguments


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
        arguments = _read_arguments(argv)
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
