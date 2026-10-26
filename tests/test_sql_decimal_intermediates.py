"""Financial SQL must reject hidden binary-floating intermediates, not rewrap them."""
from decimal import Decimal

import pytest

from agent_service.data_workflows import WorkflowError,execute


def column(name,kind='decimal',unit='CNY',aggregation='additive'):
    return {'name':name,'data_type':kind,'unit':unit,'meaning':'Explicit synthetic '+name,
            'period':'2026-09','aggregation':aggregation}


def schema(columns=None):
    return {'columns':columns or [column('id','text','', 'unknown'),column('amount')],
            'grain':['id'],'meaning':'One exact financial amount per synthetic document'}


def sql_plan(sql):
    return {'name':'Financial SQL intermediate audit','inputs':{'raw':schema()},
        'stages':[{'id':'calculated','kind':'sql','inputs':['raw'],'sql':sql,
            'description':'An explicit deterministic financial query','output_schema':schema()}],
        'output':'calculated'}


@pytest.mark.parametrize('amount',['9007199254740993','99999999999999.99','460001.30','0.13',None])
@pytest.mark.parametrize('sql',[
    'SELECT id, CAST(amount / 1 AS DECIMAL(38,12)) AS amount FROM raw',
    'SELECT id, CAST(amount // 1 AS DECIMAL(38,12)) AS amount FROM raw',
    'WITH intermediate AS (SELECT id, amount / 1 AS amount FROM raw) '
    'SELECT id, CAST(amount AS DECIMAL(38,12)) AS amount FROM intermediate',
])
def test_sql_division_is_rejected_before_decimal_cast_can_hide_precision_loss(amount,sql):
    with pytest.raises(WorkflowError) as error:
        execute(sql_plan(sql),{'raw':[{'id':'synthetic','amount':amount}]})
    assert error.value.code=='SQL_INEXACT_NUMERIC'
    assert error.value.stage_id=='calculated'
    assert 'compute' in str(error.value) and 'DECIMAL' in str(error.value)


@pytest.mark.parametrize('expression',[
    'CAST(CAST(amount AS DOUBLE) AS DECIMAL(38,12))',
    'CAST(CAST(amount AS REAL) AS DECIMAL(38,12))',
    'CAST(CAST(amount AS FLOAT4) AS DECIMAL(38,12))',
    'CAST(CAST(amount AS FLOAT8) AS DECIMAL(38,12))',
    'TRY_CAST(CAST(amount AS DOUBLE PRECISION) AS DECIMAL(38,12))',
    'CAST(amount * 1e0 AS DECIMAL(38,12))',
    'CAST(amount + 0.0E0 AS DECIMAL(38,12))',
    'CAST(AVG(amount) OVER (ORDER BY id) AS DECIMAL(38,12))',
    'CAST(amount + 99999999999999999999999999999999999999999 - 99999999999999999999999999999999999999999 AS DECIMAL(38,12))',
    'CAST(amount + 0.00000000000000000000000000000000000000001 AS DECIMAL(38,12))',
])
def test_cast_aliases_scientific_literals_and_average_cannot_smuggle_float(expression):
    with pytest.raises(WorkflowError) as error:
        execute(sql_plan('SELECT id, '+expression+' AS amount FROM raw'),
                {'raw':[{'id':'synthetic','amount':'99999999999999.99'}]})
    assert error.value.code=='SQL_INEXACT_NUMERIC'


@pytest.mark.parametrize('part',['EPOCH','JULIAN'])
def test_float_returning_date_extract_cannot_promote_financial_arithmetic(part):
    sql="SELECT id, CAST(amount + EXTRACT("+part+" FROM DATE '2026-09-01') AS DECIMAL(38,12)) AS amount FROM raw"
    with pytest.raises(WorkflowError) as error:
        execute(sql_plan(sql),{'raw':[{'id':'synthetic','amount':'99999999999999.99'}]})
    assert error.value.code=='SQL_INEXACT_NUMERIC'


@pytest.mark.parametrize(('expression','expected'),[
    ('amount','9007199254740993'),
    ('CAST(amount AS DECIMAL(38,12))','9007199254740993'),
    ('amount + CAST(1 AS DECIMAL(38,12))','9007199254740994'),
    ('amount - 1','9007199254740992'),
    ('amount * 0.5','4503599627370496.5'),
    ('ROUND(amount,2)','9007199254740993'),
    ("amount + EXTRACT(YEAR FROM DATE '2026-09-01')",'9007199254743019'),
])
def test_safe_fixed_point_sql_is_still_exact(expression,expected):
    result=execute(sql_plan('SELECT id, '+expression+' AS amount FROM raw'),
                   {'raw':[{'id':'synthetic','amount':'9007199254740993'}]})
    assert Decimal(result['output']['rows'][0]['amount'])==Decimal(expected)
    assert result['complete'] and result['requires_semantic_review']


def test_safe_sql_null_identity_remains_null_without_a_float_repair():
    result=execute(sql_plan('SELECT id, CAST(amount AS DECIMAL(38,12)) AS amount FROM raw'),
                   {'raw':[{'id':'synthetic','amount':None}]})
    assert result['output']['rows'][0]['amount'] is None


def test_scientific_looking_text_is_not_mistaken_for_a_numeric_literal():
    result=execute(sql_plan("SELECT id, amount FROM raw WHERE id = '1e0'"),
                   {'raw':[{'id':'1e0','amount':'9007199254740993.01'}]})
    assert result['output']['rows'][0]['id']=='1e0'
    assert Decimal(result['output']['rows'][0]['amount'])==Decimal('9007199254740993.01')


def compute_plan(operation='divide',rounding_scale=None):
    inputs=schema([column('id','text','', 'unknown'),column('amount'),column('denominator',unit='1',aggregation='unknown')])
    rule={'left':'amount','right':'denominator','operation':operation,'output':column('answer',aggregation='unknown')}
    if rounding_scale is not None:rule['rounding_scale']=rounding_scale
    return {'name':'Existing exact Decimal alternative','inputs':{'raw':inputs},
            'stages':[{'id':'computed','kind':'compute','input':'raw','description':'Exact Decimal calculation',
                       'calculations':[rule]}],'output':'computed'}


@pytest.mark.parametrize('amount',['9007199254740993','99999999999999.99','460001.30','0.13'])
def test_existing_compute_decimal_division_is_an_exact_available_alternative(amount):
    result=execute(compute_plan(),{'raw':[{'id':'synthetic','amount':amount,'denominator':'1'}]})
    assert Decimal(result['output']['rows'][0]['answer'])==Decimal(amount)


def test_compute_ratio_and_aggregate_then_mean_use_explicit_decimal_rounding():
    ratio=compute_plan(rounding_scale=12)
    ratio['inputs']['raw']['columns'][2]['unit']='CNY'
    ratio['stages'][0]['calculations'][0]['output']['unit']='1'
    result=execute(ratio,{'raw':[{'id':'ratio','amount':'13','denominator':'100'}]})
    assert Decimal(result['output']['rows'][0]['answer'])==Decimal('0.13')
    plan={'name':'Exact aggregate then mean','inputs':{'raw':schema()},'stages':[
        {'id':'summary','kind':'aggregate','input':'raw','by':[],'meaning':'Complete exact sum and count',
         'description':'Aggregate complete source rows','aggregations':[
             {'function':'sum','column':'amount','output':column('total')},
             {'function':'count','output':column('count','integer','1','unknown')}]},
        {'id':'mean','kind':'compute','input':'summary','description':'Explicit rounded Decimal mean',
         'calculations':[{'left':'total','right':'count','operation':'divide','rounding_scale':12,
                          'output':column('average',aggregation='unknown')}]}],'output':'mean'}
    result=execute(plan,{'raw':[{'id':'a','amount':'1'},{'id':'b','amount':'1'},{'id':'c','amount':'2'}]})
    assert Decimal(result['output']['rows'][0]['average'])==Decimal('1.333333333333')


@pytest.mark.parametrize(('amount','denominator','code'),[
    ('12','0','DIVIDE_BY_ZERO'),('12',None,'NULL_VALUE'),(None,'1','NULL_VALUE'),
])
def test_exact_compute_zero_and_null_guards_are_preserved(amount,denominator,code):
    with pytest.raises(WorkflowError) as error:
        execute(compute_plan(rounding_scale=12),{'raw':[{'id':'synthetic','amount':amount,'denominator':denominator}]})
    assert error.value.code==code


def test_sql_zero_division_is_rejected_instead_of_becoming_a_cast_null_or_infinity():
    with pytest.raises(WorkflowError) as error:
        execute(sql_plan('SELECT id, TRY_CAST(amount / 0 AS DECIMAL(38,12)) AS amount FROM raw'),
                {'raw':[{'id':'synthetic','amount':'12'}]})
    assert error.value.code=='SQL_INEXACT_NUMERIC'


def test_safe_sql_sum_count_min_max_remain_available():
    output={'columns':[column('total'),column('count','integer','1'),column('minimum'),column('maximum')],
            'grain':[],'meaning':'Complete exact financial aggregate'}
    plan=sql_plan('SELECT SUM(amount) AS total, COUNT(*) AS count, MIN(amount) AS minimum, MAX(amount) AS maximum FROM raw')
    plan['stages'][0]['output_schema']=output
    result=execute(plan,{'raw':[{'id':'a','amount':'9007199254740993.01'},{'id':'b','amount':'-0.01'}]})
    row=result['output']['rows'][0]
    assert Decimal(row['total'])==Decimal('9007199254740993') and row['count']==2
    assert Decimal(row['minimum'])==Decimal('-0.01') and Decimal(row['maximum'])==Decimal('9007199254740993.01')
