"""User parameters change only a declared typed filter and actual plan receipt."""
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.execution import process_once
from agent_service.store import get,uid
from agent_service.business_flow_engine import BusinessFlow,Handler,run
from agent_service.business_flow_data import source_table,table_workflow,validate_source,validate_table


def test_parameter_changes_exact_sum_without_changing_formula_or_schema():
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    def req(method,path,**kw):
        r=client.request(method,path,headers=headers,**kw);assert r.status_code==200,r.text;return r.json()
    pid=req('POST','/api/projects',json={'name':'业务参数','request_id':uid()})['id'];base='/api/projects/'+pid
    s=req('POST',base+'/attachments',files={'file':('income.csv','办事处,收入\n东部,9.00\n西部,15.00\n'.encode(),'text/csv')},data={'request_id':uid(),'local_processing':'true'})
    for _ in range(50):
        if get('source',s['id']).get('status') in ('ready','failed'):break
        assert process_once()
    s=get('source',s['id']);assert s['status']=='ready'
    turn=req('POST',base+'/messages',json={'text':'按办事处生成汇总报告','request_id':uid(),'source_id':s['id']})
    context={'project_id':pid,'actor_id':'owner','turn_id':turn['id'],'run_id':uid()}
    plan={'version':1,'name':'收入','inputs':{'table':{'meaning':'明细','grain':['nexus_row'],'columns':[
        {'name':'nexus_row','data_type':'text','meaning':'行号'}, {'name':'c_1','data_type':'text','meaning':'办事处'},
        {'name':'c_2','data_type':'decimal','meaning':'收入','unit':'CNY','aggregation':'additive'}]}},'stages':[
        {'id':'filtered','kind':'filter','description':'选择办事处','input':'table','conditions':[{'column':'c_1','operator':'eq','value':'东部'}]},
        {'id':'total','kind':'aggregate','description':'汇总','input':'filtered','by':['c_1'],'meaning':'收入',
         'aggregations':[{'function':'sum','column':'c_2','output':{'name':'amount','data_type':'decimal','meaning':'收入','unit':'CNY'}}]}], 'output':'total'}
    cfg={'plan':plan,'parameter_bindings':{'area':{'stage_id':'filtered','condition_index':0}}}
    doc={'name':'按地区汇总','inputs':['region'],'input_specs':{'region':{'type':'choice','label':'办事处','options':['东部','西部']}},
       'nodes':[{'id':'read','kind':'source_table','title':'读取','config':{'source_id':s['id'],'revision':s['revision'],'content_hash':s['content_hash']}},
       {'id':'sum','kind':'table_workflow','title':'筛选汇总','depends_on':['read'],
        'inputs':{'table':{'source':'node','name':'read'},'area':{'source':'input','name':'region'}},'config':cfg}], 'outputs':['sum']}
    handlers={'source_table':Handler(source_table,True,True,validate_source),'table_workflow':Handler(table_workflow,True,True,validate_table)}
    east=run(doc,{'region':'东部'},handlers,context,persist=lambda _:None)
    west=run(doc,{'region':'西部'},handlers,context,persist=lambda _:None)
    assert east['status']==west['status']=='succeeded'
    assert Decimal(east['outputs']['sum']['rows'][0]['amount'])==Decimal('9.00')
    assert Decimal(west['outputs']['sum']['rows'][0]['amount'])==Decimal('15.00')
    assert east['outputs']['sum']['plan_sha256']!=west['outputs']['sum']['plan_sha256']
    # Reuse the exact pinned workflow and inputs in independent runs, rather
    # than replaying a completed checkpoint. A different parameter must not
    # leak into the next invocation of the immutable workflow document.
    for region, expected in [('东部', '9.00'), ('西部', '15.00'), ('东部', '9.00')]:
        independent_context={**context, 'run_id':uid()}
        repeated=run(doc,{'region':region},handlers,independent_context,persist=lambda _:None)
        assert repeated['status']=='succeeded'
        assert Decimal(repeated['outputs']['sum']['rows'][0]['amount'])==Decimal(expected)
        original=east if region=='东部' else west
        assert repeated['outputs']['sum']['plan_sha256']==original['outputs']['sum']['plan_sha256']
    # The original immutable document is unchanged by parameter substitution.
    assert doc['nodes'][1]['config']['plan']['stages'][0]['conditions'][0]['value']=='东部'
    with pytest.raises(ValueError):run(doc,{'region':'外部'},handlers,context,persist=lambda _:None)
    with pytest.raises(ValueError):validate_table({'plan':plan,'parameter_bindings':{'area':{'stage_id':'total','condition_index':0}}})
    with pytest.raises(ValueError):validate_table({'plan':plan,'parameter_bindings':{'area':{'stage_id':'filtered','condition_index':0},'other':{'stage_id':'filtered','condition_index':0}}})
    invalid=BusinessFlow.model_validate(doc).model_dump(mode='json');invalid['input_specs']['region']['type']='boolean';invalid['input_specs']['region']['options']=[]
    with pytest.raises(ValueError):BusinessFlow.model_validate(invalid)
