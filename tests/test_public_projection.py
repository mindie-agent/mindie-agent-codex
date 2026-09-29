"""Exact public content, large input and excluded native event classes."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from tests.test_transcript import message, meta, write_jsonl, transcript


class PublicProjectionTests(unittest.TestCase):
    def test_only_user_public_commentary_and_final_survive_in_order(self):
        public = ['用户任务，保留原文。', '正在检查，这是公开进度。', '最终回答含 `torch==2.10.0.post2`。']
        records = [meta(), message('user', public[0]),
                   message('assistant', 'hidden-analysis-marker', channel='analysis'),
                   message('developer', 'injected-developer-marker'),
                   message('user', '<environment_context>private-context-marker</environment_context>'),
                   {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec', 'arguments': 'tool-input-marker'}},
                   message('assistant', public[1], phase='commentary'),
                   {'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'tool-output-marker'}},
                   {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': public[2]}},
                   message('assistant', public[2], phase='final_answer')]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'native.jsonl'
            write_jsonl(path, records)
            result = transcript.read_material(path, 0, session_id='task-1')
        self.assertEqual(result['text'], '### user\n' + public[0] + '\n\n### assistant:commentary\n' + public[1] + '\n\n### assistant:final_answer\n' + public[2])
        self.assertEqual(result['records'], 3)
        self.assertFalse(result['coverage'])

    def test_one_and_ten_megabyte_messages_are_not_clipped_or_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'native.jsonl'
            for size in (1024 * 1024, 10 * 1024 * 1024):
                with self.subTest(bytes=size):
                    content = 'BEGIN公开\n' + '测量 8 tokens\n' * (size // 16) + '\nEND完整'
                    write_jsonl(path, [meta(), message('user', content)])
                    started = time.monotonic()
                    result = transcript.read_material(path, 0, session_id='task-1', max_scan_bytes=256 * 1024)
                    self.assertEqual(result['text'], '### user\n' + content)
                    self.assertEqual(result['end'], path.stat().st_size)
                    self.assertFalse(result['more'])
                    self.assertFalse(result['coverage'])
                    self.assertLess(time.monotonic() - started, 10)

    def test_repeated_equal_messages_are_real_messages_not_semantic_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'native.jsonl'
            write_jsonl(path, [meta(), message('user', 'same'), message('assistant', 'same'), message('user', 'same')])
            result = transcript.read_material(path, 0, session_id='task-1')
            self.assertEqual(result['records'], 3)
            self.assertEqual(result['text'].count('same'), 3)

    def test_windows_message_newlines_match_the_canonical_public_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'native.jsonl'
            write_jsonl(path, [meta(), message('user', 'first\r\nsecond\rthird')])
            result = transcript.read_material(path, 0, session_id='task-1')
            self.assertEqual(result['text'], '### user\nfirst\nsecond\nthird')

    def test_invalid_complete_record_is_not_consumed_as_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'native.jsonl'
            write_jsonl(path, [meta()])
            boundary = path.stat().st_size
            for invalid in (b'{"type":broken}\n', b'[]\n', b'null\n', b'\xff\n'):
                with self.subTest(record=invalid):
                    path.write_bytes(path.read_bytes()[:boundary] + invalid)
                    result = transcript.read_material(path, 0, session_id='task-1')
                    self.assertEqual(result['status'], 'invalid-record')
                    self.assertEqual(result['end'], boundary)
                    self.assertTrue(result['coverage'])
                    self.assertFalse(result['text'])

    def test_public_timestamp_is_required_even_when_no_dated_message_follows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'native.jsonl'
            undated = message('user', 'unverifiable authority boundary')
            undated.pop('timestamp', None)
            write_jsonl(path, [meta(), undated])
            result = transcript.read_material(path, 0, session_id='task-1', not_before=0)
            self.assertEqual(result['status'], 'invalid-record')
            self.assertFalse(result['text'])
            self.assertLess(result['end'], path.stat().st_size)

    def test_native_text_shapes_share_filters_and_safe_attachment_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'native.jsonl'
            records = [meta(),
                dict(type='response_item', payload=dict(type='agent_message', role='user', text='<environment_context>injected marker')),
                dict(type='response_item', payload=dict(type='agent_message', text='visible<oai-mem-citation>private reference</oai-mem-citation> answer')),
                dict(type='response_item', payload=dict(type='message', role='user', content=[
                    dict(type='input_text', text='<environment_context>injected block</environment_context>'),
                    dict(type='input_text', text='Real user question'),
                    dict(type='input_image', image_url='private-image-url')]))]
            write_jsonl(path, records)
            result = transcript.read_material(path, 0, session_id='task-1')
            self.assertEqual(result['text'], '### assistant\nvisible answer\n\n### user\nReal user question\n[Image attachment omitted]')
