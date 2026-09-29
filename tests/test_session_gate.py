"""No model calls: session isolation, protocol discovery and failure bounds."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from contextlib import closing
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from tests.process_fixtures import cleanup_temporary_directory, stop_owned_knowledge_service, copy_runtime_scripts

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
from session_gate import Inactive, Sessions
import bounded_process
import mcp_gate


class SessionGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "codex.json"
        self.engine = self.root / "engine.json"
        self.admission = self.root / "codex.admission.sqlite3"
        self.engine.write_text(
            json.dumps(
                dict(
                    root=str(self.root / "data"),
                    domain="test",
                    admission_path=str(self.admission),
                )
            )
        )
        self.write_config()
        self.environment = patch.dict(
            os.environ, MINDIE_AGENT_CONFIG=str(self.config), CODEX_THREAD_ID="manual-A", MINDIE_REMOTE_STATE_DIR=str(self.root / "remote")
        )
        self.environment.start()
        self.sessions = Sessions()

    def tearDown(self):
        stop_owned_knowledge_service(self.engine)
        self.environment.stop()
        cleanup_temporary_directory(self.temp)

    def write_config(self, **extra):
        value = dict(
            python=sys.executable,
            engine_config=str(self.engine),
            admission_path=str(self.admission),
            runtime_scripts=str(SCRIPTS),
            community_config=str(self.root / "codex.community.json"),
        )
        value.update(extra)
        self.config.write_text(json.dumps(value))

    def activate(self, session="manual-A"):
        # Activation records the authorized project root from the task cwd.
        with (
            patch.dict(os.environ, CODEX_THREAD_ID=session),
            patch.object(Path, "cwd", return_value=self.root),
        ):
            return self.sessions.activate()

    def request(self, lease=None, ident=1, name="knowledge_query", meta=..., **arguments):
        """A tools/call carrying verified host turn metadata (Codex 0.153.4 shape)."""
        arguments = dict(arguments)
        if name == "knowledge_query":
            arguments.update(query="test")
        session = (lease or {}).get("mindie_session_id", "manual-A")
        if meta is ...:
            meta = {
                "x-codex-turn-metadata": {
                    "session_id": session,
                    "thread_id": session,
                    "turn_id": "turn-1",
                    "model": "gpt-5.6-luna",
                },
                "threadId": session,
            }
        return dict(
            jsonrpc="2.0",
            id=ident,
            method="tools/call",
            params=dict(name=name, arguments=arguments, _meta=meta),
        )

    def enable_sharing(self, *, enabled=True, roots=None):
        community = self.root / "codex.community.json"
        current = json.loads(self.config.read_text())
        self.write_config(
            python=current["python"],
            runtime_scripts=current["runtime_scripts"],
            community_config=str(community),
            sharing_choice="contribute",
        )
        engine = json.loads(self.engine.read_text())
        engine["community_config"] = str(community)
        self.engine.write_text(json.dumps(engine))
        community.write_text(
            json.dumps(
                dict(
                    schema="mindie-community-config/1",
                    enabled=enabled,
                    generation="g1",
                    enabled_at=time.time() if enabled else None,
                    repository="mindie-agent/knowledge",
                    branch="main",
                    project_roots=[str(root) for root in (roots or [self.root])],
                    idle_seconds=300,
                    visibility="public",
                )
            )
        )

    def bridge(self, operation, event=None, timeout=5, extra=()):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "bridge.py"), operation, *extra],
            input=json.dumps(event or {}),
            text=True,
            capture_output=True,
            timeout=timeout,
            cwd=str(self.root),
        )

    def runtime_fixture(self, *, delay=0, hook=False):
        """Keep Python executable; substitute only the selected runtime helper."""
        marker = self.root / "invocations"
        scripts = copy_runtime_scripts(self.root / "runtime-fixture")
        behavior = (
            "import sys, time\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).open('a').write('attempt\\n')\n"
            "sys.stdin.read()\n"
            f"time.sleep({delay})\n"
            "print('{}')\n"
        )
        (scripts / "runtime_call.py").write_text(behavior)
        if delay > 0:
            (scripts / "admission_ops.py").write_text(
                "import sys, runpy\n"
                "if sys.argv[1] == 'stop_capture':\n"
                + "\n".join("    " + line for line in behavior.splitlines())
                + "\nelse:\n"
                + f"    runpy.run_path({str(SCRIPTS / 'admission_ops.py')!r}, run_name='__main__')\n"
            )
        self.write_config(runtime_scripts=str(scripts))
        return marker

    def event(self, session="manual-A", turn="turn-1", cwd=None):
        return dict(
            hook_event_name="Stop",
            session_id=session,
            turn_id=turn,
            cwd=str(cwd or self.root),
            last_assistant_message="A verified result",
        )

    def test_manual_policy_and_no_registered_session_start(self):
        skill = SCRIPTS.parent / "skills/mindie-agent"
        self.assertIn(
            "allow_implicit_invocation: false",
            (skill / "agents/openai.yaml").read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            "SessionStart",
            json.loads((SCRIPTS.parent / "hooks/hooks.json").read_text())["hooks"],
        )
        skill = (skill / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("init", skill)
        self.assertIn("experience", skill)
        self.assertIn("contribution-inspect", skill)
        self.assertNotIn("SessionStart", skill)

    def test_activation_binds_only_when_community_sharing_is_enabled(self):
        marker = self.runtime_fixture()
        with patch.dict(os.environ, CODEX_THREAD_ID=""):
            self.assertEqual(self.bridge("activate").returncode, 1)
        self.assertFalse(self.sessions.path.exists())
        lease = json.loads(self.bridge("activate").stdout)
        self.assertEqual(lease["mindie_session_id"], "manual-A")
        # Sharing off: ordinary activation only — no cold start, no bind, and
        # the lease stays fully usable for read tools.
        self.assertEqual(lease.get("capture"), "disabled")
        self.assertFalse(marker.exists())
        again = json.loads(self.bridge("activate").stdout)
        self.assertEqual(
            {k: again[k] for k in ("mindie_session_id", "mindie_activation", "activated_at")},
            {k: lease[k] for k in ("mindie_session_id", "mindie_activation", "activated_at")},
        )
        self.assertFalse(marker.exists())
        # Enabling sharing admits local collection: one bounded cold start +
        # authenticated attach per activation call.
        self.enable_sharing()
        bound = json.loads(self.bridge("activate").stdout)
        self.assertEqual(bound.get("capture"), "bound")
        self.assertEqual(marker.read_text().splitlines(), ["attempt"])
        bound_again = json.loads(self.bridge("activate").stdout)
        self.assertEqual(marker.read_text().splitlines(), ["attempt", "attempt"])
        self.assertEqual(bound_again["mindie_activation"], bound["mindie_activation"])
        if os.name == "posix":
            self.assertEqual(self.sessions.path.stat().st_mode & 0o777, 0o600)
        else:
            # Windows ACL enforcement is inherited from the profile directory
            # and remains outside this mode-bit assertion.
            self.assertTrue(self.sessions.path.is_file())

    def test_inactive_hooks_create_no_state_or_runtime(self):
        marker = self.runtime_fixture()
        before = set(self.root.iterdir())
        result = self.bridge("stop", self.event())
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        result = self.bridge(
            "session-start", dict(hook_event_name="SessionStart", session_id="manual-A")
        )
        self.assertEqual(result.returncode, 1)  # retired operation, no longer served
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertFalse(marker.exists())

    def test_discovery_works_without_any_configuration(self):
        absent = self.root / "absent.json"
        messages = [
            dict(jsonrpc="2.0", id=1, method="initialize"),
            dict(jsonrpc="2.0", id=2, method="tools/list"),
        ]
        for script, argv, count in [
            ("bridge.py", ["mcp"], 3),
            ("remote_bridge.py", [], 11),
        ]:
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / script), *argv],
                input="".join(json.dumps(m) + "\n" for m in messages),
                text=True,
                capture_output=True,
                timeout=2,
                env={**os.environ, "MINDIE_AGENT_CONFIG": str(absent)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(len(replies[1]["result"]["tools"]), count)
        self.assertFalse(absent.with_suffix(".sessions.sqlite3").exists())
        self.assertFalse((self.root / "data").exists())

    def test_inactive_cross_session_and_bad_metadata_never_dispatch(self):
        lease = self.activate()
        gate = mcp_gate.Gate("knowledge")
        with patch.object(mcp_gate, "run") as run:
            # Arguments may not carry or override identity.
            self.assertTrue(
                gate.call(self.request(lease, mindie_session_id="manual-A"))["isError"]
            )
            self.assertTrue(
                gate.call(self.request(lease, mindie_activation="x"))["isError"]
            )
            # Another task's metadata has no active lease.
            self.assertTrue(
                gate.call(self.request(dict(mindie_session_id="other")))["isError"]
            )
            # Contradictory and missing metadata fail closed, no fallback.
            contradictory = {
                "x-codex-turn-metadata": {
                    "session_id": "manual-A",
                    "thread_id": "manual-A",
                    "turn_id": "t",
                },
                "threadId": "other",
            }
            self.assertTrue(gate.call(self.request(lease, meta=contradictory))["isError"])
            self.assertTrue(gate.call(self.request(lease, meta={}))["isError"])
            self.assertEqual(run.call_count, 0)

    def test_session_tree_does_not_own_the_child_thread(self):
        # Synthetic component frames only. Not an observed native child.
        def frame(thread, tree, top=..., alias=None, drop=()):
            meta = {
                "x-codex-turn-metadata": {
                    "thread_id": thread,
                    "session_id": tree,
                    "turn_id": "turn-1",
                },
                "threadId": thread if alias is None else alias,
            }
            if top is not ...:
                meta["sessionId"] = top
            for key in drop:
                if key == "threadId":
                    meta.pop("threadId", None)
                else:
                    meta["x-codex-turn-metadata"].pop(key, None)
            return meta

        root = self.request()
        self.assertEqual(mcp_gate.native_identity(root), "manual-A")
        self.assertNotIn("sessionId", root["params"]["_meta"])
        child_meta = frame("child-thread", "root-tree", "root-tree")
        self.assertEqual(
            mcp_gate.native_identity(self.request(meta=child_meta)), "child-thread"
        )
        self.assertEqual(
            mcp_gate.native_identity(
                self.request(meta=frame("child-thread", "root-tree"))
            ),
            "child-thread",
        )
        self.assertEqual(
            mcp_gate.native_identity(
                self.request(meta=frame("root-thread", "root-thread", "root-thread"))
            ),
            "root-thread",
        )
        parent = self.activate("root-tree")
        gate = mcp_gate.Gate("knowledge")
        with patch.object(mcp_gate, "run") as run:
            blocked = gate.call(self.request(parent, ident=11, meta=child_meta))
            self.assertTrue(blocked["isError"])
            run.assert_not_called()
        child = self.activate("child-thread")
        with patch.object(
            mcp_gate, "run", return_value='{"content":[],"isError":false}'
        ) as run:
            self.assertFalse(
                gate.call(self.request(child, ident=12, meta=child_meta))["isError"]
            )
            self.assertEqual(run.call_count, 1)
        rejected = [
            frame("child-thread", "root-tree", alias="other-thread"),
            frame("child-thread", "root-tree", "other-tree"),
            frame("child-thread", "root-tree", None),
            frame("child-thread", "root-tree", ""),
            frame("child-thread", "root-tree", 7),
            frame("child-thread", "root-tree", drop=("thread_id",)),
            frame("bad id", "root-tree"),
        ]
        with patch.object(mcp_gate, "run") as run:
            for meta in rejected:
                with self.subTest(meta=meta):
                    self.assertTrue(gate.call(self.request(child, meta=meta))["isError"])
            self.assertEqual(run.call_count, 0)

    def test_explain_is_gated_and_query_cannot_implicitly_activate(self):
        with patch.object(mcp_gate, "run") as run:
            gate = mcp_gate.Gate("knowledge")
            self.assertTrue(gate.call(self.request())["isError"])
            self.assertTrue(
                gate.call(self.request(name="knowledge_explain", ref="x"))["isError"]
            )
            self.assertEqual(run.call_count, 0)
        self.assertFalse(self.sessions.path.exists())

    def test_active_call_once_and_replayed_protocol_id_not_dispatched(self):
        lease = self.activate()
        gate = mcp_gate.Gate("knowledge")
        with patch.object(
            mcp_gate, "run", return_value='{"content":[],"isError":false}'
        ) as run:
            self.assertFalse(gate.call(self.request(lease))["isError"])
            self.assertTrue(gate.call(self.request(lease))["isError"])
            self.assertEqual(run.call_count, 1)

    def test_remote_job_identity_is_not_overwritten(self):
        lease = self.activate()
        request = self.request(
            lease, name="remote_job_status", session_id="remote-job-7"
        )
        with patch.object(
            mcp_gate, "run", return_value='{"content":[],"isError":false}'
        ) as run:
            self.assertFalse(mcp_gate.Gate("remote").call(request)["isError"])
            payload = json.loads(run.call_args.args[1])
            self.assertEqual(payload["arguments"]["session_id"], "remote-job-7")
            self.assertEqual(payload["remote_session_id"], "manual-A")
            self.assertEqual(run.call_args.kwargs["timeout"], 120)

    def test_artifact_transfer_timeout_is_rejected_before_runtime_dispatch(self):
        gate = mcp_gate.Gate("remote")
        for name in ("remote_artifact_push", "remote_artifact_pull"):
            for key in ("timeout_ms", "timeout"):
                with self.subTest(name=name, key=key), patch.object(mcp_gate, "run") as run:
                    arguments = {"remote_path": "/tmp/file", key: 120001}
                    if name == "remote_artifact_push":
                        arguments["local_path"] = "/tmp/file"
                    result = gate.call(self.request(name=name, **arguments))
                    self.assertTrue(result["isError"])
                    self.assertEqual(result["structuredContent"]["code"], "invalid_arguments")
                    self.assertEqual(result["structuredContent"]["execution"], "not_started")
                    self.assertIn("120000 ms", result["structuredContent"]["message"])
                    run.assert_not_called()

    def test_artifact_transfer_timeout_at_limit_is_forwarded(self):
        request = self.request(name="remote_artifact_pull", remote_path="/tmp/file", timeout_ms=120000)
        with patch.object(mcp_gate, "run", return_value='{"content":[],"isError":false}') as run:
            self.assertFalse(mcp_gate.Gate("remote").call(request)["isError"])
            self.assertEqual(run.call_args.kwargs["timeout"], 120)
            self.assertEqual(json.loads(run.call_args.args[1])["arguments"]["timeout_ms"], 120000)

    def test_rejected_reads_do_not_consume_the_failure_circuit(self):
        lease = self.activate()
        gate = mcp_gate.Gate("knowledge")
        rejected = json.dumps(
            dict(
                content=[dict(type="text", text="Knowledge read rejected: unknown reference")],
                structuredContent=dict(
                    code="read_rejected", execution="not_started",
                    message="rejected", automatic_retry=False,
                ),
                isError=True,
            )
        )
        with patch.object(mcp_gate, "run", return_value=rejected) as run:
            for i in range(3):
                result = gate.call(
                    self.request(lease, ident=100 + i, name="knowledge_explain", ref="bad-ref")
                )
                # Caller feedback is preserved: the model still sees the error.
                self.assertTrue(result["isError"])
                self.assertEqual(result["structuredContent"]["code"], "read_rejected")
            self.assertEqual(run.call_count, 3)  # dispatch never circuit-paused
            self.assertTrue(
                all(
                    "MINDIE_AGENT_CONFIG" in (c.kwargs.get("env") or {})
                    and "PYTHONPATH" not in (c.kwargs.get("env") or {})
                    for c in run.call_args_list
                )
            )
        with closing(sqlite3.connect(self.sessions.path)) as db:
            failures = db.execute(
                "SELECT failures FROM leases WHERE session='manual-A'"
            ).fetchone()[0]
        self.assertEqual(failures, 0)  # the circuit is neither consumed nor reset
        # A valid read still works on the same lease afterwards.
        with patch.object(mcp_gate, "run", return_value='{"content":[],"isError":false}'):
            self.assertFalse(gate.call(self.request(lease, ident=200))["isError"])
        # Actual runtime errors still consume the circuit and pause the lease.
        with patch.object(mcp_gate, "run", side_effect=TimeoutError("stalled")):
            for i in range(3):
                self.assertTrue(gate.call(self.request(lease, ident=300 + i))["isError"])
        with patch.object(mcp_gate, "run", return_value='{"content":[],"isError":false}') as run:
            self.assertFalse(gate.call(self.request(lease, ident=400))["isError"])
            self.assertEqual(run.call_count, 1)
        with patch.object(mcp_gate, "run", side_effect=ValueError("bad runtime response")):
            for i in range(3):
                self.assertTrue(gate.call(self.request(lease, ident=500 + i))["isError"])
        # Protocol failures are recorded diagnostically but never pause the lease.
        with patch.object(mcp_gate, "run", return_value='{"content":[],"isError":false}') as run:
            self.assertFalse(gate.call(self.request(lease, ident=600))["isError"])
            self.assertEqual(run.call_count, 1)

    def test_not_started_disposition_is_not_honored_for_mutations(self):
        import runtime_call
        from mindie_knowledge.loop.transport import RequestRejected

        lease = self.activate()
        finished = []

        def record_finish(config, session, token, succeeded):
            finished.append(succeeded)

        payload = dict(
            surface="knowledge",
            name="knowledge_feedback",
            arguments=dict(ref="x", rating="up"),
            mindie_session_id=lease["mindie_session_id"],
            mindie_activation=lease["mindie_activation"],
        )
        with (
            patch.object(
                runtime_call,
                "resolve_lease",
                return_value={"session": lease["mindie_session_id"]},
            ),
            patch.object(runtime_call, "finish_outcome", side_effect=record_finish),
            patch("mindie_knowledge.loop.cli.ensure_service", return_value={}),
            patch(
                "mindie_knowledge.loop.transport.rpc",
                side_effect=RequestRejected("bad ref"),
            ),
        ):
            with self.assertRaises(RequestRejected):
                runtime_call.call(payload)
        self.assertEqual(finished, [False])
        finished.clear()
        read_payload = dict(payload, name="knowledge_explain")
        with (
            patch.object(
                runtime_call,
                "resolve_lease",
                return_value={"session": lease["mindie_session_id"]},
            ),
            patch.object(runtime_call, "finish_outcome", side_effect=record_finish),
            patch("mindie_knowledge.loop.cli.ensure_service", return_value={}),
            patch(
                "mindie_knowledge.loop.transport.rpc",
                side_effect=RequestRejected("bad ref"),
            ),
        ):
            result = runtime_call.call(read_payload)
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["execution"], "not_started")
        self.assertEqual(finished, [])

    def test_consecutive_failures_are_per_session_and_never_pause(self):
        a, b = self.activate(), self.activate("manual-B")
        gate = mcp_gate.Gate("knowledge")
        with patch.object(mcp_gate, "run", side_effect=ValueError("bad runtime response")) as run:
            for i in range(20):
                self.assertTrue(gate.call(self.request(a, ident=i))["isError"])
            # Failure counts never gate: every admitted call still dispatches.
            self.assertEqual(run.call_count, 20)
            gate.call(self.request(b, ident=21))
            self.assertEqual(run.call_count, 21)
            # Re-binding keeps the token; only an explicit revoke rotates it.
            renewed = self.activate()
            self.assertEqual(a["mindie_activation"], renewed["mindie_activation"])
            self.sessions.deactivate()
            renewed = self.activate()
            self.assertNotEqual(a["mindie_activation"], renewed["mindie_activation"])
            gate.call(self.request(renewed, ident=22))
            self.assertEqual(run.call_count, 22)

    def test_deactivation_rejects_and_config_bytes_do_not_revoke(self):
        lease = self.activate()
        self.sessions.deactivate()
        with self.assertRaises(Inactive):
            self.sessions.check(lease["mindie_session_id"], lease["mindie_activation"])
        lease = self.activate()
        self.engine.write_text(
            json.dumps(
                dict(
                    root=str(self.root / "data"),
                    domain="changed",
                    admission_path=str(self.admission),
                )
            )
        )
        self.assertEqual(
            self.sessions.check("manual-A", lease["mindie_activation"])["session"],
            "manual-A",
        )

    def test_parallel_hook_claims_admit_once_across_instances(self):
        self.activate()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _: Sessions().claim("manual-A", "stop", "same-turn"),
                    range(110),
                )
            )
        self.assertEqual(sum(results), 1)
        self.assertFalse(Sessions().claim("manual-A", "stop", "same-turn"))

    def attempts(self, session="manual-A"):
        db = sqlite3.connect(self.sessions.path)
        try:
            return db.execute(
                "SELECT count(*) FROM attempts WHERE session=?", (session,)
            ).fetchone()[0]
        except sqlite3.Error:
            return 0
        finally:
            db.close()

    def test_hook_delivery_is_once_and_not_for_other_sessions(self):
        from mindie_knowledge.loop.store import Store

        self.enable_sharing()
        self.activate()
        store = Store(self.root / "data", "test")
        store.close()
        for event in [self.event("other"), self.event(), self.event()]:
            result = self.bridge("stop", event)
            self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        db = sqlite3.connect(self.root / "data" / "test" / "store-v3.sqlite3")
        try:
            count = db.execute("SELECT count(*) FROM captures").fetchone()[0]
            other = db.execute(
                "SELECT count(*) FROM captures WHERE session=?", ("other",)
            ).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(count, 1)
        self.assertEqual(other, 0)
        self.assertEqual(self.attempts(), 0)

    def test_repeated_stop_keeps_one_capture_without_burning_a_claim(self):
        from mindie_knowledge.loop.store import Store

        self.enable_sharing()
        self.activate()
        Store(self.root / "data", "test").close()
        started = time.monotonic()
        result = self.bridge("stop", self.event())
        self.assertLess(time.monotonic() - started, 1.9)
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        self.bridge("stop", self.event())
        db = sqlite3.connect(self.root / "data" / "test" / "store-v3.sqlite3")
        try:
            count = db.execute("SELECT count(*) FROM captures").fetchone()[0]
        finally:
            db.close()
        self.assertEqual(count, 1)
        self.assertEqual(self.attempts(), 0)

    def test_slow_capture_helper_is_killed_within_host_budget(self):
        marker = self.runtime_fixture(delay=20)
        self.enable_sharing()
        self.activate()
        started = time.monotonic()
        result = self.bridge("stop", self.event(), timeout=3)
        self.assertLess(time.monotonic() - started, 1.9)
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        self.assertEqual(marker.read_text().splitlines(), ["attempt"])

    def test_corrupt_activation_state_fails_closed(self):
        marker = self.runtime_fixture()
        self.sessions.path.write_text("corrupt")
        result = self.bridge("stop", self.event())
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        self.assertFalse(marker.exists())

    def test_absolute_deadline_output_bound_and_pre_cancel(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            bounded_process.run(
                [sys.executable, "-c", "import time; time.sleep(20)"], "", timeout=0.2
            )
        self.assertLess(time.monotonic() - started, 1)
        with self.assertRaises(ValueError):
            bounded_process.run(
                [sys.executable, "-c", "print('x'*100000)"],
                "",
                timeout=2,
                max_output=1024,
            )
        event = threading.Event()
        event.set()
        with (
            patch.object(bounded_process.subprocess, "Popen") as popen,
            self.assertRaises(RuntimeError),
        ):
            bounded_process.run(["not-started"], "", timeout=1, cancel=event)
        popen.assert_not_called()

    def test_timeout_kills_owned_descendants(self):
        marker = self.root / "grandchild-survived"
        child = (
            "import time\n"
            "from pathlib import Path\n"
            "time.sleep(0.8)\n"
            f"Path({str(marker)!r}).write_text('survived')\n"
        )
        code = (
            "import subprocess,sys,time\n"
            f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
            "time.sleep(20)\n"
        )
        with self.assertRaises(TimeoutError):
            bounded_process.run([sys.executable, "-c", code], "", timeout=0.3)
        time.sleep(1)
        self.assertFalse(marker.exists(), "owned grandchild outlived timeout cleanup")

    def test_mcp_protocol_call_and_cancellation(self):
        marker = self.runtime_fixture(delay=20)
        lease = self.activate()
        process = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "bridge.py"), "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            process.stdin.write((json.dumps(self.request(lease)) + "\n").encode())
            process.stdin.flush()
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            cancel = dict(
                jsonrpc="2.0",
                method="notifications/cancelled",
                params=dict(requestId=1),
            )
            process.stdin.write((json.dumps(cancel) + "\n").encode())
            process.stdin.flush()
            messages = queue.Queue()
            reader = threading.Thread(
                target=lambda: messages.put(process.stdout.readline()), daemon=True
            )
            reader.start()
            line = messages.get(timeout=2)
            self.assertTrue(line)
            result = json.loads(line)
            self.assertTrue(result["result"]["isError"])
            self.assertEqual(marker.read_text().splitlines(), ["attempt"])
        finally:
            process.stdin.close()
            process.wait(timeout=2)
            process.stdout.close()
            process.stderr.close()

    def test_custom_config_helper_does_not_touch_env_default_state(self):
        decoy = self.root / "decoy-home"
        decoy.mkdir()
        default_config = decoy / "codex.json"
        default_engine = decoy / "engine.json"
        default_admission = decoy / "codex.admission.sqlite3"
        default_engine.write_text(
            json.dumps(dict(root=str(decoy / "data"), domain="decoy"))
        )
        default_config.write_text(
            json.dumps(
                dict(
                    python=sys.executable,
                    engine_config=str(default_engine),
                    admission_path=str(default_admission),
                    runtime_scripts=str(SCRIPTS),
                )
            )
        )
        custom = Sessions(self.config)
        with (
            patch.dict(
                os.environ,
                MINDIE_AGENT_CONFIG=str(default_config),
                CODEX_THREAD_ID="custom-task",
            ),
            patch.object(Path, "cwd", return_value=self.root),
        ):
            lease = custom.activate()
        self.assertEqual(lease["mindie_session_id"], "custom-task")
        self.assertTrue(self.admission.is_file())
        self.assertFalse(default_admission.exists())
        self.assertEqual(sorted(p.name for p in decoy.iterdir()), ["codex.json", "engine.json"])

    def test_offline_status_and_init_do_not_start_a_service(self):
        result = self.bridge("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["service"]["state"], "not-running")
        self.assertEqual(
            payload["first_use"]["choices"], []
        )
        self.assertFalse((self.root / "data").exists())
        init = self.bridge("init")
        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(json.loads(init.stdout)["service"]["state"], "not-running")
        recorded = self.bridge("sharing-disable")
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertEqual(json.loads(recorded.stdout)["sharing_choice"], "disabled")
        again = json.loads(self.bridge("init").stdout)
        self.assertIsNone(again["first_use"])
        self.assertFalse((self.root / "data").exists())

    def test_entry_activation_migrates_legacy_state_once(self):
        # Legacy install shape: adapter-config choice key plus a legacy
        # per-adapter community file as the configured pointer.
        community = self.root / "codex.community.json"
        current = json.loads(self.config.read_text())
        self.write_config(python=current["python"], sharing_choice="read-only")
        community.write_text(
            json.dumps(
                dict(
                    schema="mindie-community-config/1",
                    enabled=False,
                    repository="owner/repo",
                    project_roots=[],
                    idle_seconds=300,
                )
            )
        )
        consent_file = self.root / "mindie-consent.json"
        shared = self.root / "mindie-community.json"
        self.assertFalse(consent_file.exists())
        self.assertFalse(shared.exists())
        result = self.bridge("activate")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        # The entry boundary migrated once and reports it verifiably.
        self.assertEqual(payload["migration"]["consent"]["status"], "migrated")
        self.assertEqual(payload["migration"]["consent"]["choice"], "read-only")
        self.assertEqual(payload["migration"]["community"]["status"], "adopted")
        self.assertEqual(payload["scripts"], str(SCRIPTS))
        saved = json.loads(consent_file.read_text())
        self.assertEqual(saved["choice"], "read-only")
        adapter = json.loads(self.config.read_text())
        self.assertEqual(adapter["community_config"], str(shared))
        engine = json.loads(self.engine.read_text())
        self.assertEqual(engine["community_config"], str(shared))
        settings = json.loads(shared.read_text())
        self.assertEqual(settings["consent_config"], str(consent_file))
        self.assertTrue(community.exists())  # legacy kept as evidence
        # One authority for every consumer: the adapter's read path and the
        # worker-facing engine config resolve to the same file.
        import sharing as sharing_mod

        self.assertEqual(sharing_mod.configured_path(), shared)
        self.assertEqual(engine["community_config"], str(shared))
        # Second entry: nothing migrated, no re-ask, same binding.
        again = json.loads(self.bridge("activate").stdout)
        self.assertNotIn("migration", again)
        self.assertEqual(again["mindie_session_id"], payload["mindie_session_id"])
        self.assertEqual(again["mindie_activation"], payload["mindie_activation"])

    def test_entry_activation_reports_legacy_choice_conflict(self):
        community = self.root / "codex.community.json"
        current = json.loads(self.config.read_text())
        self.write_config(python=current["python"], sharing_choice="read-only")
        community.write_text(
            json.dumps(
                dict(
                    schema="mindie-community-config/1",
                    enabled=True,
                    generation="g1",
                    enabled_at=1.0,
                    repository="owner/repo",
                    project_roots=[str(self.root)],
                    idle_seconds=300,
                )
            )
        )
        result = self.bridge("activate")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        # Conflicting legacy evidence is diagnosed, never guessed; the
        # read-only binding still succeeds and no consent is written.
        self.assertEqual(payload["migration"]["consent"]["status"], "conflict")
        self.assertFalse((self.root / "mindie-consent.json").exists())
        self.assertEqual(payload["migration"]["community"]["status"], "adopted")

    def test_init_without_config_returns_first_use_choices(self):
        absent = self.root / "missing-codex.json"
        env = {
            key: value
            for key, value in os.environ.items()
            if key != "PYTHONPATH"
        }
        env["MINDIE_AGENT_CONFIG"] = str(absent)
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "bridge.py"), "init"],
            text=True,
            capture_output=True,
            timeout=5,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["configured"])
        self.assertEqual(
            payload["first_use"]["choices"], []
        )
        self.assertIn("setup.py", payload["next"])
        self.assertEqual(payload["service"]["state"], "not-running")
        self.assertFalse(absent.exists())
        self.assertFalse((self.root / "data").exists())


if __name__ == "__main__":
    unittest.main()
