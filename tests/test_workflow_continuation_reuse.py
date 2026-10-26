"""Repeated model choices reuse verified task effects, not a global data cache."""
import copy,json

import pytest

from agent_service import agent,conversation,data_tools,project_data as data,provider,workspace
from agent_service.actor_context import actor_scope
from agent_service.execution import process_once
from agent_service.jobs import get_job,list_jobs,save
from agent_service.store import get,put,patch,uid,now
from agent_service.tasks import ensure
from test_project_data import project,source,csv_bytes,document,registered,replace_source


def turn(pid,text='请整理全部订单并运行加工方案',**values):
    value=put('turn',{'project_id':pid,'text':text,'interaction_mode':'work','state':'agent',
        'created_at':now(),**values},'t_'+uid())
    ensure(value)
    return get('turn',value['id'])


def choose(value,wid,call):
    return agent.opencode_step(value['id'],{'kind':'tool','tool':'run_data_workflow',
        'arguments':{'workflow_id':wid}},call)


@pytest.fixture
def completed(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    pid=project();s=source(pid,csv_bytes(count=3));doc=document(s['id'])
    doc['contract']['min_rows']=doc['contract']['max_rows']=3
    wid=registered(pid,doc);value=turn(pid)
    choose(value,wid,'session:original')
    waiting=get('turn',value['id']);assert waiting['state']=='waiting_data_workflow'
    assert process_once();assert data_tools.resume(waiting)
    original=get('turn',value['id']);run=get_job(original['agent_trace'][-1]['observation']['run_id'])
    assert run['status']=='validated' and run['validated']
    yield pid,s,doc,wid,original,run
    for item in list_jobs(target=wid):
        if item['status'] in ('queued','retrying'):save(dict(item,status='cancelled',cancel_requested=True))
    for item in __import__('agent_service.store',fromlist=['all_items']).all_items('turn'):
        if item.get('project_id')==pid:patch('turn',item['id'],state='done')


def test_new_call_and_same_call_replay_reuse_one_actual_job_without_freshness_or_publish(completed):
    pid,s,doc,wid,value,run=completed
    before=get('data_workflow',wid);original_meta=copy.deepcopy(run['data_result'])
    choose(value,wid,'session:second')
    current=get('turn',value['id']);receipt=current['agent_trace'][-1]['observation']
    assert current['state']=='agent' and receipt['reused_existing_run'] and receipt['executed'] is False
    assert receipt['run_id']==run['id'] and receipt['executed_at']==run['started_at']
    assert receipt['execution_finished_at']==run['finished_at'] and receipt['next_step']
    assert len(list_jobs(target=wid))==1 and get_job(run['id'])['data_result']==original_meta
    assert get('data_workflow',wid)==before and before['published_run'] is None
    response=choose(value,wid,'session:second')
    assert response['replayed'] and len(get('turn',value['id'])['agent_trace'])==2
    assert len(list_jobs(target=wid))==1


def resume(value,monkeypatch):
    plan={'steps':['加工完整来源','导出并交付'],'completed_steps':[0],'missing_information':[]}
    patch('turn',value['id'],state='done',outcome='paused',resume_available=True,task_plan=plan)
    monkeypatch.setattr(provider,'status',lambda:{'configured':True})
    monkeypatch.setattr(agent,'advance',lambda value:None)
    result=conversation.submit(value['project_id'],conversation.TurnInput(text='继续',
        resume_turn_id=value['id'],request_id='resume-'+uid()))
    conversation.handle(get('turn',result['id']))
    return get('turn',result['id']),plan


def test_explicit_continuation_keeps_plan_and_reuses_verified_effect(completed,monkeypatch):
    pid,s,doc,wid,value,run=completed
    continued,plan=resume(value,monkeypatch)
    assert continued['resumed_from']==value['id'] and continued['task_plan']==plan
    assert continued['task_plan']['completed_steps']==[0]
    from fastapi.testclient import TestClient
    from agent_service.api import app
    from agent_service.config import TOKEN
    response=TestClient(app).get('/api/projects/'+pid+'/conversation',headers={'Authorization':'Bearer '+TOKEN})
    assert response.status_code==200
    public=next(item for item in response.json()['turns'] if item['id']==continued['id'])
    assert public['task_plan']==plan and public['inherited_step_count']==len(value['agent_trace'])
    changed=continued['task_plan'];changed['completed_steps'].append(1)
    patch('turn',continued['id'],task_plan=changed)
    assert get('turn',value['id'])['task_plan']==plan
    choose(continued,wid,'new-session:new-choice')
    receipt=get('turn',continued['id'])['agent_trace'][-1]['observation']
    assert receipt['run_id']==run['id'] and receipt['reused_existing_run']
    assert len(list_jobs(target=wid))==1


@pytest.mark.parametrize(('base','expected'),[(-1,0),(999,1),('1',0),(True,0),(0,0),(1,1)])
def test_public_inherited_step_count_is_explicit_and_clamped(completed,base,expected):
    pid,s,doc,wid,value,run=completed
    assert 'inherited_step_count' not in conversation.public_turn(dict(value,agent_budget_base=base))
    public=conversation.public_turn(dict(value,resumed_from='t_original',agent_budget_base=base))
    assert public['inherited_step_count']==expected


def test_unrelated_clarification_does_not_silently_resume(completed,monkeypatch):
    pid,s,doc,wid,value,run=completed
    patch('turn',value['id'],state='done',outcome='paused',resume_available=True,task_plan={'completed_steps':[0]})
    monkeypatch.setattr(provider,'status',lambda:{'configured':True})
    monkeypatch.setattr(agent,'advance',lambda value:None)
    new=conversation.submit(pid,conversation.TurnInput(text='金额单位是元',request_id='clarify-'+uid()))
    conversation.handle(get('turn',new['id']))
    current=get('turn',new['id'])
    assert not current.get('resumed_from') and not current.get('agent_trace') and not current.get('task_plan')
    assert get('turn',value['id'])['resume_available']


def test_explicit_fresh_refresh_runs_once_then_reuses_within_new_task(completed):
    pid,s,doc,wid,value,old=completed
    fresh=turn(pid,'请重新运行并汇总最新完整订单')
    choose(fresh,wid,'fresh:first')
    waiting=get('turn',fresh['id']);assert waiting['state']=='waiting_data_workflow'
    assert waiting['data_workflow_job']!=old['id'] and len(list_jobs(target=wid))==2
    assert process_once();data_tools.resume(waiting)
    new=get_job(waiting['data_workflow_job']);choose(fresh,wid,'fresh:second')
    receipt=get('turn',fresh['id'])['agent_trace'][-1]['observation']
    assert receipt['run_id']==new['id'] and receipt['executed'] is False
    assert len(list_jobs(target=wid))==2


@pytest.mark.parametrize('change',['document','source_revision','engine','unknown_engine','missing_result','tampered_result'])
def test_changed_or_unverifiable_result_is_not_reused(completed,monkeypatch,change):
    pid,s,doc,wid,value,original=completed
    if change=='document':
        modified=copy.deepcopy(doc);modified['plan']['name']='Revised complete detail'
        workspace.write_file(pid,'workflows/orders.json',json.dumps(modified));data.register(pid,'workflows/orders.json')
    elif change=='source_revision':replace_source(s['id'],csv_bytes(count=3,amount_offset=10))
    elif change=='engine':monkeypatch.setattr(data,'processing_fingerprint',lambda:'e'*64)
    elif change=='unknown_engine':
        altered=copy.deepcopy(original);altered['runtime_snapshot'].pop('processing_fingerprint');save(altered)
    else:
        folder,_=data.result_folder(pid,original['id']);path=folder/'handoff'/'rows.jsonl'
        if change=='missing_result':path.unlink()
        else:path.write_text('tampered synthetic rows',encoding='utf-8')
    choose(value,wid,'session:changed')
    current=get('turn',value['id'])
    assert current['state']=='waiting_data_workflow' and current['data_workflow_job']!=original['id']
    assert len(list_jobs(target=wid))==2 and get('data_workflow',wid)['published_run'] is None


def test_current_veto_is_checked_before_any_reuse(completed):
    pid,s,doc,wid,value,original=completed
    patch('turn',value['id'],text='不要运行或修改任何资料')
    choose(value,wid,'session:veto')
    current=get('turn',value['id']);assert current['agent_trace'][-1]['status']=='failed'
    assert not current['agent_trace'][-1]['observation'].get('reused_existing_run')
    assert len(list_jobs(target=wid))==1


@pytest.mark.parametrize('scope',['other_actor','delegated','dynamic_source','upstream_result','reviewed_mapping'])
def test_nondefault_scope_and_inputs_do_not_borrow_owner_receipt(completed,scope):
    pid,s,doc,wid,value,original=completed
    parsed=data.WorkflowDocument.model_validate(doc);frozen=copy.deepcopy(original['source_snapshot'])
    if scope=='delegated':patch('turn',value['id'],collaboration_parent_turn_id='t_scoped_parent')
    elif scope=='dynamic_source':frozen[s['id']]['kind']='api'
    elif scope=='upstream_result':parsed.bindings['orders']=data.InputBinding(result_id=original['id'])
    elif scope=='reviewed_mapping':parsed.reviewed_mapping_ids=['sem_review_required']
    if scope=='other_actor':
        with actor_scope('u_'+'a'*64):assert data._reusable_workflow_run(pid,wid,parsed,frozen,value['id']) is None
    else:assert data._reusable_workflow_run(pid,wid,parsed,frozen,value['id']) is None


def test_gateway_admission_still_precedes_reuse(completed,monkeypatch):
    from agent_service import job_authority
    pid,s,doc,wid,value,original=completed
    def denied(identity):raise ValueError('fixture grant revoked')
    monkeypatch.setattr(job_authority,'for_turn',denied)
    monkeypatch.setattr(data,'_reusable_workflow_run',lambda *args:pytest.fail('reuse preceded admission'))
    with pytest.raises(ValueError,match='grant revoked'):
        data.enqueue_workflow(pid,wid,originating_turn_id=value['id'])
    assert len(list_jobs(target=wid))==1


def test_same_build_fingerprint_is_cached_and_matches_worker_process(monkeypatch):
    import subprocess,sys
    expected=data.processing_fingerprint()
    result=subprocess.run([sys.executable,'-X','utf8','-c',
        'from agent_service.project_data import processing_fingerprint; print(processing_fingerprint())'],
        capture_output=True,text=True,check=True,timeout=30)
    assert result.stdout.strip()==expected
    # The current process keeps its admitted build identity, without re-reading
    # package metadata or performing any network probe on subsequent tool calls.
    import importlib.metadata
    monkeypatch.setattr(importlib.metadata,'distributions',lambda:pytest.fail('fingerprint was not cached'))
    assert data.processing_fingerprint()==expected


@pytest.mark.parametrize('legacy',[False,True])
def test_worker_checks_new_engine_but_keeps_unknown_legacy_jobs_executable(completed,monkeypatch,legacy):
    pid,s,doc,wid,value,original=completed
    fresh=turn(pid);choose(fresh,wid,'worker:first');waiting=get('turn',fresh['id'])
    queued=get_job(waiting['data_workflow_job'])
    if legacy:
        queued['runtime_snapshot'].pop('processing_fingerprint');save(queued)
    else:monkeypatch.setattr(data,'processing_fingerprint',lambda:'c'*64)
    assert process_once();run=get_job(queued['id'])
    if legacy:assert run['status']=='validated' and run['validated']
    else:assert run['status']=='failed' and run['error_code']=='PROCESSING_ENGINE_CHANGED'
