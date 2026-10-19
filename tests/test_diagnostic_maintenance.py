"""Startup activates from saved consent; retention remains independently visible."""
from contextlib import redirect_stdout
import io
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'))
from bounded_process import ProcessResult
import diagnostic_maintenance


class DiagnosticMaintenanceTests(unittest.TestCase):
    def test_session_receipt_does_not_claim_ready_when_reporter_is_degraded(self):
        import session_maintenance
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "codex.json"
            config.write_text(json.dumps({"engine_config": str(config.with_suffix(".engine.json"))}))
            output = io.StringIO()
            with patch.object(session_maintenance, "bind_explicit_config"), \
                    patch.object(diagnostic_maintenance, "maintain",
                                 return_value={"status": "degraded", "reporting": {"status": "degraded"}}), \
                    patch("service_client.connect"), patch("feed_sync.sync", return_value={"status": "ok"}), \
                    redirect_stdout(output), self.assertRaisesRegex(RuntimeError, "incomplete"):
                session_maintenance.maintain(config)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["diagnostics"]["reporting"]["status"], "degraded")

    def test_knowledge_startup_failure_does_not_skip_authorized_reporter(self):
        import session_maintenance
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "codex.json"
            config.write_text(json.dumps({"engine_config": str(config.with_suffix(".engine.json"))}))
            stages = []

            def connect(_engine):
                stages.append("knowledge")
                raise RuntimeError("knowledge startup failed")

            with patch.dict(os.environ, MINDIE_AGENT_CONFIG=str(config)), \
                    patch.object(session_maintenance, "bind_explicit_config"), \
                    patch.object(diagnostic_maintenance, "maintain",
                                 side_effect=lambda _config: stages.append("reporter") or {"status": "ok"}), \
                    patch("service_client.connect", side_effect=connect), \
                    self.assertRaisesRegex(RuntimeError, "knowledge startup failed"):
                session_maintenance.maintain(config)
            self.assertEqual(stages, ["reporter", "knowledge"])

    def test_startup_preserves_worker_failure_while_completing_local_retention(self):
        for status in ('running', 'disabled', 'needs_choice', 'degraded'):
            results = [
                ProcessResult('completed', returncode=0, stdout=json.dumps({'status': status})),
                ProcessResult('completed', returncode=0, stdout='{"status":"ok","network":false}'),
            ]
            with self.subTest(status=status), \
                    patch.object(diagnostic_maintenance, 'runtime_env', return_value={
                        'MINDIE_DIAGNOSTICS_CONFIG': '/profile/diagnostics.json'}), \
                    patch.object(diagnostic_maintenance, 'run', side_effect=results) as command:
                result = diagnostic_maintenance.maintain(Path('/profile/adapter.json'))
                self.assertEqual(result['status'], 'degraded' if status == 'degraded' else 'ok')
                self.assertEqual(result['reporting']['status'], status)
                self.assertFalse(result['network'])
                activation, retention = command.call_args_list
                self.assertEqual(activation.kwargs['timeout'], 90.0)
                self.assertEqual(retention.kwargs['timeout'], 5.0)
                self.assertIn('activate', activation.args[0])
                self.assertEqual(retention.args[0][-2:], ['--config', '/profile/diagnostics.json'])
                self.assertNotIn('--update-running', retention.args[0])
