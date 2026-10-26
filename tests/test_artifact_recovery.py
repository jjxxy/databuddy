"""Kill export processes at publication boundaries, then recover using DB hashes."""
import hashlib
import os
import subprocess
import sys
import time

import pytest
from sqlalchemy import update

from agent_service import business_reports as reports, jobs, project_data
from agent_service.execution import checkpoint
from agent_service.result_delivery import DeliveryError, durable_cached_artifact
from agent_service.store import all_items, delete, engine, get, put, uid, now
from test_business_reports import real_result, recipe


@pytest.mark.parametrize('format', ['xlsx', 'handoff', 'docx', 'pptx'])
@pytest.mark.parametrize('boundary', ['before_rename', 'before_commit'])
def test_killed_export_recovers_verified_bytes_from_independent_pending_receipt(format, boundary):
    pid, rid = real_result()
    report_id = None
    if format in ('docx', 'pptx'):
        current = recipe()
        current['expected_schema_sha256'] = reports.inspect_result(pid, rid)['schema_sha256']
        report_id = reports.render(pid, rid, current)['id']
        target = reports.report_file(pid, report_id, 'manifest').parent / ('report.' + format)
    else:
        folder, _ = project_data.result_folder(pid, rid)
        target = folder / ('result-text_exact.xlsx' if format == 'xlsx' else 'handoff.zip')
    # os._exit bypasses exception handlers and cleanup. Both the DB receipt and
    # fsynced filesystem state must survive the abruptly terminated interpreter.
    script = '''
import os, sys
from pathlib import Path
from agent_service import business_reports as reports, project_data, result_delivery
pid,rid,report_id,fmt,boundary,target=sys.argv[1:]
module=reports if fmt in ('docx','pptx') else project_data
original_put=module.put
def fault_put(kind,value,item_id=None):
    committed=(kind=='data_export' and value.get('state')=='committed') or (kind=='business_report' and fmt in value.get('office_exports',{}))
    if boundary=='before_commit' and committed:os._exit(73)
    return original_put(kind,value,item_id)
module.put=fault_put
original_replace=result_delivery.os.replace
def fault_replace(source,destination):
    if boundary=='before_rename' and Path(destination)==Path(target):os._exit(73)
    return original_replace(source,destination)
result_delivery.os.replace=fault_replace
if fmt in ('docx','pptx'):reports.report_file(pid,report_id,fmt)
else:project_data.result_download(pid,rid,fmt)
raise SystemExit('fault boundary was not reached')
'''
    killed = subprocess.run([sys.executable, '-c', script, pid, rid, report_id or '', format,
                             boundary, str(target)], env=dict(os.environ), capture_output=True,
                            text=True, timeout=30)
    assert killed.returncode == 73, killed.stderr
    assert target.exists() == (boundary == 'before_commit')
    if report_id:
        record = get('business_report', report_id)
        trusted = record['office_export_pending'][format]['sha256']
        # A normal request to reuse the saved recipe must retain pending hashes.
        assert reports.render_saved(pid, rid, 'reports/month-end.json')['id'] == report_id
        recovered = reports.report_file(pid, report_id, format)
        record = get('business_report', report_id)
        assert record['office_exports'][format] == trusted
        assert format not in record['office_export_pending']
    else:
        receipt = next(x for x in all_items('data_export') if x['run_id'] == rid)
        trusted = receipt['sha256']
        assert receipt['state'] == 'pending'
        recovered = project_data.result_download(pid, rid, format)
        assert get('data_export', receipt['id'])['state'] == 'committed'
    assert recovered == target and hashlib.sha256(target.read_bytes()).hexdigest() == trusted
    target.write_bytes(target.read_bytes() + b'tampered')
    with pytest.raises(ValueError, match='完整性'):
        if report_id:reports.report_file(pid, report_id, format)
        else:project_data.result_download(pid, rid, format)


@pytest.mark.parametrize('published', [False, True])
def test_pending_receipt_does_not_authorize_tampered_staged_or_published_bytes(tmp_path, published):
    target = tmp_path / 'report.docx'
    candidate = target if published else tmp_path / '.report.pending.docx'
    candidate.write_bytes(b'tampered')
    trusted = hashlib.sha256(b'original').hexdigest()
    def forbidden(*args):raise AssertionError('Must not rebuild, return or commit tampered bytes')
    with pytest.raises(DeliveryError, match='完整性'):
        durable_cached_artifact(target, expected_sha256=None, pending_sha256=trusted,
            build=forbidden, save_pending=forbidden, save_committed=forbidden)
    assert candidate.read_bytes() == b'tampered'


def test_published_orphan_without_independent_receipt_is_never_adopted(tmp_path):
    target = tmp_path / 'result.xlsx';target.write_bytes(b'orphan')
    def forbidden(*args):raise AssertionError('Must not adopt or overwrite an orphan')
    with pytest.raises(DeliveryError, match='独立完整性回执'):
        durable_cached_artifact(target, expected_sha256=None, pending_sha256=None,
            build=forbidden, save_pending=forbidden, save_committed=forbidden)
    assert target.read_bytes() == b'orphan'


def _running(request):
    project = put('project', {'name': 'Lease recovery test', 'created_at': now()}, 'p_' + uid())
    target = put('data_workflow', {'project_id': project['id'], 'candidate_run': 'previous'}, 'wf_' + uid())
    def cleanup():
        # These queue fixtures deliberately contain a nonexistent prior result.
        # Remove both records even on failure so global retention can continue
        # to fail closed on real unknown dependencies in later acceptance tests.
        with engine.begin() as connection:
            connection.execute(jobs.jobs.delete().where(jobs.jobs.c.target==target['id']))
        delete('data_workflow', target['id'])
        delete('project', project['id'])
    request.addfinalizer(cleanup)
    queued = jobs.enqueue('data_workflow', target['id'])
    # Claim this row explicitly without consuming jobs owned by other tests.
    value = dict(queued, status='running', attempts=1, lease_token=uid(), lease_until=time.time()+30)
    return target, jobs.save(value)


def test_expired_worker_cannot_resurrect_lease_or_update_candidate_then_new_attempt_succeeds(request):
    target, old = _running(request);old['_deadline'] = time.monotonic()+30
    with engine.begin() as connection:
        connection.execute(update(jobs.jobs).where(jobs.jobs.c.id==old['id']).values(lease_until=time.time()-1))
    assert jobs.heartbeat(old['id'], old['lease_token']) is False
    with pytest.raises(Exception) as error:checkpoint(old)
    assert error.value.code == 'LEASE_LOST'
    patch = {'kind': 'data_workflow', 'values': {'candidate_run': old['id']}}
    with pytest.raises(RuntimeError, match='租约'):
        jobs.fenced_update(old['id'], old['lease_token'], target_patch=patch, status='validated')
    jobs.recover_expired()
    current = jobs.get_job(old['id'])
    assert current['status'] == 'retrying' and current['lease_token'] is None
    new = jobs.save(dict(current, status='running', attempts=2, lease_token=uid(), lease_until=time.time()+30))
    with pytest.raises(RuntimeError, match='租约'):
        jobs.fenced_update(old['id'], old['lease_token'], target_patch=patch, status='validated')
    assert get('data_workflow', target['id'])['candidate_run'] == 'previous'
    jobs.fenced_update(new['id'], new['lease_token'], target_patch=patch, status='validated')
    assert get('data_workflow', target['id'])['candidate_run'] == new['id']
    assert jobs.get_job(new['id'])['status'] == 'validated'
    assert jobs.heartbeat(new['id'], new['lease_token']) is False
    with pytest.raises(Exception) as error:checkpoint(dict(new, _deadline=time.monotonic()+30))
    assert error.value.code == 'LEASE_LOST'


def test_candidate_update_and_run_completion_roll_back_if_lease_expires_during_target_check(monkeypatch,request):
    target, run = _running(request);clock = [time.time()]
    monkeypatch.setattr(jobs.time, 'time', lambda: clock[0])
    class SlowComparison:
        def __eq__(self, other):
            clock[0] += 120
            return other == 'previous'
    with pytest.raises(RuntimeError, match='租约'):
        jobs.fenced_update(run['id'], run['lease_token'], status='validated',
            target_patch={'kind': 'data_workflow', 'expected': {'candidate_run': SlowComparison()},
                          'values': {'candidate_run': run['id']}})
    assert get('data_workflow', target['id'])['candidate_run'] == 'previous'
    assert jobs.get_job(run['id'])['status'] == 'running'
    # Leave the shared test queue with no live worker to be picked up later.
    clock[0] -= 120
    jobs.fenced_update(run['id'], run['lease_token'], status='cancelled')
