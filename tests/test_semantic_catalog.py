import copy
import hashlib
import json
import subprocess
import sys

import pytest

from agent_service import semantic_catalog as catalog, workspace
from agent_service.config import DATA
from agent_service.semantic_alignment import canonical_sha256, suggest_alignment
from agent_service.sources import revision
from agent_service.store import get, patch, put, seal, uid


def project():
    return put('project', {'name': '个人业务语义包'}, 'p_' + uid())['id']


def source(pid, raw=b'owner business definitions; no data rows'):
    name = uid() + '.txt'
    path = DATA / 'uploads' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    saved = put('source', {'project_id': pid, 'name': 'definitions.txt', 'kind': 'file',
                'secret_config': seal({'stored_name': name}),
                'content_hash': hashlib.sha256(raw).hexdigest(), 'status': 'ready'}, 's_' + uid())
    return revision(saved), path


def field(identifier='revenue', period='2026-10'):
    return {'id': identifier, 'name': '净收入', 'meaning': '已付款订单扣除退款后的金额',
            'kind': 'measure', 'grain': ['office', 'month'], 'unit': 'CNY',
            'value_format': 'decimal', 'temporal_kind': 'interval',
            'period': period, 'aggregation': 'sum'}


def definition(s):
    return {'key': 'finance-income', 'name': '财务净收入定义',
            'source': {'source_id': s['id'], 'locator': 'definition 1',
                       'content_sha256': s['content_hash']},
            'effective_from': '2026-01-01', 'effective_to': '2026-12-31',
            'fields': [field()]}


def drafted():
    pid = project(); s, path = source(pid)
    return pid, s, path, catalog.propose(pid, definition(s), 'authenticated-owner')


def approve(pid, record, actor='authenticated-owner'):
    return catalog.approve(pid, record['id'], record['content_sha256'], actor)


def test_owner_review_lifecycle_uses_actual_source_and_complete_fields():
    pid, s, _, draft = drafted()
    assert draft['status'] == 'draft' and draft['trust_level'] == 'unreviewed'
    assert draft['source_snapshot']['source_revision_id'] == s['revision_id']
    assert draft['content_sha256'] == canonical_sha256({
        'definition': draft['payload'], 'source_snapshot': draft['source_snapshot']})
    assert draft['created_actor_sha256'] == hashlib.sha256(b'authenticated-owner').hexdigest()
    with pytest.raises(ValueError, match='尚未通过'):
        catalog.get_approved_catalog(pid, draft['id'])
    approved = approve(pid, draft)
    assert approved['trust_level'] == approved['approval']['trust_level'] == 'owner_reviewed'
    complete = catalog.get_approved_catalog(pid, draft['id'], draft['content_sha256'])
    assert complete['payload']['fields'][0]['grain'] == ['office', 'month']
    page = catalog.list_catalogs(pid, status='approved')
    assert page['total'] == 1 and page['items'][0] == complete
    # A consumer can pass actual server-approved fields to the existing aligner.
    result = suggest_alignment(field('input'), complete['payload']['fields'])
    assert result.candidates[0].target.id == 'revenue'
    assert result.candidates[0].state == 'review_required'


def test_wrong_digest_empty_actor_and_illegal_transitions_cannot_grant_consent():
    pid, _, _, draft = drafted()
    with pytest.raises(ValueError, match='版本已变化'):
        catalog.approve(pid, draft['id'], '0' * 64, 'owner')
    with pytest.raises(ValueError, match='owner'):
        approve(pid, draft, ' ')
    with pytest.raises(ValueError, match='草稿'):
        catalog.retire(pid, draft['id'], draft['content_sha256'], 'owner')
    approved = approve(pid, draft)
    assert approve(pid, draft, 'different-owner')['approval'] == approved['approval']
    with pytest.raises(ValueError, match='固定版本'):
        catalog.get_approved_catalog(pid, draft['id'], '0' * 64)
    retired = catalog.retire(pid, draft['id'], draft['content_sha256'], 'owner')
    assert retired['status'] == 'retired' and retired['approval'] == approved['approval']
    assert catalog.retire(pid, draft['id'], draft['content_sha256'], 'someone-else') == retired
    with pytest.raises(ValueError, match='已退休'):
        catalog.get_approved_catalog(pid, draft['id'])
    with pytest.raises(ValueError, match='已退休'):
        approve(pid, draft)


def test_immutable_versions_preserve_approval_and_explicit_months():
    pid, s, _, first = drafted()
    approved = approve(pid, first)
    original_file = workspace.read_file(pid, first['definition_path'])
    assert catalog.propose(pid, definition(s), 'another-creator') == approved
    changed = definition(s); changed['fields'][0]['period'] = '2026-11'
    second = catalog.propose(pid, changed, 'owner')
    assert second['version'] == 2 and second['id'] != first['id']
    assert second['status'] == 'draft' and second['approval'] is None
    assert catalog.get_approved_catalog(pid, first['id']) == approved
    assert workspace.read_file(pid, first['definition_path']) == original_file
    page = catalog.list_catalogs(pid, limit=1)
    assert page['total'] == 2 and page['next_offset'] == 1
    assert catalog.list_catalogs(pid, offset=1, limit=1)['next_offset'] is None


def test_cross_project_access_and_unknown_project_are_rejected():
    pid, s, _, draft = drafted(); other = project()
    for fn in (catalog.get_catalog, catalog.get_approved_catalog):
        with pytest.raises(ValueError, match='当前项目'):
            fn(other, draft['id'])
    with pytest.raises(ValueError, match='当前项目'):
        catalog.propose(other, definition(s), 'owner')
    for fn in (catalog.approve, catalog.retire):
        with pytest.raises(ValueError, match='当前项目'):
            fn(other, draft['id'], draft['content_sha256'], 'owner')
    assert catalog.list_catalogs(other)['items'] == []
    for fn, args in ((catalog.get_catalog, (draft['id'],)),
                     (catalog.list_catalogs, ()),
                     (catalog.propose, (definition(s), 'owner')),
                     (catalog.approve, (draft['id'], draft['content_sha256'], 'owner'))):
        with pytest.raises(KeyError):
            fn('p_nonexistent', *args)
    for options in ({'limit': True}, {'offset': -1}, {'status': 'owner_approved'}):
        with pytest.raises(ValueError):
            catalog.list_catalogs(pid, **options)


@pytest.mark.parametrize('key,value', [('meaning', None), ('grain', None), ('unit', ''),
                                      ('aggregation', 'unknown'), ('temporal_kind', 'unknown')])
def test_incomplete_semantics_can_be_drafted_but_never_approved(key, value):
    pid = project(); s, _ = source(pid); payload = definition(s)
    payload['fields'][0][key] = value
    draft = catalog.propose(pid, payload, 'agent-candidate')
    with pytest.raises(ValueError, match='上下文不完整'):
        approve(pid, draft)
    assert catalog.get_catalog(pid, draft['id'])['status'] == 'draft'


@pytest.mark.parametrize('change', [
    {'effective_from': '本月'}, {'effective_from': '2026-02-30'},
    {'effective_to': '2025-12-31'}, {'status': 'approved'},
    {'approval': {'actor': 'model'}}, {'raw_rows': [{'secret': 'ROW'}]},
])
def test_invalid_effective_dates_or_submitted_approval_are_rejected(change):
    pid = project(); s, _ = source(pid)
    with pytest.raises(ValueError):
        catalog.propose(pid, dict(definition(s), **change), 'owner')


def test_relative_period_duplicate_ids_and_cross_source_evidence_are_not_inferred():
    pid = project(); s, _ = source(pid); payload = definition(s)
    payload['fields'][0]['period'] = '本月'
    with pytest.raises(ValueError, match='期间必须'):
        catalog.propose(pid, payload, 'owner')
    payload = definition(s); payload['fields'].append(copy.deepcopy(payload['fields'][0]))
    with pytest.raises(ValueError, match='不能重复'):
        catalog.propose(pid, payload, 'owner')
    payload = definition(s); payload['fields'][0]['provenance'] = [
        {'source_id': 'unverified-other-source', 'locator': 'A1', 'content_sha256': 'a' * 64}]
    with pytest.raises(ValueError, match='同一原件'):
        catalog.propose(pid, payload, 'owner')


def test_definition_file_has_no_quotes_credentials_or_review_authority():
    pid = project(); s, _ = source(pid); payload = definition(s)
    payload['source']['quote'] = 'PRIVATE ORIGINAL ROW'
    payload['fields'][0]['provenance'] = [dict(payload['source'], quote='OTHER PRIVATE ROW')]
    draft = catalog.propose(pid, payload, 'oidc-owner-private-subject')
    text = workspace.read_file(pid, draft['definition_path'])
    serialized = json.dumps(draft)
    for private in ('PRIVATE ORIGINAL ROW', 'OTHER PRIVATE ROW', 'oidc-owner-private-subject'):
        assert private not in text and private not in serialized
    obj = json.loads(text)
    assert not {'approval', 'status', 'actor', 'created_actor_sha256'} & obj.keys()
    obj.update(status='approved', approval={'actor': 'model'})
    workspace.write_file(pid, draft['definition_path'], json.dumps(obj))
    with pytest.raises(ValueError, match='尚未通过'):
        catalog.get_approved_catalog(pid, draft['id'])
    for secret in ('api_key: private', 'https://user:password@example.com/data', 'access_token=private',
                   'https://private-token@example.com/document',
                   'https://example.com/document?token=private123',
                   'https://example.com/document?sig=private123',
                   'https://example.com/document?client_secret=private123',
                   'https://example.com/document?X-Amz-Credential=private123',
                   'https://example.com/document?api%5Fkey=private123'):
        value = definition(s); value['description'] = secret
        with pytest.raises(ValueError, match='凭证'):
            catalog.propose(pid, value, 'owner')
    value = definition(s)
    value['source']['locator'] = 'https://example.com/report?token=private123'
    with pytest.raises(ValueError, match='凭证'):
        catalog.propose(pid, value, 'owner')


def test_actual_source_bytes_and_frozen_revision_are_verified_not_supplied_hashes():
    pid = project(); s, path = source(pid); payload = definition(s)
    supplied = copy.deepcopy(payload); supplied['source']['content_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='摘要或冻结修订'):
        catalog.propose(pid, supplied, 'owner')
    path.write_bytes(b'tampered source bytes')
    with pytest.raises(ValueError, match='原件内容已变化|来源文件内容与冻结版本不一致'):
        catalog.propose(pid, payload, 'owner')
    # Merely assigning a matching source.content_hash does not bypass byte checks.
    fake = patch('source', s['id'], content_hash='0' * 64)
    revision(fake)
    with pytest.raises(ValueError, match='原件内容已变化|来源文件内容与冻结版本不一致'):
        catalog.propose(pid, supplied, 'owner')


def test_approval_rechecks_source_but_new_revisions_do_not_change_history():
    pid, s, path, first = drafted()
    # Even identical bytes with a new server revision need a new package review.
    updated = revision(get('source', s['id']))
    with pytest.raises(ValueError, match='来源修订已变化'):
        approve(pid, first)
    second = catalog.propose(pid, definition(updated), 'owner')
    assert second['version'] == 2 and second['content_sha256'] != first['content_sha256']
    approved = approve(pid, second)
    raw = b'changed next month source definition'
    name = uid() + '.txt'; changed_path = DATA / 'uploads' / name; changed_path.write_bytes(raw)
    current = patch('source', s['id'], secret_config=seal({'stored_name': name}),
                    content_hash=hashlib.sha256(raw).hexdigest())
    current = revision(current)
    third = catalog.propose(pid, definition(current), 'owner')
    assert third['version'] == 3 and third['status'] == 'draft'
    assert path.exists() and path.read_bytes() != raw
    assert catalog.get_catalog(pid, first['id'])['status'] == 'draft'
    assert catalog.get_approved_catalog(pid, second['id']) == approved
    assert approved['source_snapshot']['source_revision_id'] == updated['revision_id']


def test_changed_original_blocks_approval_and_database_definition_tampering_is_detected():
    pid, _, path, draft = drafted()
    path.write_bytes(b'tampered after proposal')
    with pytest.raises(ValueError, match='原件内容已变化|来源文件内容与冻结版本不一致'):
        approve(pid, draft)
    changed = copy.deepcopy(draft['payload']); changed['fields'][0]['unit'] = 'USD'
    patch(catalog.RECORD_KIND, draft['id'], payload=changed)
    with pytest.raises(ValueError, match='完整性'):
        catalog.get_catalog(pid, draft['id'])
    with pytest.raises(ValueError, match='完整性'):
        catalog.list_catalogs(pid)


def test_process_death_after_definition_write_recovers_without_overwriting_version():
    pid = project(); s, _ = source(pid); payload = definition(s)
    # Exit after the atomic/fsynced definition write, before its database insert
    # and commit. This exercises real process death and SQLite rollback rather
    # than a mocked successful transaction.
    child = '''
import json, os, sys
from agent_service import semantic_catalog, workspace
original_write = workspace.write_file
def crash_after_write(*args, **kwargs):
    result = original_write(*args, **kwargs)
    os._exit(73)
workspace.write_file = crash_after_write
semantic_catalog.propose(sys.argv[1], json.loads(sys.argv[2]), 'owner')
'''
    result = subprocess.run([sys.executable, '-c', child, pid, json.dumps(payload)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 73, result.stderr
    assert catalog.list_catalogs(pid)['total'] == 0
    files = workspace.list_files(pid)
    assert len(files) == 1 and files[0]['path'].startswith('semantics/catalogs/')
    orphan_path = files[0]['path']
    orphan_bytes = workspace.path_for(pid, orphan_path).read_bytes()
    assert 'version' not in json.loads(orphan_bytes)
    # Another successful draft consumes version 1 before the original retries.
    different = copy.deepcopy(payload); different['description'] = 'another reviewed definition'
    first = catalog.propose(pid, different, 'owner')
    assert first['version'] == 1
    restored = catalog.propose(pid, payload, 'owner')
    assert restored['version'] == 2 and restored['definition_path'] == orphan_path
    assert workspace.path_for(pid, orphan_path).read_bytes() == orphan_bytes
    assert catalog.list_catalogs(pid)['total'] == 2
    assert catalog.get_catalog(pid, first['id'])['version'] == 1
