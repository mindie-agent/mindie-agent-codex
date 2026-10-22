"""Short helper lifetime with real adapter/core/locks/database.

POSIX exercises detached cold wake. Windows explicitly uses a parent-owned
service and recorded cold wake; native Scheduler acceptance is separate.
No native model or public transport is used.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing

from tests.test_sharing import SharingFixture, SCRIPTS
from tests.process_fixtures import (
    cleanup_temporary_directory, owned_fixture_process, recorded_wake_runtime,
    run_owned_fixture, stop_owned_knowledge_service, wake_requests,
)
from mindie_knowledge.loop.cli import connect, rpc


class ServiceEntryLifetimeTests(SharingFixture):
    def tearDown(self):
        # Preserve the first failure and its stage snapshot if cleanup also
        # finds incomplete state. Do not erase an unconfirmed owned service.
        result = getattr(self._outcome, "result", None)
        failed = (
            any(test is self for test, _ in result.failures + result.errors)
            if hasattr(result, "failures") and hasattr(result, "errors")
            else bool(getattr(self._outcome, "errors", None))
        )
        try:
            stop_owned_knowledge_service(self.engine)
            cleanup_temporary_directory(self.temp)
        except Exception as exc:
            self.temp._finalizer.detach()
            if not failed:
                raise
            print("secondary fixture cleanup failure; retained " + str(self.root)
                  + ": " + repr(exc), file=sys.stderr)
        finally:
            self.environment.stop()

    def stage_snapshot(self):
        from mindie_knowledge.loop.diagnostics import snapshot
        from mindie_knowledge.loop.locks import lock_held
        view = snapshot(self.engine, session="manual-A")
        domain = self.root / "data/test"
        return dict(
            service=view["service"], startup=view["startup"],
            delivery=view["delivery"],
            locks={name: lock_held(domain / name) for name in
                   ("consumer.lock", "start.lock", "state-layout.lock")},
        )

    def bridge(self, operation, *, event=None):
        completed = run_owned_fixture(
            [sys.executable, str(SCRIPTS / "bridge.py"), "--config", str(self.config), operation],
            input=json.dumps(event) if event else "",
            cwd=self.scope, timeout=20, diagnose=self.stage_snapshot,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def configure_capture(self):
        import consent

        self.write_sharing()
        consent.record_choice("contribute")
        engine = json.loads(self.engine.read_text())
        from tests.test_parallel_codex_contract import installed_scanner
        engine.pop("agent_command", None)
        engine.update(capture_mode="public-transcript", redactor_executable=installed_scanner(),
                      transcript_adapter=str(SCRIPTS / "codex_transcript.py"))
        self.engine.write_text(json.dumps(engine))
        return engine

    def stop_event(self):
        from datetime import datetime, timezone
        transcript = self.native_transcript("public.jsonl")
        transcript.write_text(transcript.read_text() +
            json.dumps(dict(type="response_item", timestamp=datetime.now(timezone.utc).isoformat(),
                payload=dict(type="message", role="assistant", phase="final_answer",
                    content=[dict(type="output_text", text="Synthetic lifetime test: public body persisted.")]))) + "\n", encoding="utf-8")
        return self.event(transcript_path=str(transcript))

    def capture_statuses(self):
        path = self.root / "data/test/state-v1/state-v4.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            return [r[0] for r in db.execute("SELECT status FROM captures")]

    def assert_organized(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            statuses = self.capture_statuses()
            if statuses == ["organized"]:
                return
            time.sleep(.05)
        self.fail(json.dumps(dict(statuses=statuses, stage=self.stage_snapshot())))

    @unittest.skipIf(os.name == "nt", "Windows uses explicit parent-owned service fixture")
    def test_activation_and_cold_stop_survive_short_helper_exit(self):
        engine = self.configure_capture()
        result = self.bridge("activate")
        self.assertEqual(result["experience"], "capture-ready")
        # The bridge AND bounded runtime helper have exited before this probe.
        self.assertTrue(rpc(connect(engine), "status", timeout=1)["worker_alive"])
        stop_owned_knowledge_service(self.engine)
        self.bridge("stop", event=self.stop_event())
        self.assert_organized()
        self.assertTrue(rpc(connect(engine), "status", timeout=1)["worker_alive"])

    def assert_parent_service_ready(self, process, engine, output):
        from mindie_knowledge.loop.locks import lock_held
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                status = rpc(connect(engine, config_path=self.engine), "status", timeout=.3)
                if status.get("worker_alive") is True and status.get("admission_frozen") is False:
                    self.assertIs(lock_held(self.root / "data/test/consumer.lock"), True)
                    return
            except TimeoutError:
                raise
            except (OSError, ValueError):
                pass
            time.sleep(.05)
        output.seek(0)
        self.fail(json.dumps(dict(stage=self.stage_snapshot(),
                                  service_output=output.read().decode("utf-8", errors="replace"),
                                  returncode=process.poll())))

    @unittest.skipUnless(os.name == "nt", "Windows parent-owned component backend")
    def test_windows_parent_owned_service_and_queued_cold_wake(self):
        engine = self.configure_capture()
        wake = recorded_wake_runtime(self.config, self.root / "test-runtime")
        scripts = wake.parent
        original = scripts / "production_runtime_call.py"
        (scripts / "runtime_call.py").rename(original)
        # Declare the backend before any helper runs. Unexpected cold startup
        # is an error, never a fallback to Scheduler on a hosted CI account.
        (scripts / "runtime_call.py").write_text(
            "from pathlib import Path\nfrom unittest.mock import patch\n"
            "from mindie_knowledge.loop import windows_service\n"
            "with patch.object(windows_service, 'request_start', "
            "side_effect=AssertionError('native Scheduler outside parent-owned fixture')):\n"
            # Preserve selected-generation __file__, so real redispatch still
            # validates interpreter/path instead of recursing into this shim.
            f"    exec(compile(Path({str(original)!r}).read_text(encoding='utf-8'), "
            "__file__, 'exec'), globals())\n",
            encoding="utf-8",
        )
        command = [sys.executable, "-m", "mindie_knowledge.loop.cli",
                   "serve", "--config", str(self.engine)]
        for phase in ("activate", "consume"):
            with tempfile.TemporaryFile() as output:
                with owned_fixture_process(command, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=output) as process:
                    self.assert_parent_service_ready(process, engine, output)
                    if phase == "activate":
                        self.assertEqual(self.bridge("activate")["experience"], "capture-ready")
                    else:
                        self.assert_organized()
                    # A real helper has exited; its Job never owns this service.
                    self.assertIsNone(process.poll())
                    self.assertTrue(rpc(connect(engine), "status", timeout=1)["worker_alive"])
                    stop_owned_knowledge_service(self.engine)
                    self.assertEqual(process.wait(timeout=8), 0)
            if phase == "activate":
                self.bridge("stop", event=self.stop_event())
                self.assertEqual(self.capture_statuses(), ["queued"])
                requests = wake_requests(wake)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]["session"], "manual-A")
                self.assertEqual(requests[0]["config"], str(self.engine))
