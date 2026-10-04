"""Foreground configuration failures cannot be turned into success receipts."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'
sys.path.insert(0, str(SCRIPTS))
import bridge
from session_gate import Sessions, Inactive


class FailureVisibilityTests(unittest.TestCase):
    def test_stop_result_failures_remain_visible_when_diagnostics_fail(self):
        with patch('diagnostic_support.failure', side_effect=OSError('fixture logging unavailable')):
            for result in (None, {}, {'stage': 'failed'}, {'stage': 'unsupported'}):
                with self.subTest(result=result):
                    self.assertFalse(bridge._observe_stop(result))
            self.assertTrue(bridge._observe_stop({'stage': 'inert', 'reason': 'not-activated'}))
            self.assertTrue(bridge._observe_stop({'stage': 'no-new-material'}))

    def test_empty_helper_output_is_not_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'adapter.json'
            path.write_text(json.dumps({'python': sys.executable, 'runtime_scripts': str(SCRIPTS),
                                        'engine_config': str(Path(directory) / 'engine.json')}))
            for output in ('', '[]', 'null'):
                with self.subTest(output=output), patch.object(bridge, 'config_path', return_value=path), patch.object(bridge, 'run', return_value=output):
                    with self.assertRaises(ValueError):
                        bridge.configure([])

    def test_corrupt_config_does_not_select_alternative_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'adapter.json'
            path.write_text('{broken')
            with self.assertRaises(ValueError):
                _ = Sessions(path).path
            self.assertEqual(path.read_text(), '{broken')
            self.assertFalse(path.with_suffix('.admission.sqlite3').exists())

    def test_refresh_reports_admission_fault(self):
        with patch.dict('os.environ', {'CODEX_THREAD_ID': 'native-task'}), patch.object(Sessions, 'check', side_effect=Inactive('MindIE admission is unavailable: OSError')):
            result = bridge._refresh_capture({'status': 'configured'})
        self.assertEqual(result['status'], 'degraded')
        self.assertEqual(result['activation']['status'], 'unavailable')

    def test_config_command_returns_failure_for_incomplete_activation(self):
        with patch.object(sys, 'argv', ['bridge.py', 'config']), patch.object(bridge, 'configure', return_value={'status': 'degraded'}):
            with self.assertRaises(SystemExit) as error:
                bridge.main()
        self.assertEqual(error.exception.code, 1)

    def test_updater_returns_nonzero_for_independent_maintenance_failure(self):
        import auto_update
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / 'updater.json'
            settings.write_text(json.dumps({'root': str(root / 'updates'),
                                             'adapter_config': str(root / 'adapter.json')}),
                                encoding='utf-8')
            for result in ({'status': 'up_to_date', 'knowledge_status': 'sync_failed'},
                           {'status': 'up_to_date', 'knowledge_status': 'degraded'},
                           {'status': 'up_to_date', 'diagnostics': {'status': 'unavailable'}},
                           {'status': 'action_required'}):
                with self.subTest(result=result), patch.object(sys, 'argv', ['auto_update.py', '--settings', str(settings), 'check']), patch.object(auto_update.Updater, 'check', return_value=result):
                    with self.assertRaises(SystemExit) as error:
                        auto_update.main()
                    self.assertEqual(error.exception.code, 1)

    def test_corrupt_adapter_cannot_become_fresh_consent(self):
        import consent
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'adapter.json'
            path.write_text('{broken')
            with self.assertRaises(consent.ConsentError) as error:
                consent.install_traces(path)
            self.assertEqual(error.exception.state, 'corrupt')
            self.assertEqual(path.read_text(), '{broken')

    def test_native_model_pipe_failure_is_not_success(self):
        import process_guard
        original = process_guard.subprocess.Popen
        class BrokenReader:
            def __init__(self, stream):
                self.stream = stream
            def readline(self, count):
                raise OSError('private output read error')
            def close(self):
                self.stream.close()
        def spawn(*args, **kwargs):
            process = original(*args, **kwargs)
            process.stdout = BrokenReader(process.stdout)
            return process
        with patch.object(process_guard.subprocess, 'Popen', spawn):
            with self.assertRaises(process_guard.InvalidResultError):
                process_guard.run_codex([sys.executable, '-c', "print('{}')"], '', timeout=3)

    def test_accounting_failure_preserves_completed_mutation_and_original_failure(self):
        import runtime_call
        payload = dict(surface='knowledge', name='knowledge_feedback', arguments={},
                       mindie_activation='fixture-token', mindie_session_id='fixture-task')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'adapter.json'
            path.write_text('{"engine_config":"synthetic"}')
            with patch.object(runtime_call, 'config_path', return_value=path), \
                 patch.object(runtime_call, 'resolve_lease', return_value={'session': 'fixture-task'}), \
                 patch.object(runtime_call, 'finish_outcome', side_effect=OSError('fixture accounting failure')), \
                 patch('mindie_knowledge.loop.cli.ensure_service', return_value={}), \
                 patch('mindie_knowledge.loop.transport.rpc', return_value={'status': 'recorded'}) as rpc:
                result = runtime_call.call(payload)
                self.assertFalse(result['isError'])
                self.assertEqual(result['structuredContent'], {'status': 'recorded'})
                self.assertEqual(result['accounting']['status'], 'failed')
                self.assertFalse(result['accounting']['automatic_retry'])
                rpc.assert_called_once()
                primary = ValueError('fixture protocol failure')
                rpc.side_effect = primary
                with self.assertRaises(ValueError) as caught:
                    runtime_call.call(payload)
                self.assertIs(caught.exception, primary)
                self.assertIn('accounting also failed: OSError', primary.__notes__[0])
                self.assertEqual(rpc.call_count, 2)
