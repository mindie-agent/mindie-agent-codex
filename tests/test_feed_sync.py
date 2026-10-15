"""Focused knowledge-sync result folding for the Codex updater."""

import json
import sys
import tempfile
import time
import unittest
from tests.process_fixtures import cleanup_temporary_directory
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

from feed_sync import fold_feed_results  # noqa: E402


def _row(repository, status, **extra):
    item = dict(repository=repository, status=status, detail=extra.get("detail", ""))
    item.update(extra)
    return item


class FoldFeedTests(unittest.TestCase):
    def test_mixed_preserves_retained_commit(self):
        payload = [
            _row("alpha", "synced", commit="aaa"),
            _row("beta", "unavailable", retained_commit="old", detail="404"),
        ]
        aggregate, rows, summary = fold_feed_results(json.dumps(payload))
        self.assertEqual(aggregate, "degraded")
        self.assertEqual(rows[1]["retained_commit"], "old")
        self.assertIn("beta:unavailable", summary)

    def test_empty_and_all_good(self):
        self.assertEqual(fold_feed_results("[]")[0], "ok")
        payload = [_row("a", "synced"), _row("b", "unchanged")]
        self.assertEqual(fold_feed_results(json.dumps(payload))[0], "ok")

    def test_pending_is_deferred_not_failed(self):
        payload = [_row("a", "deferred"), _row("b", "busy")]
        aggregate, rows, summary = fold_feed_results(json.dumps(payload))
        self.assertEqual(aggregate, "deferred")
        self.assertEqual([row["status"] for row in rows], ["deferred", "busy"])
        self.assertIn("a:deferred", summary)
        self.assertIn("b:busy", summary)
        self.assertEqual(
            fold_feed_results(json.dumps([_row("a", "deferred")]))[0],
            "deferred",
        )
        self.assertEqual(
            fold_feed_results(json.dumps([_row("a", "busy")]))[0],
            "deferred",
        )

    def test_mix_good_and_pending_is_degraded(self):
        payload = [_row("a", "synced"), _row("b", "busy")]
        self.assertEqual(fold_feed_results(json.dumps(payload))[0], "degraded")

    def test_no_good_with_error_is_sync_failed(self):
        payload = [_row("a", "unavailable"), _row("b", "deferred")]
        aggregate, rows, _ = fold_feed_results(json.dumps(payload))
        self.assertEqual(aggregate, "sync_failed")
        self.assertEqual(len(rows), 2)
        with self.assertRaises(ValueError):
            fold_feed_results(json.dumps([_row("a", "mystery")]))

    def test_partial_keeps_committed_effect_and_failed_stage(self):
        payload = [_row("a", "partial", commit="new", metadata_committed=True,
                        failed_stage="material-promotion", detail="current-file promotion failed")]
        aggregate, rows, summary = fold_feed_results(json.dumps(payload))
        self.assertEqual(aggregate, "degraded")
        self.assertEqual(rows, payload)
        self.assertIn("metadata committed", summary)
        self.assertIn("stage=material-promotion", summary)

    def test_completed_sync_with_cleanup_failure_is_not_ok(self):
        for status in ("synced", "unchanged"):
            with self.subTest(status=status):
                payload = [_row("a", status, commit="new", cleanup_status="failed",
                                cleanup_error="staging removal denied", cleanup_errors=["staging removal denied"])]
                aggregate, rows, summary = fold_feed_results(json.dumps(payload))
                self.assertEqual(aggregate, "degraded")
                self.assertEqual(rows, payload)
                self.assertIn("a:" + status, summary)
                self.assertIn("cleanup failed: staging removal denied", summary)

    def test_primary_and_cleanup_failure_are_both_visible(self):
        payload = [_row("a", "invalid", detail="invalid package", retained_commit="old",
                        cleanup_status="failed", cleanup_error="staging removal denied")]
        aggregate, rows, summary = fold_feed_results(json.dumps(payload))
        self.assertEqual(aggregate, "sync_failed")
        self.assertEqual(rows, payload)
        self.assertIn("invalid package", summary)
        self.assertIn("cleanup failed", summary)

    def test_completed_sync_with_receipt_failure_keeps_installed_commit(self):
        for status in ("synced", "unchanged"):
            with self.subTest(status=status):
                payload = [_row("a", status, commit="new", receipt_status="failed",
                                receipt_error="OSError: receipt write denied")]
                aggregate, rows, summary = fold_feed_results(json.dumps(payload))
                self.assertEqual(aggregate, "degraded")
                self.assertEqual(rows, payload)
                self.assertIn("a:" + status, summary)
                self.assertIn("receipt failed: OSError: receipt write denied", summary)

    def test_mixed_results_keep_all_secondary_failures(self):
        payload = [
            _row("a", "synced", commit="new", cleanup_status="failed",
                 cleanup_error="staging locked", receipt_status="failed",
                 receipt_error="receipt denied"),
            _row("b", "partial", commit="next", metadata_committed=True,
                 failed_stage="material-promotion"),
            _row("c", "deferred", retained_commit="old"),
        ]
        aggregate, rows, summary = fold_feed_results(json.dumps(payload))
        self.assertEqual(aggregate, "degraded")
        self.assertEqual(rows, payload)
        self.assertIn("cleanup failed: staging locked", summary)
        self.assertIn("receipt failed: receipt denied", summary)
        self.assertIn("metadata committed", summary)

    def test_malformed(self):
        for raw in ("", "not-json", "{}", json.dumps(["x"]),
                    json.dumps([{"status": "synced"}]), "junk\n[]"):
            with self.assertRaises(ValueError):
                fold_feed_results(raw)
