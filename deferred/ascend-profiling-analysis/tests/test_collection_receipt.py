"""A saved compact receipt resolves existing full collection evidence only."""
import json

import pytest
import _common as common


def test_receipt_resolves_full_record_and_retains_service_context(tmp_path):
    full = {'schema_version':1, 'analysis_status':'ok', 'remote_profile_root':'/owned/profile',
            'service_result':{'execution_id':'owned-id'}, 'benchmark_results':[{'body':'complete'}]}
    (tmp_path/'manifest.json').write_text(json.dumps(full))
    receipt = tmp_path/'receipt.json'
    receipt.write_text(json.dumps({'schema_version':'mindie.profile-collection.receipt.v1',
                                  'manifest_ref':'manifest.json'}))
    assert common.load_collection_manifest(receipt) == full


def test_receipt_cannot_override_failed_full_evidence(tmp_path):
    (tmp_path/'manifest.json').write_text(json.dumps({'analysis_status':'partial','remote_profile_root':'/owned/profile'}))
    receipt = tmp_path/'receipt.json'
    receipt.write_text(json.dumps({'schema_version':'mindie.profile-collection.receipt.v1',
                                  'manifest_ref':'manifest.json','analysis_status':'ok'}))
    with pytest.raises(RuntimeError, match='not analyzable'):
        common.load_collection_manifest(receipt)


def test_missing_ref_or_ref_loop_does_not_trigger_collection(tmp_path):
    receipt = tmp_path/'receipt.json'
    receipt.write_text(json.dumps({'schema_version':'mindie.profile-collection.receipt.v1'}))
    with pytest.raises(RuntimeError, match='manifest_ref'):
        common.load_collection_manifest(receipt)
    receipt.write_text(json.dumps({'schema_version':'mindie.profile-collection.receipt.v1','manifest_ref':'receipt.json'}))
    with pytest.raises(RuntimeError, match='not analyzable'):
        common.load_collection_manifest(receipt)
