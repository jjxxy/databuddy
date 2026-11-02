"""JSON report evidence reflects the actual selected data, including legacy runs."""
import json

from fastapi.testclient import TestClient

from agent_service.api import app
from agent_service.business_deliverables import table_report_spec
from agent_service.business_flow_data import source_table, table_workflow
from agent_service.config import TOKEN
from agent_service.data_workflows import digest
from agent_service.execution import process_once
from agent_service.store import get, patch, seal, unseal, uid


def test_json_record_path_survives_to_report_and_legacy_is_not_claimed_complete():
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    def request(method,path,**kwargs):
        response=client.request(method,path,headers=headers,**kwargs)
        assert response.status_code==200,response.text
        return response.json()
    pid=request('POST','/api/projects',json={'name':'JSON依据','request_id':uid()})['id']
    root='/api/projects/'+pid
    data={'period':'2026-10','currency':'CNY','records':[{'office':'东部','amount':'12.50'}]}
    source=request('POST',root+'/attachments',files={'file':('income.json',
        json.dumps(data,ensure_ascii=False).encode(),'application/json')},data={'request_id':uid(),'local_processing':'true'})
    for _ in range(100):
        source=get('source',source['id'])
        if source.get('status') in ('ready','failed'):break
        assert process_once()
    assert source['status']=='ready'
    turn=request('POST',root+'/messages',json={'text':'汇总收入并制作报告','request_id':uid(),'source_id':source['id']})
    context={'project_id':pid,'turn_id':turn['id'],'actor_id':'owner'}
    pin={key:source[key] for key in ('id','revision','content_hash')}
    read=source_table(context,{'source_id':pin['id'],'revision':pin['revision'],'content_hash':pin['content_hash']},{})
    selection=read['read_basis']['selection']
    assert selection=={'kind':'json_records','records_path':'records','selection_method':'inferred',
                       'wrapper_context_fields':['period','currency']}
    assert read['requires_source_review'] and read['requires_semantic_review']
    plan={'version':1,'name':'收入汇总','inputs':{'table':{'meaning':'收入明细','grain':['nexus_row'],
        'columns':[{'name':'nexus_row','data_type':'text','meaning':'资料行号'},
                   {'name':'c_1','data_type':'text','meaning':'办事处'},
                   {'name':'c_2','data_type':'decimal','meaning':'收入','unit':'CNY','aggregation':'additive'}]}},
        'stages':[{'id':'total','kind':'aggregate','description':'按办事处汇总','input':'table','by':['c_1'],
                   'meaning':'各办事处收入','aggregations':[{'function':'sum','column':'c_2',
                       'output':{'name':'amount','data_type':'decimal','meaning':'收入合计','unit':'CNY'}}]}],
        'output':'total'}
    result=table_workflow(context,{'plan':plan},{'table':read})
    assert result['selection_lineage_complete'] is True
    assert result['source_refs'][0]['read_basis']['selection']==selection
    report=table_report_spec(context,{'title':'收入','format':'html'},{'table':result})
    text=report['spec']['sections'][0]['body']
    assert 'JSON 记录路径：records' in text and '自动识别，需核对' in text
    assert '记录之外的上下文字段' in text and 'period' in text and 'currency' in text
    assert '读取范围或字段映射记录不完整' not in text
    # The previous implementation represented all JSON selections as an
    # entire source table; old receipts cannot prove which path was read.
    old_basis=dict(read['read_basis'],selection={'kind':'whole_source_table'})
    old_read=dict(read,read_basis=old_basis,read_basis_sha256=digest(old_basis))
    old_result=table_workflow(context,{'plan':plan},{'table':old_read})
    assert old_result['selection_lineage_complete'] is False
    # Directly opening an old result with its historical optimistic flag must
    # also disclose the incomplete path instead of repeating the old claim.
    old_result['selection_lineage_complete']=True
    old_report=table_report_spec(context,{'title':'旧收入','format':'html'},{'table':old_result})
    old_text=old_report['spec']['sections'][0]['body']
    assert '旧版 JSON 记录路径未记录' in old_text
    assert '读取范围或字段映射记录不完整' in old_text
    assert '读取方式：完整来源表' not in old_text
    # A configured explicit record path also selects correctly. The parser
    # does not enumerate sibling metadata in this mode, so the evidence must
    # not claim there are no external period/currency fields.
    saved=unseal(source['secret_config']);saved['records_path']='records'
    patch('source',source['id'],secret_config=seal(saved))
    explicit=source_table(context,{'source_id':pin['id'],'revision':pin['revision'],'content_hash':pin['content_hash']},{})
    explicit_selection=explicit['read_basis']['selection']
    assert explicit_selection=={'kind':'json_records','records_path':'records',
                                'selection_method':'explicit','wrapper_context_status':'not_recorded'}
    assert explicit['requires_source_review'] and explicit['requires_semantic_review']
    explicit_result=table_workflow(context,{'plan':plan},{'table':explicit})
    explicit_report=table_report_spec(context,{'title':'指定路径','format':'html'},{'table':explicit_result})
    assert '记录之外的上下文状态：未记录，需回查原始资料' in explicit_report['spec']['sections'][0]['body']


def test_inferred_json_array_needs_business_review_even_without_sibling_metadata():
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    def request(method,path,**kwargs):
        response=client.request(method,path,headers=headers,**kwargs)
        assert response.status_code==200,response.text
        return response.json()
    pid=request('POST','/api/projects',json={'name':'自动路径核对','request_id':uid()})['id']
    root='/api/projects/'+pid
    source=request('POST',root+'/attachments',files={'file':('data.json',b'{"data":[{"amount":"12.00"}]}','application/json')},
                   data={'request_id':uid(),'local_processing':'true'})
    for _ in range(100):
        source=get('source',source['id'])
        if source.get('status') in ('ready','failed'):break
        assert process_once()
    assert source['status']=='ready'
    turn=request('POST',root+'/messages',json={'text':'读取收入','request_id':uid(),'source_id':source['id']})
    result=source_table({'project_id':pid,'turn_id':turn['id'],'actor_id':'owner'},
                        {'source_id':source['id'],'revision':source['revision'],'content_hash':source['content_hash']},{})
    assert result['read_basis']['selection']['selection_method']=='inferred'
    assert result['requires_source_review'] is True and result['requires_semantic_review'] is True
