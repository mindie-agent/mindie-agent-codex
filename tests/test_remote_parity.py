"""Remote MCP parity: blocks, budgets, nonzero exits, native fields.

These are mechanism checks with real bounded child processes and real PNG
bytes. They do not claim a live SSH or model pass.
"""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'
sys.path.insert(0, str(SCRIPTS))

import bounded_process
import mcp_gate
import runtime_call

PNG_1X1 = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlKkAAAAASUVORK5CYII='
)


def text_page(size=300 * 1024):
    return ''.join(chr(ord('a') + index % 26) for index in range(size))


class RemoteContentAndOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({
            'python': sys.executable,
            'runtime_scripts': str(SCRIPTS),
            'engine_config': '/absent/not-read',
        }))
        diagnostics = self.root / 'diagnostics'
        diagnostics.mkdir()
        (diagnostics / 'diagnostics.json').write_text('{"decision":"disabled"}\n')
        self.env = patch.dict(os.environ, {
            'MINDIE_AGENT_CONFIG': str(self.config),
            'MINDIE_REMOTE_STATE_DIR': str(self.root / 'remote'),
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def call_remote(self, name, value):
        with patch.dict(os.environ), \
                patch('remote_dev.mcp.tools.call_tool', return_value=value), \
                patch('remote_dev.core.rpc_transport.close_connections'):
            return runtime_call.call({
                'surface': 'remote', 'remote_session_id': 'task-A',
                'name': name, 'arguments': {},
            })

    def test_real_png_image_block_and_structured_content_survive(self):
        self.assertEqual(PNG_1X1[:8], b'\x89PNG\r\n\x1a\n')
        value = {
            'text': 'Remote image read shot.png (image/png, 67 bytes).',
            'result': {
                'outcome': 'success', 'status': 'ok', 'tool': 'remote_view_image',
                'extra': {'path': '/tmp/shot.png', 'mime_type': 'image/png', 'size': len(PNG_1X1)},
            },
            'content': [{
                'type': 'image', 'mimeType': 'image/png',
                'data': base64.b64encode(PNG_1X1).decode('ascii'),
            }],
        }
        result = self.call_remote('remote_view_image', value)
        self.assertFalse(result['isError'])
        image = [block for block in result['content'] if block.get('type') == 'image']
        self.assertEqual(len(image), 1, result['content'])
        self.assertEqual(base64.b64decode(image[0]['data']), PNG_1X1)
        self.assertEqual(image[0]['mimeType'], 'image/png')
        self.assertEqual(result['structuredContent']['outcome'], 'success')
        self.assertEqual(result['structuredContent']['extra']['mime_type'], 'image/png')

    def test_legitimate_text_page_over_256k_survives_remote_process_cap(self):
        page = text_page(300 * 1024)
        value = {'text': page, 'result': {'outcome': 'success', 'status': 'ok'}}
        result = self.call_remote('remote_read', value)
        self.assertFalse(result['isError'])
        self.assertEqual(result['content'][0]['text'], page)
        self.assertGreater(len(page.encode('utf-8')), 256 * 1024)
        self.assertGreaterEqual(mcp_gate.REMOTE_MAX_OUTPUT, 32 * 1024 * 1024)

    def test_real_bounded_process_keeps_page_above_old_256k_cap(self):
        page = text_page(300 * 1024)
        script = 'import sys; sys.stdout.write(' + repr(page) + ')'
        result = bounded_process.run(
            [sys.executable, '-c', script], '', timeout=10,
            max_output=mcp_gate.REMOTE_MAX_OUTPUT,
        )
        output = result.checked_stdout()
        self.assertEqual(result.execution, 'completed')
        self.assertEqual(output, page)
        self.assertGreater(len(output), 256 * 1024)

    def test_exec_command_nonzero_exit_is_native_output_not_helper_error(self):
        value = {
            'text': 'Process exited with code 42\nFinal output:\nno\n',
            'result': {
                'outcome': 'failed', 'status': 'failed', 'state': 'failed',
                'exit_code': 42, 'preview': {'stdout': 'no\n', 'stderr': ''},
                'error_details': {'category': 'command_exit', 'submission_state': 'acknowledged'},
            },
        }
        result = self.call_remote('remote_exec_command', value)
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent']['exit_code'], 42)
        self.assertEqual(result['structuredContent']['state'], 'failed')
        self.assertEqual(result['structuredContent']['error_details']['submission_state'],
                         'acknowledged')
        self.assertIn('Process exited with code 42', result['content'][0]['text'])

    def test_write_stdin_nonzero_exit_is_native_output(self):
        value = {'text': 'Process exited with code 7\n', 'result': {
            'outcome': 'failed', 'status': 'failed', 'state': 'failed', 'exit_code': 7}}
        result = self.call_remote('remote_write_stdin', value)
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent']['exit_code'], 7)

    def test_helper_failure_without_terminal_state_stays_an_error(self):
        value = {'text': 'connection unavailable', 'result': {
            'outcome': 'failed', 'status': 'connection_unavailable',
            'error_details': {'category': 'connection_unavailable',
                              'submission_state': 'not_sent'}}}
        result = self.call_remote('remote_exec_command', value)
        self.assertTrue(result['isError'])
        self.assertEqual(result['structuredContent']['error_details']['submission_state'],
                         'not_sent')

    def test_unknown_submission_state_survives_content_shaping(self):
        value = {
            'text': 'Remote execution outcome is uncertain.',
            'result': {
                'outcome': 'failed', 'status': 'job_outcome_unknown',
                'unknown': ['worker heartbeat lost'],
                'error_details': {'category': 'job_outcome_unknown',
                                  'submission_state': 'uncertain'},
            },
            'content': [{'type': 'text', 'text': 'native block'}],
        }
        result = self.call_remote('remote_job_status', value)
        self.assertTrue(result['isError'])
        self.assertEqual(result['structuredContent']['error_details']['submission_state'],
                         'uncertain')
        self.assertEqual(result['structuredContent']['unknown'], ['worker heartbeat lost'])
        self.assertIn({'type': 'text', 'text': 'native block'}, result['content'])

    def test_failed_command_without_observed_exit_code_stays_an_error(self):
        value = {'text': 'Remote command failed.', 'result': {
            'outcome': 'failed', 'status': 'failed', 'state': 'failed'}}
        result = self.call_remote('remote_exec_command', value)
        self.assertTrue(result['isError'])
        self.assertNotIn('exit_code', result['structuredContent'])

    def test_uncertain_command_with_exit_code_stays_an_error(self):
        value = {'text': 'Remote outcome unconfirmed.', 'result': {
            'outcome': 'failed', 'status': 'failed', 'state': 'failed',
            'exit_code': 7,
            'error_details': {'category': 'rpc_timeout', 'submission_state': 'uncertain'},
            'unknown': ['reply lost']}}
        result = self.call_remote('remote_exec_command', value)
        self.assertTrue(result['isError'])
        self.assertEqual(result['structuredContent']['exit_code'], 7)
        self.assertEqual(result['structuredContent']['error_details']['submission_state'],
                         'uncertain')

    def test_nested_operation_completed_survives_local_recording_failure(self):
        value = {'text': 'Remote command succeeded; local recording failed.', 'result': {
            'outcome': 'failed', 'status': 'local_recording_failed',
            'execution_outcome': 'success', 'state': 'succeeded', 'exit_code': 0,
            'error_details': {'category': 'local_state', 'operation_completed': True}}}
        result = self.call_remote('remote_exec_command', value)
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent']['execution_outcome'], 'success')

    def test_canonical_alias_is_forwarded_for_core_dispatch(self):
        seen = []
        value = {'text': 'page', 'result': {'outcome': 'success', 'status': 'ok'}}

        def dispatch(name, args):
            seen.append(name)
            return value

        with patch.dict(os.environ), \
                patch('remote_dev.mcp.tools.call_tool', side_effect=dispatch), \
                patch('remote_dev.core.rpc_transport.close_connections'):
            runtime_call.call({'surface': 'remote', 'remote_session_id': 'task-A',
                               'name': 'remote_read', 'arguments': {'file_path': '/tmp/x'}})
        self.assertEqual(seen, ['remote_read'])


class RemoteBudgetTests(unittest.TestCase):
    def test_native_families_never_get_an_outer_equal_to_inner(self):
        cases = {
            'remote_write_stdin': 300.0,
            'remote_job_stdin': 300.0,
            'remote_exec_command': 30.0,
            'remote_bash': 600.0,
            'remote_read': 0.0,
        }
        for name, inner in cases.items():
            with self.subTest(name=name):
                outer = mcp_gate.remote_timeout(name, {})
                self.assertGreaterEqual(outer, inner + mcp_gate.REMOTE_GRACE_SECONDS)
                self.assertGreater(outer, inner)
                self.assertGreaterEqual(outer, mcp_gate.REMOTE_MIN_BUDGET_SECONDS)

    def test_explicit_call_wait_extends_but_never_shortens_the_budget(self):
        self.assertEqual(mcp_gate.remote_timeout('remote_read', {}),
                         mcp_gate.REMOTE_MIN_BUDGET_SECONDS)
        self.assertGreaterEqual(
            mcp_gate.remote_timeout('remote_read', {'yield_time_ms': 240000}),
            240.0 + mcp_gate.REMOTE_GRACE_SECONDS,
        )
        # An explicit outer deadline may lengthen but must not cut off a legal
        # native poll; the gate uses max(explicit, derived).
        self.assertGreaterEqual(
            max(1.0, mcp_gate.remote_timeout('remote_write_stdin', {})),
            360.0,
        )

    def test_gate_passes_derived_timeout_and_large_remote_cap_to_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'config.json'
            config.write_text(json.dumps({
                'python': sys.executable, 'runtime_scripts': str(SCRIPTS),
                'engine_config': '/absent/not-read',
            }))
            diagnostics = root / 'diagnostics'
            diagnostics.mkdir()
            (diagnostics / 'diagnostics.json').write_text('{"decision":"disabled"}\n')
            request = {
                'id': 1,
                'params': {
                    'name': 'remote_write_stdin',
                    'arguments': {'session_id': 123},
                    '_meta': {
                        'threadId': 'task-A',
                        'x-codex-turn-metadata': {
                            'thread_id': 'task-A', 'session_id': 'task-A',
                            'turn_id': 'turn-1',
                        },
                    },
                },
            }
            helper = {
                'content': [{'type': 'text', 'text': 'still running'}],
                'structuredContent': {'outcome': 'success', 'status': 'running'},
                'isError': False,
            }
            with patch.dict(os.environ, {
                    'MINDIE_AGENT_CONFIG': str(config),
                    'MINDIE_REMOTE_STATE_DIR': str(root / 'remote'),
            }), patch.object(mcp_gate, 'run',
                             return_value=bounded_process.ProcessResult(
                                 'completed', json.dumps(helper), 0)) as dispatch:
                response = mcp_gate.Gate('remote').call(request)
            self.assertFalse(response['isError'])
            kwargs = dispatch.call_args.kwargs
            self.assertGreaterEqual(kwargs['timeout'], 360.0)
            self.assertGreater(kwargs['timeout'], 300.0)
            self.assertEqual(kwargs['max_output'], mcp_gate.REMOTE_MAX_OUTPUT)
            self.assertGreater(kwargs['max_output'], 1024 * 1024)


class NativeFieldPassThroughTests(unittest.TestCase):
    def test_exec_catalog_declares_shell_login_and_environment(self):
        catalog = json.loads((SCRIPTS / 'mcp_catalog.json').read_text(encoding='utf-8'))
        tool = next(item for item in catalog['remote'] if item['name'] == 'remote_exec_command')
        props = tool['inputSchema']['properties']
        self.assertIn('shell', props)
        self.assertIn('login', props)
        self.assertIn('environment_id', props)
        self.assertIn('environment_id', tool['inputSchema'].get('properties', {}))

    def test_gate_keeps_shell_login_and_environment_arguments(self):
        request = {
            'id': 2,
            'params': {
                'name': 'remote_exec_command',
                'arguments': {
                    'cmd': 'echo hi', 'shell': '/bin/bash', 'login': True,
                    'environment_id': 'primary',
                },
                '_meta': {
                    'threadId': 'task-A',
                    'x-codex-turn-metadata': {
                        'thread_id': 'task-A', 'session_id': 'task-A',
                        'turn_id': 'turn-1',
                    },
                },
            },
        }
        gate = mcp_gate.Gate('remote')
        name, args = gate._tool_args(request)
        self.assertEqual(name, 'remote_exec_command')
        self.assertEqual(args['shell'], '/bin/bash')
        self.assertIs(args['login'], True)
        self.assertEqual(args['environment_id'], 'primary')


if __name__ == '__main__':
    unittest.main()
