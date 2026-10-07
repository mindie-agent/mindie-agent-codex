"""Fragmented native JSON must not spend the Stop budget reparsing its body."""

import json
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


if __name__ == '__main__':
    unittest.main()
