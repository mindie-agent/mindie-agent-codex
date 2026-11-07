"""Safe evidence crosses a child process; arbitrary output never becomes evidence."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import activation_diagnostics as diagnostics
from bounded_process import ProcessResult


def wrapped(value):
    error = RuntimeError("PRIVATE wrapper stderr")
    error.process_result = ProcessResult("completed", json.dumps(value), 1, stderr="PRIVATE stderr")
    return error


def test_unknown_activation_exception_is_not_misclassified_as_catalog_connectivity():
    evidence = {"exception_chain": ["KeyError"], "message": "PRIVATE secret"}
    original = wrapped(dict(schema=diagnostics.SCHEMA, kind="exception",
                            error={"type": "KeyError"}, evidence=evidence,
                            build={"version": "0.1.999", "revision": "a" * 40, "path": "PRIVATE"}))
    error = diagnostics.child_error(original)
    assert error is original
    from diagnostic_support import failure
    assert failure("release.update", "runtime_activation", "update", exception=error)["recorded"]
    import os
    root = Path(os.environ["MINDIE_DIAGNOSTICS_ROOT"])
    event = json.loads(next(root.glob("events/*/*.jsonl")).read_text().splitlines()[-1])
    assert event["package_version"] == "0.1.999"
    assert event["package_revision"] == "a" * 40
    assert event["attributes"]["category"] == "internal"
    assert event["attributes"]["exception_chain"] == ["KeyError"]
    assert "PRIVATE" not in json.dumps(event)


def test_malformed_envelope_keeps_the_original_process_failure():
    original = wrapped(dict(schema=diagnostics.SCHEMA, kind="exception", error={"type": ["KeyError"]}))
    assert diagnostics.child_error(original) is original
    assert not hasattr(original, "mindie_failure_evidence")


def test_catalog_classification_and_process_cleanup_are_preserved():
    from component_update import CatalogError
    original = wrapped(dict(schema=diagnostics.SCHEMA, kind="catalog", error={
        "type": "CatalogError", "code": "release_http_error", "status": 429}, evidence={}))
    error = diagnostics.child_error(original)
    assert isinstance(error, CatalogError)
    assert error.code == "release_http_error"
    assert error.category == "rate_limited"
    assert error.process_result is original.process_result


def test_incomplete_result_records_without_claiming_activation_or_replaying():
    value = {"status": "incomplete", "stage": "binding_publish", "binding_published": True,
             "diagnostic_evidence": {"exception_chain": ["OSError"], "errno": 28}}
    result = diagnostics.incomplete(value)
    assert result["status"] == "incomplete" and result["binding_published"]
    assert result["diagnostic"]["recorded"]
    assert "diagnostic_evidence" not in result
    import os
    root = Path(os.environ["MINDIE_DIAGNOSTICS_ROOT"])
    event = json.loads(next(root.glob("events/*/*.jsonl")).read_text().splitlines()[-1])
    assert event["attributes"]["stage"] == "runtime_activation.binding_publish"
    assert event["attributes"]["errno"] == 28
    assert event["attributes"]["category"] == "prerequisite"
    assert not event["reportable"]
    with patch("diagnostic_support.failure") as record:
        active = {"status": "active"}
        assert diagnostics.incomplete(active) is active
    record.assert_not_called()
