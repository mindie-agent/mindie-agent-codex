"""Real MCP -> lease -> Service/Engine C6 route plus query parameter forwarding.

Only the native model executable is a local stub; the MCP gate, runtime helper,
admission lease, knowledge service and Engine/SummaryLedger are real.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'
sys.path.insert(0, str(SCRIPTS))

import bridge
import export_catalog
import mcp_gate
import runtime_call
from mindie_knowledge.loop.store import Store
from tests.process_fixtures import (
    cleanup_temporary_directory,
    native_fixture,
    public_engine_config,
    stop_owned_knowledge_service,
)

try:
    from mindie_knowledge.graded.units import GradedUnit
    from mindie_knowledge.loop import summary_budget
    from mindie_knowledge import consent_store
except ImportError:  # installed 0.0.2 runtime has no C6 surface
    GradedUnit = None
    summary_budget = None
    consent_store = None


EVIDENCE = [dict(kind='hardware_spec', outcome='verified',
                 source_locator='fixture://mcp-distill',
                 content='Synthetic hardware specification for MCP distillation.',
                 evidence_refs=['ref-1'])]


@unittest.skipIf(GradedUnit is None, 'joint 0.0.3 knowledge source is required')
class ServiceCase(unittest.TestCase):
    domain = 'mindie'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scope = self.root / 'scope'
        self.scope.mkdir()
        self.data_root = self.root / 'data'
        self.config = self.root / 'codex.json'
        self.engine = self.root / 'engine.json'
        self.community = self.root / 'mindie-community.json'
        self.admission = self.root / 'codex.admission.sqlite3'
        self.consent = self.root / 'mindie-consent.json'
        self.marker = self.root / 'native-model-called'
        self.fake_codex = native_fixture(self.root / 'fake-codex', self._fake_codex())

        self.engine_config = public_engine_config(
            self.data_root, domain=self.domain, adapter_profile=self.config,
            admission_path=str(self.admission), community_config=str(self.community))
        self.engine_config['summary_command'] = [
            sys.executable, str(SCRIPTS / 'agent_worker.py')]
        self.engine_config['service_launch_context']['paths']['MINDIE_CODEX_BIN'] = str(self.fake_codex)
        self.engine.write_text(json.dumps(self.engine_config), encoding='utf-8')
        self.config.write_text(json.dumps(dict(
            python=sys.executable,
            engine_config=str(self.engine),
            community_config=str(self.community),
            admission_path=str(self.admission),
            runtime_scripts=str(SCRIPTS),
        )), encoding='utf-8')
        self._write_disabled_sharing()

        self.environment = patch.dict(os.environ, {
            'MINDIE_AGENT_CONFIG': str(self.config),
            'CODEX_THREAD_ID': 'thread-distill-1',
            'CODEX_HOME': str(self.root / 'codex-home'),
            'MINDIE_CODEX_BIN': str(self.fake_codex),
            'MINDIE_DIAGNOSTICS_ROOT': str(self.root / 'diagnostics'),
        })
        self.environment.start()
        # The production helper intentionally strips PYTHONPATH to use the
        # installed pin. This test's interpreter carries the joint source on
        # PYTHONPATH, so keep it for the helper/service subprocesses and let
        # their real joint code run against the local native model stub.
        from session_gate import runtime_env as base_runtime_env
        def joint_env(config_file=None, *, adapter=None):
            value = base_runtime_env(config_file, adapter=adapter)
            value['PYTHONPATH'] = os.environ.get('PYTHONPATH', '')
            return value
        self.runtime_env_patch = patch.object(mcp_gate, 'runtime_env', side_effect=joint_env)
        self.runtime_env_patch.start()
        self.addCleanup(self.runtime_env_patch.stop)

    def tearDown(self):
        try:
            stop_owned_knowledge_service(self.engine)
        finally:
            self.environment.stop()
            cleanup_temporary_directory(self.temp)

    def _write_disabled_sharing(self):
        self.community.write_text(json.dumps(dict(
            schema='mindie-community-config/1', enabled=False, generation=None,
            enabled_at=None, repository=None, branch='main', project_roots=[],
            idle_seconds=300, consent_config=str(self.consent),
        )), encoding='utf-8')

    def _enable_sharing(self):
        consent_store.record_choice(self.consent, 'contribute')
        self.community.write_text(json.dumps(dict(
            schema='mindie-community-config/1', enabled=True, generation='test-generation',
            enabled_at=time.time(), repository='owner/knowledge', branch='main',
            project_roots=[str(self.scope)], idle_seconds=300,
            consent_config=str(self.consent),
        )), encoding='utf-8')

    def _activate(self):
        with patch.object(Path, 'cwd', return_value=self.scope):
            value = bridge.activate('activate')
        self.assertEqual(value['status'], 'active')
        return value

    def _fake_codex(self):
        return f'''#!{sys.executable}
import json, sys
from pathlib import Path
Path({str(self.marker)!r}).write_text('called', encoding='utf-8')
prompt = sys.stdin.read()
payload = json.loads(prompt.split("\\n\\n", 1)[1])
request = payload['request']
item = request['sanitized_inputs'][0]
frontmatter = dict(
    schema='mindie-graded-unit/1', id='unit-mcp-distill-fixture',
    tier='L1', category='axiom', evidence_strength='hardware_spec',
    domain_path='hardware/ascend', inherits_from=[],
    title='Synthetic MCP distill axiom',
    triggers=dict(error_signatures=['SYNTHETIC'], keywords=['synthetic']),
    conditions=dict(soc=['Ascend910B']),
    provenance=dict(source_type='hardware_spec', source_locator=item['source_locator'],
                    source_digest=item['source_digest'],
                    evidence_refs=item['evidence_refs'], supersedes=[]),
)
decision = dict(
    schema_version='mindie-distill-decision/1',
    session_digest=request['session_digest'],
    evidence_inputs=request['evidence_inputs'],
    action='propose_graded_units', drop_reason=None,
    units=[dict(target_tier='L1', category='axiom', evidence_strength='hardware_spec',
                domain_path='hardware/ascend', operation='create',
                target_unit_id='unit-mcp-distill-fixture', frontmatter=frontmatter,
                markdown_body='Synthetic MCP distill rule.\\n')],
)
rows = [
    dict(type='turn.started'),
    dict(type='item.completed', item=dict(type='agent_message', phase='final_answer',
                                          text=json.dumps(decision))),
    dict(type='turn.completed', usage=dict(input_tokens=10, cached_input_tokens=0,
                                           output_tokens=5)),
]
for row in rows:
    print(json.dumps(row), flush=True)
'''

    def _request(self, request_id, tool, arguments, thread='thread-distill-1'):
        return {
            'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call',
            'params': {
                'name': tool, 'arguments': arguments,
                '_meta': {
                    'threadId': thread, 'sessionId': thread,
                    'x-codex-turn-metadata': {
                        'thread_id': thread, 'session_id': thread,
                        'turn_id': 'turn-' + str(request_id),
                    },
                },
            },
        }


class DistillRegistrationTests(ServiceCase):
    def test_catalog_registers_distill_and_query_parameters(self):
        gate = mcp_gate.Gate('knowledge')
        names = {tool['name'] for tool in gate.tools}
        self.assertIn('knowledge_distill', names)
        distill = next(tool for tool in gate.tools if tool['name'] == 'knowledge_distill')
        self.assertEqual(
            set(distill['inputSchema']['properties']),
            {'inputs', 'task_state', 'domain', 'related_refs', 'proposal'},
        )
        self.assertFalse({'_session_id', '_activation', '_session_verified'}
                         & set(distill['inputSchema']['properties']))
        query = next(tool for tool in gate.tools if tool['name'] == 'knowledge_query')
        self.assertLessEqual(
            {'domain', 'domain_path', 'token_budget', 'include_unknown', 'include_archived',
             'navigation_only', 'run_id'},
            set(query['inputSchema']['properties']))
        explain = next(tool for tool in gate.tools if tool['name'] == 'knowledge_explain')
        self.assertEqual(
            set(explain['inputSchema']['properties']),
            {'ref', 'refs', 'conditions', 'known_refs', 'token_budget', 'run_id'},
        )
        self.assertEqual(explain['inputSchema']['required'], [])
        feedback = next(tool for tool in gate.tools if tool['name'] == 'knowledge_feedback')
        self.assertEqual(
            feedback['inputSchema']['properties']['evidence_source']['enum'],
            ['agent_declared', 'run_manifest_verified', 'ablation_verified'],
        )
        generated = export_catalog.catalog()
        self.assertIn('knowledge_distill',
                      {tool['name'] for tool in generated['knowledge']})
        checked = json.loads((SCRIPTS / 'mcp_catalog.json').read_text())
        self.assertEqual(generated['knowledge'], checked['knowledge'])

    def test_batch_explain_and_distill_options_forward_cleanly(self):
        refs = [
            'mindie://mindie/units/unit-batch-a@1',
            'mindie://mindie/units/unit-batch-b@2',
        ]
        explain_payload = dict(
            surface='knowledge', name='knowledge_explain',
            arguments={
                'refs': refs,
                'conditions': {'soc': 'Ascend910B'},
                'known_refs': [refs[0]],
                'token_budget': 2048,
            },
            mindie_session_id='thread-distill-1',
            native_session_verified=True,
        )
        with patch.object(runtime_call, 'config_path', return_value=self.config), \
                patch('service_client.connect', return_value={}), \
                patch('mindie_knowledge.loop.transport.rpc',
                      return_value={'units': [], 'requires': [], 'unresolved': []}) as rpc:
            explained = runtime_call.call(explain_payload)
        self.assertFalse(explained['isError'])
        self.assertEqual(rpc.call_args.args[1], 'explain')
        forwarded = rpc.call_args.args[2]
        self.assertEqual(forwarded['refs'], refs)
        self.assertEqual(forwarded['conditions'], {'soc': 'Ascend910B'})
        self.assertEqual(forwarded['known_refs'], [refs[0]])
        self.assertEqual(forwarded['token_budget'], 2048)
        self.assertEqual(forwarded['_session_id'], 'thread-distill-1')

        proposal = {
            'schema_version': 'mindie-distill-decision/1',
            'session_digest': 'a' * 64, 'evidence_inputs': [],
            'action': 'noop', 'drop_reason': 'already_covered', 'units': [],
        }
        distill_payload = dict(
            surface='knowledge', name='knowledge_distill',
            arguments={
                'inputs': EVIDENCE, 'task_state': 'verified_milestone',
                'related_refs': ['unit-batch-a'], 'proposal': proposal,
            },
            mindie_session_id='thread-distill-1',
            native_session_verified=True,
        )
        with patch.object(runtime_call, 'config_path', return_value=self.config), \
                patch.object(runtime_call, '_active_lease_token',
                             return_value='lease-token'), \
                patch.object(runtime_call, 'resolve_lease',
                             return_value={'session': 'thread-distill-1'}), \
                patch.object(runtime_call, 'finish_outcome'), \
                patch('service_client.connect', return_value={}), \
                patch('mindie_knowledge.loop.transport.rpc',
                      return_value={'status': 'noop'}) as rpc:
            distilled = runtime_call.call(distill_payload)
        self.assertFalse(distilled['isError'])
        self.assertEqual(rpc.call_args.args[1], 'distill')
        forwarded = rpc.call_args.args[2]
        self.assertEqual(forwarded['related_refs'], ['unit-batch-a'])
        self.assertEqual(forwarded['proposal'], proposal)
        self.assertEqual(forwarded['task_state'], 'verified_milestone')

    def test_batch_explain_rejects_mixed_domain_authorities_before_service_start(self):
        from service_client import DomainRouteError

        payload = dict(
            surface='knowledge', name='knowledge_explain',
            arguments={
                'refs': [
                    'mindie://mindie/units/unit-batch-a@1',
                    'mindie://operators/units/unit-batch-b@2',
                ],
            },
            mindie_session_id='thread-distill-1',
            native_session_verified=True,
        )
        with patch.object(runtime_call, 'config_path', return_value=self.config), \
                patch('service_client.connect') as connect:
            with self.assertRaises(DomainRouteError):
                runtime_call.call(payload)
        connect.assert_not_called()

    def test_new_organizer_helper_is_in_the_installed_payload(self):
        from product_contract import package_files
        root = SCRIPTS.parent
        packaged = {path.relative_to(root).as_posix() for path in package_files(root)}
        self.assertIn('scripts/organizer_distill.py', packaged)
        self.assertIn('scripts/agent_worker.py', packaged)
        self.assertIn('scripts/mcp_catalog.json', packaged)

    def test_identity_fields_and_unknown_input_keys_are_rejected_before_runtime(self):
        gate = mcp_gate.Gate('knowledge')
        bad_identity = self._request(1, 'knowledge_distill', {
            'inputs': EVIDENCE, 'task_state': 'verified_milestone',
            '_session_id': 'forged', '_activation': 'forged'})
        result = gate.call(bad_identity)
        self.assertTrue(result['isError'])
        self.assertEqual(result['structuredContent']['code'], 'invalid_arguments')
        bad_item = dict(EVIDENCE[0], _activation='forged')
        result = gate.call(self._request(2, 'knowledge_distill', {
            'inputs': [bad_item], 'task_state': 'verified_milestone'}))
        self.assertTrue(result['isError'])
        self.assertEqual(result['structuredContent']['code'], 'invalid_arguments')
        self.assertFalse(self.marker.exists())

    def test_runtime_call_forwards_active_lease_and_distill_timeout(self):
        payload = dict(surface='knowledge', name='knowledge_distill',
                       arguments={'inputs': [], 'task_state': 'in_flight'},
                       mindie_session_id='thread-distill-1',
                       native_session_verified=True)
        with patch.object(runtime_call, 'config_path', return_value=self.config), \
                patch.object(runtime_call, '_active_lease_token',
                             return_value='lease-token') as active, \
                patch.object(runtime_call, 'resolve_lease',
                             return_value={'session': 'thread-distill-1'}), \
                patch.object(runtime_call, 'finish_outcome') as finish, \
                patch('service_client.connect', return_value={}), \
                patch('mindie_knowledge.loop.transport.rpc',
                      return_value={'status': 'noop'}) as rpc:
            result = runtime_call.call(payload)
        self.assertFalse(result['isError'])
        active.assert_called_once_with(json.loads(self.config.read_text()),
                                       'thread-distill-1')
        self.assertEqual(rpc.call_args.kwargs['timeout'],
                         runtime_call.distill_rpc_timeout())
        sent = rpc.call_args.args[2]
        self.assertEqual(sent['_activation'], 'lease-token')
        self.assertEqual(sent['_session_id'], 'thread-distill-1')
        self.assertNotIn('_session_verified', sent)
        finish.assert_called_once_with(json.loads(self.config.read_text()),
                                       'thread-distill-1', 'lease-token', True)

        for status in ('failed', 'outcome_unknown', 'invoking',
                       'local_receipt_failed', 'local_application_failed'):
            preserved = status.startswith('local_')
            value = {'status': status, 'attempt_id': 'fixture-attempt', 'model_replayed': False}
            if preserved:
                value.update(model_execution='completed', model_status='returned',
                             local_error={'stage': 'receipt', 'message': 'controlled storage failure'},
                             result={'schema_version': 'mindie-distill-decision/1',
                                     'session_digest': 'a' * 64, 'evidence_inputs': [],
                                     'action': 'noop', 'drop_reason': 'already_covered', 'units': []})
            with self.subTest(status=status), \
                    patch.object(runtime_call, 'config_path', return_value=self.config), \
                    patch.object(runtime_call, '_active_lease_token', return_value='lease-token'), \
                    patch.object(runtime_call, 'resolve_lease', return_value={'session': 'thread-distill-1'}), \
                    patch.object(runtime_call, 'finish_outcome') as finish, \
                    patch('service_client.connect', return_value={}), \
                    patch('mindie_knowledge.loop.transport.rpc', return_value=value):
                failed = runtime_call.call(payload)
                self.assertIs(failed['isError'], not preserved)
                self.assertIs(finish.call_args.args[-1], preserved)
                self.assertEqual(failed['structuredContent']['operation_status'], status)
                if preserved:
                    self.assertEqual(failed['structuredContent']['result'], value['result'])
                    self.assertIn('do not repeat', failed['structuredContent']['operation_message'])

    def test_mcp_helper_run_uses_distill_deadline(self):
        gate = mcp_gate.Gate('knowledge')
        with patch.object(mcp_gate, 'run', return_value=object()) as helper, \
                patch.object(mcp_gate, 'runtime_result',
                             return_value={'content': [], 'isError': False}):
            result = gate._call(
                self._request(3, 'knowledge_distill', {
                    'inputs': EVIDENCE, 'task_state': 'verified_milestone'}),
                'thread-distill-1')
        self.assertFalse(result['isError'])
        self.assertEqual(helper.call_args.kwargs['timeout'],
                         runtime_call.distill_helper_timeout())

    def test_distill_timeout_uses_shared_worker_budget(self):
        self.assertGreater(runtime_call.distill_rpc_timeout(),
                           summary_budget.worker_timeout())
        self.assertGreater(runtime_call.distill_helper_timeout(),
                           runtime_call.distill_rpc_timeout())
        self.assertEqual(mcp_gate.knowledge_timeout('knowledge_distill', None),
                         runtime_call.distill_helper_timeout())
        self.assertIsNone(mcp_gate.knowledge_timeout('knowledge_query', None))
        with self.assertRaises(mcp_gate.InvalidToolArguments):
            mcp_gate.knowledge_timeout('knowledge_distill', 12.0)
        self.assertEqual(runtime_call.distill_rpc_timeout(), summary_budget.distill_rpc_timeout())


class DistillRoutingTests(ServiceCase):
    def test_unactivated_distill_is_rejected_without_model_call(self):
        gate = mcp_gate.Gate('knowledge')
        result = gate.call(self._request(10, 'knowledge_distill', {
            'inputs': EVIDENCE, 'task_state': 'verified_milestone'}))
        self.assertTrue(result['isError'])
        self.assertEqual(result['structuredContent']['execution'], 'outcome_unconfirmed')
        self.assertFalse(self.marker.exists())

    def test_in_flight_distill_returns_noop_without_model_call(self):
        self._activate()
        self._enable_sharing()
        gate = mcp_gate.Gate('knowledge')
        result = gate.call(self._request(11, 'knowledge_distill', {
            'inputs': EVIDENCE, 'task_state': 'in_flight'}))
        self.assertFalse(result['isError'], result)
        value = result['structuredContent']
        self.assertEqual(value['status'], 'noop')
        self.assertEqual(value['decision']['drop_reason'], 'in_flight_incomplete')
        self.assertFalse(self.marker.exists())

    def test_verified_milestone_reaches_engine_and_applies_decision(self):
        self._activate()
        self._enable_sharing()
        gate = mcp_gate.Gate('knowledge')
        result = gate.call(self._request(12, 'knowledge_distill', {
            'inputs': EVIDENCE, 'task_state': 'verified_milestone'}))
        self.assertFalse(result['isError'], result)
        value = result['structuredContent']
        self.assertEqual(value['status'], 'applied')
        self.assertIn('unit-mcp-distill-fixture', value['admitted_unit_ids'])
        self.assertTrue(self.marker.exists())


class QueryParameterTests(ServiceCase):
    def setUp(self):
        super().setUp()
        store = Store(self.data_root, self.domain)
        try:
            unit = GradedUnit.from_parts({
                'schema': 'mindie-graded-unit/1',
                'id': 'unit-query-parameter-fixture',
                'tier': 'L2',
                'category': 'sop',
                'evidence_strength': 'single_observation',
                'domain_path': 'ops/ascendc',
                'inherits_from': [],
                'title': 'Synthetic HCCL ring probe',
                'triggers': {'error_signatures': ['HCCL'], 'keywords': ['hccl']},
                'conditions': {'soc': ['Ascend910B']},
                'provenance': {
                    'source_type': 'session_distill',
                    'source_locator': 'fixture://query-parameter',
                    'source_digest': 'b' * 64,
                    'evidence_refs': ['ref-query-1'],
                    'supersedes': [],
                },
            }, 'Check the HCCL ring initialization probe.\n')
            store.graded.install([unit])
        finally:
            store.close()

    def test_query_domain_budget_and_unknown_reach_shared_graded_query(self):
        gate = mcp_gate.Gate('knowledge')
        result = gate.call(self._request(20, 'knowledge_query', {
            'query': 'HCCL', 'domain_path': 'ops/ascendc', 'token_budget': 200,
            'include_unknown': True}))
        self.assertFalse(result['isError'], result)
        value = result['structuredContent']
        self.assertEqual(value['token_budget'], 200)
        self.assertTrue(value['results'])
        self.assertEqual(value['results'][0]['domain_path'], 'ops/ascendc')
        self.assertEqual(value['results'][0]['route'], 'preferred_domain')
        self.assertIn('excluded_conditions', value)

        rejected = gate.call(self._request(21, 'knowledge_query', {
            'query': 'HCCL', 'token_budget': 0}))
        self.assertTrue(rejected['isError'])
        self.assertIn('token_budget', rejected['content'][0]['text'])
        self.assertIn('16000', rejected['content'][0]['text'])


if __name__ == '__main__':
    unittest.main()
