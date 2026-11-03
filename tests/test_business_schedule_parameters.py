from datetime import datetime,timezone,timedelta
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.execution import process_once
from agent_service.store import get,patch,uid,unseal
from agent_service import provider


def test_scheduled_business_parameters_are_explicit_exact_and_version_bound(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'','model':'','share_data':True})
    c=TestClient(app);h={'Authorization':'Bearer '+TOKEN}
    def req(method,path,**kwargs):
        r=c.request(method,path,headers=h,**kwargs);assert r.status_code==200,r.text;return r.json()
    pid=req('POST','/api/projects',json={'name':'定时参数验收','request_id':uid()})['id'];base='/api/projects/'+pid
    item=req('POST',base+'/attachments',files={'file':('sales.csv','地区,金额\n东部,9.00\n西部,4.00\n'.encode(),'text/csv')},data={'request_id':uid(),'local_processing':'true'})
    assert process_once();source=get('source',item['id'])
    turn=req('POST',base+'/messages',json={'text':'按地区统计','request_id':uid(),'source_id':source['id']});patch('turn',turn['id'],state='done',outcome='needs_input')
    plan={'version':1,'name':'收入筛选','inputs':{'table':{'meaning':'收入','grain':['nexus_row'],'columns':[
        {'name':'nexus_row','data_type':'text','meaning':'行号'},{'name':'c_1','data_type':'text','meaning':'地区'},
        {'name':'c_2','data_type':'decimal','meaning':'收入','unit':'CNY','aggregation':'additive'}]}},
        'stages':[{'id':'filter','kind':'filter','description':'选择地区','input':'table','conditions':[{'column':'c_1','operator':'eq','value':'东部'},{'column':'c_2','operator':'gte','value':'0.00'}]},
        {'id':'total','kind':'aggregate','description':'汇总收入','input':'filter','by':['c_1'],'meaning':'地区收入','aggregations':[{'function':'sum','column':'c_2','output':{'name':'amount','data_type':'decimal','meaning':'合计','unit':'CNY'}}]}],'output':'total'}
    doc={'name':'地区汇总','inputs':['file','region','minimum'],'input_specs':{'region':{'type':'choice','label':'地区','options':['东部','西部']},'minimum':{'type':'decimal','label':'收入下限'}},'nodes':[
        {'id':'read','kind':'source_table','title':'读取','config':{},'inputs':{'source':{'source':'input','name':'file'}}},
        {'id':'sum','kind':'table_workflow','title':'汇总','depends_on':['read'],'inputs':{'table':{'source':'node','name':'read'},'region':{'source':'input','name':'region'},'minimum':{'source':'input','name':'minimum'}},
         'config':{'plan':plan,'parameter_bindings':{'region':{'stage_id':'filter','condition_index':0},'minimum':{'stage_id':'filter','condition_index':1}}}}],'outputs':['sum']}
    f=req('POST',base+'/business-flows',json={'request_id':uid(),'turn_id':turn['id'],'document':doc});root=base+'/business-flows/'+f['id']
    pin={'source_id':source['id'],'revision':source['revision'],'content_hash':source['content_hash']}
    r=req('POST',root+'/runs',json={'request_id':uid(),'expected_digest':f['candidate_digest'],'inputs':{'file':pin,'region':'东部','minimum':'0.00'}});assert process_once()
    assert req('GET',root+'/runs/'+r['run_id'])['status']=='succeeded'
    req('POST',root+'/publish',json={'run_id':r['run_id'],'expected_digest':f['candidate_digest']})
    body={'cron':'0 8 * * *','source_bindings':{'file':source['id']},'expected_published_digest':f['candidate_digest'],'parameter_values':{'region':'西部','minimum':'3.999999999999'}}
    value=req('PUT',root+'/schedule',json=body);assert 'parameter_values' not in value
    shown=req('GET',root+'/schedule-parameters');assert shown['published_digest']==f['candidate_digest'] and shown['parameter_values']==body['parameter_values']
    assert c.get(root+'/schedule-parameters').status_code in (401,403)
    assert unseal(get('business_flow',f['id'])['schedule_inputs'])=={'file':pin,**body['parameter_values']}
    invalid=c.put(root+'/schedule',headers=h,json={**body,'parameter_values':{'region':'西部','minimum':3.999999999999}});assert invalid.status_code==400
    assert unseal(get('business_flow',f['id'])['schedule_inputs'])=={'file':pin,**body['parameter_values']}
    from agent_service.sources import revision
    revision(get('source',source['id']))
    patch('business_flow',f['id'],next_due=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
    from agent_service.business_flows import queue_due
    queue_due();assert process_once();last=req('GET',root)['latest_run']
    assert last['status']=='succeeded'
    from agent_service.jobs import get_job
    frozen=unseal(get_job(last['id'])['secret_flow_payload'])['inputs'];assert frozen['file']['revision']==pin['revision']+1 and frozen['minimum']=='3.999999999999'
    assert req('GET',root+'/runs/'+last['id']+'/tables/sum')['rows'][0]['amount']=='4.00'
