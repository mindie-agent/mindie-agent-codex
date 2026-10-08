"""Native input is framed once, byte bounded and owned by its caller."""

import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'
sys.path.insert(0, str(SCRIPTS))
import bridge


class HookInputTests(unittest.TestCase):
    def read(self, chunks):
        with patch.object(bridge.os, 'read', side_effect=[*chunks, b'']), \
             patch.object(bridge.sys.stdin, 'fileno', return_value=0):
            return json.loads(bridge._read_hook_stdin())

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
            bridge._read_hook_stdin()


class HookOwnershipTests(unittest.TestCase):
    def test_oversized_event_is_an_explicit_failure(self):
        with patch.object(bridge, 'MAX_HOOK_BYTES', 64), \
             patch.object(bridge.os, 'read', return_value=b'{"body":"' + b'x' * 100), \
             patch.object(bridge.sys.stdin, 'fileno', return_value=0), \
             self.assertRaisesRegex(ValueError, 'byte bound'):
            bridge._read_hook_stdin()

    @unittest.skipUnless(os.name == 'posix', 'POSIX parent ownership')
    def test_owner_exit_stops_waiting_for_an_incomplete_event(self):
        with patch.object(bridge.threading.Thread, 'start'), \
             patch.object(bridge.threading.Event, 'wait', return_value=False), \
             patch.object(bridge.os, 'getppid', side_effect=[10, 11]), \
             self.assertRaisesRegex(ConnectionError, 'owner exited'):
            bridge._read_hook_stdin()


if __name__ == '__main__':
    unittest.main()
