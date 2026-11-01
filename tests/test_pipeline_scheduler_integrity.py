from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from agent_service import execution
from agent_service.errors import TaskError
from agent_service.store import get, patch, put, uid


def pipeline(monkeypatch, due=None, **overrides):
    due=due or datetime.now(timezone.utc)-timedelta(seconds=1)
    value={'project_id':'project_'+uid(),'cron':'0 9 * * *','next_due':due.isoformat(),
           'active_commit':'a'*40,'commit':'a'*40,'published_run':'published',
           'candidate_run':'candidate','release_mode':'manual'}
    value.update(overrides)
    saved=put('pipeline',value,'pipeline_'+uid())
    import agent_service.store as store
    original=store.all_items
    monkeypatch.setattr(store,'all_items',lambda kind: [get('pipeline',saved['id'])] if kind=='pipeline' else original(kind))
    return saved


def accepted(kind,target,commit,key):
    return {'kind':kind,'target':target,'commit':commit,'scheduled_key':key,'status':'queued'}


def test_failed_admission_preserves_due_and_retries(monkeypatch):
    p=pipeline(monkeypatch)
    monkeypatch.setattr(execution,'enqueue',lambda *a: (_ for _ in ()).throw(ValueError('source unavailable')))
    execution._queue_due()
    state=get('pipeline',p['id'])
    assert state['next_due']==p['next_due'] and state['published_run']=='published'
    assert state['schedule_error'] and state['schedule_retry_after']
    patch('pipeline',p['id'],schedule_retry_after=0)
    monkeypatch.setattr(execution,'enqueue',accepted)
    execution._queue_due()
    state=get('pipeline',p['id'])
    assert state['next_due']>p['next_due'] and state['schedule_error'] is None


def test_another_active_or_failed_occurrence_is_not_accepted(monkeypatch):
    p=pipeline(monkeypatch)
    monkeypatch.setattr(execution,'enqueue',lambda kind,target,commit,key:dict(accepted(kind,target,commit,key),scheduled_key='other'))
    execution._queue_due()
    state=get('pipeline',p['id']);assert state['next_due']==p['next_due'] and state['schedule_error']
    patch('pipeline',p['id'],schedule_retry_after=0)
    monkeypatch.setattr(execution,'enqueue',lambda *args:dict(accepted(*args),status='failed'))
    execution._queue_due()
    state=get('pipeline',p['id']);assert state['next_due']==p['next_due'] and state['schedule_error']


def test_local_timezone_missed_period_and_concurrent_schedule(monkeypatch):
    due=datetime.now(timezone.utc)-timedelta(days=5)
    p=pipeline(monkeypatch,due)
    monkeypatch.setattr(execution,'TIMEZONE','America/New_York')
    monkeypatch.setattr(execution,'enqueue',accepted)
    execution._queue_due()
    state=get('pipeline',p['id'])
    assert state['schedule_gap_pending'] and '未执行' in state['schedule_error']
    assert datetime.fromisoformat(state['next_due']).astimezone(ZoneInfo('America/New_York')).hour==9
    patch('pipeline',p['id'],next_due=due.isoformat())
    replacement=(datetime.now(timezone.utc)+timedelta(days=10)).isoformat()
    def racing(kind,target,commit,key):
        patch('pipeline',target,cron='0 10 * * *',next_due=replacement)
        return accepted(kind,target,commit,key)
    monkeypatch.setattr(execution,'enqueue',racing)
    execution._queue_due()
    state=get('pipeline',p['id'])
    assert state['cron']=='0 10 * * *' and state['next_due']==replacement


def test_failure_notice_only_for_current_workflow(monkeypatch):
    wid='workflow_'+uid()
    w=put('data_workflow',{'project_id':'p','cron':'0 9 * * *','active_sha256':'digest','published_run':'published','candidate_run':'candidate'},wid)
    run={'kind':'data_workflow','target':wid,'commit':'digest','scheduled_key':'scheduled',
         'workflow_refresh_published':True,'workflow_pointer_snapshot':{'published_run':'published','candidate_run':'candidate'}}
    execution._scheduled_failure_notice(run,'failed')
    assert '失败' in get('data_workflow',wid)['refresh_error']
    patch('data_workflow',wid,candidate_run='new',refresh_error='new result')
    execution._scheduled_failure_notice(run,'cancelled')
    assert get('data_workflow',wid)['refresh_error']=='new result'


def test_execute_failure_writes_workflow_notice(monkeypatch,tmp_path):
    wid='workflow_'+uid()
    put('data_workflow',{'project_id':'p','cron':'0 9 * * *','active_sha256':'digest','published_run':'published','candidate_run':'candidate'},wid)
    run={'id':'test_run','target':wid,'kind':'data_workflow','commit':'digest','scheduled_key':'scheduled',
         'workflow_refresh_published':True,'workflow_pointer_snapshot':{'published_run':'published','candidate_run':'candidate'},
         'lease_token':'lease','attempts':1,'max_attempts':1}
    monkeypatch.setattr(execution,'DATA',tmp_path)
    monkeypatch.setattr(execution,'get_job',lambda job_id:{'lease_token':'lease'})
    monkeypatch.setattr(execution,'patch_run',lambda obj,**values:obj.update(values))
    monkeypatch.setattr(execution,'checkpoint',lambda run: (_ for _ in ()).throw(TaskError('failure','TEST')))
    monkeypatch.setattr(execution,'record',lambda *args,**kwargs:None)
    execution.execute(run)
    assert run['status']=='failed' and '失败' in get('data_workflow',wid)['refresh_error']


def test_explicit_reschedule_clears_previous_gap_and_retry(monkeypatch):
    from agent_service.api import _schedule,ScheduleInput
    p=pipeline(monkeypatch,schedule_gap_pending=True,schedule_error='history missing',schedule_retry_after=9999999999)
    _schedule(p['id'],ScheduleInput(cron='0 10 * * *'))
    current=get('pipeline',p['id'])
    assert not current.get('schedule_gap_pending') and current.get('schedule_error') is None
    assert current.get('schedule_retry_after') is None
    assert datetime.fromisoformat(current['next_due'])>datetime.now(timezone.utc)
