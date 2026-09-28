"""Run the real installed import probe with changed reviewed tuning values."""
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'))
from auto_update import Updater
import setup as setup_script


class Probe(Updater):
    def __init__(self, tuning):
        self.tuning = tuning
    def command(self, argv, **kwargs):
        prefix = ('from mindie_knowledge.loop.budget import MaintenanceBudget as B\n'
                  'import mindie_knowledge.loop.cli as C\n' + self.tuning + '\n')
        result = subprocess.run([argv[0], '-c', prefix + argv[2]], env=os.environ,
                                capture_output=True, text=True, timeout=kwargs['timeout'])
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout


class RuntimeCompatibilityTests(unittest.TestCase):
    def test_reviewed_positive_bounds_are_not_frozen_at_old_tuning(self):
        Probe('B.SESSION_LIMIT=8; B.HOURLY_LIMIT=40; '
              'B.SESSION_WINDOW=7200; C.STARTUP_TIMEOUT=9; C.MAX_STARTUP_PROBES=5').probe_runtime(sys.executable)

    def test_missing_or_unbounded_safety_contract_still_fails(self):
        for tuning in (
            'B.SESSION_LIMIT=0', 'B.HOURLY_LIMIT=-1', 'B.SESSION_LIMIT=True',
            'B.SESSION_WINDOW=float("inf")', 'C.STARTUP_TIMEOUT=float("nan")',
            'C.MAX_STARTUP_PROBES=0', 'del B.SESSION_LIMIT',
        ):
            with self.subTest(tuning=tuning), self.assertRaises(RuntimeError):
                Probe(tuning).probe_runtime(sys.executable)

    def test_admission_inspection_is_part_of_the_runtime_contract(self):
        with self.assertRaisesRegex(RuntimeError, "Admission API is incomplete"):
            Probe(
                "from mindie_knowledge.loop.activation import Admission as A; "
                "A.inspect = None"
            ).probe_runtime(sys.executable)

    def test_unmodified_pinned_runtime_passes_setup_and_update_probes(self):
        # This is deliberately the real installed interpreter and unmodified
        # pinned core API. No injected compatibility attributes are present.
        setup_script.probe_runtime(sys.executable)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            settings = base / 'updater.json'
            settings.write_text(json.dumps({
                'root': str(base / 'updates'),
                'adapter_config': str(base / 'codex.json'),
                'codex_home': str(base / 'codex'),
                'codex': 'codex-fixture',
            }))
            Updater(settings).probe_runtime(sys.executable)

    def test_missing_lifetime_observation_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "locks lacks lock_held"):
            Probe("from mindie_knowledge.loop import locks; del locks.lock_held").probe_runtime(sys.executable)
