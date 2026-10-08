"""Native input and helper dispatch share one whole-Stop deadline."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'
sys.path.insert(0, str(SCRIPTS))
import bridge


class HookInputTests(unittest.TestCase):
    def read(self, chunks):
        with patch.object(bridge.os, 'read', side_effect=[*chunks, b'']), \
             patch.object(bridge.sys.stdin, 'fileno', return_value=0):
            return bridge._read_hook_stdin(1)

    def test_braces_in_fragmented_body_are_decoded_once(self):
        event = {'body': ('x' * 4095 + '}') * 128}
        raw = json.dumps(event).encode()
        # Deliberately finish every body chunk with a brace inside the string.
        prefix = raw.index(b'x')
        chunks = [raw[:prefix], *(raw[i:i+4096] for i in range(prefix, len(raw), 4096))]
        loads = json.loads
        with patch.object(bridge.json, 'loads', wraps=loads) as decode:
            result = self.read(chunks)
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(result, event)

    def test_nested_values_and_escaped_strings_across_every_split(self):
        event = {'body': 'quotes " braces }[ slash \\ 中文',
                 'nested': [{'key': '\\"'}, [], {'a': 1}], 'enabled': False,
                 'slashes': '\\' * 64}
        raw = json.dumps(event, ensure_ascii=False).encode('utf-8')
        for split in range(1, len(raw)):
            with self.subTest(split=split):
                self.assertEqual(self.read([raw[:split], raw[split:]]), event)
        self.assertEqual(self.read([raw[i:i+1] for i in range(len(raw))]), event)

    def test_invalid_or_truncated_input_is_not_a_successful_event(self):
        for raw in (b'{"body": "unfinished', b'{]', b'{} trailing', b'{"x": "\xff"}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.read([raw])

    def test_read_error_preserves_failure(self):
        with patch.object(bridge.os, 'read', side_effect=OSError('fixture read failure')), \
             patch.object(bridge.sys.stdin, 'fileno', return_value=0), \
             self.assertRaises(OSError):
            bridge._read_hook_stdin(1)


class StopDeadlineTests(unittest.TestCase):
    def run_stop(self, platform, budget, input_finished_at):
        started = 100.0
        clock = SimpleNamespace(now=started + budget / 4)
        clock.monotonic = lambda: clock.now
        event = dict(
            hook_event_name="Stop", session_id="deadline-task", turn_id="turn-1",
            cwd=str(SCRIPTS), transcript_path=str(SCRIPTS / "synthetic.jsonl"),
        )

        def read_input(timeout):
            clock.now = started + input_finished_at
            return event

        output = io.StringIO()
        with (
            patch.object(bridge, "os", SimpleNamespace(
                name=platform, path=os.path, environ={"CODEX_THREAD_ID": event["session_id"]},
            )),
            patch.object(bridge, "time", clock),
            patch.object(bridge, "_ENTRYPOINT_STARTED_AT", started),
            patch.object(bridge.sharing, "read", return_value={}),
            patch.object(bridge.sharing, "consent_allows", return_value=True),
            patch.object(bridge, "_read_hook_stdin", side_effect=read_input) as stdin,
            patch.object(bridge, "Sessions") as sessions,
            patch.object(bridge, "_record_stop") as failure,
            redirect_stdout(output),
        ):
            sessions.return_value._op.return_value = {"stage": "accepted-local"}
            result = bridge.stop()
        return result, output.getvalue(), stdin, sessions, failure

    def test_input_elapsed_time_is_deducted_from_helper_budget(self):
        for platform, budget in (("posix", bridge.HOOK_BUDGET),
                                 ("nt", bridge.WINDOWS_HOOK_BUDGET)):
            with self.subTest(platform=platform):
                result, output, stdin, sessions, failure = self.run_stop(
                    platform, budget, input_finished_at=budget * 3 / 4,
                )
                self.assertEqual((result, json.loads(output)), (0, {}))
                self.assertAlmostEqual(stdin.call_args.args[0], budget * 3 / 4)
                sessions.assert_called_once()
                self.assertAlmostEqual(sessions.call_args.kwargs["op_timeout"], budget / 4)
                operation, payload = sessions.return_value._op.call_args.args
                self.assertEqual(operation, "stop_capture")
                self.assertAlmostEqual(payload["event"]["budget_seconds"], budget / 4)
                failure.assert_not_called()

    def test_exhausted_budget_does_not_dispatch_a_helper_or_report_success(self):
        for platform, budget in (("posix", bridge.HOOK_BUDGET),
                                 ("nt", bridge.WINDOWS_HOOK_BUDGET)):
            with self.subTest(platform=platform):
                result, output, _, sessions, failure = self.run_stop(
                    platform, budget, input_finished_at=budget,
                )
                self.assertEqual((result, json.loads(output)), (1, {}))
                sessions.assert_not_called()
                failure.assert_called_once_with("budget", "budget_exhausted")


if __name__ == '__main__':
    unittest.main()
