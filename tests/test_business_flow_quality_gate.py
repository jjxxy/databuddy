import pytest
from agent_service.business_flow_engine import FlowStepError
from agent_service.business_flow_quality_gate import run,validate
from agent_service.data_workflows import digest


def value(rows):
    schema={'meaning':'金额','grain':[],'columns':[{'name':'region','data_type':'text','meaning':'地区'},
      {'name':'amount','data_type':'decimal','meaning':'金额','unit':'CNY'}]}
    return {'kind':'table_workflow_v1','complete':True,'schema':schema,'rows':rows,'row_count':len(rows),'output_sha256':digest({'schema':schema,'rows':rows})}

def test_explicit_rules_pass_preserving_decimal_values():
    table=value([{'region':'华东','amount':'18.00'},{'region':'华南','amount':'0.01'}])
    rules={'rules':[{'kind':'required','column':'amount'},{'kind':'range','column':'amount','minimum':'0','maximum':'18.00'},{'kind':'unique','columns':['region']}]}
    assert run({},rules,{'table':table})==table

@pytest.mark.parametrize('rows,rule,code',[
 ([{'region':'华东','amount':None}],{'kind':'required','column':'amount'},'CHECK_REQUIRED_FAILED'),
 ([{'region':'华东','amount':'1'},{'region':'华东','amount':'2'}],{'kind':'unique','columns':['region']},'CHECK_UNIQUE_FAILED'),
 ([{'region':'华东','amount':'18.01'}],{'kind':'range','column':'amount','maximum':'18.00'},'CHECK_RANGE_FAILED')])
def test_failure_is_explicit_and_contains_no_cell_values(rows,rule,code):
    with pytest.raises(FlowStepError) as caught:run({}, {'rules':[rule]}, {'table':value(rows)})
    assert caught.value.code==code and '18.01' not in str(caught.value)

def test_rules_require_explicit_fields_and_valid_decimal_bounds():
    with pytest.raises(ValueError):validate({'rules':[{'kind':'range','column':'amount','minimum':'1e3'}]})
    with pytest.raises(ValueError):run({}, {'rules':[{'kind':'required','column':'missing'}]}, {'table':value([])})

def test_decimal_keys_compare_by_value_without_losing_precision():
    rows=[{'region':'华东','amount':'1.00'},{'region':'华南','amount':'1.0'}]
    with pytest.raises(FlowStepError) as caught:
        run({}, {'rules':[{'kind':'unique','columns':['amount']}]}, {'table':value(rows)})
    assert caught.value.code=='CHECK_UNIQUE_FAILED'
    assert '1.00' not in str(caught.value)
