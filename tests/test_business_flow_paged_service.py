"""The authorized API pages a versioned large-result snapshot."""
import pytest
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.store import get,uid,unseal
from agent_service.execution import process_once
from agent_service import business_flow_data as data,provider,business_flows
from agent_service.actor_context import actor_scope


def test_output_snapshot_paged_through_real_api_and_actor_bound(monkeypatch):
    monkeypatch.setattr(data,'_INLINE_ROWS_LIMIT',1)
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'','model':'','share_data':True})
    c=TestClient(app);h={'Authorization':'Bearer '+TOKEN}
    def req(method,path,**kw):
        result=c.request(method,path,headers=h,**kw);assert result.status_code==200,result.text;return result.json()
    pid=req('POST','/api/projects',json={'name':'分页运行','request_id':uid()})['id'];base='/api/projects/'+pid
    source=req('POST',base+'/attachments',files={'file':('income.csv','办事处,收入\n东部,1.00\n西部,2.00\n北部,3.00\n'.encode(),'text/csv')},data={'request_id':uid(),'local_processing':'true'})
    for _ in range(50):
        if get('source',source['id'])['status'] in ('ready','failed'):break
        assert process_once()
    source=get('source',source['id']);assert source['status']=='ready'
    turn=req('POST',base+'/messages',json={'text':'按办事处生成收入汇总','request_id':uid(),'source_id':source['id']})
    schema={'meaning':'收入明细','grain':['nexus_row'],'columns':[{'name':'nexus_row','data_type':'text','meaning':'行号'},
        {'name':'c_1','data_type':'text','meaning':'办事处'}, {'name':'c_2','data_type':'decimal','meaning':'收入','unit':'CNY','aggregation':'additive'}]}
    plan={'version':1,'name':'汇总','inputs':{'table':schema},'stages':[{'id':'total','kind':'aggregate','description':'汇总','input':'table',
        'by':['c_1'],'meaning':'各办事处收入','aggregations':[{'function':'sum','column':'c_2','output':{'name':'amount','data_type':'decimal','meaning':'金额','unit':'CNY'}}]}],'output':'total'}
    document={'name':'收入','nodes':[{'id':'read','kind':'source_table','title':'读取',
        'config':{'source_id':source['id'],'revision':source['revision'],'content_hash':source['content_hash']}},
        {'id':'sum','kind':'table_workflow','title':'汇总','depends_on':['read'],
         'inputs':{'table':{'source':'node','name':'read'}},'config':{'plan':plan}}],'outputs':['sum']}
    flow=req('POST',base+'/business-flows',json={'request_id':uid(),'turn_id':turn['id'],'document':document})
    path=base+'/business-flows/'+flow['id'];run=req('POST',path+'/runs',json={'request_id':uid(),'expected_digest':flow['candidate_digest'],'inputs':{}})
    for _ in range(50):
        if get('run',run['run_id'])['status'] in ('succeeded','failed'):break
        assert process_once()
    job=get('run',run['run_id']);assert job['status']=='succeeded'
    state=unseal(job['secret_flow_checkpoint']);assert state['outputs']['sum']['rows_ref']['version']==2
    prefix=path+'/runs/'+run['run_id']+'/tables/sum'
    first=req('GET',prefix+'?offset=0&limit=2');last=req('GET',prefix+'?offset=2&limit=2')
    assert first['total']==last['total']==3 and first['next_offset']==2 and last['next_offset'] is None
    assert len(first['rows'])==2 and len(last['rows'])==1
    assert {row['amount'] for row in first['rows']+last['rows']}=={'1.00','2.00','3.00'}
    with actor_scope('u_'+'a'*64):
        with pytest.raises(ValueError):business_flows.get_run_table(pid,flow['id'],run['run_id'],'sum',0,2)
