#!/usr/bin/env python3
"""Index complete new material through one bounded native small-model call.

The worker emits an explicit outcome envelope, including failures and unknown
model outcomes. Exit zero means the envelope was delivered, not summary success.
Only the caller may scan and apply returned metadata to material files.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from mindie_knowledge.materials import summarizer
import process_guard
from process_guard import InvalidResultError, NativeFailure, NativeStartError, OutputLimitExceeded, run_codex

SUMMARY_MODEL = "gpt-5.6-luna"
SUMMARY_EFFORT = "low"
MAX_REQUEST_BYTES = 128 * 1024


class ConfigurationError(ValueError):
    pass


def identity():
    # Read the implementation actually executing, not merely its command path.
    sources = (Path(__file__), Path(process_guard.__file__), Path(summarizer.__file__))
    implementation = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    implementation["native_command"] = os.environ.get("MINDIE_CODEX_BIN", "codex")
    return summarizer.policy_identity(model=SUMMARY_MODEL, effort=SUMMARY_EFFORT,
                                      implementation=implementation)


class NativeSummaryModel:
    """LangMem `.invoke` bridge; no provider SDK, tools, history or retries."""

    def __init__(self):
        self.receipt = dict(native_started=False, turn_started=False, turn_completed=False,
                            turn_failed=False, request_rejected=False, usage=None, elapsed_ms=0, cleanup_failed=False)
        self.raw_result = None
        self.calls = 0

    def invoke(self, messages):
        from langchain_core.messages import AIMessage
        self.calls += 1
        if self.calls != 1:
            raise NativeFailure("summary attempted another model call")
        data = [dict(role=message.type, content=message.content) for message in messages]
        prompt = summarizer.render_prompt(data)
        if len(prompt.encode("utf-8")) > summarizer.MAX_PROMPT_BYTES:
            raise summarizer.SummaryInputError("complete prompt exceeds budget")
        with tempfile.TemporaryDirectory(prefix="mindie-summary-") as directory:
            schema, output = Path(directory) / "schema.json", Path(directory) / "result.json"
            schema.write_text(json.dumps(summarizer.output_schema(
                [message.id for message in messages if message.id])), encoding="utf-8")
            command = [os.environ.get("MINDIE_CODEX_BIN", "codex"), "exec", "--model", SUMMARY_MODEL,
                       "-c", f'model_reasoning_effort="{SUMMARY_EFFORT}"', "--ignore-user-config", "--ignore-rules",
                       "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check", "-C", directory,
                       "-c", "features.hooks=false", "-c", "features.apps=false",
                       "-c", "features.shell_tool=false", "-c", "features.multi_agent=false",
                       "-c", "features.unbounded_connection_retries=false", "-c", 'web_search="disabled"',
                       "--output-schema", str(schema), "--output-last-message", str(output), "--json", "-"]
            run_codex(command, prompt, timeout=summarizer.SUMMARY_TIMEOUT, receipt=self.receipt)
            if not self.receipt["turn_completed"]:
                raise NativeFailure("native execution did not report completion")
            try:
                with output.open("rb") as stream:
                    raw = stream.read(summarizer.MAX_RESPONSE_BYTES + 1)
                if len(raw) > summarizer.MAX_RESPONSE_BYTES:
                    raise OutputLimitExceeded("summary metadata exceeds output budget")
                self.raw_result = raw.decode("utf-8")
            except OutputLimitExceeded:
                raise
            except (OSError, UnicodeError):
                raise InvalidResultError("summary output is unavailable or invalid") from None
            return AIMessage(content=self.raw_result)


def _category(error):
    if getattr(error, 'mindie_category', None) == 'configuration':
        return 'configuration'
    if isinstance(error, (ConfigurationError, NativeStartError, ImportError)):
        return "configuration"
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        return "deadline"
    if isinstance(error, OutputLimitExceeded):
        return "output_limit"
    if isinstance(error, (InvalidResultError, summarizer.SummaryInputError,
                          summarizer.SummaryResultError, ValueError)):
        return "invalid_result"
    if isinstance(error, NativeFailure):
        return "native"
    return "unknown"


def _reason(error, receipt):
    if isinstance(error, summarizer.SummaryResultError):
        return error.reason
    if receipt.get("request_rejected"):
        return "native_model_rejected"
    if isinstance(error, ConfigurationError):
        return "worker_policy_changed"
    if isinstance(error, NativeStartError):
        return "native_start_failed"
    if isinstance(error, ImportError):
        return "dependency_unavailable"
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        return "native_deadline"
    if isinstance(error, OutputLimitExceeded):
        return "native_output_limit"
    if isinstance(error, InvalidResultError):
        return "native_output_unreadable" if receipt.get("turn_completed") else "result_contract_invalid"
    if isinstance(error, summarizer.SummaryInputError):
        return "request_invalid"
    if isinstance(error, NativeFailure):
        return "native_failure"
    return "unknown_failure"


def run(payload):
    # Invalid wire input is rejected before a model object exists. Valid requests
    # always receive an explicit outcome, even for configuration/parse failures.
    summarizer.validate_request(payload)
    started = time.monotonic()
    model = NativeSummaryModel()
    result, error, error_reason, status = None, None, None, "returned"
    try:
        if payload["policy_identity"] != identity():
            raise ConfigurationError("summary worker policy changed")
        result = summarizer.summarize_batch(payload, model)
    except Exception as exc:
        error = _category(exc)
        error_reason = _reason(exc, model.receipt)
        # A missing executable is known to be uncalled. A completed model with
        # invalid metadata is a known failure with its usage retained. Interruption
        # after spawn without terminal evidence has an uncertain paid outcome.
        status = ("outcome_unknown" if model.receipt["native_started"]
                  and not model.receipt["turn_completed"]
                  and not model.receipt.get("turn_failed") else "failed")
    usage = model.receipt.get("usage")
    known = bool(model.receipt["turn_completed"] and isinstance(usage, dict)
                 and all(type(usage.get(key)) is int and usage[key] >= 0 for key in summarizer.COUNTERS))
    return summarizer.outcome(payload, status=status, result=result,
                              raw_result=model.raw_result, usage=usage,
                              model_calls=int(model.receipt["native_started"]), usage_known=known,
                              elapsed_ms=round((time.monotonic() - started) * 1000), error=error, error_reason=error_reason,
                              cleanup_failed=model.receipt.get("cleanup_failed", False),
                              billing_status="rejected" if model.receipt.get("request_rejected") else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", action="store_true", help="Print actual policy identity without a model call")
    args = parser.parse_args()
    try:
        if args.identity:
            value = identity()
        else:
            raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
            if len(raw) > MAX_REQUEST_BYTES:
                raise summarizer.SummaryInputError("summary request exceeds wire budget")
            value = run(json.loads(raw))
        sys.stdout.buffer.write((summarizer.canonical(value) + "\n").encode("utf-8"))
        return 0
    except Exception as exc:
        category = _category(exc)
    from mindie_knowledge.loop.process import AGENT_ERROR_EXIT_CODES
    print("summary protocol failed: " + category, file=sys.stderr)
    return AGENT_ERROR_EXIT_CODES.get(category, 2)


if __name__ == "__main__":
    raise SystemExit(main())
