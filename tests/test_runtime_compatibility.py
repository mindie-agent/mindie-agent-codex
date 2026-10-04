"""Run the real installed import probe with changed reviewed tuning values."""
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'))
from runtime_probe import build_probe_script
import setup as setup_script


class Probe:
    def __init__(self, tuning):
        self.tuning = tuning
    def probe_runtime(self, python):
        prefix = ('from mindie_knowledge.materials import summarizer as S\n'
                  'import mindie_knowledge.loop.cli as C\n' + self.tuning + '\n')
        result = subprocess.run([python, '-c', prefix + build_probe_script(Path(setup_script.SCRIPTS) / 'codex_transcript.py')], env=os.environ,
                                capture_output=True, text=True, timeout=15)
        if result.returncode or result.stdout.strip() != "OK":
            raise RuntimeError(result.stderr + result.stdout)
        return result.stdout


class RuntimeCompatibilityTests(unittest.TestCase):
    def test_reviewed_positive_bounds_are_not_frozen_at_old_tuning(self):
        Probe('S.MAX_PROMPT_BYTES=65536; S.MAX_RESPONSE_BYTES=16384').probe_runtime(sys.executable)

    def test_missing_or_unbounded_safety_contract_still_fails(self):
        for tuning in (
            'S.MAX_PROMPT_BYTES=-1', 'S.MAX_PROMPT_BYTES=True',
            'S.MAX_PROMPT_BYTES=float("inf")', 'C.ensure_service=None',
            'S.SummaryLedger.record=None',
            'S.MAX_RESPONSE_BYTES=2*S.MAX_PROMPT_BYTES',
        ):
            with self.subTest(tuning=tuning), self.assertRaises(RuntimeError):
                Probe(tuning).probe_runtime(sys.executable)

    def test_missing_retrieval_dependency_is_rejected_before_installation(self):
        with self.assertRaisesRegex(RuntimeError, "pinned runtime import"):
            Probe("import sys; sys.modules['mindie_knowledge.materials.reme_index'] = None").probe_runtime(sys.executable)

    def test_unreviewed_langmem_version_is_rejected_before_installation(self):
        with self.assertRaisesRegex(RuntimeError, "LangMem version differs"):
            Probe("S.LANGMEM_VERSION='0.0.0'").probe_runtime(sys.executable)

    def test_admission_inspection_is_part_of_the_runtime_contract(self):
        with self.assertRaisesRegex(RuntimeError, "Admission API is incomplete"):
            Probe(
                "from mindie_knowledge.loop.activation import Admission as A; "
                "A.inspect = None"
            ).probe_runtime(sys.executable)

    def test_missing_history_import_is_rejected_before_installation(self):
        with self.assertRaisesRegex(RuntimeError, 'explicit history import is unavailable'):
            Probe('import mindie_knowledge.loop.history_import as H; H.import_transcript = None').probe_runtime(sys.executable)

    def test_unmodified_pinned_runtime_passes_candidate_private_probe(self):
        # The product envelope and setup boundary have independent tests.
        self.assertEqual(Probe('').probe_runtime(sys.executable).strip(), 'OK')

    def test_missing_lifetime_observation_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "lock_held"):
            Probe("from mindie_knowledge.loop import locks; del locks.lock_held").probe_runtime(sys.executable)
