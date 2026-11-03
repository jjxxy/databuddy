"""Real file parsing and typed exact-decimal processing through flow nodes."""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.execution import process_once
from agent_service.store import get, patch, uid
from agent_service.business_flow_engine import BusinessFlow, Handler, run
from agent_service.business_flow_data import source_table, table_workflow, validate_source, validate_table


@pytest.fixture
def created_turns():
    """Remove only test-owned messages so a later global worker cannot run them."""
    from agent_service.store import delete
    ids=[]
    try:yield ids
    finally:
        for turn_id in ids:delete('turn',turn_id)


def test_real_csv_source_and_typed_sum_preserve_duplicate_amounts(created_turns):
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    def request(method,path,**kwargs):
        response=client.request(method,path,headers=headers,**kwargs)
        assert response.status_code==200,response.text
        return response.json()
    pid=request('POST','/api/projects',json={'name':'流程数据','request_id':uid()})['id']
    root='/api/projects/'+pid
    source=request('POST',root+'/attachments',files={'file':('income.csv',
        '办事处,收入\n东部,9.00\n东部,9.00\n'.encode(),'text/csv')},
        data={'request_id':uid(),'local_processing':'true'})
    assert process_once(); source=get('source',source['id'])
    turn=request('POST',root+'/messages',json={'text':'按办事处汇总','request_id':uid(),'source_id':source['id']})
    created_turns.append(turn['id'])
    context={'project_id':pid,'turn_id':turn['id'],'actor_id':'owner'}
    plan={'version':1,'name':'精确汇总','inputs':{'table':{'meaning':'收入明细','grain':['nexus_row'],
        'columns':[{'name':'nexus_row','data_type':'text','meaning':'本次资料行号'},
                   {'name':'c_1','data_type':'text','meaning':'办事处'},
                   {'name':'c_2','data_type':'decimal','meaning':'收入','unit':'CNY','aggregation':'additive'}]}},
        'stages':[{'id':'total','kind':'aggregate','description':'按办事处汇总','input':'table','by':['c_1'],
                   'meaning':'各办事处收入','aggregations':[{'function':'sum','column':'c_2',
                       'output':{'name':'amount','data_type':'decimal','meaning':'收入合计','unit':'CNY'}}]}],
        'output':'total'}
    doc={'name':'收入业务流程','inputs':[], 'nodes':[
        {'id':'read','kind':'source_table','title':'读取收入资料','config':{
            'source_id':source['id'],'revision':source['revision'],'content_hash':source['content_hash']}},
        {'id':'aggregate','kind':'table_workflow','title':'汇总收入','depends_on':['read'],
         'inputs':{'table':{'source':'node','name':'read'}},'config':{'plan':plan}}], 'outputs':['aggregate']}
    handlers={'source_table':Handler(source_table,True,True,validate_source),
              'table_workflow':Handler(table_workflow,True,True,validate_table)}
    result=run(doc,{},handlers,context,persist=lambda value:None)
    assert result['status']=='succeeded',result
    output=result['outputs']['aggregate']
    assert output['complete'] and output['row_count']==1
    assert Decimal(output['rows'][0]['amount'])==Decimal('18.00')
    assert output['financial_evidence_status']=='requires_business_review'
    assert output['stage_receipts'][0]['checks']['complete_table'] is True
    assert output['source_lineage_complete'] is True
    assert output['selection_lineage_complete'] is True
    ref=output['source_refs'][0]
    assert {k:ref[k] for k in ('source_id','revision','content_hash','name')}=={
        'source_id':source['id'],'revision':source['revision'],'content_hash':source['content_hash'],'name':'income.csv'}
    assert ref['read_basis']['selection']=={'kind':'whole_source_table'}
    from agent_service.data_workflows import digest
    assert digest(ref['read_basis'])==ref['read_basis_sha256']
    # Later processing inherits the same pinned inputs; legacy intermediate
    # results without a lineage are explicitly marked incomplete.
    raw=source_table(context,doc['nodes'][0]['config'],{})
    with pytest.raises(ValueError,match='不支持这些读取选项'):
        source_table(context,{**doc['nodes'][0]['config'],'options':{'data_start_row':2}}, {})
    intermediate=dict(raw,kind='table_workflow_v1',source_refs=output['source_refs'],source_lineage_complete=True,
                      selection_lineage_complete=True)
    chained=table_workflow(context,{'plan':plan},{'table':intermediate})
    assert chained['source_refs']==output['source_refs'] and chained['selection_lineage_complete'] is True
    old_ref={k:ref[k] for k in ('source_id','revision','content_hash','name')}
    older=table_workflow(context,{'plan':plan},{'table':dict(intermediate,source_refs=[old_ref])})
    assert older['source_lineage_complete'] is True and older['selection_lineage_complete'] is False
    corrupt=dict(ref,read_basis=dict(ref['read_basis'],column_map={'c_1':'altered'}))
    with pytest.raises(ValueError,match='摘要不一致'):
        table_workflow(context,{'plan':plan},{'table':dict(intermediate,source_refs=[corrupt])})
    intermediate.pop('source_refs');intermediate.pop('source_lineage_complete')
    legacy=table_workflow(context,{'plan':plan},{'table':intermediate})
    assert legacy['source_refs']==[] and legacy['source_lineage_complete'] is False and legacy['selection_lineage_complete'] is False
    # The same immutable flow can take an explicitly frozen new source at run
    # time. The exact source version is part of this run's input hash.
    dynamic=doc.copy();dynamic['inputs']=['input_file'];dynamic['nodes']=[dict(n) for n in doc['nodes']]
    dynamic['nodes'][0]=dict(doc['nodes'][0],config={},inputs={'source':{'source':'input','name':'input_file'}})
    pin={'source_id':source['id'],'revision':source['revision'],'content_hash':source['content_hash']}
    dynamic_result=run(dynamic,{'input_file':pin},handlers,context,persist=lambda value:None)
    assert dynamic_result['status']=='succeeded' and dynamic_result['inputs_sha256']!=result['inputs_sha256']
    assert Decimal(dynamic_result['outputs']['aggregate']['rows'][0]['amount'])==Decimal('18.00')
    other=request('POST','/api/projects',json={'name':'其他项目','request_id':uid()})['id']
    foreign=request('POST','/api/projects/'+other+'/attachments',files={'file':('other.csv',
        '办事处,收入\n西部,1.00\n'.encode(),'text/csv')},data={'request_id':uid(),'local_processing':'true'})
    assert process_once();foreign=get('source',foreign['id'])
    foreign_pin={'source_id':foreign['id'],'revision':foreign['revision'],'content_hash':foreign['content_hash']}
    with pytest.raises(ValueError,match='不属于'):
        source_table(context,{}, {'source':foreign_pin})
    # A subsequently changed input revision is never silently treated as the
    # approved one, even though an existing output can still be inspected.
    patch('source',source['id'],revision=source['revision']+1)
    with pytest.raises(ValueError,match='版本'):
        source_table(context,doc['nodes'][0]['config'],{})


def test_model_or_untrusted_user_input_cannot_be_a_table_workflow_source():
    for source,kind in [('node','model'),('node','skill'),('input',None)]:
        doc={'name':'受限流程','inputs':['raw'] if source=='input' else [],'nodes':[], 'outputs':['table']}
        if kind:
            doc['nodes'].append({'id':'prior','kind':kind,'title':'生成'})
        doc['nodes'].append({'id':'table','kind':'table_workflow','title':'汇总',
            'depends_on':['prior'] if kind else [],
            'inputs':{'data':{'source':source,'name':'prior' if kind else 'raw'}},'config':{}})
        with pytest.raises(ValidationError,match='财务数据'):
            BusinessFlow.model_validate(doc)


def test_same_workbook_distinct_selections_and_mapping_survive_processing(created_turns):
    from io import BytesIO
    from openpyxl import Workbook
    workbook=Workbook();first=workbook.active;first.title='上半年'
    first.append(['办事处','收入']);first.append(['东部',9])
    second=workbook.create_sheet('下半年');second.append(['办事处','收入']);second.append(['西部',11])
    stream=BytesIO();workbook.save(stream)
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    def req(method,path,**kwargs):
        response=client.request(method,path,headers=headers,**kwargs)
        assert response.status_code==200,response.text
        return response.json()
    pid=req('POST','/api/projects',json={'name':'多页签追溯','request_id':uid()})['id'];root='/api/projects/'+pid
    source=req('POST',root+'/attachments',files={'file':('income.xlsx',stream.getvalue(),
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},data={'request_id':uid(),'local_processing':'true'})
    for _ in range(100):
        source=get('source',source['id'])
        if source.get('status') in ('ready','failed'):break
        assert process_once()
    assert source['status']=='ready'
    turn=req('POST',root+'/messages',json={'text':'汇总两张表','request_id':uid(),'source_id':source['id']})
    created_turns.append(turn['id'])
    context={'project_id':pid,'turn_id':turn['id'],'actor_id':'owner'}
    pin={'source_id':source['id'],'revision':source['revision'],'content_hash':source['content_hash']}
    reads=[]
    for sheet in ('上半年','下半年'):
        reads.append(source_table(context,{**pin,'options':{'sheet':sheet,'header_rows':[1]},
                                           'column_map':{'c_1':'branch'}},{}))
    schema={'meaning':'收入','grain':['branch'],'columns':[
        {'name':'nexus_row','data_type':'text','meaning':'本次资料行号'},
        {'name':'branch','data_type':'text','meaning':'办事处'},
        {'name':'c_2','data_type':'decimal','meaning':'收入','unit':'CNY','aggregation':'additive'}]}
    plan={'version':1,'name':'合并两页','inputs':{'first':schema,'second':schema},
          'stages':[{'id':'joined','kind':'union_rows','description':'合并','inputs':['first','second']}],
          'output':'joined'}
    result=table_workflow(context,{'plan':plan},{'first':reads[0],'second':reads[1]})
    assert result['row_count']==2 and result['selection_lineage_complete'] is True
    refs=result['source_refs']
    assert len(refs)==2 and {ref['source_id'] for ref in refs}=={source['id']}
    assert {ref['read_basis']['selection']['sheet'] for ref in refs}=={'上半年','下半年'}
    assert all(ref['read_basis']['column_map']=={'c_1':'branch'} for ref in refs)
    assert all(ref['read_basis']['selection']['header_rows']==[1] for ref in refs)
    assert refs[0]['read_basis_sha256']!=refs[1]['read_basis_sha256']


def test_source_node_cannot_take_a_model_output_as_dynamic_file_identity():
    doc={'name':'来源隔离','inputs':[], 'nodes':[
        {'id':'model','kind':'model','title':'生成'},
        {'id':'source','kind':'source_table','title':'读取','depends_on':['model'],
         'inputs':{'source':{'source':'node','name':'model'}}}], 'outputs':['source']}
    with pytest.raises(ValidationError,match='运行输入'):
        BusinessFlow.model_validate(doc)


def test_report_spec_cannot_claim_untrusted_input_as_processed_table():
    doc={'name':'报告隔离','inputs':['fake'], 'nodes':[
        {'id':'report','kind':'table_report_spec','title':'制作报告',
         'inputs':{'table':{'source':'input','name':'fake'}}}], 'outputs':['report']}
    with pytest.raises(ValidationError,match='已执行的数据加工'):
        BusinessFlow.model_validate(doc)
