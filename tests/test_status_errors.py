import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"


class StatusErrors(unittest.TestCase):
    def call(self, path):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "bridge.py"), "--config", str(path), "status"],
            text=True, capture_output=True, timeout=7,
        )
        return result, json.loads(result.stdout)

    def test_only_missing_configuration_offers_first_use(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adapter.json"
            process, payload = self.call(path)
            self.assertEqual(process.returncode, 0)
            self.assertTrue(payload["first_use"])
            self.assertEqual(list(Path(directory).iterdir()), [])
            path.write_text('{PRIVATE_CONFIG_MARKER')
            process, payload = self.call(path)
            self.assertEqual(process.returncode, 1)
            self.assertEqual(payload["status"], "invalid_config")
            self.assertIsNone(payload["first_use"])
            self.assertIsNot(payload["configured"], False)
            self.assertNotIn("PRIVATE_CONFIG_MARKER", process.stdout + process.stderr)

    @unittest.skipUnless(os.name == "posix", "requires a real POSIX lock")
    def test_update_lock_is_busy_not_missing(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adapter.json"
            path.write_text("{}")
            with path.with_suffix(".update.lock").open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                process, payload = self.call(path)
            self.assertEqual(process.returncode, 1)
            self.assertEqual(payload["status"], "update_busy")
            self.assertIsNone(payload["first_use"])

    def test_selected_helper_failure_has_no_raw_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "service_control.py").write_text(
                "import sys; print('PRIVATE_PROVIDER_MARKER', file=sys.stderr); raise SystemExit(7)"
            )
            path = root / "adapter.json"
            path.write_text(json.dumps(dict(
                python=sys.executable, engine_config=str(root / "engine.json"),
                runtime_scripts=str(root),
            )))
            process, payload = self.call(path)
            self.assertEqual(process.returncode, 1)
            self.assertEqual(payload["status"], "helper_failed")
            self.assertEqual(payload["error"]["stage"], "helper_run")
            self.assertEqual(payload["commands"]["status"][1], str(root / "bridge.py"))
            self.assertNotIn("PRIVATE_PROVIDER_MARKER", process.stdout + process.stderr)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO")
    def test_fifo_configuration_fails_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adapter.json"
            os.mkfifo(path)
            process, payload = self.call(path)
            self.assertEqual(process.returncode, 1)
            self.assertEqual(payload["status"], "invalid_config")
            self.assertEqual(payload["error"]["stage"], "config_read")
            self.assertIsNone(payload["first_use"])

    def test_oversized_configuration_is_a_safe_config_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adapter.json"
            path.write_text('"' + 'PRIVATE_MARKER' * 6000 + '"')
            process, payload = self.call(path)
            self.assertEqual(process.returncode, 1)
            self.assertEqual(payload["status"], "invalid_config")
            self.assertNotIn("PRIVATE_MARKER", process.stdout + process.stderr)


class ScopedStatus(unittest.TestCase):
    def test_paused_task_records_are_scoped_and_read_only(self):
        from mindie_knowledge.loop.activation import Admission
        from mindie_knowledge.loop.store import Store, session_key
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = dict(root=str(root / "data"), domain="test",
                          admission_path=str(root / "admission.sqlite3"))
            (root / "engine.json").write_text(json.dumps(engine))
            config = root / "adapter.json"
            config.write_text(json.dumps(dict(
                python=sys.executable, engine_config=str(root / "engine.json"),
                runtime_scripts=str(SCRIPTS), admission_path=engine["admission_path"],
                community_config=str(root / "community.json"), sharing_choice="read-only",
            )))
            admission = Admission(engine["admission_path"])
            admission.activate("own-task", project_root=str(root))
            with sqlite3.connect(engine["admission_path"]) as db:
                db.execute("UPDATE leases SET failures=3 WHERE session='own-task'")
            store = Store(engine["root"], "test")
            try:
                for task in ("own-task", "other-task"):
                    capture = store.add_capture(
                        root_session=session_key(task), session=task, turn="1",
                        transcript=None, summary="PRIVATE_TRANSCRIPT_MARKER",
                    )
                    store.mark_capture(capture["id"], "failed", "PRIVATE_PROVIDER_MARKER")
                    if task == "own-task":
                        own_id = capture["id"]
                    owner = store.opaque_for(session_key(task))
                    with store.db:
                        store.db.execute(
                            "INSERT INTO outbox(batch_id,revision,batch,status,detail,created,updated) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (task + "-batch", "rev", "{}", "unknown", "PRIVATE_PROVIDER_MARKER", 1, 1),
                        )
                        store.db.execute("INSERT INTO votes VALUES(?,?,?,?,?,?,?,?)",
                                         (owner, "entry", "rev", "up", "private", 1, task + "-batch", 1))
            finally:
                store.close()
            state = root / "data/test/store-v3.sqlite3"
            before = state.read_bytes(), Path(engine["admission_path"]).read_bytes()
            base_env = {key: value for key, value in os.environ.items()
                        if key not in {"CODEX_THREAD_ID", "PYTHONPATH"}}
            for task in ("own-task", None, "invalid identity"):
                env = dict(base_env)
                if task is not None:
                    env["CODEX_THREAD_ID"] = task
                process = subprocess.run(
                    [sys.executable, str(SCRIPTS / "bridge.py"), "--config", str(config), "status"],
                    env=env, text=True, capture_output=True, timeout=5,
                )
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertNotIn("PRIVATE_", process.stdout + process.stderr)
                self.assertNotIn("other-task-batch", process.stdout)
                payload = json.loads(process.stdout)
                self.assertNotIn("outbox", payload["service"])
                self.assertNotIn("token", payload["admission"])
                if task == "own-task":
                    self.assertEqual(payload["admission"]["status"], "paused")
                    self.assertEqual([row["id"] for row in payload["captures"]], [own_id])
                    self.assertEqual([row["batch_id"] for row in payload["contributions"]], ["own-task-batch"])
                    self.assertEqual(payload["commands"]["deactivate"][0], sys.executable)
                else:
                    self.assertEqual(payload["captures"], [])
                    self.assertEqual(payload["contributions"], [])
                self.assertEqual((state.read_bytes(), Path(engine["admission_path"]).read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
