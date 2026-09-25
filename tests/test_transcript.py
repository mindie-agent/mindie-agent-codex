"""Bounded Codex JSONL transcript reader: mechanism tests on controlled files.

Parser ownership moved from core; coverage stays here. These are filesystem
mechanism tests with structurally representative files, not model outcomes
and not evidence from real private sessions.
"""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import codex_transcript as transcript


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            if isinstance(record, str):
                stream.write(record)
            else:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def message(role, text, **extra):
    ctype = "input_text" if role == "user" else "output_text"
    payload = dict(
        type="message", role=role, content=[dict(type=ctype, text=text)]
    )
    payload.update(extra)
    return {
        "timestamp": "2026-09-20T01:00:00Z",
        "type": "response_item",
        "payload": payload,
    }


def meta(session="task-1"):
    return {"type": "session_meta", "payload": {"id": session, "cwd": "/work"}}


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_increment_from_zero_extracts_public_records(self):
        path = self.root / "rollout.jsonl"
        write_jsonl(
            path,
            [
                meta(),
                message("user", "Investigate the device mapping failure"),
                {
                    "type": "response_item",
                    "payload": {"type": "reasoning", "text": "hidden"},
                },
                message("assistant", "The container numbers devices from zero"),
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "shell",
                        "arguments": "npu-smi",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "output": "device 8 mapped",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "system",
                        "content": [
                            {"type": "input_text", "text": "developer secret"}
                        ],
                    },
                },
            ],
        )
        inc = transcript.read_increment(str(path), 0, session_id="task-1")
        self.assertEqual(inc["status"], "ok")
        self.assertIn("device mapping", inc["text"])
        self.assertIn("zero", inc["text"])
        self.assertIn("npu-smi", inc["text"])
        self.assertNotIn("hidden", inc["text"])
        self.assertNotIn("developer secret", inc["text"])
        self.assertEqual(inc["end"], path.stat().st_size)

    def test_nonzero_offset_reads_only_the_new_region(self):
        path = self.root / "rollout.jsonl"
        write_jsonl(path, [meta(), message("user", "first turn")])
        inc1 = transcript.read_increment(str(path), 0, session_id="task-1")
        with open(path, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(message("assistant", "second turn")) + "\n")
        inc2 = transcript.read_increment(
            str(path), inc1["end"], session_id="task-1"
        )
        self.assertEqual(inc2["status"], "ok")
        self.assertEqual(inc2["start"], inc1["end"])
        self.assertIn("second turn", inc2["text"])
        self.assertNotIn("first turn", inc2["text"])
        inc3 = transcript.read_increment(
            str(path), inc2["end"], session_id="task-1"
        )
        self.assertEqual(inc3["status"], "unchanged")
        self.assertEqual(inc3["end"], inc2["end"])

    def test_partial_trailing_record_is_not_consumed(self):
        path = self.root / "rollout.jsonl"
        write_jsonl(path, [meta(), message("user", "complete")])
        complete_end = path.stat().st_size
        with open(path, "ab") as stream:
            stream.write(
                b'{"type":"response_item","payload":{"type":"message","ro'
            )
        inc = transcript.read_increment(str(path), 0, session_id="task-1")
        self.assertEqual(inc["end"], complete_end)
        self.assertIn("complete", inc["text"])

    def test_analysis_channel_is_private_never_extracted(self):
        path = self.root / "rollout.jsonl"
        write_jsonl(
            path,
            [
                meta(),
                message("assistant", "PRIVATE chain draft", channel="analysis"),
                message("assistant", "public answer", channel="final"),
                message("assistant", "unspecified channel answer"),
            ],
        )
        inc = transcript.read_increment(str(path), 0, session_id="task-1")
        self.assertNotIn("PRIVATE", inc["text"])
        self.assertIn("public answer", inc["text"])

    def test_established_identity_does_not_fail_an_unrecognized_page(self):
        path = self.root / "rollout.jsonl"
        write_jsonl(path, [{"type": "noise", "n": i} for i in range(3)])
        identity = transcript.identify(str(path))
        with open(path, "a", encoding="utf-8", newline="\n") as stream:
            stream.write('{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"later public"}]}}\n')
        first = transcript.read_material(
            str(path), 0, session_id="task-1", expected=identity, max_scan_bytes=1024,
        )
        if first["status"] == "unknown-format":
            self.assertFalse(first["more"])
        else:
            self.assertEqual(first["status"], "ok")
            self.assertTrue(first["more"] or "later public" in first["text"])

    def test_unknown_format_reports_summary_only(self):
        path = self.root / "other.jsonl"
        write_jsonl(path, [{"foo": 1}, "not json at all\n"])
        inc = transcript.read_increment(str(path), 0)
        self.assertEqual(inc["status"], "unknown-format")
        self.assertEqual(inc["text"], "")
        self.assertGreater(inc["end"], 0)

    def test_replacement_and_truncation_stop_the_segment(self):
        path = self.root / "rollout.jsonl"
        write_jsonl(path, [meta(), message("user", "original long body" * 20)])
        identity = transcript.identify(str(path))
        size = path.stat().st_size
        write_jsonl(path, [meta(), message("user", "short")])
        inc = transcript.read_increment(str(path), size, expected=identity)
        self.assertEqual(inc["status"], "replaced")
        os.unlink(path)
        write_jsonl(path, [meta(), message("user", "new file body")])
        inc = transcript.read_increment(str(path), 0, expected=identity)
        self.assertEqual(inc["status"], "replaced")

    def test_append_preserves_anchored_identity(self):
        path = self.root / "rollout.jsonl"
        write_jsonl(path, [meta(), message("user", "first turn")])
        identity = transcript.identify(str(path))
        self.assertGreater(identity.anchor_len, 0)
        self.assertEqual(len(identity.anchor_digest), 64)
        with open(path, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(message("assistant", "appended turn")) + "\n")
        inc = transcript.read_increment(
            str(path), identity.size, session_id="task-1", expected=identity
        )
        self.assertEqual(inc["status"], "ok")
        self.assertIn("appended turn", inc["text"])
        restored = transcript.FileIdentity.unserialize(
            identity.serialize(), identity.path
        )
        self.assertIsNotNone(restored)
        self.assertTrue(
            transcript.same_file(restored, transcript.identify(str(path)))
        )

    def test_malformed_persisted_identity_fails_closed(self):
        path = self.root / "rollout.jsonl"
        write_jsonl(path, [meta(), message("user", "body")])
        self.assertIsNone(
            transcript.FileIdentity.unserialize("not json", str(path))
        )
        self.assertIsNone(
            transcript.FileIdentity.unserialize(
                '{"dev":1,"ino":2,"anchor_len":0,"anchor_digest":"'
                + ("x" * 64)
                + '"}',
                str(path),
            )
        )
        identity = transcript.identify(str(path))
        self.assertFalse(transcript.same_file(None, identity))
        self.assertFalse(transcript.same_file(identity, None))

    def test_foreign_task_transcript_is_not_read(self):
        path = self.root / "rollout.jsonl"
        write_jsonl(
            path, [meta("other-task"), message("user", "foreign private text")]
        )
        inc = transcript.read_increment(str(path), 0, session_id="task-1")
        self.assertEqual(inc["status"], "wrong-task")
        self.assertNotIn("foreign private text", inc["text"])

    def test_authorization_boundary_filters_history(self):
        path = self.root / "rollout.jsonl"
        before = message("user", "before enable")
        before["timestamp"] = "2026-09-19T01:00:00Z"
        write_jsonl(path, [before, message("user", "after enable")])
        from datetime import datetime, timezone

        boundary = datetime(2026, 9, 20, tzinfo=timezone.utc).timestamp()
        inc = transcript.read_increment(str(path), 0, not_before=boundary)
        self.assertIn("after enable", inc["text"])
        self.assertNotIn("before enable", inc["text"])

    def test_custom_tools_public_phases_and_noise_are_recognized(self):
        path = self.root / "native.jsonl"
        write_jsonl(
            path,
            [
                meta(),
                message("assistant", "private phase", phase="analysis"),
                message("assistant", "public final", phase="final_answer"),
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "functions.exec",
                        "call_id": "c1",
                        "input": 'await tools.exec_command({cmd:"npu-smi"})',
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "c1",
                        "output": "actual bounded output",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "token_count", "secret": "not public"},
                },
            ],
        )
        inc = transcript.read_material(path, 0, session_id="task-1")
        self.assertIn("public final", inc["text"])
        self.assertIn("npu-smi", inc["text"])
        self.assertIn("actual bounded output", inc["text"])
        self.assertNotIn("private phase", inc["text"])
        self.assertNotIn("not public", inc["text"])
        old = path.stat().st_size
        with path.open("a") as handle:
            handle.write(
                json.dumps({"type": "event_msg", "payload": {"type": "token_count"}})
                + "\n"
            )
        noise = transcript.read_material(path, old, session_id="task-1")
        self.assertEqual(noise["status"], "ok")
        self.assertFalse(noise["text"])
        self.assertEqual(noise["end"], path.stat().st_size)

    def test_foreign_identity_is_checked_at_nonzero_offset(self):
        path = self.root / "native.jsonl"
        write_jsonl(path, [meta("foreign"), message("user", "private")])
        offset = path.read_bytes().index(b"\n") + 1
        inc = transcript.read_material(path, offset, session_id="task-1")
        self.assertEqual(inc["status"], "wrong-task")
        self.assertFalse(inc["text"])

    def test_fork_inherited_parent_material_is_excluded(self):
        path = self.root / "fork.jsonl"
        head = meta("child")
        head["payload"].update(
            forked_from_id="parent", timestamp="2026-09-20T00:30:00Z"
        )
        old = message("user", "inherited parent secret")
        old["timestamp"] = "2026-09-20T00:00:00Z"
        write_jsonl(
            path,
            [
                head,
                meta("parent"),
                old,
                message("assistant", "child finding", phase="final_answer"),
            ],
        )
        inc = transcript.read_material(path, 0, session_id="child")
        self.assertEqual(inc["status"], "ok")
        self.assertIn("child finding", inc["text"])
        self.assertNotIn("inherited parent secret", inc["text"])

    def test_harness_catalog_is_noise_not_task_material(self):
        path = self.root / "native.jsonl"
        write_jsonl(
            path,
            [
                meta(),
                message(
                    "user",
                    "<recommended_plugins>noise catalog</recommended_plugins>",
                ),
                message("user", "actual user task"),
            ],
        )
        inc = transcript.read_material(path, 0, session_id="task-1")
        self.assertNotIn("catalog", inc["text"])
        self.assertIn("actual user task", inc["text"])

    def test_full_text_envelope_does_not_consume_the_next_record(self):
        path = self.root / "native.jsonl"
        write_jsonl(
            path,
            [meta()]
            + [message("user", f"unique-{i} " + "x" * 10000) for i in range(9)],
        )
        cursor = 0
        texts = []
        for _ in range(10):
            inc = transcript.read_material(
                path, cursor, session_id="task-1", max_text_bytes=16384
            )
            texts.append(inc["text"])
            self.assertGreater(inc["end"], cursor)
            self.assertEqual(
                inc["digest"],
                hashlib.sha256(path.read_bytes()[cursor : inc["end"]]).hexdigest(),
            )
            cursor = inc["end"]
            if not inc["more"]:
                break
        self.assertEqual(cursor, path.stat().st_size)
        joined = "\n".join(texts)
        for i in range(9):
            self.assertEqual(joined.count(f"unique-{i}"), 1)


if __name__ == "__main__":
    unittest.main()
