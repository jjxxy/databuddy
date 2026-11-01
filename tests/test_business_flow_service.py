"""A real uploaded file drives a frozen flow and a downloadable result."""
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.store import get,uid,unseal
from agent_service.execution import process_once
from agent_service import provider


def test_terminal_inflight_external_action_is_uncertain_and_never_assumed_failed():
    from agent_service.business_flows import _uncertain
    external={'status':'running','steps':[{'status':'running','replay_safe':False}]}
    safe={'status':'running','steps':[{'status':'running','replay_safe':True}]}
    assert _uncertain({'status':'failed'},external)
    assert _uncertain({'status':'cancelled'},external)
    assert not _uncertain({'status':'failed'},safe)
    assert not _uncertain({'status':'running'},external)


def test_confirmed_flow_git_job_and_actual_html(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'','model':'','share_data':True})
    client=TestClient(app);h={'Authorization':'Bearer '+TOKEN}
    def req(method,path,**kw):
        r=client.request(method,path,headers=h,**kw);assert r.status_code==200,r.text;return r.json()
    pid=req('POST','/api/projects',json={'name':'流程纵切','request_id':uid()})['id'];base='/api/projects/'+pid
    uploaded=req('POST',base+'/attachments',files={'file':('income.csv','办事处,收入\n东部,9.00\n东部,9.00\n'.encode(),'text/csv')},data={'request_id':uid(),'local_processing':'true'})
    assert process_once();source=get('source',uploaded['id'])
    turn=req('POST',base+'/messages',json={'text':'读取收入明细并生成汇总报告','request_id':uid(),'source_id':source['id']})
    plan={'version':1,'name':'精确汇总','inputs':{'table':{'meaning':'收入明细','grain':['atlas_row'],
        'columns':[{'name':'atlas_row','data_type':'text','meaning':'本次资料行号'},
                   {'name':'c_1','data_type':'text','meaning':'办事处'},
                   {'name':'c_2','data_type':'decimal','meaning':'收入','unit':'CNY','aggregation':'additive'}]}},
        'stages':[{'id':'total','kind':'aggregate','description':'按办事处汇总','input':'table','by':['c_1'],
                   'meaning':'各办事处收入','aggregations':[{'function':'sum','column':'c_2',
                       'output':{'name':'amount','data_type':'decimal','meaning':'收入合计','unit':'CNY'}}]}], 'output':'total'}
    document={'name':'收入汇总','inputs':[],'nodes':[
        {'id':'read','kind':'source_table','title':'读取资料','config':{'source_id':source['id'],'revision':source['revision'],'content_hash':source['content_hash']}},
        {'id':'aggregate','kind':'table_workflow','title':'汇总收入','depends_on':['read'],'inputs':{'table':{'source':'node','name':'read'}},'config':{'plan':plan}},
        {'id':'spec','kind':'table_report_spec','title':'整理报告','depends_on':['aggregate'],'inputs':{'table':{'source':'node','name':'aggregate'}},'config':{'title':'收入汇总','format':'html'}},
        {'id':'report','kind':'html_report','title':'生成报告','depends_on':['spec'],'inputs':{'spec':{'source':'node','name':'spec'}},'config':{'name':'收入汇总','renderer_version':'business-html-v1'}}], 'outputs':['report']}
    # Exercise the same validated Agent tool boundary as the conversation, not
    # only the management endpoint. A replay must not create another draft.
    from agent_service import agent, conversation
    from agent_service.actor_context import actor_scope
    from agent_service.store import patch
    patch('turn',turn['id'],state='agent',interaction_mode='work',discovered_tools=['create_business_flow'])
    with actor_scope('owner'):
        agent.opencode_step(turn['id'],{'kind':'tool','tool':'set_task_plan','arguments':{
            'steps':['读取资料','生成业务流程'], 'current_step':0}},'flow:plan')
        step={'kind':'tool','tool':'create_business_flow','arguments':{'document':document}}
        created=agent.opencode_step(turn['id'],step,'flow:create')
        replay=agent.opencode_step(turn['id'],step,'flow:create')
    marker=get('turn',turn['id']).get('business_flow')
    assert marker,created
    assert replay['replayed'] is True
    assert conversation.public_turn(get('turn',turn['id']))['business_flow']==marker
    # Originating turn may finish before the user approves and runs the draft.
    patch('turn',turn['id'],state='done',outcome='needs_input')
    saved=req('GET',base+'/business-flows/'+marker['id'])
    assert saved['candidate_commit'] and saved['published_run'] is None
    assert len(req('GET',base+'/business-flows')['items'])==1
    flow=base+'/business-flows/'+saved['id'];run_request=uid();body={'request_id':run_request,'expected_digest':saved['candidate_digest'],'inputs':{}}
    run=req('POST',flow+'/runs',json=body)
    assert req('POST',flow+'/runs',json=body)['run_id']==run['run_id']
    conflict=client.post(flow+'/runs',headers=h,json={**body,'request_id':uid()});assert conflict.status_code==400
    assert req('GET',flow+'/requests/'+run_request)['accepted'] is True
    assert req('GET',flow+'/requests/'+uid())['accepted'] is None
    assert process_once();status=req('GET',flow+'/runs/'+run['run_id']);assert status['status']=='succeeded',status
    assert status['plan_digest']==saved['candidate_digest'] and status['commit']==saved['candidate_commit']
    assert len(status['steps'])==4 and len(status['artifacts'])==1
    result=client.get(status['artifacts'][0]['download_url'],headers=h)
    assert result.status_code==200 and '18.00' in result.text and '未经' not in result.text
    record=unseal(get('run',run['run_id'])['secret_flow_checkpoint'])
    assert record['status']=='succeeded' and record['steps'][1]['output']['rows'][0]['amount']=='18.00'
    assert not status['financial_verified']
    # Inputs and source rows are not committed to the project repository.
    from agent_service.workspace import folder,git_content
    content=git_content(folder(pid),saved['candidate_commit'],'files/business-flows/'+saved['id']+'.json').decode()
    assert '东部' not in content
    adopted=req('POST',flow+'/publish',json={'run_id':run['run_id'],'expected_digest':saved['candidate_digest']})
    assert adopted['published_run']==run['run_id'] and adopted['published_digest']==saved['candidate_digest']
    # The same published Git version reruns without changing logic or inputs;
    # a successful clean run advances the formal result pointer.
    again=req('POST',flow+'/runs',json={'request_id':uid(),'expected_digest':saved['candidate_digest'],'inputs':{},'use_published':True})
    assert process_once();repeated=req('GET',flow+'/runs/'+again['run_id']);assert repeated['status']=='succeeded',repeated
    after=req('GET',flow);assert after['published_run']==again['run_id'] and after['published_digest']==saved['candidate_digest']
    req('PUT',flow+'/schedule',json={'cron':'* * * * *','source_bindings':{},'expected_published_digest':saved['candidate_digest']})
    from datetime import datetime,timezone,timedelta
    from agent_service.store import patch
    patch('business_flow',saved['id'],next_due=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
    from agent_service.business_flows import queue_due
    queue_due();assert process_once()
    scheduled=req('GET',flow);assert scheduled['published_run'] not in (run['run_id'],again['run_id'])
    assert scheduled['published_digest']==saved['candidate_digest']
    # A new draft does not replace the adopted version. Project rollback
    # restores both the readable definition and the formal pointer.
    published_history=__import__('agent_service.workspace',fromlist=['history']).history(pid)[0]['commit']
    revised=dict(document,name='新的收入方案')
    patch('turn',turn['id'],state='done',outcome='complete')
    next_turn=req('POST',base+'/messages',json={'text':'修改候选方案名称','request_id':uid(),'source_id':source['id']})
    changed=req('PUT',flow,json={'request_id':uid(),'turn_id':next_turn['id'],'document':revised,'expected_digest':saved['candidate_digest']})
    assert changed['candidate_digest']!=saved['candidate_digest'] and changed['published_digest']==saved['candidate_digest']
    old_run=req('POST',flow+'/runs',json={'request_id':uid(),'expected_digest':saved['candidate_digest'],'inputs':{},'use_published':True})
    assert get('run',old_run['run_id'])['flow_turn_id']==turn['id']
    assert process_once();assert req('GET',flow+'/runs/'+old_run['run_id'])['status']=='succeeded'
    changed_run=req('POST',flow+'/runs',json={'request_id':uid(),'expected_digest':changed['candidate_digest'],'inputs':{}})
    assert process_once();assert req('GET',flow+'/runs/'+changed_run['run_id'])['status']=='succeeded'
    new_formal=req('POST',flow+'/publish',json={'run_id':changed_run['run_id'],'expected_digest':changed['candidate_digest']})
    assert new_formal['cron'] is None and '定时已暂停' in new_formal['refresh_error']
    from agent_service import workspace
    workspace.restore(pid,published_history,turn['id'])
    restored=req('GET',flow);assert restored['candidate_digest']==saved['candidate_digest'] and restored['published_digest']==saved['candidate_digest']
    choices=req('GET',flow+'/input-sources');assert any(x['id']==source['id'] for x in choices['items'])
    assert all('secret_config' not in x for x in choices['items'])
    # Runtime changes between queue admission and worker execution must not
    # silently change the behavior of an already committed flow version.
    drift=req('POST',flow+'/runs',json={'request_id':uid(),'expected_digest':saved['candidate_digest'],'inputs':{}})
    from agent_service import business_flows
    real_lock=business_flows._runtime_lock
    monkeypatch.setattr(business_flows,'_runtime_lock',lambda doc:{**real_lock(doc),'python':'changed'})
    assert process_once();drift_status=req('GET',flow+'/runs/'+drift['run_id'])
    assert drift_status['status']=='failed' and '执行器或依赖已变化' in drift_status['message']
    monkeypatch.setattr(business_flows,'_runtime_lock',real_lock)
    # An interrupted external side effect is visible after refresh and cannot
    # be replayed via a fresh request; the original request remains idempotent.
    ambiguous_id=uid();ambiguous_body={'request_id':ambiguous_id,'expected_digest':saved['candidate_digest'],'inputs':{}}
    ambiguous=req('POST',flow+'/runs',json=ambiguous_body)
    from agent_service.jobs import get_job,save
    from agent_service.store import seal
    raw=get_job(ambiguous['run_id']);raw.update(status='failed',secret_flow_checkpoint=seal({'status':'unknown','steps':[]}));save(raw,raw['id'])
    assert req('GET',flow)['latest_run']['status']=='unknown'
    assert req('POST',flow+'/runs',json=ambiguous_body)['run_id']==ambiguous['run_id']
    blocked=client.post(flow+'/runs',headers=h,json={**ambiguous_body,'request_id':uid()});assert blocked.status_code==400 and '未知' in blocked.text
