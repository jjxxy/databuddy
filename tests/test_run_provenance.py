import copy
import json

import pytest

from agent_service.run_provenance import describe


@pytest.fixture
def facts():
    pipeline = {'id': 'p1', 'project_id': 'project1'}
    run = {'id': 'run1', 'target': 'p1', 'commit': 'commit1', 'scheduled_key': 'p1:2026-01-01T00:00:00+00:00',
           'created_at': '2026-01-01T00:00:00Z', 'started_at': '2026-01-01T00:01:00Z',
           'finished_at': '2026-01-01T00:02:00Z',
           'source_snapshot': {'s1': {'id': 's1', 'project_id': 'project1', 'revision': 3,
               'name': '旧版收入.csv', 'kind': 'file', 'secret_config': 'DO-NOT-LEAK',
               'connection_string': 'DO-NOT-LEAK'}}}
    manifest = {'run_id': 'run1', 'pipeline_id': 'p1', 'commit': 'commit1',
        'inputs': [{'source_id': 's1', 'source_revision': 3, 'collected_at': '2026-01-01T00:01:00Z',
                    'records': 10, 'input_hash': 'hash1', 'evidence': {'password': 'DO-NOT-LEAK'}}],
        'input_hash': 'hash2', 'parquet_hash': 'hash3',
        'runtime': {'timeout_seconds': 240, 'max_attempts': 3, 'freshness_minutes': 60,
                    'secret': 'DO-NOT-LEAK'},
        'versions': {'duckdb': '1.0', 'secret': 'DO-NOT-LEAK'},
        'quality': [{'status': 'pass', 'name': 'check'}, {'status': 'fail'}, {'status': 'warn'}]}
    return pipeline, run, manifest


def test_versioned_run_provenance_is_safe_and_truthful(facts):
    pipeline, run, manifest = facts
    result = describe(pipeline, run, manifest)
    assert result['trigger'] == 'scheduled'
    assert result['run_id'] == 'run1' and result['commit'] == 'commit1'
    assert result['inputs'][0]['name'] == '旧版收入.csv'
    assert result['inputs'][0]['revision'] == 3 and result['inputs'][0]['records'] == 10
    assert result['input_hash'] == 'hash2' and result['output_hash'] == 'hash3'
    assert result['quality'] == {'available': True, 'passed': 1, 'failed': 1, 'other': 1}
    assert result['dependencies'] == {'duckdb': '1.0'}
    assert result['replay']['available'] is False
    assert 'DO-NOT-LEAK' not in json.dumps(result, ensure_ascii=False)


def test_historical_source_does_not_take_name_from_current_or_wrong_version(facts):
    pipeline, run, manifest = facts
    old_run = copy.deepcopy(run)
    old_run['source_snapshot']['s1']['revision'] = 4
    result = describe(pipeline, old_run, manifest)
    assert result['inputs'][0]['name'] is None
    assert result['inputs'][0]['revision'] == 3
    wrong_project = copy.deepcopy(run)
    wrong_project['source_snapshot']['s1']['project_id'] = 'another-project'
    assert describe(pipeline, wrong_project, manifest)['inputs'][0]['name'] is None
    no_snapshot = dict(run, source_snapshot={})
    assert describe(pipeline, no_snapshot, manifest)['inputs'][0]['name'] is None


@pytest.mark.parametrize(('field', 'value'), [
    ('pipeline_id', 'different'), ('run_id', 'other-run'), ('commit', 'other-commit')])
def test_mismatched_manifest_is_not_attached_to_run(facts, field, value):
    pipeline, run, manifest = facts
    manifest[field] = value
    with pytest.raises(ValueError, match='不一致'):
        describe(pipeline, run, manifest)


def test_missing_environment_does_not_claim_reproducibility(facts):
    pipeline, run, manifest = facts
    manifest.pop('versions'); manifest.pop('runtime')
    run['scheduled_key'] = None
    result = describe(pipeline, run, manifest)
    assert result['trigger'] == 'other'
    assert result['dependencies'] == {} and result['runtime'] == {}
    assert result['replay']['available'] is False


def test_missing_old_checks_and_identity_policy_are_not_claimed(facts):
    pipeline, run, manifest = facts
    manifest.pop('quality')
    result = describe(pipeline, run, manifest)
    assert result['quality']['available'] is False
    assert result['identity_policy'] is None
    manifest['identity_policy'] = 'preserve_all_rows_occurrence_v1'
    assert describe(pipeline, run, manifest)['identity_policy'] == 'preserve_all_rows_occurrence_v1'


@pytest.mark.parametrize('key', ['chat:turn123', 'acceptance:agent-v8:p1', 'p1:not-a-date', 'p1:2026-01-01T00:00:00'])
def test_idempotency_keys_are_not_misreported_as_scheduled_runs(facts, key):
    pipeline, run, manifest = facts
    run['scheduled_key'] = key
    assert describe(pipeline, run, manifest)['trigger'] == 'other'
