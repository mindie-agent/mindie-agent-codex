"""Failure evidence and teardown boundaries for the lifetime component fixture."""
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tests.process_fixtures import (
    owned_fixture_process, public_engine_config, run_owned_fixture,
    stop_owned_knowledge_service,
)
from mindie_knowledge.loop.locks import StartLock


class ServiceEntryFixtureTests(unittest.TestCase):
    def test_watchdog_retains_output_and_stage_before_owned_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            ready = Path(temp) / "ready"

            @contextmanager
            def prepared_process(command, **kwargs):
                with owned_fixture_process(command, **kwargs) as process:
                    # Preparation has its own watchdog; run_owned_fixture's
                    # tested deadline starts only after this context yields.
                    deadline = time.monotonic() + 20
                    while not ready.is_file():
                        self.assertIsNone(process.poll(), "child exited before ready handshake")
                        if time.monotonic() >= deadline:
                            self.fail("child did not complete fixture ready handshake")
                        time.sleep(.01)
                    yield process

            with patch("tests.process_fixtures.owned_fixture_process", prepared_process):
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    run_owned_fixture(
                        [sys.executable, "-u", "-c",
                         "import sys,time; from pathlib import Path; "
                         "print('partial-out'); print('partial-error',file=sys.stderr); "
                         f"Path({str(ready)!r}).write_text('ready'); "
                         "sys.stdin.buffer.read(1); time.sleep(30)"],
                        input="\n", timeout=2, diagnose=lambda: {"startup": "ready"},
                    )
        self.assertIn("partial-out", caught.exception.output)
        self.assertIn("partial-error", caught.exception.stderr)
        self.assertTrue(any('"startup": "ready"' in note for note in caught.exception.__notes__))

    def test_absent_layout_is_empty_only_without_startup_or_partial_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            engine = root / "engine.json"
            engine.write_text(json.dumps(public_engine_config(root / "data")))
            stop_owned_knowledge_service(engine)
            domain = root / "data/test"
            with StartLock(domain / "start.lock"):
                with self.assertRaisesRegex(RuntimeError, "startup lock"):
                    stop_owned_knowledge_service(engine, timeout=.05)
            (domain / "state-v1").mkdir()
            with self.assertRaisesRegex(ValueError, "layout is missing"):
                stop_owned_knowledge_service(engine)
            (domain / "state-layout.json").write_text("{broken")
            with self.assertRaises(ValueError):
                stop_owned_knowledge_service(engine)


if __name__ == "__main__":
    unittest.main()
