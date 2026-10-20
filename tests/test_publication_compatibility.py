"""Actual producer packages against the independently pinned trusted validator.

The selected interpreter carries the official Git pin and its real VCS receipt.
A separately pinned validator must be installed before this suite and passed as
MINDIE_VALIDATOR_PYTHON. It never imports code from a publication tree.
"""
from contextlib import closing
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins/mindie-agent/scripts'))
import candidate_validate
import product_contract


def command(argv, *, cwd=None, timeout=600):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                            encoding='utf-8', timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:] + result.stdout[-2000:])
    return result.stdout.strip()


def verify_validator(python, revision):
    if Path(python).resolve() == Path(sys.executable).resolve():
        import mindie_knowledge.publication_check  # noqa: F401
        raw = importlib.metadata.distribution('mindie-knowledge').read_text('direct_url.json')
        receipt = json.loads(raw)
    else:
        receipt = json.loads(command([python, '-I', '-c',
            "import importlib.metadata; import mindie_knowledge.publication_check; "
            "print(importlib.metadata.distribution('mindie-knowledge').read_text('direct_url.json'))"]))
    if (receipt.get('vcs_info') or {}).get('commit_id') != revision:
        raise ValueError('trusted validator interpreter does not match its exact publication pin')


class PublicationCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='mindie-validator-compat-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.publication = product_contract.product(ROOT)[0]['publication']
        parsed = candidate_validate.publication_contract(cls.publication)
        cls.contract = parsed['contract']
        from mindie_knowledge.publication_contract import render_contract
        cls.raw_contract = render_contract(cls.contract)
        validator = cls.contract['validator']
        cls.validator_revision = validator['revision']
        installed = json.loads(importlib.metadata.distribution('mindie-knowledge').read_text('direct_url.json'))
        installed_commit = (installed.get('vcs_info') or {}).get('commit_id')
        if installed_commit != product_contract.requirements(ROOT)[0]['mindie-knowledge']:
            raise ValueError('producer interpreter does not match the exact product runtime pin')
        cls.python = os.environ.get('MINDIE_VALIDATOR_PYTHON')
        if cls.python:
            verify_validator(cls.python, cls.validator_revision)
        else:
            if installed_commit != cls.validator_revision:
                raise unittest.SkipTest(
                    'install the exact publication validator and set '
                    'MINDIE_VALIDATOR_PYTHON; the selected runtime is not that validator')
            cls.python = sys.executable
            verify_validator(cls.python, cls.validator_revision)
        cls.template_repo = cls.root / 'template-publication'
        cls.template_repo.mkdir()
        command(['git', '-c', 'core.hooksPath=' + os.devnull, '-C', str(cls.template_repo), 'init', '--quiet', '-b', 'main'])
        command(['git', '-c', 'core.hooksPath=' + os.devnull, '-C', str(cls.template_repo), 'config', 'core.autocrlf', 'false'])
        (cls.template_repo / 'publication-contract.json').write_bytes(cls.raw_contract.encode('utf-8'))
        command(['git', '-c', 'core.hooksPath=' + os.devnull, '-C', str(cls.template_repo), 'add', '.'])
        command(['git', '-c', 'core.hooksPath=' + os.devnull, '-C', str(cls.template_repo),
                 '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '--quiet', '-m', 'Synthetic public material'])
        cls.template_base = command(['git', '-c', 'core.hooksPath=' + os.devnull, '-C', str(cls.template_repo), 'rev-parse', 'HEAD'])

    def setUp(self):
        self.temp_case = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.temp_case.cleanup)
        self.directory = Path(self.temp_case.name)
        self.repo = self.directory / 'publication'
        shutil.copytree(self.template_repo, self.repo)
        self.base = self.template_base

    def git(self, *args):
        return command(['git', '-c', 'core.hooksPath=' + os.devnull, '-C', str(self.repo), *args])

    def commit(self):
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '--quiet', '-m', 'Synthetic public material')
        return self.git('rev-parse', 'HEAD')

    def produce(self, *, content=None):
        from mindie_knowledge.loop.store import Store
        from mindie_knowledge.publication_contract import GRADED_SCHEMA
        content = content or 'Synthetic evidence: an existing empty file returns zero bytes.\n'
        with closing(Store(self.directory / 'producer', self.publication['domain'])) as store:
            if self.contract['schema'] == GRADED_SCHEMA:
                files = self.produce_graded(store, content)
            else:
                doc = store.create_draft(kind='experience', title='Synthetic boundary example',
                    summary='An empty file differs from a missing file; example evidence only.',
                    content=content)
                package = store.materials.export_task(doc['entry_id'])
                files = {'tasks/' + doc['entry_id'] + '/' + path: raw
                         for path, raw in package['files'].items()}
                self.body_path = next(path for path in files if '/blocks/' in path)
        for path, raw in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw.encode('utf-8'))
        return self.commit()

    def produce_graded(self, store, content):
        from mindie_knowledge.graded.distill import prepare_request, apply_decision
        from tests.process_fixtures import installed_scanner
        request = prepare_request([dict(kind='run_manifest', outcome='observed',
            source_locator='fixture://empty-file', content=content,
            evidence_refs=['fixture-empty-file'])],
            session_digest=hashlib.sha256(content.encode()).hexdigest(),
            task_state='completed', redactor_executable=installed_scanner(),
            redaction_key=b'publication-compatibility-synthetic-evidence')
        selected = request['sanitized_inputs'][0]
        metadata = dict(schema='mindie-graded-unit/1', id='unit-empty-file-example',
            tier='L2', category='sop', evidence_strength='single_observation',
            domain_path='examples/files', inherits_from=[], title='Synthetic boundary example',
            conditions={'scenario': 'synthetic-empty-file'},
            triggers={'error_signatures': [], 'keywords': ['empty-file']},
            provenance=dict(source_type=selected['kind'], source_locator=selected['source_locator'],
                source_digest=selected['source_digest'], evidence_refs=selected['evidence_refs'],
                supersedes=[]))
        decision = dict(schema_version='mindie-distill-decision/1',
            session_digest=request['session_digest'], evidence_inputs=request['evidence_inputs'],
            action='propose_graded_units', drop_reason=None, units=[dict(
                target_unit_id=metadata['id'], target_tier=metadata['tier'],
                category=metadata['category'], evidence_strength=metadata['evidence_strength'],
                domain_path=metadata['domain_path'], operation='create',
                frontmatter=metadata, markdown_body=selected['content'])])
        applied = apply_decision(store, request, decision)
        self.assertEqual(applied['status'], 'applied')
        unit = store.graded._load().units[metadata['id']]
        self.body_path = unit.path
        return {unit.path: unit.render()}

    def trusted_check(self, revision, *, use_subprocess=False):
        args = [
            '--repo', str(self.repo), '--revision', revision, '--base-revision', self.base,
            '--domain', self.publication['domain'],
        ]
        if use_subprocess or Path(self.python).resolve() != Path(sys.executable).resolve():
            return subprocess.run([self.python, '-I', '-m', 'mindie_knowledge.publication_check', *args],
                capture_output=True, text=True, encoding='utf-8', timeout=60)
        import contextlib, io
        from mindie_knowledge import publication_check
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = publication_check.main(args) or 0
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return subprocess.CompletedProcess(args, code, out.getvalue(), err.getvalue())

    def test_current_package_passes_independent_exact_trusted_validator(self):
        revision = self.produce()
        result = self.trusted_check(revision, use_subprocess=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        checked = json.loads(result.stdout)
        self.assertEqual(checked['entries'], 1)
        self.assertTrue(checked['content_only'])
        from mindie_knowledge.publication_check import validate
        self.assertEqual(validate(self.repo, revision, self.publication['domain'],
            base_revision=self.base)['entries'], 1)

    def test_future_format_is_rejected_without_a_fixed_contract_digest(self):
        self.produce()
        field = 'unit' if 'unit' in self.contract['formats'] else 'block'
        future_format = self.contract['formats'][field].rsplit('/', 1)[0] + '/999'
        future = dict(self.contract, formats={**self.contract['formats'], field: future_format})
        raw = (json.dumps(future) + '\n').encode('utf-8')
        (self.repo / 'publication-contract.json').write_bytes(raw)
        result = self.trusted_check(self.commit())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('publication contract', result.stderr)
        from mindie_knowledge.publication_contract import ContractMismatch, parse_contract
        with self.assertRaisesRegex(ContractMismatch, 'formats'):
            parse_contract(raw, self.publication['domain'])

    def test_generated_host_placeholder_passes_exact_trusted_validator(self):
        content = 'SSH identity <redacted:username-at-host:a1b2c3d4e5f6> remains private.'
        revision = self.produce(content=content)
        result = self.trusted_check(revision)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['content_only'])
        body = (self.repo / self.body_path).read_text('utf-8')
        self.assertIn(content, body)

    def test_validator_reuse_rejects_a_wrong_actual_installed_pin(self):
        with self.assertRaisesRegex(ValueError, 'exact publication pin'):
            verify_validator(self.python, '0' * 40)

    def test_changed_body_is_rejected_by_trusted_validator(self):
        revision = self.produce()
        self.base = revision
        body = self.repo / self.body_path
        body.write_bytes(body.read_bytes().replace(b'zero bytes', b'invented result'))
        result = self.trusted_check(self.commit())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Invalid publication', result.stderr)


class CurrentFormatValidationTests(unittest.TestCase):
    def test_publication_read_preserves_git_revision_without_digest_gate(self):
        publication = dict(repository="mindie-agent/knowledge-vllm-ascend",
                           ref="main", domain="vllm-ascend",
                           verified_commit="1" * 40)
        contract = {"contract": {"schema": "mindie-publication-contract/1"},
                    "sha256": "a" * 64}
        with patch.object(candidate_validate, "publication_git",
                          side_effect=["", "", publication["verified_commit"]]):
            with patch("mindie_knowledge.publication_contract.read_git_contract",
                       return_value=contract) as read:
                parsed = candidate_validate.publication_contract(publication)
        self.assertEqual(parsed, contract)
        self.assertEqual(read.call_args.args[1], publication["verified_commit"])
        self.assertEqual(read.call_args.args[2], publication["domain"])
        self.assertNotIn("expected_sha256", read.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()