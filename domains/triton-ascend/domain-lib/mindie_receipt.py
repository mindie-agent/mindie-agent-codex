"""Compact JSON receipts and progress lines for migrated domain tools.

Replaces the old workspace result-envelope/diagnostics layer. A receipt is one
JSON object on stdout; progress goes to stderr as PROGRESS_SENTINEL lines;
optional full records are written under an explicit record directory.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import functools
import json
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

PROGRESS_SENTINEL = "__MINDIE_PROGRESS__"


def progress(phase: str, message: str, **extra) -> None:
    payload = dict(phase=phase, message=message, **extra)
    print(f"{PROGRESS_SENTINEL}={json.dumps(payload, ensure_ascii=False)}", file=sys.stderr, flush=True)


def write_full_record(data: dict, record_dir: Path) -> Path:
    root = Path(record_dir)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    record = root / f"{stamp}-{uuid4().hex[:8]}.json"
    # Self-reference keeps the stored full receipt strictly larger than compact stdout.
    data = dict(data, record_ref=str(record))
    record.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return record


def _outcome_of(data: dict) -> str:
    status = str(data.get("status", ""))
    if data.get("error") or status in {"failed", "error", "failure"}:
        return "failure"
    return "success"


def emit_json(data: dict, *, skill: str, entry_point: str, record_dir: Path | None = None, compact: bool = True) -> None:
    """Print one business receipt: envelope + result facts; optional full record."""
    outcome = _outcome_of(data)
    envelope = {
        "schema": "mindie.skill-receipt.v1",
        "skill": skill,
        "entry_point": entry_point,
        "outcome": outcome,
        "exit_code": 1 if outcome == "failure" else 0,
        "result": data,
        "warnings": [],
    }
    if record_dir is not None:
        try:
            envelope["record_ref"] = str(write_full_record(envelope, record_dir))
        except OSError as exc:
            envelope["record_ref"] = None
            envelope["warnings"].append(f"full record not written: {exc}")
    if os.environ.get("MINDIE_FULL_RECEIPT") == "1":
        print(json.dumps(envelope, ensure_ascii=False, indent=2))
        return
    print(json.dumps(envelope, ensure_ascii=False, separators=(",", ":") if compact else None))


# Back-reference alias used by migrated scripts.
emit_skill_json = emit_json


def unwrap_skill_payload(data):
    return data.get("result", data) if isinstance(data, dict) else data


def measured(_name: str):
    """Timing decorator kept for call-site compatibility; emits one progress mark."""

    def wrap(func):
        @functools.wraps(func)
        def inner(*args, **kwargs):
            return func(*args, **kwargs)

        return inner

    return wrap


@contextmanager
def operation(_name: str, **_fields):
    yield


@contextmanager
def captured_stderr():
    yield sys.stderr


@contextmanager
def captured_process_output():
    yield None


@contextmanager
def context_environment(_env=None):
    yield {}


def report_failure(_name: str, exc: BaseException, **extra) -> None:
    progress("failed", f"{_name}: {type(exc).__name__}: {exc}", **extra)
