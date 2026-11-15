"""Candidate subprocess ownership and fail-closed product receipt boundaries."""
from contextlib import nullcontext
from pathlib import Path
import json
import os
import shutil
import struct
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
import bootstrap
import bounded_process
import setup as setup_script
import candidate_validate
import product_contract
from bounded_process import run

RECEIPT = "import json,sys; value=json.loads(sys.argv[sys.argv.index('--identity')+1]); value['status']='validated'; print(json.dumps(value),flush=True)\n"


def write_launcher(path, *, subsystem=2, magic=0x20B):
    """Minimal, never-executed PE32/PE32+ header with one subsystem."""
    header = bytearray(64 + 24 + 70)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 60, 64)
    header[64:68] = b"PE\0\0"
    struct.pack_into("<H", header, 84, 70)
    struct.pack_into("<H", header, 88, magic)
    struct.pack_into("<H", header, 156, subsystem)
    Path(path).write_bytes(header)
    return Path(path)


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
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", ".venv"))
        candidate_validate.adapter_check(self.source)
        (self.scripts / "capture_errors.py").unlink()
        with self.assertRaisesRegex(ValueError, "missing bounded runtime entry: capture_errors.py"):
            candidate_validate.adapter_check(self.source)

    def test_candidate_requires_both_exact_native_interrupt_controls(self):
        shutil.copytree(ROOT / "plugins/mindie-agent", self.scripts.parent,
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", ".venv", "build", "*.egg-info"))
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
            self.assertEqual(kwargs['timeout'], 180.0)
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

    def _service_validate(self, tmp_path, subsystem, *, previous=None):
        expected = product_contract.identity(self.source, 'a' * 40)
        executable = tmp_path / 'Scripts' / 'python.exe'
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'')
        if subsystem is not None:
            write_launcher(executable.with_name('pythonw.exe'), subsystem=subsystem)
        with patch.object(candidate_validate, 'os', SimpleNamespace(name='nt')), \
                patch.object(candidate_validate.sys, 'executable', str(executable)), \
                patch.object(candidate_validate, 'adapter_check'), \
                patch.object(candidate_validate, 'installed_revisions'), \
                patch.object(candidate_validate, 'runtime_check'), \
                patch.object(candidate_validate, 'publication_contract') as publication:
            result = candidate_validate.validate(
                self.source, expected, previous, require_windows_service=True)
        return expected, result, publication

    def test_required_service_capability_accepts_only_a_gui_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            expected, result, publication = self._service_validate(tmp_path, 2)
            self.assertEqual(result, dict(expected, status='validated'))
            publication.assert_called_once()

            previous = dict(expected, status='validated')
            expected, result, publication = self._service_validate(
                tmp_path / 'cached', 2, previous=previous)
            self.assertEqual(result, previous)
            publication.assert_not_called()

    def test_required_service_capability_rejects_cui_or_missing_launcher(self):
        for name, subsystem, code in (
            ('cui', 3, 'windowless_interpreter_invalid'),
            ('missing', None, 'windowless_interpreter_missing'),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                tmp_path = Path(directory)
                with self.assertRaises(candidate_validate.CheckFailure) as caught:
                    self._service_validate(tmp_path, subsystem)
                self.assertEqual(caught.exception.failure, dict(
                    stage='windows_service_interpreter', code=code))

                # A generic verified receipt is never service-capability proof.
                expected = product_contract.identity(self.source, 'a' * 40)
                with self.assertRaises(candidate_validate.CheckFailure) as caught:
                    self._service_validate(
                        tmp_path / 'cached', subsystem,
                        previous=dict(expected, status='validated'))
                self.assertEqual(caught.exception.failure, dict(
                    stage='windows_service_interpreter', code=code))

    def test_api_only_validation_remains_usable_without_a_gui_launcher(self):
        for subsystem in (3, None):
            with self.subTest(subsystem=subsystem), tempfile.TemporaryDirectory() as directory:
                tmp_path = Path(directory)
                expected = product_contract.identity(self.source, 'a' * 40)
                executable = tmp_path / 'Scripts' / 'python.exe'
                executable.parent.mkdir(parents=True)
                executable.write_bytes(b'')
                if subsystem is not None:
                    write_launcher(executable.with_name('pythonw.exe'), subsystem=subsystem)
                with patch.object(candidate_validate, 'os', SimpleNamespace(name='nt')), \
                        patch.object(candidate_validate.sys, 'executable', str(executable)), \
                        patch.object(candidate_validate, 'adapter_check'), \
                        patch.object(candidate_validate, 'installed_revisions'), \
                        patch.object(candidate_validate, 'runtime_check'), \
                        patch.object(candidate_validate, 'publication_contract'):
                    result = candidate_validate.validate(self.source, expected)
                self.assertEqual(result, dict(expected, status='validated'))

    def test_failure_receipt_accepts_the_windows_service_stage(self):
        expected = product_contract.identity(self.source, 'a' * 40)
        for code in ('windowless_interpreter_missing', 'windowless_interpreter_invalid'):
            with self.subTest(code=code):
                failure = dict(stage='windows_service_interpreter', code=code)
                output = json.dumps(dict(expected, status='failed', failure=failure))
                error = product_contract.failure_receipt(output, expected, 1)
                self.assertIsInstance(error, product_contract.CandidateValidationError)
                self.assertEqual((error.stage, error.code), ('windows_service_interpreter', code))
        for failure in (
            dict(stage='windows_service_interpreter', code='api_incompatible'),
            dict(stage='runtime_api', code='windowless_interpreter_invalid'),
        ):
            with self.subTest(failure=failure):
                output = json.dumps(dict(expected, status='failed', failure=failure))
                error = product_contract.failure_receipt(output, expected, 1)
                self.assertEqual((error.stage, error.code), ('process', 'nonzero_exit'))

    def test_probe_forwards_the_service_requirement_to_the_candidate(self):
        seen = []

        def command(argv, **kwargs):
            seen.append(argv)
            return run(argv, '', **kwargs).checked_stdout()

        product_contract.probe(sys.executable, self.scripts, command,
                               revision='a' * 40, require_windows_service=True)
        self.assertIn('--require-windows-service', seen[-1])
        product_contract.probe(sys.executable, self.scripts, command,
                               revision='a' * 40, require_windows_service=False)
        self.assertNotIn('--require-windows-service', seen[-1])

    def test_candidate_cli_accepts_the_service_requirement_flag(self):
        import io
        expected = product_contract.identity(self.source, 'a' * 40)
        stdout = SimpleNamespace(buffer=io.BytesIO())
        argv = ['candidate_validate.py', '--source', str(self.source),
                '--identity', json.dumps(expected), '--require-windows-service']
        with patch.object(candidate_validate, 'validate',
                          return_value=dict(expected, status='validated')) as validate, \
                patch.object(candidate_validate.sys, 'argv', argv), \
                patch.object(candidate_validate.sys, 'stdout', stdout):
            self.assertEqual(candidate_validate.main(), 0)
        validate.assert_called_once_with(self.source, expected, None,
                                         require_windows_service=True)

    def test_cached_baseline_runtime_rechecks_the_windows_service_launcher(self):
        data = Path(self.temp.name) / 'data'
        source = Path(self.temp.name) / 'bootstrap-source'
        source.mkdir()
        version, key = '0.1.32', 'a' * 64
        runtime = data / 'runtimes' / key[:24]
        python = runtime / 'Scripts/python.exe'
        release = data / 'releases' / version
        release.mkdir(parents=True)
        receipt = release / 'runtime.json'
        validation = dict(schema='mindie-candidate-check/1', status='validated')
        receipt.write_text(json.dumps(dict(key=key, python=str(python), validation=validation)))
        scripts = Path(self.temp.name) / 'installed' / 'scripts'

        class WindowsOs:
            name = 'nt'

            def __getattr__(self, item):
                return getattr(os, item)

        with patch.object(bootstrap, 'os', WindowsOs()), \
                patch.object(bootstrap, 'native_version', return_value=version), \
                patch.object(bootstrap, 'runtime_key', return_value=key), \
                patch.object(bootstrap, 'exclusive_lock', side_effect=lambda *a, **kw: nullcontext()), \
                patch.object(bootstrap, 'preparing', side_effect=lambda stage: nullcontext()), \
                patch.object(bootstrap, 'readback', return_value=scripts), \
                patch.object(bootstrap, 'publish_entry') as publish, \
                patch.object(bootstrap, 'probe', return_value=validation) as probe, \
                patch.object(bootstrap, 'validate_receipt') as validate_receipt:
            result = bootstrap._baseline_runtime(source, data)
            self.assertEqual(result, (python, scripts, release))
            validate_receipt.assert_not_called()
            publish.assert_called_once()
            args, kwargs = probe.call_args
            self.assertEqual(args[:2], (python, scripts))
            self.assertEqual(kwargs, dict(verified_receipt=validation,
                                          require_windows_service=True))

            probe.side_effect = product_contract.CandidateValidationError(dict(
                stage='windows_service_interpreter', code='windowless_interpreter_missing'))
            publish.reset_mock()
            with self.assertRaises(product_contract.CandidateValidationError):
                bootstrap._baseline_runtime(source, data)
            publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
