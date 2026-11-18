"""Foreground configuration failures cannot be turned into success receipts."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'
sys.path.insert(0, str(SCRIPTS))
from bounded_process import ProcessResult
import bridge
import mcp_gate
from session_gate import Sessions, Inactive


class FailureVisibilityTests(unittest.TestCase):
    def test_helper_failure_projection_accepts_only_static_codes(self):
        for failure in ({"stage": "transcript", "code": "private/path"},
                        {"stage": "transcript", "code": []},
                        {"stage": "private/path", "code": "transcript_outside_profile"},
                        {"stage": "transcript", "code": "transcript_outside_profile",
                         "detail": "private task content"}):
            with self.subTest(failure=failure):
                self.assertIsNone(Inactive("private detail", failure=failure).failure)
        self.assertEqual(
            Inactive("private detail", failure={"stage": "transcript",
                "code": "transcript_outside_profile"}).failure,
            {"stage": "transcript", "code": "transcript_outside_profile"},
        )

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
                with self.subTest(output=output), patch.object(bridge, 'config_path', return_value=path), patch.object(bridge, 'run', return_value=ProcessResult("completed", output, 0)):
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
        with patch.dict('os.environ', {'CODEX_THREAD_ID': 'native-task'}), patch.object(Sessions, 'active_lease', side_effect=Inactive('MindIE admission is unavailable: OSError')):
            result = bridge._refresh_capture({'status': 'configured'})
        self.assertEqual(result['status'], 'degraded')
        self.assertEqual(result['configuration_status'], 'configured')
        self.assertEqual(result['activation']['status'], 'unavailable')

    def test_config_command_returns_failure_for_incomplete_activation(self):
        with patch.object(sys, 'argv', ['bridge.py', 'config']), patch.object(bridge, 'configure', return_value={'status': 'degraded'}):
            with self.assertRaises(SystemExit) as error:
                bridge.main()
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
        from mindie_knowledge.loop.activation import Admission
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'adapter.json'
            admission = Path(directory) / 'admission.sqlite3'
            path.write_text(json.dumps(dict(engine_config='synthetic', admission_path=str(admission))))
            lease = Admission(admission).activate('fixture-task', project_root=directory)
            payload = dict(surface='knowledge', name='knowledge_feedback', arguments={},
                           mindie_activation=lease['token'], mindie_session_id='fixture-task')
            with patch.object(runtime_call, 'config_path', return_value=path), \
                 patch.object(runtime_call, 'finish_outcome', side_effect=OSError('fixture accounting failure')), \
                 patch('service_client.connect', return_value={}), \
                 patch('mindie_knowledge.loop.transport.rpc', return_value={'status': 'recorded'}) as rpc:
                result = runtime_call.call(payload)
                self.assertFalse(result['isError'])
                self.assertTrue(result['receipt_unconfirmed'])
                self.assertIn('OSError', result['receipt_warning'])
                self.assertEqual(result['structuredContent']['status'], 'recorded')
                self.assertTrue(result['structuredContent']['receipt_unconfirmed'])
                self.assertIn('OSError', result['structuredContent']['receipt_warning'])
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

    def test_cli_input_and_schema_reasons_are_closed_and_allowlisted(self):
        for reason in sorted(mcp_gate.CLI_INPUT_REASONS):
            with self.subTest(reason=reason):
                exc = mcp_gate.InvalidToolArguments(
                    'PRIVATE-EXCEPTION-TEXT', stage='cli_input', reason=reason)
                exc.request_not_started = True
                result = mcp_gate.call_failure(exc)
                structured = result['structuredContent']
                self.assertEqual(structured['code'], 'invalid_arguments')
                self.assertEqual(structured['stage'], 'cli_input')
                self.assertEqual(structured['reason'], reason)
                self.assertEqual(structured['execution'], 'not_started')
                self.assertFalse(structured['automatic_retry'])
                self.assertNotIn('PRIVATE-EXCEPTION-TEXT', json.dumps(result))

        for stage, reason in (('input_read', 'input_empty'),
                              ('cli_input', 'private_reason'),
                              ('private_stage', 'input_empty')):
            with self.subTest(stage=stage, reason=reason):
                result = mcp_gate.call_failure(mcp_gate.InvalidToolArguments(
                    'PRIVATE-EXCEPTION-TEXT', stage=stage, reason=reason))
                self.assertEqual(result['structuredContent']['code'], 'invalid_arguments')
                self.assertNotIn('stage', result['structuredContent'])
                self.assertNotIn('reason', result['structuredContent'])
                self.assertNotIn('PRIVATE-EXCEPTION-TEXT', json.dumps(result))

    def test_input_json_position_is_optional_and_typed(self):
        exc = mcp_gate.InvalidToolArguments(
            'private', stage='cli_input', reason='input_json', line=2, column=7)
        structured = mcp_gate.call_failure(exc)['structuredContent']
        self.assertEqual((structured['line'], structured['column']), (2, 7))
        for line, column in ((True, 3), (1.5, 3), ('2', 3), (None, None)):
            with self.subTest(line=line, column=column):
                exc = mcp_gate.InvalidToolArguments(
                    'private', stage='cli_input', reason='input_json',
                    line=line, column=column)
                structured = mcp_gate.call_failure(exc)['structuredContent']
                self.assertNotIn('line', structured)
                if type(column) is int:
                    self.assertEqual(structured['column'], column)
                else:
                    self.assertNotIn('column', structured)

    def test_input_unavailable_is_not_started_and_never_echoes_the_path(self):
        result = mcp_gate.call_failure(mcp_gate.InputUnavailableError(
            'cannot read /private/path SECRET-INPUT-NAME'))
        structured = result['structuredContent']
        self.assertEqual(structured['code'], 'input_unavailable')
        self.assertEqual(structured['stage'], 'input_read')
        self.assertEqual(structured['execution'], 'not_started')
        self.assertFalse(structured['automatic_retry'])
        serialized = json.dumps(result)
        self.assertNotIn('/private/path', serialized)
        self.assertNotIn('SECRET-INPUT-NAME', serialized)

    def test_schema_rejections_are_distinct_and_never_echo_caller_keys(self):
        gate = mcp_gate.Gate('knowledge')

        def failure(request):
            try:
                gate._tool_args(request)
            except mcp_gate.InvalidToolArguments as exc:
                return mcp_gate.call_failure(exc)
            self.fail('request was accepted')

        result = failure(dict(params=dict(name='knowledge_missing', arguments={})))
        self.assertEqual(result['structuredContent']['reason'], 'unknown_operation')
        self.assertEqual(result['structuredContent']['stage'], 'tool_schema')

        result = failure(dict(params=dict(name='knowledge_feedback', arguments={})))
        self.assertEqual(result['structuredContent']['reason'], 'schema_missing_field')
        self.assertEqual(result['structuredContent']['field'], 'ref')

        unknown = 'SECRET-UNKNOWN-KEY-NAME'
        result = failure(dict(params=dict(
            name='knowledge_feedback', arguments={'ref': 'x', unknown: 1})))
        self.assertEqual(result['structuredContent']['reason'], 'schema_unknown_field')
        self.assertNotIn(unknown, json.dumps(result))

        result = failure(dict(params=dict(name='knowledge_feedback', arguments=[])))
        self.assertEqual(result['structuredContent']['reason'], 'input_not_object')

        inputs = dict(kind='hardware_spec', outcome='verified', source_locator='fixture://x',
                      content='fixture', evidence_refs=[], **{unknown: 1})
        result = failure(dict(params=dict(
            name='knowledge_distill',
            arguments=dict(inputs=[inputs], task_state='verified_milestone'))))
        self.assertEqual(result['structuredContent']['reason'], 'schema_unknown_input_field')
        self.assertNotIn(unknown, json.dumps(result))

        for reason in ('unknown_operation', 'schema_missing_field', 'schema_unknown_field',
                       'schema_unknown_input_field'):
            with self.subTest(reason=reason):
                exc = mcp_gate.InvalidToolArguments(
                    'Invalid MindIE tool arguments (unknown keys; missing keys)',
                    stage='tool_schema', reason=reason)
                result = mcp_gate.call_failure(exc)
                self.assertEqual(result['structuredContent']['reason'], reason)
                self.assertEqual(result['structuredContent']['stage'], 'tool_schema')
                self.assertNotIn('unknown keys; missing keys', result['content'][0]['text'])
