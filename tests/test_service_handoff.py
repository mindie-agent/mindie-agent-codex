"""Real service lifetime and OS lock evidence, independent of TCP timing."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"

def make_config(root):
    engine = root / "engine.json"
    engine.write_text(json.dumps({"root": str(root / "data"), "domain": "test"}))
    adapter = root / "adapter.json"
    adapter.write_text(json.dumps({"engine_config": str(engine)}))
    return adapter

sys.path.insert(0, str(SCRIPTS))
import service_handoff
from mindie_knowledge.loop.cli import connection_path, rpc
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
                        if lock_held(consumer) is True:
                            try:
                                connection = service_handoff.connect(config)
                                status = rpc(connection, "status", timeout=.3)
                                if status.get("admission_frozen") is False:
                                    break
                            except (OSError, ValueError):
                                pass
                        time.sleep(.05)
                    else:
                        self.fail("service readiness deadline")
                    result = service_handoff.stop(engine_path)
                    self.assertEqual(result, {"idle": True, "service": "stopped"})
                    self.assertIs(lock_held(consumer), False)
                    self.assertEqual(process.wait(timeout=3), 0)
                    self.assertEqual(service_handoff.stop(engine_path)["service"], "absent")
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=3)

    def test_idle_acknowledgement_does_not_prove_lock_release(self):
        with tempfile.TemporaryDirectory() as temp:
            adapter = make_config(Path(temp))
            engine_path = json.loads(adapter.read_text())["engine_config"]
            config = json.loads(Path(engine_path).read_text())
            consumer = connection_path(config).with_name("consumer.lock")
            connection = {"url": "http://127.0.0.1:1", "token": "test"}
            with StartLock(consumer), patch.object(service_handoff, "connect", return_value=connection), patch.object(service_handoff, "rpc", return_value={"idle": True}):
                with self.assertRaisesRegex(RuntimeError, "exit unconfirmed"):
                    service_handoff.stop(engine_path)
                self.assertIs(lock_held(consumer), True)


if __name__ == "__main__":
    unittest.main()
