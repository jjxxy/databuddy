"""Large source data stays out of database checkpoints and is integrity-checked."""
from decimal import Decimal
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import DATA, TOKEN
from agent_service.execution import process_once
from agent_service.store import get, uid
from agent_service.business_flow_engine import Handler,run
from agent_service.business_flow_data import source_table,table_workflow,validate_source,validate_table,materialize_table


def test_100000_rows_from_real_csv_exact_sum_encrypted_snapshot(monkeypatch):
    client=TestClient(app); headers={'Authorization':'Bearer '+TOKEN}
    def request(method,path,**kwargs):
        r=client.request(method,path,headers=headers,**kwargs);assert r.status_code==200,r.text;return r.json()
    pid=request('POST','/api/projects',json={'name':'大数据验证','request_id':uid()})['id'];base='/api/projects/'+pid
    content=('办事处,收入\n'+('东部,1.25\n'*100000)).encode()
    source=request('POST',base+'/attachments',files={'file':('income.csv',content,'text/csv')},
                   data={'request_id':uid(),'local_processing':'true'})
    assert process_once(); source=get('source',source['id'])
    turn=request('POST',base+'/messages',json={'text':'按办事处汇总并生成报告','request_id':uid(),'source_id':source['id']})
    context={'project_id':pid,'actor_id':'owner','turn_id':turn['id'],'run_id':uid()}
    plan={'version':1,'name':'精确汇总','inputs':{'table':{'meaning':'收入明细','grain':['nexus_row'],
        'columns':[{'name':'nexus_row','data_type':'text','meaning':'本次资料行号'},
                   {'name':'c_1','data_type':'text','meaning':'办事处'},
                   {'name':'c_2','data_type':'decimal','meaning':'收入','unit':'CNY','aggregation':'additive'}]}},
        'stages':[{'id':'total','kind':'aggregate','description':'按办事处汇总','input':'table','by':['c_1'],
                   'meaning':'各办事处收入','aggregations':[{'function':'sum','column':'c_2',
                       'output':{'name':'amount','data_type':'decimal','meaning':'收入合计','unit':'CNY'}}]}], 'output':'total'}
    document={'name':'十万行收入汇总','nodes':[
       {'id':'read','kind':'source_table','title':'读取','config':{'source_id':source['id'],
           'revision':source['revision'],'content_hash':source['content_hash']}},
       {'id':'total','kind':'table_workflow','title':'汇总','depends_on':['read'],
           'inputs':{'table':{'source':'node','name':'read'}},'config':{'plan':plan}}], 'outputs':['total']}
    handlers={'source_table':Handler(source_table,True,True,validate_source),
              'table_workflow':Handler(table_workflow,True,True,validate_table)}
    checkpoints=[]
    result=run(document,{},handlers,context,persist=checkpoints.append)
    assert result['status']=='succeeded',result
    assert Decimal(result['outputs']['total']['rows'][0]['amount'])==Decimal('125000.00')
    # Recover a durable checkpoint without re-reading the source: encrypted
    # rows are bound to this exact run and must survive a worker restart.
    saved=next(c for c in checkpoints if len(c['steps'])==1 and c['steps'][0]['status']=='succeeded')
    def should_not_reread(*args):raise AssertionError('source read repeated after checkpoint')
    recovery_handlers={**handlers,'source_table':Handler(should_not_reread,True,True,validate_source)}
    resumed=run(document,{},recovery_handlers,context,checkpoint=saved,persist=lambda value:None)
    assert resumed['status']=='succeeded' and Decimal(resumed['outputs']['total']['rows'][0]['amount'])==Decimal('125000.00')
    # The same 100,000-row flow travels through the real project API and worker.
    saved_flow=request('POST',base+'/business-flows',json={'request_id':uid(),'turn_id':turn['id'],'document':document})
    flow=base+'/business-flows/'+saved_flow['id']
    job=request('POST',flow+'/runs',json={'request_id':uid(),'expected_digest':saved_flow['candidate_digest'],'inputs':{}})
    assert process_once()
    status=request('GET',flow+'/runs/'+job['run_id'])
    assert status['status']=='succeeded',status
    from agent_service.store import unseal
    checkpoint=unseal(get('run',job['run_id'])['secret_flow_checkpoint'])
    assert Decimal(checkpoint['outputs']['total']['rows'][0]['amount'])==Decimal('125000.00')
    assert checkpoint['steps'][0]['output']['rows_ref']['row_count']==100000
    # Report consumes the authenticated referenced table rather than assuming
    # all rows fit inline in the checkpoint.
    from agent_service import business_flow_data as flow_data,business_deliverables as delivery
    monkeypatch.setattr(flow_data,'_INLINE_ROWS_LIMIT',0)
    referenced=flow_data._compact_table(context,result['outputs']['total'])
    report=delivery.table_report_spec(context,{'title':'核对','format':'html'},{'table':referenced})
    # Current reports carry verified results in a structured table; inspect
    # the exact value and row count rather than looking in the old text body.
    details=report['spec']['sections'][1]['table']
    assert len(details['rows'])==report['row_count']==1
    assert details['rows'][0][0]=='东部' and Decimal(details['rows'][0][1])==Decimal('125000.00')
    assert report['table_sha256']==result['outputs']['total']['output_sha256']
    rendered=delivery.render_html(context,{'name':'large-sum-report','renderer_version':delivery.HTML_RENDERER},{'spec':report})
    from agent_service import workspace_artifacts
    html_path,_=workspace_artifacts.file(pid,rendered['artifact_id'])
    html=html_path.read_text(encoding='utf-8')
    assert '东部' in html and '125000.00' in html and '不代表业务口径或财经数字已审核' in html
    assert rendered['financial_verified'] is False
    # A full storage quota rejects new snapshots without deleting the old ones.
    monkeypatch.setenv('NEXUS_FLOW_SNAPSHOT_TOTAL_MB','1')
    with pytest.raises(ValueError,match='存储容量不足'):
        flow_data._compact_table(context,{'rows':[{'value':'a'*1100000}]})
    src=result['steps'][0]['output'];ref=src['rows_ref']
    assert ref['row_count']==100000 and 'rows' not in src
    assert len(str(checkpoints[-1]).encode())<100000
    path=DATA/'business-flow-tables'/ref['scope']/(ref['sha256']+'.enc')
    ciphertext=path.read_bytes();assert b'1.25' not in ciphertext and '东部'.encode() not in ciphertext
    # A different actor, project or run cannot reuse a saved reference.
    for field in ('project_id','actor_id','turn_id','run_id'):
        with pytest.raises(ValueError,match='不一致'):
            materialize_table({**context,field:'other'},src)
    altered=bytearray(ciphertext);altered[-1]^=1;path.write_bytes(altered)
    with pytest.raises(ValueError,match='校验失败'):
        materialize_table(context,src)
