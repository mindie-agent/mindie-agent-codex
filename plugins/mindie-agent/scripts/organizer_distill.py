#!/usr/bin/env python3
"""Select the explicit C6 distillation branch for the native organizer worker.

[Role]
Consumes an already prepared `mindie-distill-request/1` (including its worker
receipt fields) and returns one C6 `mindie-distill-decision/1` through the
existing `mindie-summary-outcome/1` envelope.

[Invariants]
- Only an exact new-schema request enters this branch; ordinary summary requests
  and ordinary Stop never do.
- Stop is not completion: shared `value_gate` blocks in-flight/failed evidence
  with the shared noop decision before any paid model call.
- Prompt, schema, noop and decision admission are the installed shared runtime's
  functions; this module has no second curation or task state machine.
- Only `sanitized_inputs` supplied in the request are consumed. No ledger or
  filesystem material discovery, no unit installation and no Git/GitHub write.

[Timeouts & Locks]
- One bounded native call through `NativeSummaryModel`; no retry or replay.

[Side-Effect Policy]
- No direct I/O. The caller's model invocation owns its private scratch profile.
"""
from __future__ import annotations

import json

from mindie_knowledge.materials import summarizer

REQUEST_SCHEMA = "mindie-distill-request/1"
_MODEL_STATES = {"verified_milestone", "completed"}


def _shared(distill, *names):
    values = [getattr(distill, name, None) for name in names]
    if not all(callable(value) for value in values):
        raise ImportError("installed runtime does not provide the C6 organizer API")
    return values


def decide(request, model):
    """Return a validated DistillationDecision or the shared noop decision."""
    if not isinstance(request, dict) or request.get("schema_version") != REQUEST_SCHEMA:
        raise summarizer.SummaryInputError("unsupported distillation request schema")
    from mindie_knowledge.graded import distill
    value_gate, noop_decision, parse_validate_result = _shared(
        distill, "value_gate", "noop_decision", "parse_validate_result")
    gate = value_gate(request)
    if gate is None and request.get("task_state") not in _MODEL_STATES:
        gate = "in_flight_incomplete"
    if gate is not None:
        return noop_decision(request, gate)

    raw = model.invoke_distill(request)
    try:
        return parse_validate_result(raw, request)
    except (ValueError, KeyError, TypeError):
        try:
            json.loads(raw)
        except (ValueError, TypeError):
            reason = "result_json_invalid"
        else:
            reason = "result_contract_invalid"
        raise summarizer.SummaryResultError(
            "returned C6 decision violates the shared contract", reason=reason) from None
