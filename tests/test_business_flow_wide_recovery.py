"""End-to-end 100k-row, 12-column fixture; run separately from memory-heavy suites."""
from decimal import Decimal
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.execution import process_once
from agent_service.store import get,uid
from agent_service.business_flow_engine import Handler,run
from agent_service.business_flow_data import source_table,table_workflow,validate_source,validate_table


def test_wide_100000_csv_is_exact_and_resumes_snapshot():
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    def req(method,path,**kwargs):
        response=client.request(method,path,headers=headers,**kwargs)
        assert response.status_code==200,response.text
        return response.json()
    pid=req('POST','/api/projects',json={'name':'宽表测试','request_id':uid()})['id'];base='/api/projects/'+pid
    header='办事处,收入,'+','.join('字段'+str(i) for i in range(1,11))+'\n'
    data=(header+('东部,1.25,'+','.join(['维度']*10)+'\n')*100000).encode('utf-8')
    source=req('POST',base+'/attachments',files={'file':('wide.csv',data,'text/csv')},
               data={'request_id':uid(),'local_processing':'true'})
    for _ in range(30):
        if get('source',source['id'])['status']=='ready':break
        assert process_once()
    source=get('source',source['id']);assert source['status']=='ready'
    turn=req('POST',base+'/messages',json={'text':'按办事处汇总','request_id':uid(),'source_id':source['id']})
    context={'project_id':pid,'actor_id':'owner','turn_id':turn['id'],'run_id':uid()}
    columns=[{'name':'nexus_row','data_type':'text','meaning':'读取行号'},
             {'name':'c_1','data_type':'text','meaning':'办事处'},
             {'name':'c_2','data_type':'decimal','meaning':'收入','unit':'CNY','aggregation':'additive'}]
    columns += [{'name':'c_'+str(i),'data_type':'text','meaning':'维度字段'} for i in range(3,13)]
    plan={'version':1,'name':'宽表精确汇总','budget':{'timeout_seconds':180},
          'inputs':{'table':{'meaning':'全部明细','grain':['nexus_row'],'columns':columns}},
          'stages':[{'id':'total','kind':'aggregate','description':'按办事处汇总','input':'table',
                     'by':['c_1'],'meaning':'收入汇总','aggregations':[{'function':'sum','column':'c_2',
                     'output':{'name':'amount','data_type':'decimal','meaning':'收入','unit':'CNY'}}]}],
          'output':'total'}
    doc={'name':'宽表汇总','nodes':[{'id':'read','kind':'source_table','title':'读取',
            'config':{'source_id':source['id'],'revision':source['revision'],'content_hash':source['content_hash']}},
           {'id':'total','kind':'table_workflow','title':'汇总','depends_on':['read'],
            'inputs':{'table':{'source':'node','name':'read'}},'config':{'plan':plan}}], 'outputs':['total']}
    handlers={'source_table':Handler(source_table,True,True,validate_source),
              'table_workflow':Handler(table_workflow,True,True,validate_table)}
    checkpoints=[];first=run(doc,{},handlers,context,persist=checkpoints.append)
    assert first['status']=='succeeded',first
    assert first['steps'][0]['output']['rows_ref']['row_count']==100000
    assert Decimal(first['outputs']['total']['rows'][0]['amount'])==Decimal('125000.00')
    source_checkpoint=next(item for item in checkpoints if len(item['steps'])==1 and item['steps'][0]['status']=='succeeded')
    def fail_repeated_read(*args):raise AssertionError('A valid checkpoint must be reused')
    handlers['source_table']=Handler(fail_repeated_read,True,True,validate_source)
    restored=run(doc,{},handlers,context,checkpoint=source_checkpoint,persist=lambda value:None)
    assert restored['status']=='succeeded' and Decimal(restored['outputs']['total']['rows'][0]['amount'])==Decimal('125000.00')
