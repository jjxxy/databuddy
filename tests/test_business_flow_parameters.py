"""Business inputs have real consumers, frozen exact values and strict types."""
import pytest
from agent_service.business_flow_engine import BusinessFlow,Handler,_prepare,digest,run


def flow():
    specs={'period':{'type':'date','label':'统计日期'},'amount':{'type':'decimal','label':'金额'},
           'count':{'type':'integer','label':'份数'},'flag':{'type':'boolean','label':'是否汇总'},
           'kind':{'type':'choice','label':'类型','options':['收入','支出']},'name':{'type':'text','label':'名称'}}
    return {'name':'参数测试','inputs':list(specs),'input_specs':specs,
            'nodes':[{'id':'m_'+name,'kind':'model','title':spec['label'],'inputs':{'content':{'source':'input','name':name}}} for name,spec in specs.items()],
            'outputs':['m_amount']}


def test_legacy_definition_does_not_change_hash():
    doc={'name':'legacy','inputs':[],'nodes':[{'id':'read','kind':'source_table','title':'读取','config':{'source_id':'s','revision':1,'content_hash':'x'}}],'outputs':['read']}
    before=BusinessFlow.model_validate(doc).model_dump(mode='json')
    assert 'input_specs' not in before
    assert digest(before)==digest(BusinessFlow.model_validate(before).model_dump(mode='json'))


def test_parameters_are_checked_and_consumed_exactly():
    values={'period':'2026-10-31','amount':'12345678901234567890.0123','count':7,'flag':False,'kind':'收入','name':'东部'}
    seen=[]
    def handler(ctx,config,inputs):seen.append(inputs['content']);return inputs['content']
    handler_set={'model':Handler(handler,True,True)}
    result=run(flow(),values,handler_set,{},persist=lambda _:None)
    assert result['status']=='succeeded' and seen==list(values.values())
    for key,bad in [('period','2026-02-30'),('amount',3.14),('amount','1e3'),('count',True),('count',2**53),('flag','false'),('kind','未知'),('name','  ')]:
        with pytest.raises(ValueError,match='流程参数'):_prepare(flow(),dict(values,**{key:bad}),handler_set)


def test_unsupported_financial_binding_and_unused_parameters_rejected():
    doc=flow();doc['nodes'][0]['kind']='table_workflow'
    with pytest.raises(ValueError):BusinessFlow.model_validate(doc)
    doc=flow();doc['nodes'][0]['inputs']={}
    with pytest.raises(ValueError,match='未使用'):BusinessFlow.model_validate(doc)


def test_parameter_can_only_bind_compatible_original_table_filter():
    doc={'name':'收入筛选','inputs':['threshold'], 'input_specs':{'threshold':{'type':'decimal','label':'最低收入'}},
         'nodes':[{'id':'read','kind':'source_table','title':'资料','config':{'source_id':'s','revision':1,'content_hash':'x'}},
                  {'id':'filter','kind':'table_workflow','title':'筛选','depends_on':['read'],
                   'inputs':{'table':{'source':'node','name':'read'},'p_threshold':{'source':'input','name':'threshold'}},
                   'config':{'parameter_bindings':{'p_threshold':{'stage_id':'only','condition_index':0}},
                             'plan':{'inputs':{'table':{'columns':[{'name':'amount','data_type':'decimal'}]}},
                                     'stages':[{'id':'only','kind':'filter','input':'table','conditions':[{'column':'amount','operator':'gte','value':'0'}]}]}}}],
         'outputs':['filter']}
    BusinessFlow.model_validate(doc)
    doc['input_specs']['threshold']['type']='text'
    with pytest.raises(ValueError,match='参数类型'):BusinessFlow.model_validate(doc)
    doc['input_specs']['threshold']['type']='decimal';doc['nodes'][1]['config']['plan']['stages'][0]['input']='transformed'
    with pytest.raises(ValueError,match='原始输入表'):BusinessFlow.model_validate(doc)
