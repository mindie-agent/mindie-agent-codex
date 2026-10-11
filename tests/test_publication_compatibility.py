"""Actual producer packages against the independently pinned trusted validator.

The second interpreter installs the official Git pin normally, with its own
dependencies and VCS receipt. It never imports code from a publication tree.
"""
from contextlib import closing
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

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
    receipt = json.loads(command([python, '-I', '-c',
        "import importlib.metadata; import mindie_knowledge.publication_check; "
        "print(importlib.metadata.distribution('mindie-knowledge').read_text('direct_url.json'))"]))
    if (receipt.get('vcs_info') or {}).get('commit_id') != revision:
        raise ValueError('trusted validator interpreter does not match its exact publication pin')
    command([python, '-m', 'pip', 'check'])


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
        if hashlib.sha256(cls.raw_contract.encode('utf-8')).hexdigest() != cls.publication['contract_sha256']:
            raise ValueError('fixture must preserve the exact selected publication declaration bytes')
        validator = cls.contract['validator']
        cls.validator_revision = validator['revision']
        cls.python = os.environ.get('MINDIE_VALIDATOR_PYTHON')
        if not cls.python:
            env = cls.root / 'validator-env'
            command([sys.executable, '-m', 'venv', str(env)])
            cls.python = str(env / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python'))
            command([cls.python, '-m', 'pip', 'install', '--disable-pip-version-check',
                     'mindie-knowledge @ git+https://github.com/' + validator['repository'] + '@' + cls.validator_revision])
        # A supplied interpreter is also verified; invalid reuse fails rather
        # than silently building a replacement or running the current runtime.
        verify_validator(cls.python, cls.validator_revision)
        installed = json.loads(importlib.metadata.distribution('mindie-knowledge').read_text('direct_url.json'))
        if (installed.get('vcs_info') or {}).get('commit_id') != product_contract.requirements(ROOT)[0]['mindie-knowledge']:
            raise ValueError('producer interpreter does not match the exact product runtime pin')

    def setUp(self):
        self.temp_case = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.temp_case.cleanup)
        self.directory = Path(self.temp_case.name)
        self.repo = self.directory / 'publication'
        self.repo.mkdir()
        self.git('init', '--quiet', '-b', 'main')
        self.git('config', 'core.autocrlf', 'false')
        (self.repo / 'publication-contract.json').write_bytes(self.raw_contract.encode('utf-8'))
        self.base = self.commit()

    def git(self, *args):
        return command(['git', '-c', 'core.hooksPath=' + os.devnull, '-C', str(self.repo), *args])

    def commit(self):
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '--quiet', '-m', 'Synthetic public material')
        return self.git('rev-parse', 'HEAD')

    def produce(self):
        from mindie_knowledge.loop.store import Store
        with closing(Store(self.directory / 'producer', self.publication['domain'])) as store:
            doc = store.create_draft(kind='experience', title='Synthetic boundary example',
                summary='An empty file differs from a missing file; example evidence only.',
                content='Synthetic evidence: an existing empty file returns zero bytes.\n')
            package = store.materials.export_task(doc['entry_id'])
        for path, content in package['files'].items():
            target = self.repo / 'tasks' / doc['entry_id'] / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode('utf-8'))
        return self.commit()

    def trusted_check(self, revision, *, digest=None):
        return subprocess.run([self.python, '-I', '-m', 'mindie_knowledge.publication_check',
            '--repo', str(self.repo), '--revision', revision, '--base-revision', self.base,
            '--domain', self.publication['domain'], '--contract-sha256',
            digest or self.publication['contract_sha256']],
            capture_output=True, text=True, encoding='utf-8', timeout=60)

    def test_current_package_passes_independent_exact_trusted_validator(self):
        revision = self.produce()
        result = self.trusted_check(revision)
        self.assertEqual(result.returncode, 0, result.stderr)
        checked = json.loads(result.stdout)
        self.assertEqual(checked['entries'], 1)
        self.assertTrue(checked['content_only'])
        from mindie_knowledge.publication_check import validate
        self.assertEqual(validate(self.repo, revision, self.publication['domain'],
            base_revision=self.base, expected_contract_sha256=self.publication['contract_sha256'])['entries'], 1)

    def test_wrong_contract_digest_and_future_format_are_rejected(self):
        revision = self.produce()
        result = self.trusted_check(revision, digest='0' * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('publication contract', result.stderr)
        future = dict(self.contract, formats=dict(self.contract['formats'], block='mindie-material-block/999'))
        raw = (json.dumps(future) + '\n').encode('utf-8')
        (self.repo / 'publication-contract.json').write_bytes(raw)
        result = self.trusted_check(self.commit(), digest=hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('publication contract', result.stderr)
        from mindie_knowledge.publication_contract import ContractMismatch, parse_contract
        with self.assertRaisesRegex(ContractMismatch, 'formats'):
            parse_contract(raw, self.publication['domain'], hashlib.sha256(raw).hexdigest())

    def test_validator_reuse_rejects_a_wrong_actual_installed_pin(self):
        with self.assertRaisesRegex(ValueError, 'exact publication pin'):
            verify_validator(self.python, '0' * 40)

    def test_changed_body_is_rejected_by_trusted_validator(self):
        revision = self.produce()
        self.base = revision
        body = next((self.repo / 'tasks').glob('*/blocks/*.md'))
        body.write_bytes(body.read_bytes().replace(b'zero bytes', b'invented result'))
        result = self.trusted_check(self.commit())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Invalid publication', result.stderr)


if __name__ == '__main__':
    unittest.main()
