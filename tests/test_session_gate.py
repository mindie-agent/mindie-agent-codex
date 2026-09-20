"""No model calls: session isolation, protocol discovery and failure bounds."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import selectors
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

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
        self.engine.write_text(
            json.dumps(dict(root=str(self.root / "data"), domain="test"))
        )
        self.config.write_text(
            json.dumps(dict(python=sys.executable, engine_config=str(self.engine)))
        )
        self.environment = patch.dict(
            os.environ, MINDIE_AGENT_CONFIG=str(self.config), CODEX_THREAD_ID="manual-A"
        )
        self.environment.start()
        self.sessions = Sessions()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

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
        config = json.loads(self.config.read_text())
        config["community_config"] = str(community)
        self.config.write_text(json.dumps(config))
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

    def bridge(self, operation, event=None, timeout=3):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "bridge.py"), operation],
            input=json.dumps(event or {}),
            text=True,
            capture_output=True,
            timeout=timeout,
            cwd=str(self.root),
        )

    def runtime_fixture(self, *, delay=0, hook=False):
        marker = self.root / "invocations"
        runner = self.root / "runtime"
        runner.write_text(
            f"#!{sys.executable}\nimport sys,time\nfrom pathlib import Path\np=Path({str(marker)!r})\nwith p.open('a') as f: f.write('attempt\\n')\nsys.stdin.read()\ntime.sleep({delay})\nprint('{{}}')\n"
        )
        runner.chmod(0o755)
        python = str(runner)
        if hook:
            # Use an already installed interpreter for the 1.2s hook boundary.
            # macOS may delay the first exec of a newly written executable while
            # assessing it; that is unrelated to the hook delivery protocol.
            package = self.root / "imports/mindie_knowledge/loop"
            package.mkdir(parents=True)
            (package.parent / "__init__.py").touch()
            (package / "__init__.py").touch()
            (package / "cli.py").write_text(runner.read_text().split("\n", 1)[1])
            env = patch.dict(os.environ, PYTHONPATH=str(self.root / "imports"))
            env.start()
            self.addCleanup(env.stop)
            python = sys.executable
        self.config.write_text(
            json.dumps(dict(python=python, engine_config=str(self.engine)))
        )
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
            (skill / "agents/openai.yaml").read_text(),
        )
        self.assertNotIn(
            "SessionStart",
            json.loads((SCRIPTS.parent / "hooks/hooks.json").read_text())["hooks"],
        )

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
            {k: again[k] for k in ("mindie_session_id", "mindie_activation", "expires_at")},
            {k: lease[k] for k in ("mindie_session_id", "mindie_activation", "expires_at")},
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
        self.assertEqual(self.sessions.path.stat().st_mode & 0o777, 0o600)

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
            run.assert_not_called()

    def test_explain_is_gated_and_query_cannot_implicitly_activate(self):
        with patch.object(mcp_gate, "run") as run:
            gate = mcp_gate.Gate("knowledge")
            self.assertTrue(gate.call(self.request())["isError"])
            self.assertTrue(
                gate.call(self.request(name="knowledge_explain", ref="x"))["isError"]
            )
            run.assert_not_called()
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
            self.assertEqual(payload["mindie_session_id"], "manual-A")
            self.assertEqual(run.call_args.kwargs["timeout"], 65)

    def test_consecutive_failure_circuit_is_per_session_and_explicitly_reset(self):
        a, b = self.activate(), self.activate("manual-B")
        gate = mcp_gate.Gate("knowledge")
        with patch.object(mcp_gate, "run", side_effect=TimeoutError("stalled")) as run:
            for i in range(20):
                self.assertTrue(gate.call(self.request(a, ident=i))["isError"])
            self.assertEqual(run.call_count, 3)
            gate.call(self.request(b, ident=21))
            self.assertEqual(run.call_count, 4)
            renewed = self.activate()
            self.assertNotEqual(a["mindie_activation"], renewed["mindie_activation"])
            gate.call(self.request(renewed, ident=22))
            self.assertEqual(run.call_count, 5)

    def test_deactivation_expiry_and_config_change_reject_before_dispatch(self):
        lease = self.activate()
        self.sessions.deactivate()
        with self.assertRaises(Inactive):
            self.sessions.check(lease["mindie_session_id"], lease["mindie_activation"])
        lease = self.activate()
        with sqlite3.connect(self.sessions.path) as db:
            db.execute("UPDATE leases SET expires=0")
        with self.assertRaises(Inactive):
            self.sessions.check("manual-A", lease["mindie_activation"])
        lease = self.activate()
        self.engine.write_text('{"domain":"changed"}')
        with self.assertRaises(Inactive):
            self.sessions.check("manual-A", lease["mindie_activation"])

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

    def test_hook_delivery_is_once_and_not_for_other_sessions(self):
        marker = self.runtime_fixture(hook=True)
        self.enable_sharing()
        self.activate()
        for event in [self.event("other"), self.event(), self.event()]:
            result = self.bridge("stop", event)
            self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        self.assertEqual(marker.read_text().splitlines(), ["attempt"])

    def test_timed_out_hook_consumes_attempt_and_always_finishes(self):
        marker = self.runtime_fixture(delay=20, hook=True)
        self.enable_sharing()
        self.activate()
        started = time.monotonic()
        result = self.bridge("stop", self.event())
        self.assertLess(time.monotonic() - started, 1.9)
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        self.bridge("stop", self.event())
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
        marker = self.root / "child-pid"
        code = f"import os,time\npid=os.fork()\nif pid:\n open({str(marker)!r},'w').write(str(pid))\ntime.sleep(20)\n"
        with self.assertRaises(TimeoutError):
            bounded_process.run([sys.executable, "-c", code], "", timeout=0.3)
        pid = marker.read_text()
        result = subprocess.run(
            ["ps", "-p", pid, "-o", "stat="], capture_output=True, text=True
        )
        self.assertTrue(result.returncode != 0 or result.stdout.strip().startswith("Z"))

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
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(2))
            result = json.loads(process.stdout.readline())
            self.assertTrue(result["result"]["isError"])
            self.assertEqual(marker.read_text().splitlines(), ["attempt"])
        finally:
            process.stdin.close()
            process.wait(timeout=2)
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
