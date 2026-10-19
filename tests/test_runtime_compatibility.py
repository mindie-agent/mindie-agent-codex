"""Run the real installed import probe with changed reviewed tuning values."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'))
from runtime_probe import build_probe_script
import setup as setup_script

PROBE_SCRIPT = build_probe_script(Path(setup_script.SCRIPTS) / 'codex_transcript.py')


class Probe:
    def __init__(self, tuning, *, use_subprocess=False):
        self.tuning = tuning
        self.use_subprocess = use_subprocess

    def probe_runtime(self, python):
        prefix = ('from mindie_knowledge.materials import summarizer as S\n'
                  'import mindie_knowledge.loop.cli as C\n' + self.tuning + '\n')
        if self.use_subprocess:
            result = subprocess.run([python, '-c', prefix + PROBE_SCRIPT], env=os.environ,
                                    capture_output=True, text=True, timeout=15)
            if result.returncode or result.stdout.strip() != "OK":
                raise RuntimeError(result.stderr + result.stdout)
            return result.stdout

        from mindie_knowledge.materials import summarizer as S
        import mindie_knowledge.loop.cli as C
        from mindie_knowledge.loop.activation import Admission as A
        import mindie_knowledge.loop.history_import as H
        import mindie_knowledge.filesystem as F
        import mindie_knowledge.loop.service_context as SC
        import mindie_knowledge.loop.summary_scratch as SS
        import mindie_knowledge.git_layout as G
        from mindie_knowledge.loop import locks as L
        import mindie_knowledge.materials.reme_index as RI

        snapshots = [
            (S, dict(S.__dict__)),
            (S.SummaryLedger, dict(S.SummaryLedger.__dict__)),
            (C, dict(C.__dict__)),
            (A, dict(A.__dict__)),
            (H, dict(H.__dict__)),
            (F, dict(F.__dict__)),
            (SC, dict(SC.__dict__)),
            (SS, dict(SS.__dict__)),
            (G, dict(G.__dict__)),
            (L, dict(L.__dict__)),
        ]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                exec(prefix + PROBE_SCRIPT, {})
        except BaseException as exc:
            raise RuntimeError(f"pinned runtime import: {exc}\n{buf.getvalue()}") from exc
        finally:
            sys.modules['mindie_knowledge.materials.reme_index'] = RI
            for target, saved in snapshots:
                for k in list(target.__dict__):
                    if k not in saved:
                        try:
                            delattr(target, k)
                        except (AttributeError, TypeError):
                            pass
                for k, v in saved.items():
                    if target.__dict__.get(k) is not v:
                        try:
                            setattr(target, k, v)
                        except (AttributeError, TypeError):
                            pass

        out = buf.getvalue()
        if out.strip() != "OK":
            raise RuntimeError(out)
        return out


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

    def test_unmodified_pinned_runtime_passes_candidate_private_probe(self):
        self.assertEqual(Probe('', use_subprocess=True).probe_runtime(sys.executable).strip(), 'OK')


if __name__ == '__main__':
    unittest.main()
