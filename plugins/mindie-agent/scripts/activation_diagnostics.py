"""Carry safe activation failure evidence across the owned child boundary.

[Role]
Emits and consumes the activation error envelope; shared diagnostics owns all
stack, exception-chain and public-field projection.

[Invariants]
- Never copies messages, paths, raw stderr, commands or configuration into evidence.
- Legacy catalog envelopes keep their classification; arbitrary child exceptions
  stay internal unless an explicit supported exception type proves otherwise.
- Evidence failure cannot replace the original activation result.

[Timeouts & Locks]
- At most 64 KiB of already-captured stdout is decoded. No locks or subprocesses.

[Side-Effect Policy]
- Child errors write one structured stdout envelope and one local diagnostic.
  Incomplete activations use the same consent-aware recorder; parents reuse the
  reference, including when an older parent cannot decode the richer envelope.
"""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA = "mindie.activation.failure/1"
COMPONENT = "mindie-agent-codex"
_STAGES = frozenset({"installation_lock", "binding_read", "new_owner_ready",
                     "old_owner_retirement", "new_owner_readiness", "domain_retirement", "binding_publish"})
_CLASSES = {cls.__name__: cls for cls in (
    OSError, FileNotFoundError, PermissionError, TimeoutError, ConnectionError,
    ImportError, ModuleNotFoundError)}


def _typed(name, details):
    cls = _CLASSES.get(name) if isinstance(name, str) else None
    if cls is None:
        return None
    number = details.get("errno") if isinstance(details, dict) else None
    if issubclass(cls, OSError) and type(number) is int:
        return cls(number, "activation helper failed")
    return cls("activation helper failed")


def evidence(exception):
    from mindie_diagnostics.integration import exception_evidence
    return exception_evidence(COMPONENT, exception, source_root=Path(__file__).parent)


def emit_error(exception):
    """Leave the traceback on local stderr; only safe facts cross into issues."""
    from component_update import CatalogError, typed_catalog_error
    typed = typed_catalog_error(exception)
    catalog = isinstance(typed, CatalogError)
    error = {"type": type(typed).__name__}
    if catalog:
        # Existing typed catalog projection already admits only static codes.
        from component_update import _CODE
        code = getattr(typed, "code", None)
        if isinstance(code, str) and _CODE.fullmatch(code):
            error["code"] = code
        status = getattr(typed, "status", None)
        if type(status) is int and 100 <= status <= 599:
            error["status"] = status
    from diagnostic_support import build_metadata, failure
    diagnostic = failure("release.update", "runtime_activation", "update", exception=typed, exit_code=1)
    print(json.dumps(dict(schema=SCHEMA, kind="catalog" if catalog else "exception",
                          error=error, build=build_metadata(), evidence=evidence(exception),
                          diagnostic=diagnostic), sort_keys=True))


def child_error(exception):
    """Preserve process facts while decoding only an explicit owned envelope."""
    from component_update import _copy_process, typed_catalog_error
    result = getattr(exception, "process_result", None)
    raw = getattr(result, "stdout", None)
    if not isinstance(raw, str) or len(raw) > 65536:
        return typed_catalog_error(exception)
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return typed_catalog_error(exception)
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        return typed_catalog_error(exception)
    error = value.get("error")
    if not isinstance(error, dict):
        return exception
    if not isinstance(error.get("type"), str):
        return exception
    kind = value.get("kind")
    if kind == "catalog" and error["type"] == "CatalogError":
        typed = typed_catalog_error(exception)
    elif kind == "exception":
        recovered = _typed(error.get("type"), value.get("evidence"))
        typed = _copy_process(exception, recovered) if recovered is not None else exception
    else:
        return exception
    # record_failure revalidates the transport projection before persistence.
    from mindie_diagnostics.evidence import validate_evidence
    typed.mindie_failure_evidence = validate_evidence(COMPONENT, value.get("evidence"))
    from diagnostic_support import _project_build, reference
    inner = reference(value)
    if inner is not None:
        typed.mindie_diagnostic = inner
    build = value.get("build")
    typed.mindie_failure_build = _project_build(build) if isinstance(build, dict) else {}
    return typed


def incomplete(value):
    """A zero-exit incomplete activation is still a failure, never an update success."""
    if value.get("status") != "incomplete":
        return value
    from diagnostic_support import failure, reference
    if reference(value) is not None:
        return value
    from mindie_diagnostics.evidence import validate_evidence
    details = validate_evidence(COMPONENT, value.get("diagnostic_evidence"))
    chain = details.get("exception_chain", [])
    error = _typed(chain[0] if chain else None, details)
    if error is None:
        error = RuntimeError("activation did not complete")
    error.mindie_failure_evidence = details
    from diagnostic_support import _project_build
    build = value.get("diagnostic_build")
    error.mindie_failure_build = _project_build(build) if isinstance(build, dict) else {}
    value = dict(value)
    value.pop("diagnostic_evidence", None)
    value.pop("diagnostic_build", None)
    stage = value.get("stage")
    stage = "runtime_activation." + stage if isinstance(stage, str) and stage in _STAGES else "runtime_activation"
    value["diagnostic"] = failure("release.update", stage, "update", exception=error, exit_code=0)
    return value
