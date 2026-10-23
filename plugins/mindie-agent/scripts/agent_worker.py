#!/usr/bin/env python3
"""Run one bounded native organizer call for summary or explicit C6 distillation.

[Role]
Implements `NativeSummaryModel` and the CLI worker (`agent_worker.py`) that
invokes `codex exec` (`gpt-5.6-luna`) in a sandboxed temporary workspace. The
ordinary summary request keeps its existing path; only an explicit
`mindie-distill-request/1` selects the shared C6 branch.

[Invariants]
- Emits an explicit JSON outcome envelope (`status="returned"`, `"failed"` or
  `"outcome_unknown"`). Exit 0 means the envelope was delivered, not success.
- Verifies `worker_policy` (`summary_provider`) before spawning `codex exec` so
  route changes abort before any model call.
- A Stop or in-flight task never becomes a completed milestone; the shared
  `value_gate`/noop decision runs before any paid C6 call.
- C6 prompt/schema/validation are imported from `mindie_knowledge.graded.distill`
  at call time; the old summary path must start without the new API.

[Timeouts & Locks]
- Native execution uses at most 120s and leaves cleanup time inside the parent
  worker deadline.

[Side-Effect Policy]
- Creates and cleans up one private temporary profile; never modifies user
  workspaces, material stores or publication state directly.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from mindie_knowledge.materials import summarizer
from mindie_knowledge.loop import summary_scratch
from mindie_knowledge.loop.summary_budget import native_timeout
import process_guard
import summary_provider
from process_guard import InvalidResultError, NativeFailure, NativeStartError, OutputLimitExceeded, run_codex

SUMMARY_MODEL = os.environ.get("MINDIE_SUMMARY_MODEL") or "gpt-5.6-luna"
SUMMARY_EFFORT = "low"
MAX_REQUEST_BYTES = 128 * 1024
# A prepared C6 request carries its sanitized evidence plus the worker receipt
# fields; it is bounded separately so old summary wire limits do not change.
MAX_WIRE_BYTES = 512 * 1024
MAX_DISTILL_PROMPT_BYTES = 320 * 1024
DISTILL_REQUEST_SCHEMA = "mindie-distill-request/1"


class ConfigurationError(ValueError):
    pass


def _distill_capability():
    """Advertise C6 only when the installed runtime actually provides it."""
    try:
        from mindie_knowledge.graded import distill
    except ImportError:
        return [], None
    required = ("build_prompt", "decision_schema", "parse_validate_result", "validate_decision")
    if (getattr(distill, "REQUEST_SCHEMA", None) != DISTILL_REQUEST_SCHEMA
            or not all(callable(getattr(distill, name, None)) for name in required)):
        return [], None
    return [DISTILL_REQUEST_SCHEMA], Path(distill.__file__)


def identity(route=None):
    # Read the implementation actually executing, not merely its command path.
    sources = [Path(__file__), Path(process_guard.__file__), Path(summarizer.__file__),
               Path(summary_provider.__file__), Path(summary_scratch.__file__)]
    capabilities, distill_source = _distill_capability()
    if distill_source is not None:
        sources.append(distill_source)
    implementation = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    implementation["native_command"] = os.environ.get("MINDIE_CODEX_BIN", "codex")
    route = summary_provider.selected(SUMMARY_MODEL) if route is None else route
    if route["provider"] is not None:
        implementation["provider_route_sha256"] = route["route_sha256"]
    value = summarizer.policy_identity(model=route["model"], effort=SUMMARY_EFFORT,
                                       implementation=implementation)
    # Capabilities extend the validated policy identity; they do not replace
    # the ordinary summary identity fields.
    value["capabilities"] = capabilities
    value["fingerprint"] = summarizer.digest({key: item for key, item in value.items()
                                              if key != "fingerprint"})
    return value


class NativeSummaryModel:
    """LangMem `.invoke` bridge; no provider SDK, tools, history or retries."""

    def __init__(self):
        self.receipt = dict(native_started=False, turn_started=False, turn_completed=False,
                            turn_failed=False, request_rejected=False, usage=None, elapsed_ms=0, cleanup_failed=False)
        self.raw_result = None
        self.calls = 0
        self.route = None

    def _invoke(self, prompt, schema, *, prefix):
        """One native call shared by summary and explicit C6 distillation."""
        self.calls += 1
        if self.calls != 1:
            raise NativeFailure("worker attempted another model call")
        directory, managed = summary_scratch.create_profile(prefix=prefix)
        try:
            route = self.route or summary_provider.selected(SUMMARY_MODEL)
            env = summary_provider.environment(route, directory)
            schema_path = Path(directory) / "schema.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            command = [os.environ.get("MINDIE_CODEX_BIN", "codex"), "exec", "--model", route["model"],
                       "-c", f'model_reasoning_effort="{SUMMARY_EFFORT}"', "--ignore-rules",
                       "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check", "-C", directory,
                       "-c", "features.hooks=false", "-c", "features.apps=false",
                       "-c", "features.shell_tool=false", "-c", "features.multi_agent=false",
                       "-c", "features.plugins=false", "-c", "features.skip_host_skill_discovery=true",
                       "-c", "suppress_unstable_features_warning=true",
                       "-c", "features.skill_search=false",
                       "-c", "features.unbounded_connection_retries=false", "-c", 'web_search="disabled"',
                       "--output-schema", str(schema_path), "--json", "-"]
            if env is None:
                command.insert(2, "--ignore-user-config")
            run_codex(command, prompt, timeout=native_timeout(120.0),
                      receipt=self.receipt, defer_cleanup=managed, env=env)
            if not self.receipt["turn_completed"]:
                raise NativeFailure("native execution did not report completion")
            try:
                message = self.receipt.get('final_message')
                if not isinstance(message, str) or not message:
                    raise InvalidResultError("completed worker message is unavailable")
                raw = message.encode('utf-8')
                if len(raw) > summarizer.MAX_RESPONSE_BYTES:
                    raise OutputLimitExceeded("worker metadata exceeds output budget")
                self.raw_result = message
            except OutputLimitExceeded:
                raise
            except UnicodeError:
                raise InvalidResultError("worker output is unavailable or invalid") from None
            return self.raw_result
        finally:
            try:
                if not managed:
                    shutil.rmtree(directory)
            except TimeoutError:
                raise
            except OSError as exc:
                self.receipt["cleanup_failed"] = True
                try:
                    from diagnostic_support import failure
                    failure("summary", stage="temporary-profile-cleanup", category="internal",
                            exception=exc,
                            reportable=False)
                except Exception:
                    pass

    def invoke(self, messages):
        from langchain_core.messages import AIMessage
        data = [dict(role=message.type, content=message.content) for message in messages]
        prompt = summarizer.render_prompt(data)
        if len(prompt.encode("utf-8")) > summarizer.MAX_PROMPT_BYTES:
            raise summarizer.SummaryInputError("complete prompt exceeds budget")
        schema = summarizer.output_schema([message.id for message in messages if message.id])
        self._invoke(prompt, schema, prefix="mindie-summary-")
        return AIMessage(content=self.raw_result)

    def invoke_distill(self, request):
        """Use only the shared C6 prompt/schema; never a local contract copy."""
        from mindie_knowledge.graded import distill
        build_prompt = getattr(distill, "build_prompt", None)
        decision_schema = getattr(distill, "decision_schema", None)
        if not callable(build_prompt) or not callable(decision_schema):
            raise ImportError("installed runtime does not provide the C6 prompt/schema")
        prompt = build_prompt(request)
        if not isinstance(prompt, str) or not prompt.strip():
            raise summarizer.SummaryInputError("shared C6 prompt is unavailable")
        if len(prompt.encode("utf-8")) > MAX_DISTILL_PROMPT_BYTES:
            raise summarizer.SummaryInputError("complete C6 prompt exceeds budget")
        schema = decision_schema()
        if not isinstance(schema, dict):
            raise summarizer.SummaryInputError("shared C6 decision schema is unavailable")
        return self._invoke(prompt, schema, prefix="mindie-distill-")


def _category(error):
    if getattr(error, 'mindie_category', None) == 'configuration':
        return 'configuration'
    if isinstance(error, (ConfigurationError, summary_provider.ProviderConfigurationError,
                          NativeStartError, ImportError)):
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
    if isinstance(error, (summary_provider.ProviderConfigurationError,
                          summary_scratch.ScratchOwnershipError)):
        return "configuration_invalid"
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
        reason = getattr(error, 'reason', 'native_failure')
        if isinstance(reason, str) and reason.startswith('native_') and reason in summarizer.SUMMARY_FAILURE_REASONS:
            return reason
        return 'native_failure'
    return "unknown_failure"


def run(payload):
    # Invalid wire input is rejected before a model object exists. Valid requests
    # always receive an explicit outcome, even for configuration/parse failures.
    # Only the explicit new schema selects C6; ordinary summary requests remain
    # on the existing summarizer path. A future C6 version fails closed instead
    # of silently being treated as a summary request.
    version = payload.get("schema_version") if isinstance(payload, dict) else None
    if (isinstance(version, str) and version.startswith("mindie-distill-request/")
            and version != DISTILL_REQUEST_SCHEMA):
        raise summarizer.SummaryInputError("unsupported distillation request schema")
    distill_request = version == DISTILL_REQUEST_SCHEMA
    summarizer.validate_request(payload)
    started = time.monotonic()
    model = NativeSummaryModel()
    result, error, error_reason, status = None, None, None, "returned"
    try:
        route = summary_provider.selected(SUMMARY_MODEL)
        if payload["policy_identity"] != identity(route):
            raise ConfigurationError("summary worker policy changed")
        model.route = route
        if distill_request:
            from organizer_distill import decide as decide_distillation
            result = decide_distillation(payload, model)
        else:
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
            raw = sys.stdin.buffer.read(MAX_WIRE_BYTES + 1)
            if len(raw) > MAX_WIRE_BYTES:
                raise summarizer.SummaryInputError("worker request exceeds wire budget")
            payload = json.loads(raw)
            distill = (isinstance(payload, dict)
                       and payload.get("schema_version") == DISTILL_REQUEST_SCHEMA)
            if not distill and len(raw) > MAX_REQUEST_BYTES:
                raise summarizer.SummaryInputError("summary request exceeds wire budget")
            value = run(payload)
        sys.stdout.buffer.write((summarizer.canonical(value) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0
    except Exception as exc:
        category = _category(exc)
    from mindie_knowledge.loop.process import AGENT_ERROR_EXIT_CODES
    print("summary protocol failed: " + category, file=sys.stderr)
    return AGENT_ERROR_EXIT_CODES.get(category, 2)


if __name__ == "__main__":
    raise SystemExit(main())
