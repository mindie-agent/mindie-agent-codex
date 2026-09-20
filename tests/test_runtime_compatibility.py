"""Run the real installed import probe with changed reviewed tuning values."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'))
from auto_update import Updater


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


def test_reviewed_positive_bounds_are_not_frozen_at_old_tuning():
    Probe('B.SESSION_LIMIT=8; B.HOURLY_LIMIT=40; B.FAILURE_LIMIT=4; '
          'B.SESSION_WINDOW=7200; C.STARTUP_TIMEOUT=9; C.MAX_STARTUP_PROBES=5').probe_runtime(sys.executable)


@pytest.mark.parametrize('tuning', [
    'B.SESSION_LIMIT=0', 'B.HOURLY_LIMIT=-1', 'B.FAILURE_LIMIT=True',
    'B.SESSION_WINDOW=float("inf")', 'C.STARTUP_TIMEOUT=float("nan")',
    'C.MAX_STARTUP_PROBES=0', 'del B.SESSION_LIMIT',
])
def test_missing_or_unbounded_safety_contract_still_fails(tuning):
    with pytest.raises(RuntimeError):
        Probe(tuning).probe_runtime(sys.executable)
