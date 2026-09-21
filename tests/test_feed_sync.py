"""Focused knowledge-sync result folding for the Codex updater."""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

from auto_update import Updater, atomic, fold_feed_results  # noqa: E402


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

    def test_malformed(self):
        for raw in ("", "not-json", "{}", json.dumps(["x"]),
                    json.dumps([{"status": "synced"}]), "junk\n[]"):
            with self.assertRaises(ValueError):
                fold_feed_results(raw)


class CheckKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "adapter.json"
        self.engine = self.root / "engine.json"
        atomic(self.engine, {"root": str(self.root / "data")})
        atomic(
            self.config,
            dict(python=sys.executable, engine_config=str(self.engine)),
        )
        atomic(
            self.root / "updater.json",
            dict(
                root=str(self.root),
                adapter_config=str(self.config),
                repository="unused",
                channel="main",
                python=sys.executable,
                codex="codex",
                uv="uv",
                codex_home=str(self.root),
            ),
        )
        self.updater = Updater(self.root / "updater.json")
        self.updater.save("up_to_date", current={"revision": "abc"})

    def tearDown(self):
        self.temp.cleanup()

    def test_records_results_and_clears_error(self):
        self.updater.save(
            "up_to_date",
            knowledge_error="old",
            knowledge_status="sync_failed",
        )
        payload = json.dumps([_row("a", "synced", commit="c1")])
        with patch.object(self.updater, "command", return_value=payload):
            with patch("auto_update.update_lock"):
                self.updater.check_knowledge()
        state = self.updater.state
        self.assertEqual(state["knowledge_status"], "ok")
        self.assertIsNone(state["knowledge_error"])
        self.assertEqual(state["knowledge_results"][0]["repository"], "a")
        self.assertEqual(state["status"], "up_to_date")

    def test_malformed_clears_stale_results_and_keeps_plugin_state(self):
        self.updater.save(
            "up_to_date",
            knowledge_results=[_row("stale", "synced")],
            knowledge_feeds=1,
            knowledge_status="ok",
        )
        with patch.object(self.updater, "command", return_value=""):
            with patch("auto_update.update_lock"):
                with patch.object(self.updater, "_check_plugin",
                                  return_value=self.updater.state):
                    result = self.updater._check_locked()
        self.assertEqual(result["status"], "up_to_date")
        self.assertEqual(result["knowledge_status"], "sync_failed")
        self.assertIsNone(result.get("knowledge_results"))
        self.assertIsNone(result.get("knowledge_feeds"))
        self.assertTrue(result["knowledge_error"])

    def test_plugin_still_runs_after_knowledge_failure(self):
        plugin = {"status": "installed"}
        with patch.object(self.updater, "command", side_effect=RuntimeError("boom")):
            with patch("auto_update.update_lock"):
                with patch.object(
                    self.updater, "_check_plugin", return_value=plugin
                ) as check_plugin:
                    result = self.updater._check_locked()
        check_plugin.assert_called_once()
        self.assertEqual(result["status"], "installed")
        self.assertEqual(self.updater.state["knowledge_status"], "sync_failed")


if __name__ == "__main__":
    unittest.main()
