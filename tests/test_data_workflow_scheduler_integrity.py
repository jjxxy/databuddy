"""Real workflow scheduling must preserve time zones and accepted work."""
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo

import pytest

from agent_service import config,project_data as data
from agent_service.execution import process_once
from agent_service.jobs import get_job,list_jobs
from agent_service.store import delete,get,patch,put
from test_project_data import document,project,registered,run,source


def prepared():
    pid=project();src=source(pid);wid=registered(pid,document(src['id']))
    first=run(pid,wid);data.publish_result(pid,first['id'],first['commit'])
    return pid,src,wid,first


def finish(job_id):
    for _ in range(100):
        job=get_job(job_id)
        if job['status'] not in ('queued','running','retrying'):return job
        assert process_once()
    raise AssertionError('requested job did not finish')


@pytest.mark.parametrize(('zone','first_due','expected_next'),[
    ('Asia/Shanghai','2026-03-07T01:00:00+00:00','2026-03-08T01:00:00+00:00'),
    # The US spring clock change moves 09:00 local from 14:00 UTC to 13:00 UTC.
    ('America/New_York','2026-03-07T14:00:00+00:00','2026-03-08T13:00:00+00:00'),
])
def test_daily_schedule_preserves_wall_time_across_timezones_and_dst(monkeypatch,zone,first_due,expected_next):
    pid,src,wid,first=prepared();due=datetime.fromisoformat(first_due)
    clock=[due-timedelta(minutes=1)]
    monkeypatch.setattr(config,'TIMEZONE',zone);monkeypatch.setattr(data,'_schedule_now',lambda:clock[0])
    try:
        data.schedule(pid,wid,cron='0 9 * * *')
        assert datetime.fromisoformat(get('data_workflow',wid)['next_due'])==due
        clock[0]=due;data.queue_due()
        state=get('data_workflow',wid)
        assert datetime.fromisoformat(state['next_due'])==datetime.fromisoformat(expected_next)
        assert datetime.fromisoformat(state['next_due']).astimezone(ZoneInfo(zone)).hour==9
        job=list_jobs(target=wid)[0]
        assert job['scheduled_key']==wid+':'+first_due and job['status']=='queued'
        assert finish(job['id'])['status']=='succeeded'
    finally:patch('data_workflow',wid,cron=None,next_due=None,watch_sources=False)


def test_due_run_waits_for_active_job_without_losing_occurrence(monkeypatch):
    pid,src,wid,first=prepared();due=datetime(2026,5,1,1,tzinfo=timezone.utc)
    clock=[due-timedelta(minutes=1)]
    monkeypatch.setattr(config,'TIMEZONE','Asia/Shanghai');monkeypatch.setattr(data,'_schedule_now',lambda:clock[0])
    try:
        data.schedule(pid,wid,cron='0 9 * * *')
        manual=data.enqueue_workflow(pid,wid)
        clock[0]=due;data.queue_due()
        state=get('data_workflow',wid)
        assert datetime.fromisoformat(state['next_due'])==due
        assert '已有任务' in state['refresh_error'] and state['published_run']==first['id']
        assert datetime.fromisoformat(state['refresh_retry_after'])==due+timedelta(seconds=30)
        count=len(list_jobs(target=wid));data.queue_due()
        assert len(list_jobs(target=wid))==count
        assert finish(manual['id'])['status']=='succeeded'
        # A scheduler restart reads the persisted due occurrence and retry time.
        clock[0]=due+timedelta(seconds=31);data.queue_due()
        accepted=list_jobs(target=wid)[0]
        assert accepted['id']!=manual['id'] and accepted['scheduled_key']==wid+':'+due.isoformat()
        assert datetime.fromisoformat(get('data_workflow',wid)['next_due'])>due
        assert finish(accepted['id'])['status']=='succeeded'
        assert get('data_workflow',wid)['published_run']==accepted['id']
        count=len(list_jobs(target=wid));data.queue_due();assert len(list_jobs(target=wid))==count
    finally:patch('data_workflow',wid,cron=None,next_due=None,watch_sources=False)


def test_missing_source_does_not_abort_other_schedules_and_is_retried(monkeypatch):
    pid,bad_source,bad_wid,bad_first=prepared();other_pid,good_source,good_wid,good_first=prepared()
    due=datetime(2026,5,1,1,tzinfo=timezone.utc);clock=[due-timedelta(minutes=1)]
    monkeypatch.setattr(config,'TIMEZONE','Asia/Shanghai');monkeypatch.setattr(data,'_schedule_now',lambda:clock[0])
    try:
        data.schedule(pid,bad_wid,cron='0 9 * * *');data.schedule(other_pid,good_wid,cron='0 9 * * *')
        delete('source',bad_source['id']);clock[0]=due
        data.queue_due()
        bad=get('data_workflow',bad_wid);good=get('data_workflow',good_wid)
        assert datetime.fromisoformat(bad['next_due'])==due and bad['published_run']==bad_first['id']
        assert '来源配置不可用' in bad['refresh_error']
        assert datetime.fromisoformat(good['next_due'])>due
        good_job=list_jobs(target=good_wid)[0];assert finish(good_job['id'])['status']=='succeeded'
        put('source',bad_source,bad_source['id'])
        clock[0]=due+timedelta(seconds=31);data.queue_due()
        restored=list_jobs(target=bad_wid)[0]
        assert restored['scheduled_key']==bad_wid+':'+due.isoformat()
        assert get('data_workflow',bad_wid)['refresh_error'] is None
        assert finish(restored['id'])['status']=='succeeded'
    finally:
        put('source',bad_source,bad_source['id'])
        for wid in (bad_wid,good_wid):patch('data_workflow',wid,cron=None,next_due=None,watch_sources=False)


def test_schedule_update_does_not_overwrite_a_concurrent_user_edit():
    pid,src,wid,first=prepared()
    data.schedule(pid,wid,cron='0 9 * * *');snapshot=get('data_workflow',wid)
    data.schedule(pid,wid,cron='0 10 * * *')
    assert not data._schedule_patch_if_current(snapshot,next_due='2000-01-01T00:00:00+00:00',refresh_error='old')
    assert get('data_workflow',wid)['cron']=='0 10 * * *'
    patch('data_workflow',wid,cron=None,next_due=None,watch_sources=False)


def test_missed_schedule_occurrences_are_disclosed_not_automatically_replayed(monkeypatch):
    pid,src,wid,first=prepared();due=datetime(2026,5,1,1,tzinfo=timezone.utc)
    clock=[due-timedelta(minutes=1)]
    monkeypatch.setattr(config,'TIMEZONE','Asia/Shanghai');monkeypatch.setattr(data,'_schedule_now',lambda:clock[0])
    try:
        data.schedule(pid,wid,cron='0 9 * * *')
        clock[0]=due+timedelta(days=2);before=len(list_jobs(target=wid));data.queue_due()
        assert len(list_jobs(target=wid))==before
        state=get('data_workflow',wid)
        assert state['schedule_gap_pending'] is True and '已暂停' in state['refresh_error']
        assert datetime.fromisoformat(state['next_due'])==due
        assert state['published_run']==first['id']
        # The paused schedule cannot silently execute at a later tick.
        clock[0]=due+timedelta(days=3);data.queue_due()
        assert len(list_jobs(target=wid))==before
        assert get('data_workflow',wid)['published_run']==first['id']
        data.schedule(pid,wid,cron='0 9 * * *')
        assert get('data_workflow',wid)['schedule_gap_pending'] is False
    finally:patch('data_workflow',wid,cron=None,next_due=None,watch_sources=False)


def test_failed_source_change_is_not_reported_as_success_or_blindly_replayed(monkeypatch):
    from test_project_data import replace_source,csv_bytes
    pid,src,wid,first=prepared();clock=[datetime(2026,5,1,1,tzinfo=timezone.utc)]
    monkeypatch.setattr(data,'_schedule_now',lambda:clock[0])
    try:
        data.schedule(pid,wid,watch_sources=True)
        replace_source(src['id'],csv_bytes(duplicate=True));data.queue_due()
        failed=list_jobs(target=wid)[0];assert finish(failed['id'])['status']=='failed'
        count=len(list_jobs(target=wid));data.queue_due()
        state=get('data_workflow',wid)
        assert '上次自动更新失败' in state['refresh_error'] and state['published_run']==first['id']
        assert datetime.fromisoformat(state['refresh_retry_after'])==clock[0]+timedelta(seconds=30)
        clock[0]+=timedelta(seconds=31);data.queue_due()
        assert len(list_jobs(target=wid))==count and '上次自动更新失败' in get('data_workflow',wid)['refresh_error']
        # A genuinely new corrected source version is a new, explicit input.
        replace_source(src['id'],csv_bytes(amount_offset=3))
        clock[0]+=timedelta(seconds=31);data.queue_due()
        corrected=list_jobs(target=wid)[0]
        assert corrected['id']!=failed['id'] and finish(corrected['id'])['status']=='succeeded'
        assert get('data_workflow',wid)['published_run']==corrected['id']
    finally:patch('data_workflow',wid,cron=None,next_due=None,watch_sources=False)


def test_schedule_admission_does_not_overwrite_edit_made_during_enqueue(monkeypatch):
    pid,src,wid,first=prepared();due=datetime(2026,5,1,1,tzinfo=timezone.utc)
    clock=[due-timedelta(minutes=1)]
    monkeypatch.setattr(config,'TIMEZONE','Asia/Shanghai');monkeypatch.setattr(data,'_schedule_now',lambda:clock[0])
    data.schedule(pid,wid,cron='0 9 * * *');clock[0]=due
    enqueue=data.enqueue_workflow;replacement='2026-05-02T02:00:00+00:00'
    def concurrent_edit(*args,**kwargs):
        result=enqueue(*args,**kwargs)
        patch('data_workflow',wid,cron='0 10 * * *',next_due=replacement)
        return result
    monkeypatch.setattr(data,'enqueue_workflow',concurrent_edit)
    try:
        data.queue_due()
        state=get('data_workflow',wid)
        assert state['cron']=='0 10 * * *' and state['next_due']==replacement
        job=list_jobs(target=wid)[0];assert finish(job['id'])['status']=='succeeded'
    finally:patch('data_workflow',wid,cron=None,next_due=None,watch_sources=False)
