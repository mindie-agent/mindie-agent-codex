"""Stop keeps one dispatch identity while the canonical binding is replaced."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import entry
import bridge
import session_gate
from bounded_process import ProcessResult
from update_lock import update_lock


class StopBindingTests(unittest.TestCase):
    def test_snapshot_framing_preserves_native_bound_with_escaped_maximum(self):
        native_limit = 1024
        prefix, suffix = b'{"body":"', b'"}'
        body_size = native_limit - len(prefix) - len(suffix)
        raw = prefix + b"\\\\" * (body_size // 2) + b"x" * (body_size % 2) + suffix
        self.assertEqual(len(raw), native_limit)
        encoded = json.dumps(dict(adapter={"text": "x" * 65520},
                                  hook_input=raw.decode()), ensure_ascii=False).encode()
        self.assertGreater(len(encoded), native_limit)
        with patch.object(bridge, "MAX_HOOK_BYTES", native_limit), \
                patch.object(bridge.sys.stdin, "fileno", return_value=0), \
                patch.object(bridge.os, "read", return_value=raw):
            self.assertEqual(bridge._read_hook_stdin(), raw)
        with patch.object(bridge, "MAX_HOOK_BYTES", native_limit), \
                patch.object(bridge.sys.stdin, "fileno", return_value=0), \
                patch.object(bridge.os, "read", return_value=raw + b" "), \
                self.assertRaisesRegex(ValueError, "byte bound"):
            bridge._read_hook_stdin()
        with patch.object(bridge, "MAX_HOOK_BYTES", native_limit), \
                patch.object(sys, "argv", ["bridge.py", "stop", "--snapshot"]), \
                patch.object(bridge.sys.stdin, "fileno", return_value=0), \
                patch.object(bridge.os, "read", return_value=encoded), \
                patch.object(bridge, "stop", return_value=0) as stop, \
                self.assertRaises(SystemExit) as exited:
            bridge.main()
        self.assertEqual(exited.exception.code, 0)
        self.assertEqual(stop.call_args.kwargs["hook_input"], raw.decode())

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "codex.json"
        self.adapter = dict(python=sys.executable, runtime_scripts=str(SCRIPTS),
                            engine_config=str(self.root / "engine.json"),
                            community_config=str(self.root / "mindie-community.json"),
                            native_context={"CODEX_HOME": str(self.root / "native")},
                            diagnostics_context={"MINDIE_DIAGNOSTICS_ROOT": str(self.root / "diagnostics")},
                            product_validation={})
        self.config.write_text(json.dumps(self.adapter))
        (self.root / "engine.json").write_text(json.dumps(dict(
            transcript_adapter=str(SCRIPTS / "codex_transcript.py"), product_validation={})))
        self.release = self.root / "release"
        self.release.mkdir()
        (self.release / "runtime.json").write_text(json.dumps(dict(python=sys.executable)))

    def test_native_stop_entry_reads_binding_once_without_cutover_lock(self):
        calls = []

        def dispatch(argv, data, **options):
            self.config.write_text(json.dumps(dict(self.adapter, python="/different/python")))
            calls.append((argv, json.loads(data), options["env"]))
            return ProcessResult("completed", "{}", 0)

        with patch.dict(os.environ, MINDIE_AGENT_CONFIG=str(self.config)), \
                patch.object(sys, "argv", ["entry.py", "--release", str(self.release), "stop"]), \
                patch.object(bridge, "_read_hook_stdin", return_value=b'{"hook_event_name":"Stop"}'), \
                patch.object(entry, "update_lock", side_effect=AssertionError("Stop took cutover lock")), \
                patch.object(entry, "bind_explicit_config"), \
                patch("bounded_process.run", side_effect=dispatch), redirect_stdout(io.StringIO()):
            self.assertEqual(entry.main(), 0)
        argv, envelope, env = calls[0]
        self.assertEqual(argv[0], self.adapter["python"])
        self.assertEqual(argv[-2:], ["stop", "--snapshot"])
        self.assertEqual(envelope["adapter"], self.adapter)
        self.assertEqual(env["CODEX_HOME"], self.adapter["native_context"]["CODEX_HOME"])
        self.assertEqual(env["MINDIE_DIAGNOSTICS_ROOT"], self.adapter["diagnostics_context"]["MINDIE_DIAGNOSTICS_ROOT"])

    def test_admission_dispatch_keeps_snapshot_under_real_exclusive_lock(self):
        self.config.write_text("corrupt canonical must not be reread")
        result = ProcessResult("completed", '{"ok":true,"result":{"stage":"accepted-local"}}', 0)
        with update_lock(self.config, exclusive=True), \
                patch.object(session_gate, "run", return_value=result) as run:
            value = session_gate.Sessions(self.config, stop_adapter=self.adapter)._op(
                "stop_capture", {"event": {"session_id": "thread"}})
        self.assertEqual(value["stage"], "accepted-local")
        self.assertEqual(json.loads(run.call_args.args[1])["_adapter"], self.adapter)
        self.assertEqual(run.call_args.args[0][0], self.adapter["python"])
        self.assertEqual(run.call_args.kwargs["env"]["CODEX_HOME"],
                         self.adapter["native_context"]["CODEX_HOME"])
        with update_lock(self.config, exclusive=True):
            with self.assertRaises(BlockingIOError):
                session_gate.Sessions(self.config).active_lease("thread")

    def test_accepted_queue_consumer_fault_warns_but_pending_does_not(self):
        with patch.object(bridge, "_record_stop") as record:
            for details in (dict(wake="failed"), dict(reason="worker-dead")):
                self.assertFalse(bridge._observe_stop(dict(stage="accepted-local", **details)))
            self.assertEqual(record.call_count, 2)
            for details in (dict(runtime="retired"), dict(runtime="starting"), dict(runtime="unknown")):
                self.assertTrue(bridge._observe_stop(dict(stage="accepted-local", **details)))
            self.assertEqual(record.call_count, 2)

    def test_isolated_native_entry_imports_resolve_installed_siblings(self):
        for name in ("entry", "harness_update", "release_activation", "session_maintenance",
                     "bridge", "setup", "feed_sync", "release_metadata", "remote_bridge"):
            with self.subTest(name=name):
                result = subprocess.run([sys.executable, "-I", "-c",
                    "import runpy,sys;runpy.run_path(sys.argv[1],run_name='import_only')",
                    str(SCRIPTS / (name + ".py"))], capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
