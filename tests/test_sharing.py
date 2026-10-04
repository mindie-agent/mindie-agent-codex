"""Community sharing gate: config states, scope, transitions and hook capture.

All checks are local mechanism tests with controlled files and stub runtimes;
no model, service or network is started.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from tests.process_fixtures import cleanup_temporary_directory
from unittest.mock import patch
from tests.process_fixtures import stop_owned_knowledge_service

ROOT = Path(__file__).resolve().parents[1]
from tests.process_fixtures import public_engine_config

SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import sharing
from session_gate import Sessions


class SharingFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scope = self.root / "scope"
        self.scope.mkdir()
        self.codex_home = self.root / "codex-home"
        (self.codex_home / "sessions").mkdir(parents=True)
        self.native_transcript()
        self.config = self.root / "codex.json"
        self.engine = self.root / "engine.json"
        self.community = self.root / "mindie-community.json"
        self.admission = self.root / "codex.admission.sqlite3"
        self.engine.write_text(
            json.dumps(
                public_engine_config(self.root / "data", admission_path=str(self.admission))
            )
        )
        self.config.write_text(
            json.dumps(
                dict(
                    python=sys.executable,
                    engine_config=str(self.engine),
                    community_config=str(self.community),
                    admission_path=str(self.admission),
                    runtime_scripts=str(SCRIPTS),
                )
            )
        )
        self.environment = patch.dict(
            os.environ, MINDIE_AGENT_CONFIG=str(self.config), CODEX_THREAD_ID="manual-A",
            CODEX_HOME=str(self.codex_home), MINDIE_DIAGNOSTICS_ROOT=str(self.root / "diagnostics")
        )
        self.environment.start()
        self.sessions = Sessions()

    def tearDown(self):
        try:
            stop_owned_knowledge_service(self.engine)
        finally:
            self.environment.stop()
            cleanup_temporary_directory(self.temp)

    def settings(self, **overrides):
        value = dict(
            schema="mindie-community-config/1",
            enabled=True,
            generation="g1",
            enabled_at=time.time(),
            repository="mindie-agent/knowledge",
            branch="main",
            project_roots=[str(self.scope)],
            idle_seconds=300,
            visibility="public",
        )
        value.update(overrides)
        return value

    def write_sharing(self, **overrides):
        self.community.write_text(json.dumps(self.settings(**overrides)))
        engine = json.loads(self.engine.read_text())
        engine["community_config"] = str(self.community)
        self.engine.write_text(json.dumps(engine))

    def prepare_store(self):
        from mindie_knowledge.loop.store import Store

        store = Store(self.root / "data", "test")
        store.close()

    def captures(self):
        path = self.root / "data" / "test" / "state-v4.sqlite3"
        if not path.is_file():
            return 0
        db = sqlite3.connect(path)
        try:
            return db.execute("SELECT count(*) FROM captures").fetchone()[0]
        finally:
            db.close()

    def activate(self):
        with (
            patch.dict(os.environ, CODEX_THREAD_ID="manual-A"),
            patch.object(Path, "cwd", return_value=self.scope),
        ):
            return self.sessions.activate()

    def native_transcript(self, name="synthetic-transcript.jsonl", *, owner="manual-A", scope=None):
        path = self.codex_home / "sessions" / name
        path.write_text(json.dumps(dict(type="session_meta",
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload=dict(id=owner, cwd=str(scope or self.scope)))) + "\n", encoding="utf-8")
        return path

    def event(self, cwd=None, **extra):
        event = dict(
            hook_event_name="Stop",
            transcript_path=str(self.codex_home / "sessions/synthetic-transcript.jsonl"),
            session_id="manual-A",
            turn_id="turn-1",
            cwd=str(cwd or self.scope),
        )
        event.update(extra)
        return event

    def bridge(self, operation, event=None, timeout=5):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "bridge.py"), operation],
            input=json.dumps(event) if event is not None else "",
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    def bridge_stop_held_open(self, payload=None, timeout=3):
        """Real stop subprocess whose stdin is never closed by the parent."""
        process = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "bridge.py"), "stop"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        started = time.monotonic()
        try:
            if payload is not None:
                process.stdin.write(json.dumps(payload).encode())
                process.stdin.flush()
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
            self.fail("stop hook did not consume the complete event on unclosed stdin")
        elapsed = time.monotonic() - started
        stdout = process.stdout.read()
        stderr = process.stderr.read()
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
        return process.returncode, stdout, stderr, elapsed

    def attempts(self, session="manual-A"):
        db = sqlite3.connect(self.sessions.path)
        try:
            return db.execute(
                "SELECT count(*) FROM attempts WHERE session=?", (session,)
            ).fetchone()[0]
        finally:
            db.close()


class GateTests(SharingFixture):
    def test_large_native_stop_forwards_reference_without_copying_body(self):
        from capture_config import prepare
        from datetime import datetime, timezone
        from contextlib import closing
        from mindie_knowledge.loop.store import Store
        self.write_sharing()
        self.activate()
        self.prepare_store()
        config = json.loads(self.engine.read_text())
        config.update(prepare(sys.executable, SCRIPTS))
        config['transcript_adapter'] = str(SCRIPTS / 'codex_transcript.py')
        config.pop('summary_command')  # This case verifies body delivery, with zero model calls.
        self.engine.write_text(json.dumps(config))
        transcript_path = Path(self.event()['transcript_path'])
        for size in (129 * 1024, 1024 * 1024, 10 * 1024 * 1024):
            message = dict(type='response_item', timestamp=datetime.now(timezone.utc).isoformat(),
                           payload=dict(type='message', role='user', content=[dict(type='input_text', text=f'public-marker-{size}')]))
            with transcript_path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(message) + '\n')
            event = self.event(turn_id=f'large-{size}', last_assistant_message='公开结果' * (size // 12))
            result = self.bridge('stop', event, timeout=5)
            self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        path = self.root / 'data/test/state-v4.sqlite3'
        db = sqlite3.connect(path)
        try:
            rows = db.execute('SELECT summary, transcript FROM captures').fetchall()
        finally:
            db.close()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(summary == '' for summary, _ in rows))
        self.assertTrue(any(transcript == str(Path(event['transcript_path']).resolve()) for _, transcript in rows))
        with closing(Store(self.root / 'data', 'test')) as store:
            until = time.monotonic() + 5
            while time.monotonic() < until:
                docs = store.drafts_changed()
                if docs and docs[0]['content'].count('public-marker-') == 3:
                    break
                time.sleep(.05)
            self.assertEqual(docs[0]['content'].count('public-marker-'), 3)

    def test_missing_disabled_and_malformed_config_skip_before_any_claim(self):
        self.activate()
        states = [
            "missing",
            "disabled",
            "malformed",
            "wrong-schema",
        ]
        for state in states:
            with self.subTest(state=state):
                self.community.unlink(missing_ok=True)
                if state == "disabled":
                    self.write_sharing(enabled=False, enabled_at=None)
                elif state == "malformed":
                    self.community.write_text("{not json")
                elif state == "wrong-schema":
                    self.community.write_text(json.dumps(dict(schema="other/1")))
                result = self.bridge("stop", self.event(last_assistant_message="Done"))
                expected = 0 if state == "disabled" else 1
                self.assertEqual((result.returncode, json.loads(result.stdout)), (expected, {}))
                self.assertEqual(self.attempts(), 0)

    def test_scope_comes_from_native_metadata_not_the_event_cwd(self):
        self.write_sharing()
        self.activate()
        outside = self.root / "outside"
        outside.mkdir()
        # A relative event cwd is rejected at envelope validation.
        result = self.bridge(
            "stop", self.event(cwd="relative/path", last_assistant_message="Done")
        )
        self.assertEqual((result.returncode, json.loads(result.stdout)), (1, {}))
        self.assertEqual(self.attempts(), 0)
        self.prepare_store()
        # An absolute event cwd outside the scope does not block capture: the
        # authorized scope comes from the named native transcript metadata.
        result = self.bridge(
            "stop", self.event(cwd=str(outside), last_assistant_message="Done")
        )
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        self.assertEqual(self.captures(), 1)
        self.assertEqual(self.attempts(), 0)
        # Native metadata outside the approved roots captures nothing, even
        # when the event cwd points inside an allowed directory.
        self.native_transcript(scope=outside)
        result = self.bridge(
            "stop",
            self.event(
                turn_id="turn-2", cwd=str(self.scope), last_assistant_message="Done"
            ),
        )
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        self.assertEqual(self.captures(), 1)
        self.assertEqual(self.attempts(), 0)

    def test_transcript_event_without_final_summary_is_valid(self):
        # No final summary, but a transcript location: the event is admitted.
        self.write_sharing()
        self.activate()
        self.prepare_store()
        result = self.bridge("stop", self.event(transcript_path=str(self.codex_home / "sessions/synthetic-transcript.jsonl")))
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        self.assertEqual(self.captures(), 1)
        self.assertEqual(self.attempts(), 0)
        # The same turn is one row, and recursion is dropped.
        self.bridge("stop", self.event(transcript_path=str(self.codex_home / "sessions/synthetic-transcript.jsonl")))
        self.bridge(
            "stop",
            self.event(
                turn_id="turn-2",
                transcript_path=str(self.codex_home / "sessions/synthetic-transcript.jsonl"),
                stop_hook_active=True,
            ),
        )
        self.assertEqual(self.captures(), 1)
        self.assertEqual(self.attempts(), 0)

    def test_transcript_owned_by_another_session_is_never_captured(self):
        # The hook process gets no native thread identity from this host, so
        # the forwarded transcript artifact is the binding evidence: a Stop
        # event naming this task but forwarding ANOTHER session's transcript
        # writes no capture row and no model work.
        self.write_sharing()
        self.activate()
        self.prepare_store()
        foreign = self.codex_home / "sessions/foreign.jsonl"
        foreign.write_text(
            json.dumps(
                dict(
                    type="session_meta",
                    timestamp="2026-09-27T00:00:00Z",
                    payload=dict(id="other-task", cwd=str(self.scope)),
                )
            )
            + "\n"
        )
        result = self.bridge("stop", self.event(transcript_path=str(foreign)))
        self.assertEqual((result.returncode, json.loads(result.stdout)), (1, {}))
        self.assertEqual(self.captures(), 0)
        self.assertEqual(self.attempts(), 0)
        # The task's own transcript captures normally.
        own = self.codex_home / "sessions/own.jsonl"
        own.write_text(
            json.dumps(
                dict(
                    type="session_meta",
                    timestamp="2026-09-27T00:00:00Z",
                    payload=dict(id="manual-A", cwd=str(self.scope)),
                )
            )
            + "\n"
        )
        result = self.bridge("stop", self.event(transcript_path=str(own)))
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        self.assertEqual(self.captures(), 1)

    def test_env_thread_disagreeing_with_event_session_drops_capture(self):
        # When the host does supply CODEX_THREAD_ID to the hook process, an
        # event naming a different session is an anomaly: fail closed.
        self.write_sharing()
        self.activate()
        self.prepare_store()
        with patch.dict(os.environ, CODEX_THREAD_ID="child-task"):
            result = self.bridge("stop", self.event(last_assistant_message="Done"))
        self.assertEqual((result.returncode, json.loads(result.stdout)), (1, {}))
        self.assertEqual(self.captures(), 0)
        self.assertEqual(self.attempts(), 0)


    def test_transcript_reference_without_ready_store_reports_failure(self):
        self.write_sharing()
        self.activate()
        result = self.bridge("stop", self.event())
        self.assertEqual((result.returncode, json.loads(result.stdout)), (1, {}))
        self.assertEqual(self.attempts(), 0)

    def test_complete_event_with_held_open_stdin_forwards_once(self):
        self.write_sharing()
        self.activate()
        self.prepare_store()
        payload = self.event(last_assistant_message="Done")
        code, stdout, _stderr, elapsed = self.bridge_stop_held_open(payload)
        self.assertEqual((code, json.loads(stdout)), (0, {}))
        self.assertLess(elapsed, 2.0)
        self.assertEqual(self.captures(), 1)
        self.assertEqual(self.attempts(), 0)
        code, stdout, _stderr, elapsed = self.bridge_stop_held_open(payload)
        self.assertEqual((code, json.loads(stdout)), (0, {}))
        self.assertLess(elapsed, 2.0)
        self.assertEqual(self.captures(), 1)

    def test_unconfigured_unclosed_stdin_records_fault_without_capture(self):
        self.activate()
        before = self.attempts()
        code, stdout, _stderr, elapsed = self.bridge_stop_held_open()
        self.assertEqual((code, json.loads(stdout)), (1, {}))
        self.assertLess(elapsed, 0.75)
        self.assertEqual(self.attempts(), before)
        self.assertFalse(self.community.exists())

    def test_verified_native_metadata_repairs_incomplete_automatic_binding(self):
        self.write_sharing()
        self.prepare_store()
        # A verified current transcript supplies the missing capture metadata.
        lease = self.activate()
        db = sqlite3.connect(self.sessions.path)
        with db:
            db.execute(
                "UPDATE leases SET project_root=NULL, root_session=NULL, activated_at=NULL"
            )
        db.close()
        self.assertEqual(
            self.sessions.check("manual-A", lease["mindie_activation"])["session"],
            "manual-A",
        )
        result = self.bridge("stop", self.event(last_assistant_message="Done"))
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        self.assertEqual(self.captures(), 1)
        self.assertEqual(self.attempts(), 0)

    def test_sharing_toggle_does_not_invalidate_ordinary_activation(self):
        self.write_sharing()
        lease = self.activate()
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.write_sharing(
                    enabled=enabled, enabled_at=time.time() if enabled else None
                )
                # Fingerprint covers static bindings only: the lease survives.
                self.assertEqual(
                    self.sessions.check("manual-A", lease["mindie_activation"])[
                        "session"
                    ],
                    "manual-A",
                )


class CommandTests(SharingFixture):
    def test_enable_disable_status_cycle_is_atomic_and_bounded(self):
        result = self.bridge("sharing-status")
        self.assertEqual(json.loads(result.stdout)["state"], "unconfigured")
        # Enable requires recorded settings; unconfigured fails clearly.
        result = self.bridge("sharing-enable")
        self.assertEqual(result.returncode, 1)
        self.write_sharing(enabled=False, enabled_at=None)
        started = time.monotonic()
        enabled = json.loads(self.bridge("sharing-enable").stdout)
        self.assertEqual(enabled["status"], "enabled")
        first = enabled["generation"]
        self.assertGreaterEqual(enabled["enabled_at"], started)
        settings = sharing.validate(json.loads(self.community.read_text()))
        self.assertTrue(settings["enabled"])
        if os.name == "posix":
            self.assertEqual(self.community.stat().st_mode & 0o777, 0o600)
        else:
            # The Windows ACL comes from the private profile directory; this
            # assertion checks creation but does not claim ACL validation.
            self.assertTrue(self.community.is_file())
        # Disable keeps settings and bumps the generation; the generation
        # change itself is the core-observed cancellation signal.
        started = time.monotonic()
        disabled = json.loads(self.bridge("sharing-disable").stdout)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(disabled["status"], "disabled")
        self.assertNotEqual(disabled["generation"], first)
        self.assertIn("rereads this generation", disabled["cancel"])
        settings = sharing.validate(json.loads(self.community.read_text()))
        self.assertFalse(settings["enabled"])
        self.assertIsNone(settings["enabled_at"])
        status = json.loads(self.bridge("sharing-status").stdout)
        self.assertEqual(status["state"], "disabled")
        self.assertEqual(status["repository"], "mindie-agent/knowledge")

    def test_reenable_uses_fresh_enabled_at_without_backfill(self):
        self.write_sharing(enabled=True, enabled_at=time.time() - 10000)
        json.loads(self.bridge("sharing-disable").stdout)
        time.sleep(0.02)
        enabled = json.loads(self.bridge("sharing-enable").stdout)
        self.assertGreater(enabled["enabled_at"], time.time() - 5)
        self.assertIn("no backfill", enabled["note"])

    def test_toggle_preserves_sibling_component_extension_keys(self):
        # fork/account plus structured bot, transaction and transport settings
        # owned by the community component survive an adapter toggle verbatim.
        extensions = dict(
            fork="sample-user/knowledge-vllm-ascend",
            account="sample-user",
            bot={"account": "sample-bot", "grok_argv": ["grok"]},
            transaction_seconds=120,
            operation_limit=60,
            config_path="/private/local/path.json",
        )
        self.write_sharing(**extensions)
        json.loads(self.bridge("sharing-disable").stdout)
        after = json.loads(self.community.read_text())
        for key, value in extensions.items():
            self.assertEqual(after[key], value, key)
        self.assertFalse(after["enabled"])
        json.loads(self.bridge("sharing-enable").stdout)
        again = json.loads(self.community.read_text())
        for key, value in extensions.items():
            self.assertEqual(again[key], value, key)

    def test_malformed_status_still_reports_and_fails_closed(self):
        self.community.write_text('{"schema": "mindie-community-config/1"}')
        status = json.loads(self.bridge("sharing-status").stdout)
        self.assertEqual(status["state"], "malformed")
        self.assertIn("fail-closed", status["capture"])

    def test_disable_survives_a_concurrent_settings_write(self):
        # One writer pauses inside the settings os.replace (inside the
        # community write lock). The concurrent disable must wait for it —
        # never overlap the read-merge-replace — then land on the freshly
        # enabled file: the stored flag ends disabled, both exit 0.
        self.write_sharing(enabled=True)
        script = self.root / "settings_race.py"
        script.write_text(
            "import os, sys, time\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "os.environ['MINDIE_AGENT_CONFIG'] = sys.argv[2]\n"
            "role, root = sys.argv[3], Path(sys.argv[4])\n"
            "real_replace = os.replace\n"
            "def paused(src, dst):\n"
            "    if role == 'stamp' and Path(dst).name == 'mindie-community.json':\n"
            "        (root / 'at-replace').write_text('1')\n"
            "        deadline = time.time() + 10\n"
            "        while not (root / 'release-stamp').exists():\n"
            "            if time.time() > deadline:\n"
            "                raise SystemExit('stamp was not released')\n"
            "            time.sleep(0.01)\n"
            "    return real_replace(src, dst)\n"
            "os.replace = paused\n"
            "import sharing\n"
            "sharing.set_enabled(role == 'stamp')\n"
            "(root / f'done-{role}').write_text('1')\n"
        )
        env = dict(os.environ)
        stamp = subprocess.Popen(
            [sys.executable, str(script), str(SCRIPTS), str(self.config), "stamp", str(self.root)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        children = [stamp]
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not (self.root / "at-replace").exists():
                if stamp.poll() is not None:
                    break
                time.sleep(0.01)
            stamp_err = stamp.stderr.read() if stamp.poll() is not None else ""
            self.assertTrue(
                (self.root / "at-replace").is_file(),
                "stamp never reached the settings replace: " + stamp_err,
            )
            disable = subprocess.Popen(
                [sys.executable, str(script), str(SCRIPTS), str(self.config), "disable", str(self.root)],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            children.append(disable)
            deadline = time.time() + 2
            while time.time() < deadline and disable.poll() is None and not (self.root / "done-disable").exists():
                time.sleep(0.01)
            overlapped = (self.root / "done-disable").exists()
            (self.root / "release-stamp").write_text("1")
            finished = [child.communicate(timeout=20) for child in children]
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait()
        codes = [child.returncode for child in children]
        saved = json.loads(self.community.read_text())
        self.assertEqual(
            (overlapped, codes, saved["enabled"]),
            (False, [0, 0], False),
            f"overlapped={overlapped} codes={codes} enabled={saved['enabled']} out={finished}",
        )


if __name__ == "__main__":
    unittest.main()
