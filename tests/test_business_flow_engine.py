"""Real execution and restart boundaries of the reusable workflow core."""
import copy
import pytest
from pydantic import ValidationError

from agent_service.business_flow_engine import BusinessFlow, Handler, run


def document():
    return {'version':1,'name':'收入报告','inputs':['source'],
        'nodes':[
            {'id':'read','kind':'read','title':'读取资料','inputs':{'source':{'source':'input','name':'source'}}},
            {'id':'sum','kind':'sum','title':'计算总额','depends_on':['read'],
             'inputs':{'rows':{'source':'node','name':'read'}}},
            {'id':'report','kind':'report','title':'生成报告','depends_on':['sum'],
             'inputs':{'total':{'source':'node','name':'sum'}}}],
        'outputs':['report']}


def test_flow_uses_frozen_references_records_and_reuses_completed_results():
    calls=[];saved=[]
    handlers={
        'read':Handler(lambda ctx,cfg,values:(calls.append('read') or values['source']),True,True),
        'sum':Handler(lambda ctx,cfg,values:(calls.append('sum') or {'amount':'18.00','rows':len(values['rows'])}),True,True),
        'report':Handler(lambda ctx,cfg,values:(calls.append('report') or {'artifact_id':'fixed','total':values['total']}),True,False),
    }
    inputs={'source':[{'amount':'9.00'},{'amount':'9.00'}]}
    result=run(document(),inputs,handlers,{},persist=saved.append)
    assert result['status']=='succeeded' and calls==['read','sum','report']
    assert result['outputs']['report']['total']=={'amount':'18.00','rows':2}
    assert all(s['status']=='succeeded' and len(s['input_sha256'])==64 and len(s['output_sha256'])==64 for s in result['steps'])
    assert run(document(),inputs,handlers,{},checkpoint=result,persist=saved.append)['status']=='succeeded'
    assert calls==['read','sum','report'],'completed effects must not run again'


def test_invalid_graph_and_unregistered_handlers_are_rejected_before_execution():
    invalid=document();invalid['nodes'][0]['inputs']['source']={'source':'node','name':'report'}
    with pytest.raises(ValidationError):BusinessFlow.model_validate(invalid)
    cycle=document();cycle['nodes'][0]['depends_on']=['report']
    with pytest.raises(ValidationError):BusinessFlow.model_validate(cycle)
    duplicate=document();duplicate['nodes'][1]['id']='read'
    with pytest.raises(ValidationError):BusinessFlow.model_validate(duplicate)
    saved=[]
    with pytest.raises(ValueError,match='未提供'):
        run(document(),{'source':[]},{}, {},persist=saved.append)
    assert not saved
    # A missing later capability must be detected before the first step runs.
    effects=[]
    with pytest.raises(ValueError,match='未提供'):
        run(document(),{'source':[]},{'read':Handler(lambda *_:effects.append('read'),True,True)}, {},persist=saved.append)
    assert not effects and not saved


def test_unconfirmed_side_effect_after_crash_is_not_reexecuted():
    doc={'name':'报告','inputs':[],'nodes':[{'id':'make','kind':'office','title':'创建报告'}],'outputs':['make']}
    effects=[];saved=[]
    def persist(value):
        # Fail after actual write before its success is persisted.
        if value['steps'] and value['steps'][-1]['status']=='succeeded':raise RuntimeError('storage unavailable')
        saved.append(copy.deepcopy(value))
    handlers={'office':Handler(lambda ctx,cfg,values:(effects.append('wrote') or {'artifact':'v1'}),True,False)}
    with pytest.raises(RuntimeError):run(doc,{},handlers,{},persist=persist)
    assert effects==['wrote'] and saved[-1]['steps'][0]['status']=='running'
    recovered=run(doc,{},handlers,{},checkpoint=saved[-1],persist=saved.append)
    assert recovered['status']=='unknown' and recovered['steps'][0]['error_code']=='EXECUTION_OUTCOME_UNKNOWN'
    assert effects==['wrote']


def test_safe_read_can_resume_after_crash_and_handler_version_is_bound():
    doc={'name':'读取','inputs':[],'nodes':[{'id':'one','kind':'read','title':'读'}],'outputs':['one']}
    calls=[];saved=[]
    def persist(value):
        if value['steps'] and value['steps'][0]['status']=='succeeded':raise RuntimeError('offline')
        saved.append(copy.deepcopy(value))
    handler=Handler(lambda ctx,cfg,values:(calls.append(1) or {'value':'1'}),True,True,version='1')
    with pytest.raises(RuntimeError):run(doc,{}, {'read':handler},{},persist=persist)
    with pytest.raises(ValueError,match='不一致'):
        run(doc,{}, {'read':Handler(handler.run,True,True,version='2')},{},checkpoint=saved[-1],persist=lambda x:None)
    assert run(doc,{}, {'read':handler},{},checkpoint=saved[-1],persist=lambda x:None)['status']=='succeeded'
    assert len(calls)==2


def test_tampered_result_or_changed_input_cannot_be_replayed():
    handler=Handler(lambda ctx,cfg,inputs:inputs['v'],True,True)
    doc={'name':'计算','inputs':['v'],'nodes':[{'id':'x','kind':'read','title':'读',
        'inputs':{'v':{'source':'input','name':'v'}}}],'outputs':['x']}
    result=run(doc,{'v':{'amount':'0.01'}},{'read':handler},{},persist=lambda x:None)
    corrupt=copy.deepcopy(result);corrupt['steps'][0]['output']['amount']='999'
    with pytest.raises(ValueError,match='摘要'):
        run(doc,{'v':{'amount':'0.01'}},{'read':handler},{},checkpoint=corrupt,persist=lambda x:None)
    with pytest.raises(ValueError,match='不一致'):
        run(doc,{'v':{'amount':'0.02'}},{'read':handler},{},checkpoint=result,persist=lambda x:None)
    corrupt=copy.deepcopy(result);corrupt['steps'][0]['status']='running'
    with pytest.raises(ValueError,match='未完成'):
        run(doc,{'v':{'amount':'0.01'}},{'read':handler},{},checkpoint=corrupt,persist=lambda x:None)


def test_private_error_content_not_returned_and_unsafe_error_is_unknown():
    doc={'name':'写入','inputs':[],'nodes':[{'id':'write','kind':'write','title':'写'}],'outputs':['write']}
    def fail(ctx,cfg,inputs):raise ValueError('secret-content')
    result=run(doc,{}, {'write':Handler(fail,False,False)}, {},persist=lambda x:None)
    assert result['status']=='unknown' and 'secret-content' not in str(result)
    assert run(doc,{}, {'write':Handler(fail,False,False)}, {},checkpoint=result,persist=lambda x:None)==result


@pytest.mark.parametrize(('code','expected'),[
    ('INVALID_EFFECTIVE_INTERVAL','生效区间无效'),
    ('OVERLAPPING_EFFECTIVE_INTERVALS','有效期重叠'),
    ('INVALID_BUSINESS_DATE','业务日期缺失'),
    ('MISSING_LINEAGE','无法追溯'),
    ('TYPE_MISMATCH','日期格式'),
])
def test_temporal_lookup_errors_are_actionable_and_do_not_leak_raw_values(code,expected):
    from agent_service.data_workflows import WorkflowError
    doc={'name':'对照规则','inputs':[],'nodes':[{'id':'match','kind':'read','title':'核对规则'}],'outputs':['match']}
    def fail(*args):raise WorkflowError(code,'private_customer_data')
    result=run(doc,{}, {'read':Handler(fail,True,True)}, {},persist=lambda value:None)
    receipt=result['steps'][0]
    assert result['status']=='failed' and receipt['error_code']==code
    assert expected in receipt['error_message'] and 'private_customer_data' not in str(result)


def test_missing_inputs_and_oversized_or_non_json_values_rejected():
    with pytest.raises(ValueError,match='输入'):
        run(document(),{}, {}, {},persist=lambda x:None)
    with pytest.raises(ValueError,match='安全保存'):
        run(document(),{'source':'x'*(2*1024*1024)}, {}, {},persist=lambda x:None)
    with pytest.raises(ValueError,match='JSON'):
        run(document(),{'source':float('nan')}, {}, {},persist=lambda x:None)


def test_report_commentary_requires_real_model_or_skill_node():
    flow={'name':'报告','inputs':['fake'], 'nodes':[
        {'id':'source','kind':'source_table','title':'读','inputs':{'source':{'source':'input','name':'fake'}}},
        {'id':'table','kind':'table_workflow','title':'计算','depends_on':['source'],
         'inputs':{'table':{'source':'node','name':'source'}}},
        {'id':'comment','kind':'model','title':'说明'},
        {'id':'report','kind':'table_report_spec','title':'报告','depends_on':['table','comment'],
         'inputs':{'table':{'source':'node','name':'table'},'commentary':{'source':'node','name':'comment'}}}],
        'outputs':['report']}
    BusinessFlow.model_validate(flow)
    flow['nodes'][2]['kind']='skill'
    BusinessFlow.model_validate(flow)
    flow['nodes'][2]['kind']='source_table'
    flow['nodes'][2]['config']={'source_id':'x','revision':1,'content_hash':'a'*64}
    with pytest.raises(ValidationError,match='模型或技能'):
        BusinessFlow.model_validate(flow)
    flow['nodes'][3]['inputs']['commentary']={'source':'input','name':'fake'}
    with pytest.raises(ValidationError,match='模型或技能'):
        BusinessFlow.model_validate(flow)
