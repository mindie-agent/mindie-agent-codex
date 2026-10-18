"""Session-start and explicit feed refresh; shared Feed owns intervals/backoff."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

_FEED_OK = frozenset({"synced", "unchanged"})
_FEED_PENDING = frozenset({"busy", "deferred"})
_FEED_ERROR = frozenset({"unavailable", "invalid", "exhausted"})
_FEED_PARTIAL = frozenset({"partial"})
_FEED_KNOWN = _FEED_OK | _FEED_PENDING | _FEED_ERROR | _FEED_PARTIAL


def fold_feed_results(output):
    """Fold core `sync` stdout, which MUST be one JSON list (no prefix/suffix).

    Empty list is ok. busy/deferred means not completed now, not a failed
    attempt: all-pending folds to deferred. Mix of completed (synced/unchanged)
    with pending or failed folds to degraded. No completed rows plus at least
    one unavailable/invalid/exhausted folds to sync_failed. A partial commit
    or completed sync with failed cleanup or receipt recording folds to degraded;
    its original completion and secondary failures remain separate in the row.
    Unknown status or malformed stdout raises.
    """
    text = (output or "").strip()
    if not text:
        raise ValueError("knowledge sync returned empty output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("knowledge sync returned non-JSON") from exc
    if not isinstance(payload, list):
        raise ValueError("knowledge sync result is not a list")
    rows = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("knowledge sync row is not an object")
        status = item.get("status")
        repo = item.get("repository")
        if not isinstance(status, str) or not status:
            raise ValueError("knowledge sync row missing status")
        if not isinstance(repo, str) or not repo:
            raise ValueError("knowledge sync row missing repository")
        if status not in _FEED_KNOWN:
            raise ValueError("knowledge sync unknown status: " + status)
        rows.append(item)
    good = sum(1 for row in rows if row["status"] in _FEED_OK)
    pending = sum(1 for row in rows if row["status"] in _FEED_PENDING)
    failed = sum(1 for row in rows if row["status"] in _FEED_ERROR
                 or row.get("cleanup_status") == "failed"
                 or row.get("receipt_status") == "failed")
    partial = any(row["status"] in _FEED_PARTIAL for row in rows)
    if not rows or (good == len(rows) and not failed):
        aggregate = "ok"
        summary = None
    elif partial or (good and (pending or failed)):
        aggregate = "degraded"
        summary = _feed_error_summary(rows)
    elif failed:
        aggregate = "sync_failed"
        summary = _feed_error_summary(rows)
    elif pending:
        aggregate = "deferred"
        summary = _feed_error_summary(rows)
    else:
        aggregate = "sync_failed"
        summary = _feed_error_summary(rows)
    return aggregate, rows, summary


def _feed_error_summary(rows):
    parts = []
    for row in rows:
        cleanup_failed = row.get("cleanup_status") == "failed"
        receipt_failed = row.get("receipt_status") == "failed"
        if row["status"] in _FEED_OK and not cleanup_failed and not receipt_failed:
            continue
        details = []
        if row.get("metadata_committed") is True:
            details.append("metadata committed")
        if row.get("failed_stage"):
            details.append("stage=" + str(row["failed_stage"])[:80])
        if cleanup_failed:
            details.append("cleanup failed: " + str(row.get("cleanup_error") or "unknown cause")[:80])
        if receipt_failed:
            details.append("receipt failed: " + str(row.get("receipt_error") or "unknown cause")[:80])
        detail = row.get("detail") or row.get("cause")
        if detail:
            details.append(str(detail)[:80])
        parts.append(f"{row['repository']}:{row['status']}:" + ", ".join(details))
    return "; ".join(parts)[:240]



def sync(config, *, timeout: float | None = 120.0):
    import sys
    from bounded_process import run
    result = run([sys.executable, '-m', 'mindie_knowledge.loop.cli', 'sync',
                  '--config', str(config)], '', timeout=timeout)
    status, rows, detail = fold_feed_results(result.stdout)
    if result.cleanup:
        return dict(status='degraded', feeds=rows, detail=detail,
                    cleanup=result.cleanup, execution=result.execution)
    return dict(status=status, feeds=rows, detail=detail)


if __name__ == "__main__":
    import sys
    from product_contract import read_json
    value = sync(read_json(sys.argv[1])[0]["engine_config"])
    print(json.dumps(value))
    raise SystemExit(0 if value["status"] == "ok" else 1)
