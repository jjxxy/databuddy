import pytest
from agent_service.business_flow_engine import FlowStepError
from agent_service.business_flow_quality_gate import run
from agent_service.data_workflows import digest


def table(rows, **column_override):
    cols=[{'name':'entity','data_type':'text','meaning':'法人'},
          {'name':'debit','data_type':'decimal','meaning':'借方','unit':'CNY','period':'2026-01','aggregation':'additive'},
          {'name':'credit','data_type':'decimal','meaning':'贷方','unit':'CNY','period':'2026-01','aggregation':'additive'}]
    cols[2].update(column_override)
    schema={'meaning':'凭证','grain':[],'columns':cols}
    return {'kind':'table_workflow_v1','complete':True,'schema':schema,'rows':rows,'row_count':len(rows),'output_sha256':digest({'schema':schema,'rows':rows})}

RULE={'rules':[{'kind':'balance','left_column':'debit','right_column':'credit','group_by':['entity']}]}


def test_grouped_balance_exact_and_preserves_result():
    value=table([{'entity':'A','debit':'0.1','credit':'0'}, {'entity':'A','debit':'0.2','credit':'0.3'}, {'entity':'B','debit':'1.00','credit':'1'}])
    assert run({},RULE,{'table':value})==value


def test_offsets_cannot_hide_out_of_balance_entities():
    value=table([{'entity':'A','debit':'10','credit':'0'}, {'entity':'B','debit':'0','credit':'10'}])
    with pytest.raises(FlowStepError,match='平衡') as error:run({},RULE,{'table':value})
    assert error.value.code=='CHECK_BALANCE_FAILED' and '10' not in str(error.value)


@pytest.mark.parametrize('rows',[[],[{'entity':'A','debit':None,'credit':'0'}],[{'entity':'','debit':'0','credit':'0'}],[{'entity':'A','debit':0.1,'credit':'0.1'}]])
def test_empty_missing_or_inexact_data_is_not_accepted(rows):
    with pytest.raises(FlowStepError):run({},RULE,{'table':table(rows)})


@pytest.mark.parametrize('override',[{'unit':'USD'},{'period':'2026-02'},{'aggregation':'non_additive'}])
def test_different_business_semantics_are_not_implicitly_balanced(override):
    with pytest.raises(ValueError):run({},RULE,{'table':table([{'entity':'A','debit':'0','credit':'0'}],**override)})


def test_integer_columns_cannot_accept_fractional_values():
    value=table([{'entity':'A','debit':'1.2','credit':'1.2'}])
    value['schema']['columns'][1]['data_type']='integer'
    value['schema']['columns'][2]['data_type']='integer'
    value['output_sha256']=digest({'schema':value['schema'],'rows':value['rows']})
    with pytest.raises(FlowStepError):run({},RULE,{'table':value})
