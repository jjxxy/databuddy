"""Business versions must restore together and never silently restart schedules."""
import copy
import json

import pytest

from agent_service import project_data as data, workspace
from agent_service.data_workflows import digest
from agent_service.store import get, patch, put, seal, uid
from test_project_data import document, project, registered, run, source


def test_restore_data_workflow_reinstates_the_formal_version_and_pauses_schedule():
    pid=project();s=source(pid);doc=document(s['id']);wid=registered(pid,doc)
    first=run(pid,wid);data.publish_result(pid,first['id'],first['commit'])
    data.schedule(pid,wid,cron='0 9 * * *')
    first_version=workspace.checkpoint(pid,reason='首次正式方案')['commit']
    newdoc=copy.deepcopy(doc);newdoc['plan']['name']='New calculation'
    workspace.write_file(pid,'workflows/orders.json',json.dumps(newdoc));data.register(pid,'workflows/orders.json')
    second=run(pid,wid);data.publish_result(pid,second['id'],second['commit'])
    patch('data_workflow',wid,schedule_uncertain_run='unresolved',schedule_uncertain_runs=['unresolved'])
    workspace.checkpoint(pid,reason='修改正式方案')
    workspace.restore(pid,first_version,'restore-test')
    saved=get('data_workflow',wid)
    assert data.load_document(pid,'workflows/orders.json').plan.name==doc['plan']['name']
    assert saved['active_sha256']==digest(data.WorkflowDocument.model_validate(doc).model_dump())
    assert saved['published_run']==first['id'] and saved['published_run']!=second['id']
    assert saved['schedule_resume_required'] is True
    assert saved['schedule_uncertain_runs']==['unresolved']
    assert saved['schedule_uncertain_run']=='unresolved'
    assert '状态未知' in saved['refresh_error']


def test_old_checkpoint_without_workflow_state_fails_before_restoring_files():
    pid=project();s=source(pid);wid=registered(pid,document(s['id']))
    version=workspace.checkpoint(pid,reason='current')
    target=workspace.resolve(pid,version['commit'])
    from agent_service.store import unseal
    old=unseal(target['secret_state']);old.pop('data_workflows')
    patch('checkpoint',target['id'],secret_state=seal(old))
    current=workspace.read_file(pid,'workflows/orders.json')
    with pytest.raises(ValueError,match='未保存数据加工流程状态'):
        workspace.restore(pid,version['commit'],'restore-old')
    assert workspace.read_file(pid,'workflows/orders.json')==current
    assert not get('data_workflow',wid).get('archived')


def test_restore_reinstates_timezone_but_requires_explicit_rescheduling():
    pid=project();fid='flow_'+uid();turn='turn_'+uid()
    put('business_flow',{'project_id':pid,'actor_id':'owner','name':'定时报告','turn_id':turn,
        'path':'flows/report.json','cron':'0 9 * * *','next_due':'2026-01-01T01:00:00+00:00',
        'schedule_timezone':'Asia/Shanghai','schedule_inputs':seal({'period':'2026-01'}),
        'schedule_source_bindings':{},'published_digest':'published'},fid)
    first=workspace.checkpoint(pid,reason='上海计划')['commit']
    patch('business_flow',fid,schedule_timezone='America/New_York',schedule_inputs=seal({'period':'2026-02'}))
    second=workspace.checkpoint(pid,reason='纽约计划')['commit']
    assert first!=second
    workspace.restore(pid,first,'restore-timezone')
    restored=get('business_flow',fid)
    assert restored['schedule_timezone']=='Asia/Shanghai'
    assert restored['schedule_resume_required'] is True and '已暂停' in restored['refresh_error']
    from agent_service import business_flows
    from agent_service.jobs import list_jobs
    business_flows.queue_due();assert not list_jobs(target=fid)


def test_restore_journal_blocks_reads_and_new_jobs_until_files_are_restored(monkeypatch):
    from fastapi.testclient import TestClient
    from agent_service.api import app
    from agent_service.config import TOKEN
    from agent_service.jobs import enqueue,list_jobs
    pid=project();s=source(pid);wid=registered(pid,document(s['id']))
    version=workspace.checkpoint(pid,reason='before')['commit']
    workspace.write_file(pid,'memo.md','after')
    workspace.checkpoint(pid,reason='after')
    original=workspace._apply_restore
    observations=[]
    def probe(journal):
        client=TestClient(app)
        result=client.get(f'/api/projects/{pid}/data-workflows/{wid}',headers={'Authorization':'Bearer '+TOKEN})
        observations.append((result.status_code,result.text))
        with pytest.raises(ValueError,match='回退尚未完成'):
            enqueue('data_workflow',wid,extra={'project_id':pid})
        return original(journal)
    monkeypatch.setattr(workspace,'_apply_restore',probe)
    workspace.restore(pid,version,'restore-guard')
    assert observations and observations[0][0]>=400 and '回退尚未完成' in observations[0][1]
    assert not list_jobs(target=wid)
    assert TestClient(app).get(f'/api/projects/{pid}/data-workflows/{wid}',headers={'Authorization':'Bearer '+TOKEN}).status_code==200


def test_prepared_job_from_old_project_revision_cannot_enter_after_restore():
    from agent_service.jobs import enqueue,list_jobs
    pid=project();s=source(pid);wid=registered(pid,document(s['id']))
    version=workspace.checkpoint(pid,reason='before')['commit']
    old_revision=get('project',pid).get('context_visibility_revision',0)
    workspace.write_file(pid,'memo.md','changed');workspace.checkpoint(pid,reason='after')
    workspace.restore(pid,version,'restore-stale')
    assert get('project',pid)['context_visibility_revision']>old_revision
    with pytest.raises(ValueError,match='项目版本在准备运行期间已变化'):
        enqueue('data_workflow',wid,extra={'project_id':pid,'workspace_visibility_revision':old_revision})
    assert not list_jobs(target=wid)


def test_old_or_reconfigured_schedules_never_auto_adopt_results():
    from agent_service.project_data import _schedule_blocks_auto_adoption as data_guard
    from agent_service.releases import _schedule_blocks_auto_adoption as pipeline_guard
    for guard,kind in ((data_guard,'data_workflow'),(pipeline_guard,'pipeline')):
        target={'cron':'0 9 * * *','schedule_generation':'new'}
        run={'target':'item','scheduled_key':'item:2026-01-01T01:00:00+00:00',
             'schedule_snapshot':{'generation':'old'}}
        assert guard(run,target)
        run['schedule_snapshot']['generation']='new'
        assert not guard(run,target)
        assert guard(run,{**target,'schedule_resume_required':True})
        assert guard(run,{**target,'schedule_gap_pending':True})
        assert guard(run,{**target,'schedule_uncertain_run':'unknown'})
        assert guard({**run,'schedule_snapshot':None},target)
