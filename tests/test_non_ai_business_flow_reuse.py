"""Real queued manual and scheduled runs of a fixed non-AI business flow."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json

from fastapi.testclient import TestClient

from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.execution import process_once
from agent_service.store import get, patch, uid, unseal
from agent_service import provider, workflow_model_skill


def test_fixed_non_ai_flow_manual_and_scheduled_runs_do_not_call_model(monkeypatch):
    calls=[]
    def forbidden(*args, **kwargs):
        calls.append('model')
        raise AssertionError('non-AI flow must not invoke model gateway')
    monkeypatch.setattr(provider, 'complete', forbidden)
    monkeypatch.setattr(workflow_model_skill, 'run_model', forbidden)
    monkeypatch.setattr(workflow_model_skill, 'run_skill', forbidden)
    monkeypatch.setattr(provider, 'settings', lambda: {'base_url':'', 'model':'', 'share_data':True})
    c=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    def req(method,path,**kwargs):
        response=c.request(method,path,headers=headers,**kwargs)
        assert response.status_code==200,response.text
        return response.json()
    pid=req('POST','/api/projects',json={'name':'无模型重复运行','request_id':uid()})['id']
    base='/api/projects/'+pid
    item=req('POST',base+'/attachments',files={'file':('sales.csv','地区,金额\n东部,100.01\n东部,18.14\n西部,4.00\n'.encode(),'text/csv')},data={'request_id':uid(),'local_processing':'true'})
    assert process_once()
    source=get('source',item['id']);assert source['status']=='ready'
    turn=req('POST',base+'/messages',json={'text':'按地区计算收入','request_id':uid(),'source_id':source['id']})
    patch('turn',turn['id'],state='done',outcome='needs_input')
    plan={'version':1,'name':'收入','inputs':{'table':{'meaning':'明细','grain':['nexus_row'],'columns':[
        {'name':'nexus_row','data_type':'text','meaning':'行号'},
        {'name':'c_1','data_type':'text','meaning':'地区'},
        {'name':'c_2','data_type':'decimal','meaning':'收入','unit':'CNY','aggregation':'additive'}]}},
        'stages':[{'id':'filter','kind':'filter','description':'选择地区','input':'table','conditions':[{'column':'c_1','operator':'eq','value':'东部'}]},
        {'id':'total','kind':'aggregate','description':'汇总','input':'filter','by':['c_1'],'meaning':'地区收入','aggregations':[{'function':'sum','column':'c_2','output':{'name':'amount','data_type':'decimal','meaning':'合计','unit':'CNY'}}]}],'output':'total'}
    doc={'name':'无模型收入汇总','inputs':['region'],'input_specs':{'region':{'type':'choice','label':'地区','options':['东部','西部']}},'nodes':[
        {'id':'read','kind':'source_table','title':'读取','config':{'source_id':source['id'],'revision':source['revision'],'content_hash':source['content_hash']}},
        {'id':'sum','kind':'table_workflow','title':'汇总','depends_on':['read'],'inputs':{'table':{'source':'node','name':'read'},'region':{'source':'input','name':'region'}},
         'config':{'plan':plan,'parameter_bindings':{'region':{'stage_id':'filter','condition_index':0}}}}],'outputs':['sum']}
    flow=req('POST',base+'/business-flows',json={'request_id':uid(),'turn_id':turn['id'],'document':doc})
    root=base+'/business-flows/'+flow['id'];digest=flow['candidate_digest']
    first=req('POST',root+'/runs',json={'request_id':uid(),'expected_digest':digest,'inputs':{'region':'东部'}})
    assert process_once()
    req('POST',root+'/publish',json={'run_id':first['run_id'],'expected_digest':digest})
    run_ids=[first['run_id']]
    for _ in range(2):
        result=req('POST',root+'/runs',json={'request_id':uid(),'expected_digest':digest,'use_published':True,'inputs':{'region':'东部'}})
        assert process_once();run_ids.append(result['run_id'])
    req('PUT',root+'/schedule',json={'cron':'0 8 * * *','source_bindings':{},'expected_published_digest':digest,'parameter_values':{'region':'东部'}})
    patch('business_flow',flow['id'],next_due=(datetime.now(timezone.utc)-timedelta(seconds=5)).isoformat())
    from agent_service.business_flows import queue_due
    queue_due();assert process_once();run_ids.append(req('GET',root)['latest_run']['id'])
    assert len(set(run_ids))==4
    output_hashes=set()
    from agent_service.jobs import get_job
    from agent_service.business_flow_engine import BusinessFlow
    normalized=BusinessFlow.model_validate(doc).model_dump(mode='json')
    for rid in run_ids:
        status=req('GET',root+'/runs/'+rid)
        assert status['status']=='succeeded' and status['plan_digest']==digest
        rows=req('GET',root+'/runs/'+rid+'/tables/sum')['rows']
        assert len(rows)==1 and Decimal(rows[0]['amount'])==Decimal('118.15')
        output_hashes.add(hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest())
        payload=unseal(get_job(rid)['secret_flow_payload'])
        assert payload['inputs']=={'region':'东部'} and payload['document']==normalized
    assert len(output_hashes)==1
    assert calls==[]
