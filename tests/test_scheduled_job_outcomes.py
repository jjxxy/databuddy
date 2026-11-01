"""Terminal scheduler receipts survive cancellation and worker loss."""
import time

import pytest

from agent_service import jobs
from agent_service.store import get, patch, put, uid


created_targets=[]


@pytest.fixture(autouse=True)
def clean_test_jobs():
    yield
    from sqlalchemy import delete
    from agent_service.store import items,transaction
    with transaction() as c:
        c.execute(delete(jobs.jobs).where(jobs.jobs.c.target.in_(created_targets)))
        c.execute(delete(items).where(items.c.id.in_(created_targets)))
    created_targets.clear()


def scheduled(kind, status='queued', attempts=0):
    ident=kind+'_'+uid(); created_targets.append(ident); project='p_'+uid(); generation=uid(); commit='a'*40
    common={'project_id':project,'cron':'0 9 * * *','schedule_generation':generation,'published_run':'formal',
            'candidate_run':'candidate'}
    snap={'kind':kind,'generation':generation,'project_id':project,'actor_id':'owner','published_run':'formal'}
    if kind=='pipeline':
        common.update(active_commit=commit,commit=commit,release_mode='manual')
        snap['candidate_run']='candidate'
    elif kind=='data_workflow':
        common.update(active_sha256=commit)
        snap['candidate_run']='candidate'
    else:
        common.update(actor_id='owner',published_digest=commit,published_commit='git',published_turn_id='turn')
        snap.update(actor_id='owner',published_commit='git',published_turn_id='turn')
    put(kind,common,ident)
    job={'kind':kind,'target':ident,'commit':commit,'status':status,'scheduled_key':'s_'+uid(),
         'schedule_snapshot':snap,'workflow_refresh_published':True,'attempts':attempts,'max_attempts':3,
         'lease_token':'token' if status=='running' else None,'lease_until':time.time()-1 if status=='running' else None}
    return ident,jobs.save(job)


@pytest.mark.parametrize('kind',['pipeline','data_workflow','business_flow'])
def test_queued_cancellation_is_visible_and_keeps_formal_result(kind):
    ident,run=scheduled(kind)
    assert jobs.request_cancel(run['id'])['status']=='cancelled'
    result=get(kind,ident);field='schedule_error' if kind=='pipeline' else 'refresh_error'
    assert '已取消' in result[field] and result['published_run']=='formal'


@pytest.mark.parametrize('kind',['pipeline','data_workflow','business_flow'])
def test_worker_loss_stops_blind_replay_and_reports_unknown(kind):
    ident,run=scheduled(kind,'running',1)
    jobs.recover_expired()
    saved=jobs.get_job(run['id']);result=get(kind,ident)
    field='schedule_error' if kind=='pipeline' else 'refresh_error'
    assert saved['status']=='failed' and saved['error_code']=='WORKER_LOST'
    assert '未知' in result[field] and result['published_run']=='formal'
    assert jobs.heartbeat(run['id'],'token') is False


@pytest.mark.parametrize('kind',['pipeline','data_workflow','business_flow'])
def test_late_terminal_job_does_not_override_new_schedule_or_result(kind):
    ident,run=scheduled(kind)
    field='schedule_error' if kind=='pipeline' else 'refresh_error'
    patch(kind,ident,schedule_generation=uid(),published_run='new',**{field:'新设置'})
    jobs.request_cancel(run['id'])
    result=get(kind,ident)
    assert result[field]=='新设置' and result['published_run']=='new'


def test_already_started_cancellation_does_not_claim_external_action_undone():
    ident,run=scheduled('pipeline','retrying',1)
    jobs.request_cancel(run['id'])
    assert '外部操作状态可能未知' in get('pipeline',ident)['schedule_error']


def test_fenced_business_flow_failure_also_records_notice():
    ident,run=scheduled('business_flow','running',1)
    run['lease_until']=time.time()+30
    jobs.save(run,run['id'])
    jobs.fenced_update(run['id'],'token',status='failed')
    assert '失败' in get('business_flow',ident)['refresh_error']


def test_unscheduled_worker_loss_keeps_existing_retry_policy():
    ident='p_'+uid();created_targets.append(ident);put('pipeline',{'project_id':'p'},ident)
    job=jobs.save({'kind':'pipeline','target':ident,'commit':'a','status':'running',
                   'attempts':1,'max_attempts':3,'lease_token':'t','lease_until':time.time()-1})
    jobs.recover_expired()
    assert jobs.get_job(job['id'])['status']=='retrying'


def test_worker_loss_rechecks_lease_after_candidate_scan(monkeypatch):
    """A renewed lease observed under the row lock must never be expired."""
    from sqlalchemy import update
    ident,run=scheduled('pipeline','running',1)
    # Simulate a heartbeat after discovery but before acquisition of the row.
    original=jobs.transaction
    from contextlib import contextmanager
    @contextmanager
    def raced_transaction():
        monkeypatch.setattr(jobs,'transaction',original)
        with original() as c:
            c.execute(update(jobs.jobs).where(jobs.jobs.c.id==run['id']).values(lease_until=time.time()+60))
        with original() as c:
            yield c
    monkeypatch.setattr(jobs,'transaction',raced_transaction)
    jobs.recover_expired()
    assert jobs.get_job(run['id'])['status']=='running'
    assert not get('pipeline',ident).get('schedule_error')


def test_business_flow_scheduler_cas_preserves_fast_failure():
    from agent_service import business_flows
    ident,run=scheduled('business_flow')
    old=get('business_flow',ident)
    jobs.request_cancel(run['id'])
    assert not business_flows._patch_schedule_if_current(old,refresh_error=None,next_due='tomorrow')
    assert '已取消' in get('business_flow',ident)['refresh_error']


def test_multiple_unknown_runs_require_separate_reconciliation_and_explicit_reschedule():
    from agent_service.schedule_resolution import resolve
    from agent_service.api import _schedule,ScheduleInput
    ident,first=scheduled('pipeline','running',1)
    second=dict(first,id=uid(),scheduled_key='s_'+uid())
    second=jobs.save(second,second['id'])
    jobs.recover_expired()
    target=get('pipeline',ident)
    assert set(target['schedule_uncertain_runs'])=={first['id'],second['id']}
    first_id=target['schedule_uncertain_run'];other=next(x for x in target['schedule_uncertain_runs'] if x!=first_id)
    project=target['project_id']
    with pytest.raises(ValueError):resolve('other_project',first_id,'owner','executed','查看外部回执')
    with pytest.raises(ValueError):resolve(project,first_id,'u_'+'a'*64,'executed','查看外部回执')
    with pytest.raises(ValueError):resolve(project,other,'owner','executed','查看外部回执')
    _schedule(ident,ScheduleInput(cron='0 10 * * *'))
    assert get('pipeline',ident)['schedule_uncertain_run']==first_id
    result=resolve(project,first_id,'owner','executed','查看外部回执')
    assert result['remaining_count']==1 and get('pipeline',ident)['schedule_uncertain_run']==other
    assert resolve(project,first_id,'owner','executed','查看外部回执')['remaining_count']==1
    with pytest.raises(ValueError):resolve(project,first_id,'owner','not_executed','查看外部回执')
    result=resolve(project,other,'owner','not_executed','外部系统确认没有执行')
    assert result['remaining_count']==0
    target=get('pipeline',ident)
    assert target['schedule_uncertain_run'] is None and target['schedule_resume_required']
    _schedule(ident,ScheduleInput(cron='0 10 * * *'))
    assert not get('pipeline',ident)['schedule_resume_required']


def test_old_missing_actor_schedules_are_paused_visibly(monkeypatch):
    from datetime import datetime,timedelta,timezone
    from agent_service import execution
    from agent_service.store import all_items
    ident,run=scheduled('pipeline')
    jobs.request_cancel(run['id'])
    due=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    patch('pipeline',ident,schedule_actor_id=None,next_due=due,schedule_error=None)
    import agent_service.store as store
    monkeypatch.setattr(store,'all_items',lambda kind:[get('pipeline',ident)] if kind=='pipeline' else all_items(kind))
    monkeypatch.setattr(execution,'enqueue',lambda *args,**kwargs:pytest.fail('must not enqueue without actor'))
    execution._queue_due()
    target=get('pipeline',ident)
    assert target['next_due']==due and '重新设置' in target['schedule_error']


def test_legacy_scheduler_key_identification_excludes_chat_and_agent_operations():
    assert jobs.is_scheduler_key('pipeline','abc','abc:2026-11-01T09:00:00+00:00')
    assert jobs.is_scheduler_key('data_workflow','abc','abc:'+'a'*64)
    assert not jobs.is_scheduler_key('pipeline','abc','chat:request')
    assert not jobs.is_scheduler_key('data_workflow','abc','dataop_123')
    assert not jobs.is_scheduler_key('business_flow','abc','bf-run:'+'a'*64)
    assert not jobs.is_scheduler_key('pipeline','abc','abc:2026-11-01T09:00:00')


@pytest.mark.parametrize('kind',['pipeline','data_workflow'])
def test_legacy_queued_task_never_executes_without_reauthorization(kind):
    ident,run=scheduled(kind)
    run['schedule_snapshot']=None
    run['scheduled_key']=ident+':2026-11-01T09:00:00+00:00'
    jobs.save(run,run['id'])
    jobs._quarantine_legacy_job(run['id'],ident)
    assert jobs.get_job(run['id'])['status']=='cancelled'
    target=get(kind,ident);field='schedule_error' if kind=='pipeline' else 'refresh_error'
    assert target['schedule_resume_required'] and '重新设置' in target[field]


@pytest.mark.parametrize('kind',['pipeline','data_workflow'])
def test_legacy_lost_task_requires_reauthorization_then_explicit_review(kind):
    from agent_service.schedule_resolution import resolve
    ident,run=scheduled(kind,'running',1)
    run['schedule_snapshot']=None
    run['scheduled_key']=ident+':2026-11-01T09:00:00+00:00'
    jobs.save(run,run['id'])
    jobs.recover_expired()
    target=get(kind,ident);saved=jobs.get_job(run['id']);project=target['project_id']
    assert saved['status']=='failed' and saved['schedule_snapshot']['legacy_unattributed']
    assert target['schedule_uncertain_run']==run['id']
    with pytest.raises(ValueError,match='先重新设置定时'):
        resolve(project,run['id'],'owner','not_executed','核对外部回执')
    patch(kind,ident,schedule_generation=uid(),schedule_actor_id='owner')
    with pytest.raises(ValueError):resolve(project,run['id'],'u_'+'a'*64,'not_executed','核对外部回执')
    assert resolve(project,run['id'],'owner','not_executed','核对外部回执')['resolved']
    assert get(kind,ident)['schedule_resume_required']


def test_legacy_business_flow_loss_preserves_original_unknown_resolution():
    from agent_service.business_flows import _resolution_view
    ident,run=scheduled('business_flow','running',1)
    run['schedule_snapshot']=None
    jobs.save(run,run['id'])
    jobs.recover_expired()
    saved=jobs.get_job(run['id'])
    assert saved['status']=='failed'
    # Historical flow engine records the running step before its side effect.
    state={'status':'running','steps':[{'status':'running','replay_safe':False}]}
    saved.update(project_id=get('business_flow',ident)['project_id'],actor_id='owner')
    assert _resolution_view(saved,state)['can_confirm_not_executed']


def test_manual_runs_cannot_bypass_unknown_schedule(monkeypatch):
    from agent_service import execution,project_data,business_flows
    for kind in ('pipeline','data_workflow','business_flow'):
        ident,run=scheduled(kind,'running',1)
        jobs.recover_expired()
        target=get(kind,ident)
        if kind=='business_flow':
            put('project',{'name':'test'},target['project_id'])
            created_targets.append(target['project_id'])
        with pytest.raises(ValueError,match='待核对'):
            if kind=='pipeline':execution.enqueue('pipeline',ident,run['commit'])
            elif kind=='data_workflow':project_data.enqueue_workflow(target['project_id'],ident)
            else:business_flows.submit_run(target['project_id'],ident,'owner',uid(),run['commit'],{},True)


def test_claim_checks_only_selected_normal_job_even_with_large_backlog(monkeypatch):
    from contextlib import contextmanager
    from sqlalchemy import update
    from agent_service.store import engine
    # A mixture of modern scheduled and conversation jobs must not each be
    # locked and re-read on every worker claim.
    for i in range(80):
        target='backlog_'+uid();created_targets.append(target)
        key=(target+':2026-11-01T09:00:00+00:00') if i%2 else ('chat:'+uid())
        jobs.save({'kind':'pipeline','target':target,'commit':'commit','status':'queued',
                   'scheduled_key':key,'schedule_snapshot':{'kind':'pipeline'} if i%2 else None})
    selected='selected_'+uid();created_targets.append(selected)
    job=jobs.save({'kind':'pipeline','target':selected,'commit':'commit','status':'queued','scheduled_key':'chat:'+uid()})
    with engine.begin() as c:c.execute(update(jobs.jobs).where(jobs.jobs.c.id==job['id']).values(created=1))
    monkeypatch.setattr(jobs,'recover_expired',lambda:None)
    original=jobs.transaction;calls=[]
    @contextmanager
    def counted():
        calls.append(1)
        with original() as c:yield c
    monkeypatch.setattr(jobs,'transaction',counted)
    assert jobs.claim()['id']==job['id']
    assert len(calls)==1


def test_claim_quarantines_legacy_head_before_claiming_next(monkeypatch):
    from sqlalchemy import update
    from agent_service.store import engine
    monkeypatch.setattr(jobs,'recover_expired',lambda:None)
    legacy=[]
    for index in range(3):
        ident,run=scheduled('pipeline')
        run['schedule_snapshot']=None;run['scheduled_key']=ident+':2026-11-01T09:00:00+00:00'
        jobs.save(run,run['id']);legacy.append(run['id'])
    normal_target='normal_'+uid();created_targets.append(normal_target)
    normal=jobs.save({'kind':'pipeline','target':normal_target,'commit':'x','status':'queued','scheduled_key':'chat:'+uid()})
    with engine.begin() as c:
        for i,ident in enumerate(legacy):c.execute(update(jobs.jobs).where(jobs.jobs.c.id==ident).values(created=i+1))
        c.execute(update(jobs.jobs).where(jobs.jobs.c.id==normal['id']).values(created=5))
    assert jobs.claim()['id']==normal['id']
    assert [jobs.get_job(ident)['status'] for ident in legacy]==['cancelled']*3


def test_claim_rejects_legacy_job_with_missing_target(monkeypatch):
    from sqlalchemy import update
    from agent_service.store import engine
    monkeypatch.setattr(jobs,'recover_expired',lambda:None)
    target='missing_'+uid();created_targets.append(target)
    job=jobs.save({'kind':'pipeline','target':target,'commit':'x','status':'queued',
                   'scheduled_key':target+':2026-11-01T09:00:00+00:00'})
    with engine.begin() as c:c.execute(update(jobs.jobs).where(jobs.jobs.c.id==job['id']).values(created=1))
    jobs.claim()
    result=jobs.get_job(job['id'])
    assert result['status']=='failed' and result['error_code']=='LEGACY_SCHEDULE_TARGET_UNVERIFIABLE'
