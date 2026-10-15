"""Native Interrupt addresses exact in-flight calls without a worker slot."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import mcp_gate


def request(identifier, thread="thread-a", turn="turn-1"):
    return dict(jsonrpc="2.0", id=identifier, method="tools/call", params=dict(
        name="fixture_read", arguments={}, _meta={
            "threadId": thread,
            "x-codex-turn-metadata": dict(thread_id=thread, session_id="parent",
                                         turn_id=turn)}))


def interrupt(identifier, thread="thread-a", turn="turn-1"):
    return dict(jsonrpc="2.0", id=identifier, method="tools/call", params=dict(
        name=mcp_gate.INTERRUPT_TOOL,
        arguments=dict(thread_id=thread, turn_id=turn),
        _meta=dict(threadId=thread, progressToken=identifier)))


class NativeInterruptTests(unittest.TestCase):
    def test_only_exact_pending_thread_and_turn_are_signalled(self):
        scopes = [("thread-a", "turn-1"), ("thread-a", "turn-2"),
                  ("thread-b", "turn-1"), ("thread-a", "turn-1")]
        pending = {index: (threading.Event(), scope) for index, scope in enumerate(scopes)}
        result = mcp_gate.interrupt_turn(interrupt(10), pending)
        self.assertEqual(result, dict(content=[dict(type="text", text="{}")], isError=False))
        self.assertEqual([event.is_set() for event, _ in pending.values()],
                         [True, False, False, True])
        # A completed call needs no cancellation. No state is retained that
        # could cancel a later independent request in the same thread.
        self.assertEqual(mcp_gate.interrupt_turn(interrupt(11), {}), result)
        self.assertEqual(len(pending), 4)

    def test_invalid_or_contradictory_control_never_cancels(self):
        mutations = [
            lambda p: p["arguments"].update(turn_id=None),
            lambda p: p["arguments"].update(thread_id=True),
            lambda p: p["arguments"].update(extra="not-supported"),
            lambda p: p["_meta"].update(threadId="thread-b"),
            lambda p: p.pop("_meta"),
            lambda p: p["_meta"].update({"x-codex-turn-metadata": dict(
                thread_id="thread-a", session_id="parent", turn_id="turn-2")}),
            lambda p: p["_meta"].update({"x-codex-turn-metadata": None}),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                message = interrupt(10)
                mutate(message["params"])
                event = threading.Event()
                with self.assertRaises(ValueError):
                    mcp_gate.interrupt_turn(message, {1: (event, ("thread-a", "turn-1"))})
                self.assertFalse(event.is_set())

    def test_pending_identity_requires_native_thread_and_turn(self):
        self.assertEqual(mcp_gate.native_turn_identity(request(1)), ("thread-a", "turn-1"))
        for value in (None, True, "", "bad/id"):
            with self.subTest(turn=value):
                message = request(1)
                message["params"]["_meta"]["x-codex-turn-metadata"]["turn_id"] = value
                with self.assertRaises(ValueError):
                    mcp_gate.native_turn_identity(message)

    def test_hidden_control_bypasses_full_capacity_and_preserves_standard_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = """
import json,sys,time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import mcp_gate
root = Path(sys.argv[2])
class Gate:
    config = root / 'unused.json'
    tools = [dict(name='fixture_read')]
    def __init__(self, surface): pass
    def call(self, message, cancel):
        identity = str(message['id'])
        (root / (identity + '.started')).write_text('ready')
        cancel.wait()
        (root / (identity + '.cancelled')).write_text('cancelled')
        return dict(content=[], isError=True)
with patch.object(mcp_gate, 'Gate', Gate), \\
     patch('update_trigger.Maintenance'), \\
     patch('diagnostic_support.attach_pending', side_effect=lambda value: value), \\
     patch('diagnostic_support.acknowledge_pending'), \\
     patch('importlib.metadata.version', return_value='0.0.1'):
    mcp_gate.serve('remote-dev')
"""
            process = subprocess.Popen([sys.executable, "-I", "-c", code, str(SCRIPTS), str(root)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            replies = queue.Queue()
            reader = threading.Thread(
                target=lambda: [replies.put(json.loads(line)) for line in process.stdout],
                daemon=True)
            reader.start()

            def send(value):
                process.stdin.write((json.dumps(value) + "\n").encode())
                process.stdin.flush()

            def receive(expected):
                observed = {}
                for _ in expected:
                    value = replies.get(timeout=10)
                    observed[value["id"]] = value["result"]
                self.assertEqual(set(observed), set(expected))
                return observed

            try:
                send(dict(jsonrpc="2.0", id=0, method="tools/list"))
                self.assertNotIn(mcp_gate.INTERRUPT_TOOL,
                                 [row["name"] for row in receive([0])[0]["tools"]])
                for identifier, thread, turn in (
                    (1, "thread-a", "turn-1"), (2, "thread-a", "turn-2"),
                    (3, "thread-b", "turn-1"), (4, "thread-a", "turn-1")):
                    send(request(identifier, thread, turn))
                deadline = time.monotonic() + 10
                while len(list(root.glob("*.started"))) != 4 and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(len(list(root.glob("*.started"))), 4)
                send(request(5))
                self.assertTrue(receive([5])[5]["isError"])
                invalid = interrupt(8)
                invalid["params"]["_meta"]["threadId"] = "another-thread"
                send(invalid)
                self.assertTrue(receive([8])[8]["isError"])
                self.assertEqual(list(root.glob("*.cancelled")), [])
                send(interrupt(9))
                self.assertFalse(receive([9, 1, 4])[9]["isError"])
                self.assertFalse((root / "2.cancelled").exists())
                self.assertFalse((root / "3.cancelled").exists())
                send(dict(jsonrpc="2.0", method="notifications/cancelled",
                          params=dict(requestId=2)))
                self.assertTrue(receive([2])[2]["isError"])
                send(interrupt(10, thread="thread-b"))
                self.assertFalse(receive([10, 3])[10]["isError"])
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                reader.join(timeout=2)
                error = process.stderr.read().decode()
                process.stdout.close()
                process.stderr.close()
            self.assertEqual(process.returncode, 0, error)


if __name__ == "__main__":
    unittest.main()
