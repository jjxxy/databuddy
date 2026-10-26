import copy
import hashlib
import threading

import pytest

from agent_service import semantic_catalog as catalog, semantic_service as service, workspace
from agent_service.config import DATA
from agent_service.sources import revision
from agent_service.store import all_items, patch, put, seal, uid


def field(identifier='revenue'):
    return {'id': identifier, 'name': '净收入', 'meaning': '已付款订单扣除退款后的金额',
            'kind': 'measure', 'grain': ['office', 'month'], 'unit': 'CNY',
            'value_format': 'decimal', 'temporal_kind': 'interval',
            'period': '2026-10', 'aggregation': 'sum'}


def package():
    pid = put('project', {'name': '语义包匹配'}, 'p_' + uid())['id']
    raw = b'approved business definition'; name = uid() + '.txt'
    path = DATA / 'uploads' / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
    source = revision(put('source', {'project_id': pid, 'name': 'definitions.txt', 'kind': 'file',
        'secret_config': seal({'stored_name': name}), 'content_hash': hashlib.sha256(raw).hexdigest(),
        'status': 'ready'}, 's_' + uid()))
    record = catalog.propose(pid, {'key': 'income', 'name': '净收入口径',
        'source': {'source_id': source['id'], 'locator': 'definition 1', 'content_sha256': source['content_hash']},
        'effective_from': '2026-01-01', 'effective_to': '2026-12-31', 'fields': [field()]}, 'owner')
    return pid, record


def propose(pid, record, **kwargs):
    return service.propose_from_catalog(pid, field('source'), record['id'],
        record['content_sha256'], as_of_date=kwargs.pop('as_of_date', '2026-10-01'), **kwargs)


def test_reviewed_package_targets_are_server_resolved_and_still_need_mapping_review():
    pid, record = package()
    with pytest.raises(ValueError, match='尚未通过'):
        propose(pid, record)
    catalog.approve(pid, record['id'], record['content_sha256'], 'owner')
    aligned = propose(pid, record)
    assert aligned['status'] == 'proposed'
    assert aligned['catalog_binding']['as_of_date'] == '2026-10-01'
    assert aligned['catalog_binding']['trust_level'] == 'owner_reviewed'
    assert aligned['payload']['candidates'][0]['target'] == record['payload']['fields'][0]
    with pytest.raises(ValueError, match='人工核对'):
        service.get_reviewed_alignment(pid, aligned['id'])
    service.review(pid, aligned['id'], aligned['content_sha256'], 'revenue', 'owner')
    assert service.get_reviewed_alignment(pid, aligned['id']).target_id == 'revenue'
    # An identical ad hoc target does not inherit the package binding/consent.
    ad_hoc = service.propose(pid, field('source'), record['payload']['fields'])
    assert ad_hoc['id'] != aligned['id'] and ad_hoc['status'] == 'proposed'
    assert 'catalog_binding' not in ad_hoc


@pytest.mark.parametrize('day', ['2025-12-31', '2027-01-01', '2026-02-30', '', '2026-1-1'])
def test_applicability_date_is_explicit_and_validated(day):
    pid, record = package(); catalog.approve(pid, record['id'], record['content_sha256'], 'owner')
    with pytest.raises(ValueError): propose(pid, record, as_of_date=day)


def test_retirement_blocks_future_consumption_but_preserves_history():
    pid, record = package(); catalog.approve(pid, record['id'], record['content_sha256'], 'owner')
    aligned = propose(pid, record)
    service.review(pid, aligned['id'], aligned['content_sha256'], 'revenue', 'owner')
    catalog.retire(pid, record['id'], record['content_sha256'], 'owner')
    assert service.get_proposal(pid, aligned['id'])['status'] == 'approved'
    with pytest.raises(ValueError, match='已退休'): propose(pid, record)
    with pytest.raises(ValueError, match='已退休'): service.get_reviewed_alignment(pid, aligned['id'])


def test_binding_cannot_be_forged_by_editing_project_definition_or_target():
    pid, record = package(); catalog.approve(pid, record['id'], record['content_sha256'], 'owner')
    aligned = propose(pid, record)
    workspace.write_file(pid, aligned['definition_path'], '{"status":"approved"}')
    assert service.get_proposal(pid, aligned['id'])['status'] == 'proposed'
    changed = copy.deepcopy(aligned['catalog_binding']); changed['as_of_date'] = '2026-11-01'
    patch(service.RECORD_KIND, aligned['id'], catalog_binding=changed)
    with pytest.raises(ValueError, match='完整性'): service.get_proposal(pid, aligned['id'])


def test_package_reference_is_project_and_digest_bound():
    pid, record = package(); catalog.approve(pid, record['id'], record['content_sha256'], 'owner')
    other = put('project', {'name': 'other'}, 'p_' + uid())['id']
    with pytest.raises(ValueError, match='当前项目'): propose(other, record)
    with pytest.raises(ValueError, match='固定版本'):
        service.propose_from_catalog(pid, field('source'), record['id'], '0' * 64, as_of_date='2026-10-01')


@pytest.mark.parametrize('damage', [
    {'catalog_binding': None},
    {'schema_version': 1},
    {'schema_version': 1, 'catalog_binding': None},
])
def test_partial_metadata_binding_loss_cannot_bypass_retirement(damage):
    pid, record = package(); catalog.approve(pid, record['id'], record['content_sha256'], 'owner')
    aligned = propose(pid, record)
    service.review(pid, aligned['id'], aligned['content_sha256'], 'revenue', 'owner')
    catalog.retire(pid, record['id'], record['content_sha256'], 'owner')
    with pytest.raises(ValueError, match='已退休'):
        service.get_reviewed_alignment(pid, aligned['id'])
    # A partial metadata fault must not reinterpret a catalog-bound review as
    # an ad hoc legacy review. This does not model a database administrator
    # rewriting all data, hashes, identities and review receipts together.
    patch(service.RECORD_KIND, aligned['id'], **damage)
    with pytest.raises(ValueError):
        service.get_proposal(pid, aligned['id'])
    with pytest.raises(ValueError):
        service.get_reviewed_alignment(pid, aligned['id'])


def test_retirement_between_catalog_read_and_save_creates_no_proposal(monkeypatch):
    pid, record = package(); catalog.approve(pid, record['id'], record['content_sha256'], 'owner')
    read = threading.Event(); resume = threading.Event(); results = []; errors = []
    original = catalog.get_approved_catalog

    def paused_catalog_read(*args, **kwargs):
        value = original(*args, **kwargs)
        read.set()
        if not resume.wait(5):raise RuntimeError('fixture catalog-read coordination timed out')
        return value

    monkeypatch.setattr(catalog, 'get_approved_catalog', paused_catalog_read)

    def create_proposal():
        try:results.append(propose(pid, record))
        except Exception as error:errors.append(error)

    worker = threading.Thread(target=create_proposal)
    worker.start()
    try:
        assert read.wait(5)
        retired = catalog.retire(pid, record['id'], record['content_sha256'], 'owner')
        assert retired['status'] == 'retired'
    finally:
        resume.set(); worker.join(5)
    assert not worker.is_alive()
    assert not results and len(errors) == 1 and isinstance(errors[0], ValueError)
    assert not [r for r in all_items(service.RECORD_KIND) if r.get('project_id') == pid]
    assert not [f for f in workspace.list_files(pid) if f['path'].startswith('semantics/proposals/')]


def test_review_waits_for_retirement_project_lock_before_starting_transaction(monkeypatch):
    pid, record = package(); catalog.approve(pid, record['id'], record['content_sha256'], 'owner')
    aligned = propose(pid, record)
    held = threading.Event(); release = threading.Event(); review_attempted = threading.Event()
    review_done = threading.Event(); reviews = []; review_errors = []; retirements = []; retire_errors = []
    original_lock = workspace.lock

    class CoordinatedLock:
        def __init__(self, project_id):self.real = original_lock(project_id)
        def __enter__(self):
            name = threading.current_thread().name
            if name == 'catalog-review-after-retire':review_attempted.set()
            acquired = self.real.__enter__()
            if name == 'catalog-retire-before-review':
                # Pause after the actual project lock is held but before the
                # retirement DB transaction. A DB-wide SQLite write lock must
                # not be the reason the competing review is serialized.
                held.set()
                if not release.wait(5):
                    self.real.__exit__(None, None, None)
                    raise RuntimeError('fixture retirement-lock coordination timed out')
            return acquired
        def __exit__(self, *args):return self.real.__exit__(*args)

    monkeypatch.setattr(workspace, 'lock', CoordinatedLock)

    def retire_package():
        try:retirements.append(catalog.retire(pid, record['id'], record['content_sha256'], 'owner'))
        except Exception as error:retire_errors.append(error)

    def approve_alignment():
        try:reviews.append(service.review(pid, aligned['id'], aligned['content_sha256'], 'revenue', 'owner'))
        except Exception as error:review_errors.append(error)
        finally:review_done.set()

    retirement = threading.Thread(target=retire_package, name='catalog-retire-before-review')
    review = threading.Thread(target=approve_alignment, name='catalog-review-after-retire')
    retirement.start()
    try:
        assert held.wait(5)
        review.start()
        assert review_attempted.wait(3)
        assert not review_done.is_set()
    finally:
        release.set(); retirement.join(5)
        if review.ident is not None:review.join(5)
    assert not retirement.is_alive() and not review.is_alive()
    assert not retire_errors and retirements[0]['status'] == 'retired'
    assert not reviews and len(review_errors) == 1 and isinstance(review_errors[0], ValueError)
    assert service.get_proposal(pid, aligned['id'])['status'] == 'proposed'
