"""Gateway authority survives async workflow handoff, cancellation and revocation.

Uses real CSV workflow execution, with no model or external service calls.
"""
import inspect
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete as sql_delete

from agent_service import agent_gateway as gateway, project_data as data, execution
from agent_service.jobs import jobs, get_job, save, list_jobs
from agent_service.store import put, get, patch, all_items, delete, engine, uid, now
from test_project_data import project, source, document, registered


@pytest.fixture
def case():
    kinds=('project','source','source_revision','data_workflow','turn','agent_client','gateway_task')
    before={kind:{r['id'] for r in all_items(kind)} for kind in kinds}
    before_jobs={r['id'] for r in list_jobs(100000)}
    pid=project();src=source(pid);wid=registered(pid,document(src['id']))
    client=put('agent_client',{'name':'workflow-test','project_ids':[pid],'approved_model_ids':['finance'],
        'interaction_mode':'work','revision':uid(),'actor_id':'u_'+'a'*64,'enabled':True,
        'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()},'agc_'+uid())
    turn=put('turn',{'project_id':pid,'actor_id':client['actor_id'],'model_id':'finance',
        'gateway_client_id':client['id'],'gateway_client_revision':client['revision'],
        'gateway_actor_id':client['actor_id'],'gateway_deadline':time.time()+600,
        'interaction_mode':'work','state':'waiting_data_workflow','text':'读取并刷新已批准加工流程','created_at':now()},'t_'+uid())
    put('gateway_task',{'client_id':client['id'],'client_revision':client['revision'],
        'project_id':pid,'turn_id':turn['id'],'state':'submitted'},'agt_'+uid())
    yield pid,wid,client,turn
    with engine.begin() as c:
        for run in list_jobs(100000):
            if run['id'] not in before_jobs:c.execute(sql_delete(jobs).where(jobs.c.id==run['id']))
    for kind in kinds:
        for item in all_items(kind):
            if item['id'] not in before[kind]:delete(kind,item['id'])


def enqueue(case, *, link=True):
    """Compatibility branch permits the same cancellation regression on v0.19.0."""
    pid,wid,client,turn=case
    kw={'originating_turn_id':turn['id']} if 'originating_turn_id' in inspect.signature(data.enqueue_workflow).parameters else {}
    run=data.enqueue_workflow(pid,wid,scheduled_key='gateway-test-'+turn['id'],**kw)
    if link:patch('turn',turn['id'],data_workflow_job=run['id'])
    return run


def execute(run):
    running=save(dict(get_job(run['id']),status='running',attempts=1,lease_token=uid(),lease_until=time.time()+60))
    execution.execute(running)
    return get_job(run['id'])


def assert_no_result(case,run):
    value=get_job(run['id']);workflow=get('data_workflow',case[1])
    assert value['status'] in ('cancelled','failed'),value
    assert not value.get('validated'),value
    assert workflow.get('candidate_run')!=run['id']
    assert workflow.get('published_run')!=run['id']


def test_cancel_gateway_workflow_stops_real_queued_work(case):
    run=enqueue(case)
    gateway._cancel_turn(get('turn',case[3]['id']))
    assert get_job(run['id'])['cancel_requested'] is True
    assert get_job(run['id'])['status']=='cancelled'
    assert_no_result(case,run)


def test_cancel_before_turn_job_link_is_not_lost(case):
    run=enqueue(case,link=False)
    assert 'data_workflow_job' not in get('turn',case[3]['id'])
    gateway._cancel_turn(get('turn',case[3]['id']))
    assert get_job(run['id'])['cancel_requested'] is True
    assert_no_result(case,run)


def test_gateway_job_persists_exact_server_authority(case):
    run=enqueue(case)
    authority=run['gateway_authority'];client,turn=case[2:]
    assert authority['turn_id']==turn['id']
    assert authority['client_id']==client['id']
    assert authority['client_revision']==client['revision']
    assert authority['actor_id']==client['actor_id']
    assert authority['project_id']==case[0]


@pytest.mark.parametrize('change',['expire','deadline','revoke','rotate'])
def test_stale_gateway_authority_rejected_before_source_read(case,monkeypatch,change):
    run=enqueue(case);client,turn=case[2:]
    if change=='expire':patch('agent_client',client['id'],expires_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
    elif change=='deadline':patch('turn',turn['id'],gateway_deadline=time.time()-1)
    elif change=='revoke':gateway.revoke_client(client['id'],client['revision'])
    else:gateway.rotate_client(client['id'],client['revision'])
    calls=[]
    monkeypatch.setattr(data,'read_input',lambda *a,**k:calls.append(True))
    execute(run)
    assert calls==[]
    assert_no_result(case,run)


def test_missing_authority_for_linked_gateway_job_fails_closed(case,monkeypatch):
    run=enqueue(case)
    corrupt=get_job(run['id']);corrupt.pop('gateway_authority',None);save(corrupt)
    calls=[]
    monkeypatch.setattr(data,'read_input',lambda *a,**k:calls.append(True))
    execute(run)
    assert calls==[]
    assert_no_result(case,run)


def test_normal_owner_job_still_executes(case):
    run=data.enqueue_workflow(case[0],case[1]);result=execute(run)
    assert result['status']=='validated',result
    assert result['validated'] and result['records']==215


def test_gateway_does_not_borrow_or_cancel_owner_active_job(case):
    run=data.enqueue_workflow(case[0],case[1])
    with pytest.raises(ValueError):enqueue(case)
    gateway._cancel_turn(get('turn',case[3]['id']))
    assert not get_job(run['id'])['cancel_requested']
    assert execute(run)['status']=='validated'


def test_owner_does_not_borrow_gateway_active_job(case):
    run=enqueue(case)
    with pytest.raises(ValueError):data.enqueue_workflow(case[0],case[1])
    assert get_job(run['id'])['gateway_authority']['turn_id']==case[3]['id']


def test_cancel_mid_read_stops_final_publication(case,monkeypatch):
    # First publish a normal owner run: later same-plan refresh normally auto-publishes.
    owner=data.enqueue_workflow(case[0],case[1]);execute(owner)
    data.publish_result(case[0],owner['id'],owner['commit'])
    run=enqueue(case);original=data.read_input
    def cancel_after_read(*args,**kwargs):
        result=original(*args,**kwargs)
        gateway._cancel_turn(get('turn',case[3]['id']))
        return result
    monkeypatch.setattr(data,'read_input',cancel_after_read)
    execute(run)
    assert_no_result(case,run)
    assert get('data_workflow',case[1])['published_run']==owner['id']


@pytest.mark.parametrize('field,value',[('client_revision','stale-revision'),('actor_id','owner'),('turn_id','missing-turn')])
def test_persisted_authority_tampering_fails_closed(case,monkeypatch,field,value):
    run=enqueue(case)
    changed=get_job(run['id']);changed['gateway_authority'][field]=value;save(changed)
    calls=[];monkeypatch.setattr(data,'read_input',lambda *a,**k:calls.append(True))
    execute(run)
    assert calls==[]
    assert_no_result(case,run)


def test_revoke_immediately_before_auto_publish_retains_previous_result(case,monkeypatch):
    owner=data.enqueue_workflow(case[0],case[1]);execute(owner)
    data.publish_result(case[0],owner['id'],owner['commit'])
    run=enqueue(case);original=data.publish_result
    def revoke_then_publish(*args,**kwargs):
        gateway.revoke_client(case[2]['id'],case[2]['revision'])
        return original(*args,**kwargs)
    monkeypatch.setattr(data,'publish_result',revoke_then_publish)
    result=execute(run)
    assert result['status']=='cancelled',result
    # Generated bytes can remain; publication is the authority-bearing mutation.
    assert get('data_workflow',case[1])['published_run']==owner['id']


def test_expiry_immediately_before_candidate_commit_is_fenced(case,monkeypatch):
    run=enqueue(case);original=execution.patch_run
    def expire_then_patch(current,**values):
        if values.get('target_patch'):
            patch('agent_client',case[2]['id'],expires_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
        return original(current,**values)
    monkeypatch.setattr(execution,'patch_run',expire_then_patch)
    result=execute(run)
    assert result['status']=='cancelled',result
    assert get('data_workflow',case[1]).get('candidate_run')!=run['id']


def test_valid_gateway_workflow_and_explicit_owner_adoption(case):
    run=enqueue(case);result=execute(run)
    assert result['status']=='validated',result
    assert result['validated'] and result['records']==215
    # Completed task authority may expire; the workspace owner may still adopt
    # a previously verified result explicitly (not a continuing worker commit).
    patch('turn',case[3]['id'],state='done',outcome='complete')
    patch('agent_client',case[2]['id'],enabled=False)
    adopted=data.publish_result(case[0],run['id'],run['commit'])
    assert adopted['published_run']==run['id']


@pytest.mark.parametrize('change',['expire','revoke'])
def test_authority_loss_after_source_read_prevents_publication(case,monkeypatch,change):
    run=enqueue(case);original=data.read_input
    def lose_grant_after_read(*args,**kwargs):
        result=original(*args,**kwargs)
        if change=='expire':patch('agent_client',case[2]['id'],expires_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
        else:gateway.revoke_client(case[2]['id'],case[2]['revision'])
        return result
    monkeypatch.setattr(data,'read_input',lose_grant_after_read)
    execute(run)
    assert_no_result(case,run)


def test_same_client_different_turn_cannot_borrow_active_job(case):
    run=enqueue(case)
    other=put('turn',dict(case[3],id='t_'+uid()))
    with pytest.raises(ValueError):
        data.enqueue_workflow(case[0],case[1],originating_turn_id=other['id'])
    gateway._cancel_turn(other)
    assert not get_job(run['id'])['cancel_requested']
    assert execute(run)['status']=='validated'


def test_revoke_between_initial_check_and_admission_lock_cannot_enqueue(case,monkeypatch):
    from contextlib import contextmanager
    from agent_service import job_authority
    from agent_service.errors import Cancelled
    original=job_authority.client_guard;fired=False
    @contextmanager
    def interleave(identity):
        nonlocal fired
        if not fired:
            fired=True
            gateway.revoke_client(case[2]['id'],case[2]['revision'])
        with original(identity):yield
    monkeypatch.setattr(job_authority,'client_guard',interleave)
    with pytest.raises((ValueError,Cancelled)):
        enqueue(case)
    assert fired
    assert list_jobs(target=case[1])==[]


def test_new_workspace_job_checkpoints_never_scan_turn_history(case,monkeypatch):
    from agent_service import job_authority
    run=data.enqueue_workflow(case[0],case[1])
    assert run['authority_origin']=={'version':1,'kind':'workspace'}
    monkeypatch.setattr(job_authority,'all_items',lambda *a:pytest.fail('new workspace checkpoint scanned legacy history'))
    for _ in range(20):job_authority.check(run)
    assert execute(run)['status']=='validated'


def test_queue_overwrites_caller_supplied_origin_marker(case):
    from agent_service import jobs as queue
    run=queue.enqueue('data_workflow',case[1],extra={'project_id':case[0],
        'authority_origin':{'version':999,'kind':'gateway'}})
    assert run['authority_origin']=={'version':1,'kind':'workspace'}


@pytest.mark.parametrize('marker',[None,{},'workspace',{'version':True,'kind':'workspace'},
    {'version':2,'kind':'workspace'},{'version':1,'kind':'unknown'},
    {'version':1,'kind':'workspace','extra':True},{'version':1,'kind':'gateway'}])
def test_malformed_origin_marker_fails_closed(case,marker):
    from agent_service import job_authority
    from agent_service.errors import Cancelled
    run=data.enqueue_workflow(case[0],case[1])
    run['authority_origin']=marker
    with pytest.raises(Cancelled):job_authority.check(run)


def test_gateway_marker_cannot_be_downgraded_to_workspace(case):
    from agent_service import job_authority
    from agent_service.errors import Cancelled
    run=enqueue(case)
    assert run['authority_origin']=={'version':1,'kind':'gateway'}
    run['authority_origin']={'version':1,'kind':'workspace'}
    with pytest.raises(Cancelled):job_authority.check(run)


def test_unmarked_legacy_linked_gateway_job_still_fails_closed(case,monkeypatch):
    from agent_service import job_authority
    from agent_service.errors import Cancelled
    run=enqueue(case)
    run.pop('authority_origin');run.pop('gateway_authority')
    seen=[];original=job_authority.all_items
    def history(kind):
        seen.append(kind)
        return original(kind)
    monkeypatch.setattr(job_authority,'all_items',history)
    with pytest.raises(Cancelled):job_authority.check(run)
    assert seen==['turn']


def test_legacy_dedup_does_not_silently_upgrade_origin(case):
    run=data.enqueue_workflow(case[0],case[1])
    run.pop('authority_origin');save(run)
    again=data.enqueue_workflow(case[0],case[1])
    assert again['id']==run['id'] and 'authority_origin' not in again
