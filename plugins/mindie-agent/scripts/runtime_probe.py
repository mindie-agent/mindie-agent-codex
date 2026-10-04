"""Candidate-private runtime checks; the installed updater never imports this API."""

import os


PROBE_MODULES = (
    "mindie_knowledge.loop.cli",
    "mindie_knowledge.loop.documents",
    "mindie_knowledge.loop.activation",
    "mindie_knowledge.materials.reme_index",
    "langmem.short_term",
    "remote_dev.mcp.server",
)


def build_probe_script(transcript_adapter):
    """Return a side-effect-free probe for the configured interpreter.

    Keep setup and update acceptance on the current complete-material queue,
    summary ledger and bounded native worker contract.
    """
    adapter = os.fspath(transcript_adapter)
    return f'''import importlib.metadata, importlib.util, inspect, math
missing = []
missing_packages = [name for name in ("mindie_knowledge", "remote_dev")
                    if importlib.util.find_spec(name) is None]
if missing_packages:
    missing.append("missing packages: " + ", ".join(missing_packages))
else:
    try:
        from mindie_knowledge.loop.cli import STARTUP_TIMEOUT, MAX_STARTUP_PROBES, load_transcript_adapter
        from mindie_knowledge.loop.activation import Admission
        from mindie_knowledge.materials.summarizer import SummaryLedger, SUMMARY_TIMEOUT, MAX_PROMPT_BYTES, MAX_RESPONSE_BYTES, LANGMEM_VERSION
        from mindie_knowledge.materials.reme_index import ReMeIndex
        from langmem.short_term import summarize_messages
        from mindie_knowledge.loop.transcript_capture import SUMMARY_SECONDS
        from mindie_knowledge.loop.process import spawn_service
        from mindie_knowledge.loop.engine import Engine
        from mindie_knowledge.loop.store import Store
        from mindie_knowledge.publication_contract import read_git_contract, parse_contract
        from mindie_knowledge.loop.transcript_redaction import install_scanner
        from mindie_knowledge.loop.history_import import import_transcript
        from mindie_knowledge.loop.transport import Service
        from mindie_knowledge.loop import documents, locks
        from mindie_knowledge.community import submit_batch, reconcile_batch
        from remote_dev.mcp.tools import call_tool
    except Exception as exc:
        missing.append(f"pinned runtime import ({{type(exc).__name__}}: {{exc}})")
    else:
        if set(inspect.signature(Store.explain).parameters) != {{"self", "ref"}}:
            missing.append("knowledge explain is not the ref-only block API")
        if "continuation" not in inspect.signature(Store.query).parameters:
            missing.append("knowledge query lacks related-result continuation")
        if not callable(read_git_contract) or not callable(parse_contract):
            missing.append("publication contract API is incomplete")
        if not callable(load_transcript_adapter):
            missing.append("load_transcript_adapter is unavailable")
        if not callable(import_transcript):
            missing.append("explicit history import is unavailable")
        positive_bounds = (("SUMMARY_TIMEOUT", SUMMARY_TIMEOUT), ("SUMMARY_SECONDS", SUMMARY_SECONDS),
                           ("MAX_PROMPT_BYTES", MAX_PROMPT_BYTES), ("MAX_RESPONSE_BYTES", MAX_RESPONSE_BYTES))
        for name, value in positive_bounds:
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                missing.append(name + " must be finite and positive")
        valid_bounds = all(type(value) in (int, float) and math.isfinite(value) and value > 0
                           for _, value in positive_bounds)
        if valid_bounds and not SUMMARY_TIMEOUT < SUMMARY_SECONDS:
            missing.append("summary invocation lifetime bounds are inconsistent")
        if not callable(ReMeIndex) or not callable(summarize_messages):
            missing.append("material indexing dependencies are unavailable")
        if importlib.metadata.version("langmem") != LANGMEM_VERSION:
            missing.append("LangMem version differs from the reviewed runtime")
        if not all(callable(getattr(SummaryLedger, name, None)) for name in ("prepare", "claim", "record", "recover", "complete", "retry", "usage_totals")):
            missing.append("summary outcome ledger API is incomplete")
        if valid_bounds and not MAX_RESPONSE_BYTES <= MAX_PROMPT_BYTES:
            missing.append("summary protocol byte bounds are inconsistent")
        if not callable(getattr(locks, "lock_held", None)):
            missing.append("core locks lacks lock_held")
        if not callable(call_tool):
            missing.append("remote_dev call_tool is unavailable")
        if not callable(getattr(Engine, "stop_if_idle", None)):
            missing.append("Engine.stop_if_idle is unavailable")
        if "capture_mode" not in inspect.signature(Engine).parameters:
            missing.append("core lacks deterministic public transcript capture")
        if not callable(getattr(Service, "_stop_if_idle", None)):
            missing.append("Service._stop_if_idle is unavailable")
        if not all(callable(getattr(documents, name, None)) for name in ("render_entry", "parse_entry", "revision_of")):
            missing.append("knowledge document API is incomplete")
        if not callable(submit_batch) or not callable(reconcile_batch):
            missing.append("community receipt API is incomplete")
        if "path" not in inspect.signature(Admission.__init__).parameters:
            missing.append("Admission does not take an explicit admission path")
        methods = ("activate", "inspect", "check", "resolve", "claim", "finish", "deactivate", "capture_lease", "active_lease", "scope_root", "allows_hash", "leases")
        if not all(callable(getattr(Admission, name, None)) for name in methods):
            missing.append("Admission API is incomplete")
        if "admission" not in inspect.signature(Service).parameters:
            missing.append("Service does not accept admission")
        for name, value in (("STARTUP_TIMEOUT", STARTUP_TIMEOUT), ("MAX_STARTUP_PROBES", MAX_STARTUP_PROBES)):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                missing.append(name + " must be finite and positive")
        if type(MAX_STARTUP_PROBES) is not int:
            missing.append("MAX_STARTUP_PROBES must be an integer")
        try:
            module = load_transcript_adapter({{"transcript_adapter": {adapter!r}}})
            if module is None or not all(hasattr(module, name) for name in ("FileIdentity", "identify", "read_material", "history_source")):
                missing.append("transcript adapter API is incomplete")
        except Exception as exc:
            missing.append(f"transcript adapter ({{type(exc).__name__}}: {{exc}})")

print("MISSING: " + "; ".join(missing) if missing else "OK", flush=True)
'''
