"""Candidate subprocess ownership and fail-closed product receipt boundaries."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import bounded_process
import setup as setup_script
import candidate_validate
import product_contract
from bounded_process import run

RECEIPT = "import json,sys; value=json.loads(sys.argv[sys.argv.index('--identity')+1]); value['status']='validated'; print(json.dumps(value),flush=True)\n"


class RuntimeProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name)
        self.scripts = self.source / "plugins/mindie-agent/scripts"
        self.scripts.mkdir(parents=True)
        for filename in ('product-contract.json', 'pyproject.toml', 'uv.lock'):
            shutil.copyfile(SCRIPTS.parent / filename, self.scripts.parent / filename)
        shutil.copytree(SCRIPTS.parent / '.codex-plugin', self.scripts.parent / '.codex-plugin')
        self.validator = self.scripts / 'candidate_validate.py'
        self.validator.write_text(RECEIPT)

    def invoke(self, *, command=None):
        return product_contract.probe(sys.executable, self.scripts,
            command or (lambda argv, **kw: run(argv, '', **kw).checked_stdout()), revision='a' * 40)

    def test_release_requires_format_and_protection_without_version_ordering(self):
        from mindie_knowledge import state_layout
        import receipt_layout
        plugin = self.scripts.parent
        manifest = plugin / '.codex-plugin/plugin.json'
        manifest.write_text(json.dumps(dict(version='0.0.2')))
        project = plugin / 'pyproject.toml'
        current = tomllib.loads(project.read_text())["project"]["version"]
        project.write_text(project.read_text().replace(f'version = "{current}"', 'version = "0.0.2"'))
        for knowledge, receipt in ((None, None), ('0.0.1', None), (None, '0.0.1'),
                                   ('invalid', '0.0.1')):
            with self.subTest(knowledge=knowledge, receipt=receipt), \
                    patch.object(state_layout, 'RELEASE_VERSION', knowledge), \
                    patch.object(receipt_layout, 'RELEASE_VERSION', receipt):
                with self.assertRaisesRegex(ValueError, 'release_state_undeclared'):
                    candidate_validate.release_check(plugin)
        for knowledge, receipt in (('0.0.1', '0.0.1'), ('4.2.0', '0.0.1'),
                                   ('0.0.1', '9.0.0')):
            with self.subTest(knowledge=knowledge, receipt=receipt), \
                    patch.object(state_layout, 'RELEASE_VERSION', knowledge), \
                    patch.object(receipt_layout, 'RELEASE_VERSION', receipt):
                candidate_validate.release_check(plugin)
        for layout in (state_layout, receipt_layout):
            for invalid in (None, True, 0, '1'):
                with self.subTest(layout=layout.__name__, format=invalid), \
                        patch.object(layout, 'FORMAT', invalid):
                    with self.assertRaisesRegex(ValueError, 'release_state_undeclared'):
                        candidate_validate.release_check(plugin)
        manifest.write_text(json.dumps(dict(version='0.1.0+codex.development')))
        project.write_text(project.read_text().replace('version = "0.0.2"', 'version = "0.1.0+codex.development"'))
        with patch.object(state_layout, 'RELEASE_VERSION', None), \
                patch.object(receipt_layout, 'RELEASE_VERSION', None):
            candidate_validate.release_check(plugin)

    def test_native_manifest_must_match_packaged_version(self):
        manifest = self.scripts.parent / '.codex-plugin/plugin.json'
        version = product_contract.native_version(self.scripts.parent)
        manifest.write_text(json.dumps(dict(version=f'{version}.1')))
        with self.assertRaisesRegex(ValueError, 'versions differ'):
            candidate_validate.release_check(self.scripts.parent)

    def test_candidate_script_owns_validation_not_current_private_probe(self):
        # Candidate may replace its private API completely. The running updater
        # invokes only its candidate entry and verifies its exact receipt.
        (self.scripts / 'runtime_probe.py').write_text('raise RuntimeError("future private module")')
        with patch.object(candidate_validate, 'runtime_check', side_effect=AssertionError('current probe')):
            receipt = self.invoke()
        self.assertEqual(receipt['candidate_revision'], 'a' * 40)
        self.assertEqual(receipt['runtime'], product_contract.requirements(self.source)[0])

    def test_candidate_requires_safe_capture_failure_projection_module(self):
        shutil.copytree(ROOT / "plugins/mindie-agent", self.scripts.parent,
                        dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
        candidate_validate.adapter_check(self.source)
        (self.scripts / "capture_errors.py").unlink()
        with self.assertRaisesRegex(ValueError, "missing bounded runtime entry: capture_errors.py"):
            candidate_validate.adapter_check(self.source)

    def test_candidate_requires_both_exact_native_interrupt_controls(self):
        shutil.copytree(ROOT / "plugins/mindie-agent", self.scripts.parent,
                        dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "build", "*.egg-info"))
        path = self.scripts.parent / "hooks/hooks.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        candidate_validate.adapter_check(self.source)
        for change in ("missing", "server", "turn"):
            with self.subTest(change=change):
                value = json.loads(json.dumps(original))
                entries = value["hooks"]["Interrupt"][0]["hooks"]
                if change == "missing":
                    entries.pop()
                elif change == "server":
                    entries[0]["server"] = "foreign-server"
                else:
                    entries[0]["input"]["turn_id"] = "unscoped-turn"
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Interrupt must target both exact"):
                    candidate_validate.adapter_check(self.source)

    def test_exit1_after_valid_receipt_is_not_success(self):
        self.validator.write_text(RECEIPT + "raise RuntimeError('controlled failure')\n")
        with self.assertRaisesRegex(product_contract.CandidateValidationError, 'process: nonzero_exit'):
            self.invoke()

    def test_candidate_pin_failure_reaches_setup_and_updater_without_stderr(self):
        self.validator.write_text(
            "import sys; from types import SimpleNamespace\n"
            + "sys.path.insert(0, " + repr(str(SCRIPTS)) + ")\n"
            + "import candidate_validate as candidate\n"
            + "candidate.adapter_check = lambda source: None\n"
            + "candidate.importlib.metadata.distribution = lambda name: SimpleNamespace(read_text=lambda _: '{\"vcs_info\":{\"commit_id\":\"wrong\"}}')\n"
            + "print('PRIVATE_STDERR_SENTINEL', file=sys.stderr)\n"
            + "raise SystemExit(candidate.main())\n")
        with self.assertRaises(product_contract.CandidateValidationError) as caught:
            self.invoke()
        error = caught.exception
        self.assertEqual((error.stage, error.code, error.component),
                         ('runtime_pins', 'revision_mismatch', 'mindie-knowledge'))
        self.assertNotIn('PRIVATE_STDERR', str(error))
        with patch.object(setup_script, 'SCRIPTS', self.scripts):
            with self.assertRaises(SystemExit) as setup_error:
                setup_script.probe_runtime(sys.executable)
        self.assertIn('runtime_pins: revision_mismatch', str(setup_error.exception))
        self.assertNotIn('PRIVATE_STDERR', str(setup_error.exception))

    def test_nonzero_failure_mapper_is_identical_in_threaded_pipe_runner(self):
        failure = "value['status']='failed'; value['failure']={'stage':'runtime_api','code':'api_incompatible'}"
        self.validator.write_text(RECEIPT.replace("value['status']='validated'", failure) + "raise SystemExit(1)\n")
        def threaded(argv, **kwargs):
            process = bounded_process._spawn(argv, subprocess.DEVNULL, None)
            return bounded_process._run_windows(process, kwargs['timeout'], kwargs['max_output'],
                                                None, on_failure=kwargs['on_failure'])
        with self.assertRaisesRegex(product_contract.CandidateValidationError, 'runtime_api: api_incompatible'):
            self.invoke(command=threaded)

    def test_failure_receipt_rejects_arbitrary_fields_and_wrong_identity(self):
        for mutation in (
            "value['failure']['code']='PRIVATE_STDERR_SENTINEL'",
            "value['failure']['message']='PRIVATE_STDERR_SENTINEL'",
            "value['source_sha256']='b'*64",
        ):
            failure = "value['status']='failed'; value['failure']={'stage':'runtime_api','code':'api_incompatible'}; " + mutation
            self.validator.write_text(RECEIPT.replace("value['status']='validated'", failure) + "raise SystemExit(1)\n")
            with self.subTest(mutation=mutation), self.assertRaises(product_contract.CandidateValidationError) as caught:
                self.invoke()
            self.assertEqual((caught.exception.stage, caught.exception.code), ('process', 'nonzero_exit'))
            self.assertNotIn('PRIVATE_STDERR', str(caught.exception))

    def test_malformed_wrong_and_duplicate_receipts_fail(self):
        for output in ('{}', 'OK', '{"status":"validated","status":"validated"}'):
            self.validator.write_text('print(' + repr(output) + ')')
            with self.subTest(output=output), self.assertRaises(ValueError):
                self.invoke()
        self.validator.write_text(RECEIPT.replace("value['status']='validated'", "value['status']='validated'; value['runtime']={}"))
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.invoke()

    def test_candidate_output_is_bounded_and_only_explicit_test_deadline_stops_it(self):
        self.validator.write_text("print('x' * 65537)")
        with self.assertRaises(ValueError):
            self.invoke()
        self.validator.write_text('import time; time.sleep(30)')
        def quick_command(argv, **kwargs):
            self.assertIsNone(kwargs['timeout'])
            self.assertEqual(kwargs['max_output'], 65536)
            return run(argv, '', **dict(kwargs, timeout=0.1)).checked_stdout()
        with self.assertRaises(TimeoutError):
            self.invoke(command=quick_command)

    def test_prepared_receipt_cannot_be_reused_after_source_or_pin_changes(self):
        receipt = self.invoke()
        self.validator.write_text(RECEIPT + '# changed\n')
        with self.assertRaisesRegex(ValueError, 'does not match'):
            product_contract.validate_receipt(json.dumps(receipt), product_contract.identity(self.source, 'a' * 40))
        path = self.scripts.parent / 'pyproject.toml'
        path.write_text(path.read_text().replace(product_contract.requirements(self.source)[0]['mindie-knowledge'], 'b' * 40))
        with self.assertRaises(ValueError):
            product_contract.validate_receipt(json.dumps(receipt), product_contract.identity(self.source, 'a' * 40))

    def test_candidate_checks_runtime_pin_independently_of_publication_validator(self):
        expected = product_contract.identity(self.source, 'a' * 40)
        with patch.object(candidate_validate.importlib.metadata, 'distribution', return_value=SimpleNamespace(read_text=lambda _: '{"vcs_info":{"commit_id":"wrong"}}')):
            with self.assertRaisesRegex(ValueError, 'revision_mismatch'):
                candidate_validate.installed_revisions(expected['runtime'])
        with (patch.object(candidate_validate, 'adapter_check'),
              patch.object(candidate_validate, 'installed_revisions'),
              patch.object(candidate_validate, 'runtime_check'),
              patch.object(candidate_validate, 'publication_contract') as publication):
            from mindie_knowledge.publication_contract import make_contract, render_contract, parse_contract
            raw = render_contract(make_contract(expected['publication']['domain'], 'b' * 40))
            publication.return_value = parse_contract(raw, expected['publication']['domain'])
            self.assertEqual(candidate_validate.validate(self.source, expected)['status'], 'validated')
            publication.assert_called_once_with(expected['publication'])

    def test_local_recheck_reuses_only_verified_publication_and_rechecks_runtime(self):
        expected = product_contract.identity(self.source, 'a' * 40)
        previous = dict(expected, status='validated')
        with (patch.object(candidate_validate, 'adapter_check'),
              patch.object(candidate_validate, 'installed_revisions') as installed,
              patch.object(candidate_validate, 'runtime_check') as runtime,
              patch.object(candidate_validate, 'publication_contract', side_effect=AssertionError('unexpected network'))):
            self.assertEqual(candidate_validate.validate(self.source, expected, previous), previous)
            installed.assert_called_once_with(expected['runtime'])
            runtime.assert_called_once()
            wrong = dict(previous, product_sha256='b' * 64)
            with self.assertRaisesRegex(ValueError, 'receipt_mismatch'):
                candidate_validate.validate(self.source, expected, wrong)

    def test_candidate_checks_content_hash_failure_without_receipt(self):
        expected = product_contract.identity(self.source, 'a' * 40)
        with (patch.object(candidate_validate, 'adapter_check'),
              patch.object(candidate_validate, 'installed_revisions'),
              patch.object(candidate_validate, 'runtime_check'),
              patch.object(candidate_validate, 'publication_contract', side_effect=ValueError('content contract hash differs'))):
            with self.assertRaisesRegex(ValueError, 'publication_contract: read_failed'):
                candidate_validate.validate(self.source, expected)


if __name__ == '__main__':
    unittest.main()
