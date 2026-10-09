"""Real service lifetime and OS lock evidence, independent of TCP timing."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from tests.process_fixtures import public_engine_config

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"

def make_config(root):
    engine = root / "engine.json"
    engine.write_text(json.dumps(public_engine_config(root / "data")))
    adapter = root / "adapter.json"
    adapter.write_text(json.dumps({"engine_config": str(engine)}))
    return adapter

sys.path.insert(0, str(SCRIPTS))
import service_handoff
from mindie_knowledge.loop.cli import connection_path, rpc, connect
from mindie_knowledge.loop.locks import StartLock, lock_held


class ServiceHandoffTests(unittest.TestCase):
    def test_real_stop_releases_lifetime_and_allows_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            adapter = make_config(Path(temp))
            engine_path = json.loads(adapter.read_text())["engine_config"]
            config = json.loads(Path(engine_path).read_text())
            consumer = connection_path(config).with_name("consumer.lock")
            processes = []
            try:
                for _ in range(2):
                    process = subprocess.Popen(
                        [sys.executable, "-m", "mindie_knowledge.loop.cli", "serve", "--config", engine_path],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    processes.append(process)
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        self.assertIsNone(process.poll(), "service exited before readiness")
                        # A lock probe briefly takes a free lock. Do not make
                        # the readiness observer compete with service startup.
                        try:
                            connection = connect(config, config_path=engine_path)
                            status = rpc(connection, "status", timeout=.3)
                            if status.get("admission_frozen") is False:
                                self.assertIs(lock_held(consumer), True)
                                break
                        except (OSError, ValueError):
                            pass
                        time.sleep(.05)
                    else:
                        self.fail("service readiness deadline")
                    result = service_handoff.stop(engine_path)
                    self.assertTrue(result["idle"])
                    self.assertEqual(result["service"], "stopped")
                    self.assertEqual(result["retirement"]["status"], "retired")
                    self.assertIs(lock_held(consumer), False)
                    self.assertEqual(process.wait(timeout=3), 0)
                    self.assertEqual(service_handoff.stop(engine_path)["service"], "absent")
                    service_handoff.restore(engine_path, result["retirement"])
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=3)

    def test_retirement_delegates_without_reinterpreting_missing_endpoint(self):
        with patch.object(service_handoff, "retire_service", side_effect=OSError("ownership unconfirmed")):
            with self.assertRaisesRegex(OSError, "ownership unconfirmed"):
                service_handoff.stop("engine.json")

    def test_restore_without_old_listener_still_checks_existing_authorization(self):
        with patch.object(service_handoff, "restore_service", return_value={"status": "not-needed"}), \
             patch.object(service_handoff, "config_at", return_value={"admission_path": "admission.sqlite3"}), \
             patch.object(service_handoff, "Admission") as admission, \
             patch.object(service_handoff, "ensure_service", return_value={"url": "local"}) as ensure, \
             patch.object(service_handoff, "rpc", return_value={"admission_frozen": False}):
            admission.return_value.leases.return_value = [{"enabled": True}]
            self.assertEqual(service_handoff.restore("new-engine.json")["status"], "restored")
            ensure.assert_called_once_with("new-engine.json")


if __name__ == "__main__":
    unittest.main()
