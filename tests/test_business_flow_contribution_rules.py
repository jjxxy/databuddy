"""Eligibility and grouping of actual aggregate input; never financial approval."""
import copy
from types import SimpleNamespace
import pytest

from agent_service.data_workflows import WorkflowPlan, TableSchema
from agent_service.business_flows import _aggregate_contribution, _matching_aggregate_rows


def plan():
    return {'name':'按分组求和','inputs':{'input':{'meaning':'完整明细','grain':['id'],
        'columns':[{'name':'id','data_type':'text','meaning':'记录号'},
                   {'name':'g','data_type':'decimal','meaning':'分组','unit':'1','nullable':True},
                   {'name':'amount','data_type':'decimal','meaning':'金额','unit':'CNY','aggregation':'additive'}]}},
        'stages':[{'id':'total','kind':'aggregate','description':'求和','input':'input','by':['g'],
                   'meaning':'金额汇总','aggregations':[{'function':'sum','column':'amount',
                       'output':{'name':'total','data_type':'decimal','meaning':'金额合计','unit':'CNY'}}]}],
        'output':'total'}


def test_group_matching_uses_typed_equality_preserves_duplicates_and_nulls():
    config=WorkflowPlan.model_validate(plan());stage=config.stages[0];schema=config.inputs['input']
    rows=[{'id':'1','g':'1.00','amount':'2.00'},{'id':'2','g':'1.0','amount':'2.00'},
          {'id':'3','g':None,'amount':'9.00'},{'id':'4','g':'2','amount':'2.00'}]
    assert [r['id'] for r in _matching_aggregate_rows(rows,{'g':'1'},schema,stage)]==['1','2']
    assert [r['id'] for r in _matching_aggregate_rows(rows,{'g':None},schema,stage)]==['3']
    assert _matching_aggregate_rows(rows,{'g':'3'},schema,stage)==[]
    ungrouped=stage.model_copy(update={'by':[]})
    assert _matching_aggregate_rows(rows,{},schema,ungrouped)==rows
    assert _matching_aggregate_rows([],{},schema,ungrouped)==[]


@pytest.mark.parametrize('kind,column', [('min','amount'),('max','amount'),('count','amount'),('concat','g')])
def test_unsupported_aggregates_do_not_claim_row_contributions(monkeypatch,kind,column):
    from agent_service import business_flows
    config=WorkflowPlan.model_validate(plan());stage=config.stages[0]
    rule=stage.aggregations[0].model_copy(update={'function':kind,'column':column})
    stage=stage.model_copy(update={'aggregations':[rule]})
    config=config.model_copy(update={'stages':[stage]})
    monkeypatch.setattr(business_flows,'_fx_evidence_plan',lambda *a:config)
    output={'contribution_evidence':{'stage_id':'total','input_id':'input','schema':{},'row_count':0,
                                     'output_sha256':'0'*64,'rows_ref':{}}}
    with pytest.raises(ValueError,match='不支持'):
        _aggregate_contribution({},'process',output)


def test_count_all_and_empty_global_group_capture_real_input():
    from agent_service.data_workflows import execute
    spec=plan();spec['stages'][0]['by']=[]
    spec['stages'][0]['aggregations']=[{'function':'count','column':None,
        'output':{'name':'count','data_type':'integer','meaning':'记录数','unit':'1'}}]
    captured=[]
    result=execute(spec,{'input':[]},on_aggregate_input=lambda stage,schema,rows,receipt:captured.append((stage,schema,rows,receipt)))
    assert result['output']['rows']==[{'count':0}]
    stage,schema,rows,receipt=captured[0]
    assert rows==[] and receipt['inputs'][0]['rows']==0
    assert _matching_aggregate_rows(rows,{},TableSchema.model_validate(schema),stage)==[]
    records=[{'id':'1','g':None,'amount':'2.00'},{'id':'2','g':None,'amount':'2.00'}]
    captured=[]
    result=execute(spec,{'input':records},on_aggregate_input=lambda stage,schema,rows,receipt:captured.append((stage,schema,rows,receipt)))
    assert result['output']['rows']==[{'count':2}]
    stage,schema,rows,_=captured[0]
    assert len(_matching_aggregate_rows(rows,{},TableSchema.model_validate(schema),stage))==2
