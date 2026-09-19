"""Serving boundary checks: preserve facts while reducing remote operations."""
from __future__ import annotations

import io
import json
import os
import sys

from pathlib import Path
for _p in Path(__file__).resolve().parents:
    if (_p / "domain-lib").is_dir():
        if str(_p / "domain-lib") not in sys.path:
            sys.path.insert(0, str(_p / "domain-lib"))
        break
else:
    raise RuntimeError("MindIE domain-lib not found; use the installed plugin")
ROOT = Path(__file__).resolve().parents[1]  # the skill package directory
import tempfile
import shutil
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path[:0] = [str(ROOT / 'scripts'),
               str(ROOT.parents[1] / 'domain-lib')]
import _serving_common as common
import _serving_start as start
import mindie_receipt as envelope


class ReceiptTests(unittest.TestCase):
    def test_compact_and_full_keep_same_business_facts(self):
        facts = {'status': 'ready', 'execution_id': 'owned-one', 'ready': True,
                 'base_url': 'http://worker:8000', 'resources_released': False,
                 'readiness': {'ready': True, 'phases': [{'phase': 'first-token-ok'}]}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(common, 'ROOT', Path(tmp)):
            for full in ('0', '1'):
                stream = io.StringIO()
                with mock.patch.dict(os.environ, MINDIE_FULL_RECEIPT=full), mock.patch('sys.stdout', stream):
                    common.print_json(facts)
                result = json.loads(stream.getvalue())
                self.assertEqual(envelope.unwrap_skill_payload(result), facts)
                if full == '0':
                    stored = json.loads(Path(result['record_ref']).read_text(encoding='utf-8'))
                    self.assertEqual(envelope.unwrap_skill_payload(stored), facts)
                    self.assertLess(len(stream.getvalue()), len(json.dumps(stored)))

    def test_record_failure_preserves_failure_cause_and_exit(self):
        stream = io.StringIO()
        facts = {'status': 'failed', 'execution_id': 'owned-one',
                 'error': 'ImportError: native dependency missing', 'resources_released': True}
        with mock.patch.dict(os.environ, MINDIE_FULL_RECEIPT='0'), \
             mock.patch.object(envelope, 'write_full_record', side_effect=PermissionError('read-only')), \
             mock.patch('sys.stdout', stream):
            common.print_json(facts)
        result = json.loads(stream.getvalue())
        self.assertEqual(result['outcome'], 'failure')
        self.assertEqual(result['exit_code'], 1)
        self.assertEqual(result['result'], facts)
        self.assertIsNone(result['record_ref'])
        self.assertIn('read-only', result['warnings'][0])

    def test_terminal_wait_receipt_avoids_another_tail_request(self):
        client = SimpleNamespace(observe=mock.Mock(side_effect=AssertionError('duplicate RPC')))
        details = start.startup_failure_details(client, 'owned-one',
                                               {'stderr': 'ImportError: selected runtime is broken'})
        self.assertIn('selected runtime is broken', details['log_error'])
        client.observe.assert_not_called()


class ProbeTests(unittest.TestCase):
    def test_healthy_service_uses_one_remote_call_with_bounded_http_budget(self):
        reply = SimpleNamespace(returncode=0, stdout='__HEALTH__=200\n__MODELS_CODE__=200\n'
                                '__MODELS_BEGIN__\n{"data":[{"id":"one"}]}\n__MODELS_END__\n'
                                '__TOKEN_CODE__=200\n')
        with mock.patch.object(common, 'ssh_exec', return_value=reply) as remote:
            result = common.probe_service('endpoint', 8000, served_model="model's name", timeout=2)
        self.assertTrue(result['health'])
        self.assertTrue(result['first_token'])
        self.assertEqual(result['models']['data'][0]['id'], 'one')
        remote.assert_called_once()
        command = remote.call_args.args[1]
        self.assertEqual(command.count('--noproxy'), 3)
        self.assertEqual(command.count('--max-time 0.500'), 2)
        self.assertIn('--max-time 1.000', command)
        self.assertEqual(remote.call_args.kwargs['timeout'], 2)

    def test_status_never_sends_completion_and_rejects_bad_models(self):
        reply = SimpleNamespace(returncode=0, stdout='__HEALTH__=200\n__MODELS_CODE__=500\n'
                                '__MODELS_BEGIN__\n{"data":[1]}\n__MODELS_END__\n')
        with mock.patch.object(common, 'ssh_exec', return_value=reply) as remote:
            result = common.probe_service('endpoint', 8000)
        self.assertTrue(result['health'])
        self.assertIsNone(result['models'])
        self.assertNotIn('/completions', remote.call_args.args[1])

    def test_ready_path_does_not_fetch_logs_or_sleep(self):
        running, logs = mock.Mock(return_value=True), mock.Mock(side_effect=AssertionError('unneeded tail'))
        with mock.patch.object(start, 'probe_service', return_value={
                'health': True, 'models': {'data': [1]}, 'first_token': True}), \
             mock.patch.object(start.time, 'sleep') as sleep:
            result = start.wait_for_ready('endpoint', 8000, 5, 'one', still_running=running, log_text=logs)
        self.assertTrue(result['ready'])
        running.assert_called_once()
        logs.assert_not_called()
        sleep.assert_not_called()


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash') and shutil.which('curl'),
                     'requires local POSIX Bash/curl; never uses a remote endpoint')
class HttpProtocolTests(unittest.TestCase):
    def test_200_headers_without_completed_body_never_mean_ready(self):
        mode = 'healthy'

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, body, delay=0, code=200):
                self.send_response(code)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if delay:
                    time.sleep(delay)
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                if self.path == '/health':
                    self.answer(b'healthy', 1 if mode == 'health-timeout' else 0)
                else:
                    self.answer(b'{"data":[{"id":"model"}]}', code=500 if mode == 'models-error' else 200)

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                self.answer(b'{"choices":[{"text":"Hello"}]}', 1 if mode == 'completion-timeout' else 0)

        def local(endpoint, script, *, check, timeout):
            self.assertEqual(endpoint, 'LOOPBACK_ONLY')
            return subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=timeout + 1)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for mode in ('healthy', 'completion-timeout', 'health-timeout', 'models-error'):
                with self.subTest(mode=mode), mock.patch.object(common, 'ssh_exec', side_effect=local) as remote:
                    result = common.probe_service('LOOPBACK_ONLY', server.server_port,
                                                  served_model='model', timeout=.8)
                remote.assert_called_once()
                self.assertEqual(result['first_token'], mode == 'healthy')
                if mode.endswith('timeout'):
                    self.assertTrue(result['probe_error'])
                if mode == 'health-timeout':
                    self.assertFalse(result['health'])
                if mode == 'models-error':
                    self.assertIsNone(result['models'])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
