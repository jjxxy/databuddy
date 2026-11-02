"""Conversation-requested flow execution uses the real versioned job queue."""
import pytest
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.store import get, patch, uid
from agent_service.execution import process_once
from agent_service.actor_context import actor_scope
from agent_service import agent, business_flow_tools, provider


def test_agent_creates_and_runs_frozen_flow_with_real_receipt(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'','model':'','share_data':True})
    client=TestClient(app); h={'Authorization':'Bearer '+TOKEN}
    def req(method,path,**kw):
        response=client.request(method,path,headers=h,**kw)
        assert response.status_code==200,response.text
        return response.json()
    pid=req('POST','/api/projects',json={'name':'会话流程','request_id':uid()})['id'];base='/api/projects/'+pid
    source=req('POST',base+'/attachments',files={'file':('orders.csv','region,amount\nEast,9.00\nEast,9.00\nWest,4.00\n'.encode(),'text/csv')},data={'request_id':uid(),'local_processing':'true'})
    assert process_once();source=get('source',source['id'])
    turn=req('POST',base+'/messages',json={'text':'请读取资料，汇总收入，生成报告并运行工作流','request_id':uid(),'source_id':source['id']})
    plan={'version':1,'name':'收入汇总','inputs':{'table':{'meaning':'收入明细','grain':['nexus_row'],'columns':[
        {'name':'nexus_row','data_type':'text','meaning':'资料行号'}, {'name':'c_1','data_type':'text','meaning':'地区'},
        {'name':'c_2','data_type':'decimal','meaning':'收入','unit':'CNY','aggregation':'additive'}]}},
        'stages':[{'id':'total','kind':'aggregate','description':'按地区汇总','input':'table','by':['c_1'],'meaning':'收入',
        'aggregations':[{'function':'sum','column':'c_2','output':{'name':'amount','data_type':'decimal','meaning':'收入合计','unit':'CNY'}}]}], 'output':'total'}
    doc={'name':'收入工作流','inputs':[],'nodes':[
        {'id':'read','kind':'source_table','title':'读取','config':{'source_id':source['id'],'revision':source['revision'],'content_hash':source['content_hash']}},
        {'id':'total','kind':'table_workflow','title':'汇总','depends_on':['read'],'inputs':{'table':{'source':'node','name':'read'}},'config':{'plan':plan}},
        {'id':'spec','kind':'table_report_spec','title':'报告准备','depends_on':['total'],'inputs':{'table':{'source':'node','name':'total'}},'config':{'title':'收入','format':'html'}},
        {'id':'report','kind':'html_report','title':'生成报告','depends_on':['spec'],'inputs':{'spec':{'source':'node','name':'spec'}},'config':{'name':'收入','renderer_version':'business-html-v1'}}], 'outputs':['report']}
    patch('turn',turn['id'],state='agent',interaction_mode='work',discovered_tools=['create_business_flow','run_business_flow','inspect_business_flow_run'])
    with actor_scope('owner'):
        agent.opencode_step(turn['id'],{'kind':'tool','tool':'set_task_plan','arguments':{'steps':['读取资料','汇总并生成报告'],'current_step':0}},'chat-flow:plan')
        agent.opencode_step(turn['id'],{'kind':'tool','tool':'create_business_flow','arguments':{'document':doc}},'chat-flow:create')
        flow_id=get('turn',turn['id'])['business_flow']['id']
        flow=req('GET',base+'/business-flows/'+flow_id)
        args={'flow_id':flow_id,'expected_digest':flow['candidate_digest'],'inputs':{}}
        step={'kind':'tool','tool':'run_business_flow','arguments':args}
        first=agent.opencode_step(turn['id'],step,'chat-flow:run')
        replay=agent.opencode_step(turn['id'],step,'chat-flow:run')
        assert replay['replayed'] is True
        assert process_once()
        current=req('GET',base+'/business-flows/'+flow_id);run=current['latest_run']
        assert run['status']=='succeeded' and run['plan_digest']==flow['candidate_digest']
        page=req('GET',base+'/business-flows/'+flow_id+'/runs/'+run['id']+'/tables/total?offset=0&limit=20')
        assert {row['c_1']:row['amount'] for row in page['rows']}=={'East':'18.00','West':'4.00'}
        assert len(run['artifacts'])==1 and not run['financial_verified']
        assert business_flow_tools.execute(get('turn',turn['id']),'inspect_business_flow_run',{'flow_id':flow_id,'run_id':run['id']})['id']==run['id']
        with pytest.raises(ValueError,match='版本已变化'):
            business_flow_tools.execute(get('turn',turn['id']),'run_business_flow',{**args,'expected_digest':'0'*64})


def test_step_by_step_request_exposes_real_flow_tools():
    from agent_service.agent_context import selected
    selected_tools=selected({'text':'第一步读取资料，第二步汇总收入，第三步生成报告并运行工作流','interaction_mode':'work'})
    assert {'business_flow_guide','inspect_business_flow','run_business_flow','inspect_business_flow_run'} <= set(selected_tools)


def test_chat_flow_run_rejects_external_model_and_explicit_veto(monkeypatch):
    from agent_service import business_flows
    fid='bf_'+'a'*40
    doc={'name':'需另行确认','inputs':[], 'nodes':[{'id':'model','kind':'model','title':'调用模型','config':{}}], 'outputs':['model']}
    monkeypatch.setattr(business_flows,'get_view',lambda *_:{'candidate_digest':'b'*64,'candidate_document':doc})
    turn={'id':'t_'+uid(),'project_id':'p_'+uid(),'actor_id':'owner','interaction_mode':'work','text':'请运行工作流'}
    args={'flow_id':fid,'expected_digest':'b'*64,'inputs':{}}
    with actor_scope('owner'):
        with pytest.raises(ValueError,match='单独确认'):
            business_flow_tools.execute(turn,'run_business_flow',args)
        with pytest.raises(ValueError,match='未执行'):
            business_flow_tools.execute({**turn,'text':'不要运行工作流，只解释步骤'},'run_business_flow',args)
