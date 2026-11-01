"""Business acceptance with real dlt/dbt, Git and frozen application releases.

The model is not called: validated rules in this fixture are deliberately
explicit. Passing this case is not evidence of general financial accuracy.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

from fastapi.testclient import TestClient

from agent_service.api import app
from agent_service.codegen import git
from agent_service.config import TOKEN
from agent_service.execution import process_once, queue_due
from agent_service.jobs import get_job
from agent_service.store import get, patch, uid


def test_revenue_refund_two_sources_candidate_release_schedule_and_rollback():
    client=TestClient(app)
    headers={'Authorization':'Bearer '+TOKEN}
    def request(method,path,**kwargs):
        response=client.request(method,path,headers=headers,**kwargs)
        assert response.status_code==200, response.text
        return response.json()
    project=request('POST','/api/projects',json={'name':'收入退款验收','request_id':uid()})
    pid=project['id']; root='/api/projects/'+pid
    def upload(name,content):
        item=request('POST',root+'/attachments',files={'file':(name,content,'text/csv')},
                     data={'request_id':uid(),'local_processing':'true'})
        assert process_once()
        source=get('source',item['id']); assert source['status']=='ready'
        return source
    income=upload('income.csv','订单号,办事处,不含税收入\nA,东部,100.25\nB,东部,30.00\nC,西部,20.00\n'.encode())
    refunds=upload('refunds.csv','订单号,不含税退款\nA,10.10\nB,2.00\nC,0.00\n'.encode())
    spec=dict(income['proposed_spec'],primary_key='c_1',group_by='c_2',amount_field='m_net',
              amount_label='不含税收入扣退款',computations=[{'name':'m_net','label':'净收入',
              'operation':'subtract','left_field':'c_3','right_field':'j1_c_2'}],
              joins=[{'source_id':refunds['id'],'left_field':'c_1','right_field':'c_1',
                      'label':'退款明细','required_match':True}],filters=[])
    # Even a legacy auto setting may not promote a newly generated version.
    body={'source_id':income['id'],'name':'办事处净收入','spec':spec,'release_mode':'auto',
          'application':{'title':'办事处净收入','columns':['c_1','c_2','m_net'],'chart':'bar'}}
    created=request('POST','/api/pipelines',json=body)
    aid=created['id']; pipeline='/api/pipelines/'+aid; app_route=root+'/applications/'+aid
    plan_route=app_route+'/business-plan'
    assert process_once()
    first=get('pipeline',aid); first_run=first['candidate_run']; first_commit=first['commit']
    assert first_run and not first.get('published_run')
    candidate=request('GET',plan_route)['candidate']; assert candidate['commit']==first_commit
    assert candidate['run_id']==first_run
    assert candidate['plan']['rules']==json.loads(git(first['repository'],'show',first_commit+':plan.json'))['spec']
    assert {source['id'] for source in candidate['plan']['sources']}=={income['id'],refunds['id']}
    evidence=request('GET',app_route,params={'preview':True})['run_evidence']
    assert evidence['run_id']==first_run and evidence['commit']==first_commit
    assert evidence['identity_policy']=='business_primary_key_sha256_v1'
    assert {v['source_id'] for v in evidence['inputs']}=={income['id'],refunds['id']}
    assert all(v['input_hash'] for v in evidence['inputs']) and evidence['output_hash']
    initial=request('GET',app_route+'/data',params={'preview':True})
    assert initial['total']==3 and Decimal(initial['metric']['total'])==Decimal('138.15')
    request('POST',pipeline+'/publish',json={'run_id':first_run})
    assert get('pipeline',aid)['active_commit']==first_commit
    assert request('GET',plan_route,params={'run_id':first_run})['selected']['role']=='published'

    revised=dict(spec,filters=[{'field':'c_2','operator':'eq','data_type':'text','value':'东部'}])
    request('PUT',pipeline,json={**body,'spec':revised})
    assert process_once()
    current=get('pipeline',aid); second_run=current['candidate_run']; second_commit=current['commit']
    assert second_run!=first_run and second_commit!=first_commit
    plans=request('GET',plan_route)
    assert plans['published']['commit']==first_commit and plans['candidate']['commit']==second_commit
    assert plans['published']['plan']['rules']['filters']==[]
    assert plans['candidate']['plan']['rules']['filters'][0]['value']=='东部'
    assert any(change['field']=='filters' for change in plans['changes'])
    assert Decimal(request('GET',app_route+'/data')['metric']['total'])==Decimal('138.15')
    assert Decimal(request('GET',app_route+'/data',params={'preview':True})['metric']['total'])==Decimal('118.15')

    # A scheduled update uses the *published* commit, not the newer candidate.
    request('POST',pipeline+'/schedule',json={'cron':'0 9 * * *'})
    patch('pipeline',aid,next_due=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
    queue_due();assert process_once()
    scheduled=get('pipeline',aid)
    assert scheduled['active_commit']==first_commit and scheduled['commit']==second_commit
    assert get_job(scheduled['published_run'])['commit']==first_commit
    assert Decimal(request('GET',app_route+'/data')['metric']['total'])==Decimal('138.15')
    assert request('GET',plan_route)['candidate']['commit']==second_commit

    request('POST',pipeline+'/publish',json={'run_id':second_run})
    assert get('pipeline',aid)['active_commit']==second_commit
    assert Decimal(request('GET',app_route+'/data')['metric']['total'])==Decimal('118.15')
    request('POST',pipeline+'/rollback',json={'run_id':first_run})
    rolled=get('pipeline',aid);assert rolled['active_commit']==first_commit
    assert Decimal(request('GET',app_route+'/data')['metric']['total'])==Decimal('138.15')
    assert request('GET',plan_route)['published']['commit']==first_commit
    assert json.loads(git(first['repository'],'show',first_commit+':plan.json'))['spec']['filters']==[]


def test_identical_source_rows_preserve_financial_count_and_value_on_rerun():
    client=TestClient(app); headers={'Authorization':'Bearer '+TOKEN}
    def request(method,path,**kwargs):
        response=client.request(method,path,headers=headers,**kwargs)
        assert response.status_code==200, response.text
        return response.json()
    pid=request('POST','/api/projects',json={'name':'重复明细守恒','request_id':uid()})['id']
    source=request('POST',f'/api/projects/{pid}/attachments',files={'file':('detail.csv',
        '办事处,收入,退款\n东部,10.25,1.25\n东部,10.25,1.25\n'.encode(),'text/csv')},
        data={'request_id':uid(),'local_processing':'true'})
    assert process_once();source=get('source',source['id'])
    spec=dict(source['proposed_spec'],primary_key=None,group_by='c_1',amount_field='m_net',
              computations=[{'name':'m_net','label':'净收入','operation':'subtract','left_field':'c_2','right_field':'c_3'}])
    pipeline=request('POST','/api/pipelines',json={'source_id':source['id'],'name':'重复明细',
        'spec':spec,'release_mode':'manual','application':{'title':'重复明细','columns':['c_1','m_net']}})
    assert process_once(); pipeline=get('pipeline',pipeline['id'])
    data=f'/api/projects/{pid}/applications/{pipeline["id"]}/data'
    first=request('GET',data,params={'preview':True})
    assert first['total']==2 and Decimal(first['metric']['total'])==Decimal('18.00')
    assert request('GET',f'/api/projects/{pid}/applications/{pipeline["id"]}',params={'preview':True})['run_evidence']['identity_policy']=='preserve_all_rows_occurrence_v1'
    first_ids={row['object_id'] for row in first['rows']}
    assert len(first_ids)==2
    request('POST','/api/pipelines/'+pipeline['id']+'/publish',json={'run_id':pipeline['candidate_run']})
    commit=pipeline['commit'];request('POST','/api/pipelines/'+pipeline['id']+'/run');assert process_once()
    repeated=request('GET',data); assert repeated['total']==2 and Decimal(repeated['metric']['total'])==Decimal('18.00')
    assert {row['object_id'] for row in repeated['rows']}==first_ids
    assert get('pipeline',pipeline['id'])['active_commit']==commit


def test_duplicate_refund_join_keys_fail_candidate_instead_of_overcounting():
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    def request(method,path,**kwargs):
        response=client.request(method,path,headers=headers,**kwargs)
        assert response.status_code==200,response.text
        return response.json()
    pid=request('POST','/api/projects',json={'name':'重复退款匹配校验','request_id':uid()})['id']
    def upload(name,text):
        source=request('POST',f'/api/projects/{pid}/attachments',files={'file':(name,text.encode(),'text/csv')},
                       data={'request_id':uid(),'local_processing':'true'})
        assert process_once();return get('source',source['id'])
    income=upload('income.csv','订单号,办事处,收入\nA,东部,100\n')
    refund=upload('refund.csv','订单号,退款\nA,10\nA,20\n')
    spec=dict(income['proposed_spec'],primary_key='c_1',group_by='c_2',amount_field='m_net',
        computations=[{'name':'m_net','label':'净收入','operation':'subtract','left_field':'c_3','right_field':'j1_c_2'}],
        joins=[{'source_id':refund['id'],'left_field':'c_1','right_field':'c_1','label':'退款','required_match':True}])
    pipeline=request('POST','/api/pipelines',json={'source_id':income['id'],'name':'检查退款匹配',
        'spec':spec,'release_mode':'manual','application':{'title':'检查退款匹配','columns':['c_1','m_net']}})
    assert process_once()
    state=get('pipeline',pipeline['id'])
    assert not state.get('published_run') and not state.get('candidate_run')
    from agent_service.jobs import list_jobs
    latest=list_jobs(limit=1,target=pipeline['id'])[0]
    assert latest['status']=='failed' and not latest.get('validated')


def test_agent_proposal_requires_explicit_approval_before_code_or_run(monkeypatch):
    from agent_service import agent, provider, workspace
    from agent_service.conversation import tick
    from agent_service.store import all_items
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    def request(method,path,**kwargs):
        response=client.request(method,path,headers=headers,**kwargs)
        assert response.status_code==200,response.text
        return response.json()
    pid=request('POST','/api/projects',json={'name':'先确认再生成','request_id':uid()})['id']
    root='/api/projects/'+pid
    source=request('POST',root+'/attachments',files={'file':('income.csv',
        '办事处,收入,退款\n东部,100.25,10.10\n'.encode(),'text/csv')},
        data={'request_id':uid(),'local_processing':'true'})
    assert process_once();source=get('source',source['id'])
    turn=request('POST',root+'/messages',json={'text':'按办事处汇总收入扣退款，生成数据应用',
        'request_id':uid(),'source_id':source['id']})
    tid=turn['id']; original=get('turn',tid); session=original['session_id']
    # Emulate orchestrator's version boundary before the first model step.
    workspace.begin(original)
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://fixture.invalid/v1','model':'fixture','share_data':True})
    spec=dict(source['proposed_spec'],primary_key=None,group_by='c_1',amount_field='m_net',
        computations=[{'name':'m_net','label':'净收入','operation':'subtract','left_field':'c_2','right_field':'c_3'}])
    patch('turn',tid,state='agent',task_plan={'steps':['读取资料','生成应用'],'completed_steps':[], 'current_step':1},
          discovered_tools=['create_data_application'])
    agent.opencode_step(tid,{'kind':'tool','tool':'create_data_application',
        'arguments':{'source_id':source['id'],'name':'净收入','spec':spec,
                     'application':{'title':'净收入','columns':['c_1','m_net']}}},'session:business-proposal')
    pending=get('turn',tid)
    assert pending['state']=='done' and pending['outcome']=='needs_input'
    # Recover an interrupted worker after it persisted the proposal but before
    # the task was marked waiting. It must never proceed without approval.
    from agent_service.conversation import handle
    patch('turn',tid,state='agent',outcome=None,answer='')
    handle(get('turn',tid))
    pending=get('turn',tid)
    assert pending['state']=='done' and pending['outcome']=='needs_input'
    workspace.finalize(pending)
    before=get('turn',tid)['work_version']['commit']
    proposal=request('GET',root+'/conversation',params={'session_id':session})['turns'][-1]['business_proposal']
    assert proposal['state']=='pending' and proposal['steps']
    assert not [p for p in all_items('pipeline') if p.get('project_id')==pid]
    route=root+'/business-proposals/'+proposal['id']+'/decision'
    bad=client.post(route,headers=headers,json={'digest':'0'*64,'session_id':session,'approve':True})
    assert bad.status_code>=400
    assert get('turn',tid)['outcome']=='needs_input'
    wrong_session=client.post(route,headers=headers,json={'digest':proposal['digest'],'session_id':'other_session','approve':True})
    assert wrong_session.status_code>=400
    # A changed source version invalidates the shown proposal rather than
    # silently generating from a different input configuration.
    old_hash=source['content_hash']
    patch('source',source['id'],content_hash='f'*64)
    drift=client.post(route,headers=headers,json={'digest':proposal['digest'],'session_id':session,'approve':True})
    assert drift.status_code>=400 and get('turn',tid)['outcome']=='needs_input'
    assert not [p for p in all_items('pipeline') if p.get('project_id')==pid]
    patch('source',source['id'],content_hash=old_hash)
    expires=get('business_proposal',proposal['id'])['expires']
    patch('business_proposal',proposal['id'],expires=1)
    expired=client.post(route,headers=headers,json={'digest':proposal['digest'],'session_id':session,'approve':True})
    assert expired.status_code>=400 and get('turn',tid)['outcome']=='needs_input'
    patch('business_proposal',proposal['id'],expires=expires)
    approved=request('POST',route,json={'digest':proposal['digest'],'session_id':session,'approve':True})
    assert approved['state']=='approved' and get('turn',tid)['state']=='queued'
    request('POST',route,json={'digest':proposal['digest'],'session_id':session,'approve':True})
    tick()
    current=get('turn',tid)
    assert current['state']=='waiting_run',current
    pipelines=[p for p in all_items('pipeline') if p.get('project_id')==pid]
    assert len(pipelines)==1 and pipelines[0]['commit'] and not pipelines[0].get('published_run')
    commit=pipelines[0]['commit']
    assert process_once()
    pipeline=get('pipeline',pipelines[0]['id'])
    assert pipeline['candidate_run'] and not pipeline.get('published_run')
    data=request('GET',root+'/applications/'+pipeline['id']+'/data',params={'preview':True})
    assert data['total']==1 and Decimal(data['metric']['total'])==Decimal('90.15')
    assert pipeline['commit']==commit
    request('POST',route,json={'digest':proposal['digest'],'session_id':session,'approve':True})
    assert len([p for p in all_items('pipeline') if p.get('project_id')==pid])==1
    assert git(pipeline['repository'],'rev-list','--count','HEAD')=='1'
    # After a pending human confirmation, the *same turn* later finishes;
    # its new code must also be committed to project history.
    tick()
    assert get('turn',tid)['state']=='agent'
    agent.opencode_step(tid,{'kind':'final','status':'needs_input',
        'answer':'候选结果已生成，请核对并决定是否发布。'},'session:deliver-candidate')
    assert get('turn',tid)['state']=='done'
    workspace.finalize(get('turn',tid))
    version=get('turn',tid)['work_version']['commit']
    assert version!=before
    project_repo=workspace.folder(pid)
    path='pipelines/'+pipeline['id']+'/business-plan.json'
    assert json.loads(git(project_repo,'show',version+':'+path))['rules']==json.loads(git(pipeline['repository'],'show',commit+':plan.json'))['spec']


def test_agent_two_source_proposal_runs_confirmed_join(monkeypatch):
    """An approved cross-source request uses the actual Agent confirmation path."""
    from agent_service import agent, provider
    from agent_service.conversation import tick
    from agent_service.store import all_items
    client=TestClient(app); headers={'Authorization':'Bearer '+TOKEN}
    def request(method,path,**kwargs):
        response=client.request(method,path,headers=headers,**kwargs)
        assert response.status_code==200,response.text
        return response.json()
    pid=request('POST','/api/projects',json={'name':'确认双源分析','request_id':uid()})['id']
    root='/api/projects/'+pid
    def upload(name,content):
        result=request('POST',root+'/attachments',files={'file':(name,content.encode(),'text/csv')},
                       data={'request_id':uid(),'local_processing':'true'})
        assert process_once()
        return get('source',result['id'])
    income=upload('income.csv','订单号,办事处,收入\nA,东部,100.25\nB,东部,30.00\n')
    refund=upload('refund.csv','订单号,退款\nA,10.10\nB,2.00\n')
    turn=request('POST',root+'/messages',json={'text':'按办事处汇总收入并扣除退款',
                 'request_id':uid(),'source_ids':[income['id'],refund['id']]})
    tid=turn['id']; session=get('turn',tid)['session_id']
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://fixture.invalid/v1','model':'fixture','share_data':True})
    spec=dict(income['proposed_spec'],primary_key='c_1',group_by='c_2',amount_field='m_net',
        computations=[{'name':'m_net','label':'净收入','operation':'subtract','left_field':'c_3','right_field':'j1_c_2'}],
        joins=[{'source_id':refund['id'],'left_field':'c_1','right_field':'c_1','label':'退款','required_match':True}],filters=[])
    patch('turn',tid,state='agent',task_plan={'steps':['读取资料','生成应用'],'completed_steps':[]},
          discovered_tools=['create_data_application'])
    agent.opencode_step(tid,{'kind':'tool','tool':'create_data_application',
        'arguments':{'source_id':income['id'],'name':'净收入','spec':spec,
                     'application':{'title':'净收入','columns':['c_1','c_2','m_net']}}},'session:two-source')
    pending=get('turn',tid)
    assert pending['outcome']=='needs_input' and not [p for p in all_items('pipeline') if p.get('project_id')==pid]
    proposal=request('GET',root+'/conversation',params={'session_id':session})['turns'][-1]['business_proposal']
    request('POST',root+'/business-proposals/'+proposal['id']+'/decision',
            json={'digest':proposal['digest'],'session_id':session,'approve':True})
    tick()
    assert get('turn',tid)['state']=='waiting_run',get('turn',tid)
    assert process_once()
    pipelines=[p for p in all_items('pipeline') if p.get('project_id')==pid]
    assert len(pipelines)==1 and pipelines[0]['candidate_run'] and not pipelines[0].get('published_run')
    data=request('GET',root+'/applications/'+pipelines[0]['id']+'/data',params={'preview':True})
    assert data['total']==2 and Decimal(data['metric']['total'])==Decimal('118.15')
