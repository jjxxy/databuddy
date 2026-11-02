"""Scheduling from the workbench is bound to a reviewed published version."""
from fastapi.testclient import TestClient

from agent_service.api import app
from agent_service.config import TOKEN, TIMEZONE
from agent_service import project_data as data
from agent_service.store import get, patch
from test_project_data import project, source, document, registered, run


def test_schedule_binds_adopted_version_and_refuses_stale_or_unknown():
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    pid=project();s=source(pid);wid=registered(pid,document(s['id']));root=f'/api/projects/{pid}/data-workflows/{wid}'
    draft=client.get(root,headers=headers).json()
    assert draft['timezone']==TIMEZONE and not draft['published_run']
    body={'workflow_id':wid,'cron':'15 9 * * *','watch_sources':False,'expected_published_sha256':draft['draft_sha256']}
    assert client.put(root+'/schedule',headers=headers,json=body).status_code==400
    job=run(pid,wid);data.publish_result(pid,job['id'],job['commit'])
    adopted=client.get(root,headers=headers).json();assert adopted['published_sha256']==job['commit']
    bad=dict(body,expected_published_sha256='0'*64)
    assert client.put(root+'/schedule',headers=headers,json=bad).status_code==400
    assert get('data_workflow',wid).get('cron') is None
    ok=client.put(root+'/schedule',headers=headers,json=body);assert ok.status_code==200,ok.text
    current=get('data_workflow',wid);assert current['cron']=='15 9 * * *' and current['schedule_actor_id']=='owner'
    patch('data_workflow',wid,schedule_uncertain_run='unknown-run')
    blocked=client.put(root+'/schedule',headers=headers,json=body);assert blocked.status_code==400
    paused=client.put(root+'/schedule',headers=headers,json=dict(body,cron=None));assert paused.status_code==200,paused.text
    assert get('data_workflow',wid)['cron'] is None
    # Once the unknown run has actually been resolved, user may explicitly
    # set a new schedule, even when the resolution requests manual resumption.
    patch('data_workflow',wid,schedule_uncertain_run=None,schedule_resume_required=True)
    resumed=client.put(root+'/schedule',headers=headers,json=body);assert resumed.status_code==200,resumed.text
    assert get('data_workflow',wid)['schedule_resume_required'] is False


def test_legacy_schedule_call_without_new_version_argument_remains_compatible():
    pid=project();s=source(pid);wid=registered(pid,document(s['id']));job=run(pid,wid)
    data.publish_result(pid,job['id'],job['commit'])
    assert data.schedule(pid,wid,cron='0 9 * * *')['cron']=='0 9 * * *'


def test_schedule_rechecks_adopted_version_after_initial_read(monkeypatch):
    import pytest
    pid=project();s=source(pid);wid=registered(pid,document(s['id']));job=run(pid,wid)
    data.publish_result(pid,job['id'],job['commit'])
    original_owned=data.owned
    def changed_between_reads(*args,**kwargs):
        old=original_owned(*args,**kwargs)
        patch('data_workflow',wid,active_sha256='f'*64)
        return old
    monkeypatch.setattr(data,'owned',changed_between_reads)
    with pytest.raises(ValueError,match='正式版本已变化'):
        data.schedule(pid,wid,cron='0 9 * * *',expected_published_sha256=job['commit'])
    assert get('data_workflow',wid).get('cron') is None
