"""Idle switching preserves exact service facts, without a model or native install."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import release_activation
from session_gate import runtime_binding


class ActivationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "codex.json"
        self.old = dict(python="/old/python", runtime_scripts="/old/scripts",
                        engine_config=str(self.root / "old.json"),
                        product_validation={"old": True}, consent="preserved")
        self.new = dict(self.old, python="/new/python", runtime_scripts="/new/scripts",
                        engine_config=str(self.root / "new.json"),
                        product_validation={"new": True})
        self.config.write_text(json.dumps(self.old))
        self.prepared = dict(adapter=self.new, expected_binding=runtime_binding(self.old))
        self.original = self.config.read_bytes()
        for module, name, value in (
            ("mindie_knowledge.loop.cli", "config_at", {}),
            ("mindie_knowledge.loop.lifecycle", "inspect_retirement", None),
            ("mindie_knowledge.loop.lifecycle", "retire_service", {"idle": True}),
            ("mindie_knowledge.loop.cli", "prepare_service", "starting"),
        ):
            mock = patch(module + "." + name, return_value=value).start()
            self.addCleanup(patch.stopall)
            setattr(self, name, mock)

    def run_activation(self, states):
        with patch("mindie_knowledge.loop.handoff._probe", side_effect=states):
            return release_activation.activate(self.config, self.prepared, control_seconds=.5)

    def test_idle_prepare_ready_then_publish_preserves_consent(self):
        result = self.run_activation(["absent", "ready"])
        self.assertEqual(result["status"], "active")
        self.retire_service.assert_called_once()
        self.prepare_service.assert_called_once()
        self.assertEqual(json.loads(self.config.read_text()), self.new)

    def test_busy_leaves_old_binding_and_never_starts(self):
        self.retire_service.return_value = {"idle": False}
        result = self.run_activation([])
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(self.config.read_bytes(), self.original)
        self.prepare_service.assert_not_called()

    def test_retired_cleanup_failure_remains_incomplete_without_start(self):
        self.inspect_retirement.return_value = {"status": "retired"}
        self.retire_service.return_value = {"idle": True, "cleanup_failed": True}
        result = self.run_activation(["absent"])
        self.assertEqual(result["stage"], "old_owner_retirement")
        self.assertTrue(result["retirement"]["cleanup_failed"])
        self.prepare_service.assert_not_called()
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_retired_absent_reconciles_old_cleanup_before_start(self):
        self.inspect_retirement.return_value = {"status": "retired"}
        result = self.run_activation(["absent", "absent", "ready"])
        self.assertEqual(result["status"], "active")
        self.retire_service.assert_called_once()
        self.prepare_service.assert_called_once()

    def test_ready_after_write_failure_only_finishes_publication(self):
        self.inspect_retirement.return_value = {"status": "retired"}
        with patch.object(release_activation, "_atomic", side_effect=OSError("private detail")):
            result = self.run_activation(["ready"])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["stage"], "binding_publish")
        self.assertEqual(result["service"], "ready")
        self.assertNotIn("private detail", json.dumps(result))
        self.assertEqual(self.config.read_bytes(), self.original)
        result = self.run_activation(["ready"])
        self.assertEqual(result["status"], "active")
        self.retire_service.assert_not_called()
        self.prepare_service.assert_not_called()

    def test_unknown_or_starting_candidate_is_not_started_again(self):
        self.inspect_retirement.return_value = {"status": "retired"}
        for state in ("unknown", "starting", "worker-dead", "stopping"):
            with self.subTest(state=state):
                result = self.run_activation([state])
                self.assertEqual(result["service"], state)
                self.assertEqual(result["status"], "incomplete")
        self.retire_service.assert_not_called()
        self.prepare_service.assert_not_called()
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_competing_binding_never_retires_or_overwrites(self):
        self.config.write_text(json.dumps(dict(self.old, python="/third/python")))
        before = self.config.read_bytes()
        result = self.run_activation([])
        self.assertEqual(result["reason"], "installation_changed")
        self.retire_service.assert_not_called()
        self.prepare_service.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before)

    def test_control_expiry_does_not_kill_candidate_or_publish(self):
        with patch("mindie_knowledge.loop.handoff._probe", return_value="starting"):
            self.inspect_retirement.return_value = {"status": "retired"}
            result = release_activation.activate(self.config, self.prepared, control_seconds=.001)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(self.config.read_bytes(), self.original)
        self.prepare_service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
