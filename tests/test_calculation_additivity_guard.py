import pytest

from agent_service.data_workflows import Calculation, Column, WorkflowError, _calculation_semantics, execute


def col(name, meaning, unit, aggregation='additive'):
    return Column(name=name, data_type='decimal', meaning=meaning, unit=unit,
                  period='2026-10', aggregation=aggregation)


@pytest.mark.parametrize(('operation', 'right_meaning'), [
    ('divide', '件数'),
    ('divide', '行级含税系数'),
    ('multiply', '行级比例'),
])
def test_dimensionless_column_is_not_proof_of_additive_result(operation, right_meaning):
    fields={'amount':col('amount','收入','CNY'), 'factor':col('factor',right_meaning,'1')}
    rule=Calculation(left='amount',right='factor',operation=operation,
                     output=col('result','运算结果','CNY'))
    with pytest.raises(WorkflowError) as exc:
        _calculation_semantics(rule, fields)
    assert exc.value.code=='NON_ADDITIVE'


@pytest.mark.parametrize('operation', ['add', 'subtract'])
def test_add_subtract_of_compatible_additive_measures_remains_allowed(operation):
    fields={'left':col('left','本期收入','CNY'), 'right':col('right','可加金额','CNY')}
    rule=Calculation(left='left',right='right',operation=operation,
                     output=col('result','运算结果','CNY'))
    _calculation_semantics(rule, fields)


def scale_plan():
    column=lambda name,meaning,unit,aggregation: col(name,meaning,unit,aggregation).model_dump()
    return {'version':1,'name':'受核对的逐笔税额换算',
            'inputs':{'sales':{'meaning':'订单','grain':['id'],'columns':[
                {'name':'id','data_type':'text','meaning':'订单号'},
                column('amount','含税金额','CNY','additive'),
                column('factor','税率系数','1','non_additive')]}},
            'stages':[{'kind':'compute','id':'converted','description':'逐笔换算',
                'input':'sales','calculations':[{'left':'amount','right':'factor','operation':'divide',
                    'constant_scaling':True,'rounding_scale':2,
                    'output':column('net','不含税金额','CNY','additive')}]}],
            'output':'converted'}


def test_declared_constant_scale_is_verified_and_requires_business_review():
    plan=scale_plan(); rows=[{'id':'a','amount':'113.00','factor':'1.13'},
                            {'id':'b','amount':'226.00','factor':'1.13'}]
    result=execute(plan,{'sales':rows})
    assert result['requires_semantic_review'] is True
    assert [row['net'] for row in result['output']['rows']]==['100.00','200.00']
    rows[1]['factor']='1.20'
    with pytest.raises(WorkflowError) as exc:execute(plan,{'sales':rows})
    assert exc.value.code=='NON_CONSTANT_SCALE'


@pytest.mark.parametrize(('factor','code'), [('0','DIVIDE_BY_ZERO'),(None,'NULL_VALUE')])
def test_constant_factor_never_allows_zero_divisor_or_missing(factor,code):
    with pytest.raises(WorkflowError) as exc:
        execute(scale_plan(),{'sales':[{'id':'a','amount':'113.00','factor':factor}]})
    assert exc.value.code==code


def test_constant_claim_does_not_turn_additive_quantity_into_a_rate():
    plan=scale_plan()
    plan['inputs']['sales']['columns'][2].update(meaning='件数',aggregation='additive')
    with pytest.raises(WorkflowError) as exc:
        execute(plan,{'sales':[{'id':'a','amount':'100.00','factor':'2'},
                               {'id':'b','amount':'100.00','factor':'2'}]})
    assert exc.value.code=='NON_ADDITIVE'


def test_compute_rounding_is_half_even_and_guidance_discloses_limit():
    from agent_service.workflow_guidance import guide
    rows=execute(scale_plan(),{'sales':[{'id':'a','amount':'1.005','factor':'1'}]})['output']['rows']
    assert rows[0]['net']=='1.00'  # HALF_UP would produce 1.01: they are not interchangeable.
    instruction=' '.join(guide()['instructions'])
    assert 'half-even' in instruction and 'half-up' in instruction and '不能满足' in instruction
