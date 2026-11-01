"""Independent Decimal oracle for frozen per-transaction FX conversion."""
import random
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP, localcontext

import pytest

from agent_service.data_workflows import WorkflowError, execute
from test_data_workflows import fx_case


def test_fx_seeded_values_match_an_independent_decimal_oracle():
    # Amounts/rates are deliberately generated as exact decimal strings and
    # independently multiplied, not by the service's own arithmetic helper.
    for seed in range(60):
        randomizer=random.Random(seed)
        plan,inputs=fx_case()
        for row in inputs['transactions']:
            row['amount']=str(Decimal(randomizer.randint(-10**15,10**15))/Decimal(10000))
        for row in inputs['rates']:
            row['rate']=str(Decimal(randomizer.randint(1,10**8))/Decimal(10000))
        mode=('exact','half_even','half_up')[seed%3]
        plan['stages'][1]['rounding_mode']=mode
        if mode!='exact':plan['stages'][1]['rounding_scale']=2
        rows=execute(plan,inputs)['output']['rows']
        for actual in rows:
            with localcontext() as context:
                context.prec=100
                expected=Decimal(actual['amount'])*Decimal(actual['matched_rate'])
                if mode!='exact':
                    expected=expected.quantize(Decimal('0.01'),
                        rounding=ROUND_HALF_EVEN if mode=='half_even' else ROUND_HALF_UP)
            assert Decimal(actual['converted'])==expected


def test_fx_extreme_precision_requires_declared_rounding_and_respects_capacity():
    plan,inputs=fx_case()
    inputs['transactions'][0]['amount']='99999999999999999999999999.123456789012'
    inputs['rates'][0]['rate']='0.000000000001'
    with pytest.raises(WorkflowError,match='represent|exact'):
        execute(plan,inputs)
    plan['stages'][1].update(rounding_mode='half_even',rounding_scale=12)
    actual=execute(plan,inputs)['output']['rows'][0]
    with localcontext() as context:
        context.prec=100
        expected=(Decimal(inputs['transactions'][0]['amount'])*Decimal(inputs['rates'][0]['rate'])).quantize(
            Decimal('0.000000000001'),rounding=ROUND_HALF_EVEN)
    assert Decimal(actual['converted'])==expected
    inputs['rates'][0]['rate']='2'
    with pytest.raises(WorkflowError,match='represent|exact'):
        execute(plan,inputs)


def test_fx_rounds_each_transaction_before_any_later_total():
    plan,inputs=fx_case()
    plan['stages'][1].update(rounding_mode='half_up',rounding_scale=2)
    for row in inputs['transactions']:
        row['amount']='0.005'
    for row in inputs['rates']:
        row['rate']='1'
    result=execute(plan,inputs)
    amounts=[Decimal(row['converted']) for row in result['output']['rows']]
    assert amounts==[Decimal('0.01')]*3
    assert sum(amounts)==Decimal('0.03')
    assert (Decimal('0.005')*3).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)==Decimal('0.02')
    assert result['stages'][1]['checks']['rounding_scope']=='per_transaction'
