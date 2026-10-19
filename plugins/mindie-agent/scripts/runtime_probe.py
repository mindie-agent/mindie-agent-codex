"""Candidate-owned runtime checks invoked by the fixed runtime preparation.

[Role]
Builds a side-effect-free probe script to validate that `mindie_knowledge`,
`remote_dev`, and transcript adapter exports are importable in a candidate
Python interpreter.

[Invariants]
- Verifies `PROBE_MODULES` and transcript adapter callables (`FileIdentity`,
  `identify`, `read_material`).

[Timeouts & Locks]
- Pure string builder; execution timeout is enforced by the caller via
  `bounded_process.run`.

[Side-Effect Policy]
- Probe script performs imports and attribute checks only; writes no state.
"""

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
    """Return a side-effect-free probe for the configured interpreter."""
    adapter = os.fspath(transcript_adapter)
    return f'''import importlib.util, math
missing = []
missing_packages = [name for name in ("mindie_knowledge", "remote_dev")
                    if importlib.util.find_spec(name) is None]
if missing_packages:
    missing.append("missing packages: " + ", ".join(missing_packages))
else:
    try:
        from mindie_knowledge.loop.cli import ensure_service, prepare_service, load_transcript_adapter
        from mindie_knowledge.loop.activation import Admission
        from mindie_knowledge.loop.lifecycle import retire_service, restore_service, inspect_retirement
        from mindie_knowledge.materials.summarizer import SummaryLedger, MAX_PROMPT_BYTES, MAX_RESPONSE_BYTES
        from mindie_knowledge.materials.reme_index import ReMeIndex
        from langmem.short_term import summarize_messages
        from mindie_knowledge.loop.process import bounded_run, spawn_service
        from mindie_knowledge.loop.engine import Engine
        from mindie_knowledge.loop.store import Store
        from mindie_knowledge.state_layout import state_root
        from mindie_knowledge.filesystem import resolved_path, path_identity
        from mindie_knowledge.loop.service_context import validate as validate_service_context
        from mindie_knowledge.loop.summary_scratch import create_profile, ScratchOwnershipError
        from mindie_knowledge.git_layout import validate_config_feed_layout
        from mindie_knowledge.publication_contract import read_git_contract, parse_contract
        from mindie_knowledge.loop.transcript_redaction import install_scanner
        from mindie_knowledge.loop.history_import import import_transcript
        from mindie_knowledge.loop.transport import Service
        from mindie_knowledge.loop import documents, locks
        from mindie_knowledge.releases import select_release, validate_compatibility
        from mindie_knowledge.releases_io import fetch_releases, verify_release_source
        from mindie_knowledge.community import submit_batch, reconcile_batch
        from remote_dev.mcp.tools import call_tool
    except Exception as exc:
        missing.append(f"pinned runtime import ({{type(exc).__name__}}: {{exc}})")
    else:
        for name, value in (("MAX_PROMPT_BYTES", MAX_PROMPT_BYTES), ("MAX_RESPONSE_BYTES", MAX_RESPONSE_BYTES)):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                missing.append(name + " must be finite and positive")
        if (all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in (MAX_PROMPT_BYTES, MAX_RESPONSE_BYTES))
                and not MAX_RESPONSE_BYTES <= MAX_PROMPT_BYTES):
            missing.append("summary protocol byte bounds are inconsistent")
        required_callables = (
            ensure_service, prepare_service, load_transcript_adapter, import_transcript,
            state_root, resolved_path, path_identity, validate_service_context,
            create_profile, validate_config_feed_layout, read_git_contract, parse_contract,
            ReMeIndex, summarize_messages, bounded_run, spawn_service, Engine, Store, Service,
            retire_service, restore_service, inspect_retirement, select_release,
            validate_compatibility, fetch_releases, verify_release_source, submit_batch,
            reconcile_batch, call_tool, getattr(locks, "lock_held", None),
            getattr(Admission, "inspect", None), getattr(SummaryLedger, "record", None),
        )
        if not all(callable(fn) for fn in required_callables):
            missing.append("core runtime callable API is incomplete")
        try:
            module = load_transcript_adapter({{"transcript_adapter": {adapter!r}}})
            if module is None or not all(hasattr(module, name) for name in ("FileIdentity", "identify", "read_material", "history_source")):
                missing.append("transcript adapter API is incomplete")
        except Exception as exc:
            missing.append(f"transcript adapter ({{type(exc).__name__}}: {{exc}})")

print("MISSING: " + "; ".join(missing) if missing else "OK", flush=True)
'''
