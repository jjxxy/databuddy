import copy
from decimal import Decimal
import pytest
from pydantic import ValidationError
from agent_service.data_workflows import execute, WorkflowPlan, WorkflowError, validate_sql, digest


def col(name,kind='text',meaning=None,unit='',aggregation='unknown',**kw):
    return {'name':name,'data_type':kind,'meaning':meaning or name,'unit':unit,
            'aggregation':aggregation,**kw}


def schema(columns,grain,meaning='orders'):
    return {'columns':columns,'grain':grain,'meaning':meaning}


def base():
    columns=[col('id'),col('office'),col('amount','decimal',unit='CNY',aggregation='additive'),
             col('refund','decimal',unit='CNY',aggregation='additive'),col('status')]
    plan={'version':1,'name':'Net revenue','inputs':{'orders':schema(columns,['id'])},
          'stages':[{'kind':'filter','id':'paid','description':'Only paid orders','input':'orders',
                     'conditions':[{'column':'status','operator':'eq','value':'paid'}]},
                    {'kind':'compute','id':'net','description':'Deduct refunds','input':'paid',
                     'calculations':[{'output':col('net','decimal',unit='CNY',aggregation='additive'),
                                      'left':'amount','right':'refund','operation':'subtract'}]},
                    {'kind':'aggregate','id':'offices','description':'Aggregate to office grain','input':'net','by':['office'],
                     'meaning':'Net revenue by office','aggregations':[{'function':'sum','column':'net',
                        'output':col('net_total','decimal',unit='CNY',aggregation='additive')},
                        {'function':'count','output':col('orders','integer',unit='1',aggregation='additive')}]}],
          'output':'offices'}
    rows=[{'id':'a','office':'East','amount':'10.123456789012','refund':'1.000000000001','status':'paid'},
          {'id':'b','office':'East','amount':'30.000000000001','refund':'2','status':'paid'},
          {'id':'c','office':'West','amount':'100','refund':'0','status':'unpaid'}]
    return plan,{'orders':rows}


def test_exact_financial_dag_receipts_and_replay_do_not_mutate_inputs():
    plan,inputs=base();original=copy.deepcopy(inputs);seen=[]
    result=execute(plan,inputs,on_stage=lambda receipt:seen.append(receipt))
    assert result['complete']
    assert result['output']['rows']==[{'office':'East','net_total':'37.123456789012','orders':2}]
    assert result['output']['schema']['grain']==['office']
    assert [s['stage_id'] for s in seen]==['paid','net','offices']
    assert result['stages'][1]['inputs'][0]['sha256']==result['stages'][0]['output_sha256']
    assert inputs==original and execute(plan,inputs)==result
    assert result['output']['sha256']==digest({k:result['output'][k] for k in ('schema','rows')})


def test_stage_receipt_callback_is_a_dependency_barrier():
    plan,inputs=base();seen=[]
    def barrier(receipt):
        seen.append(receipt['stage_id'])
        raise RuntimeError('durable receipt persistence failed')
    with pytest.raises(RuntimeError,match='persistence'):
        execute(plan,inputs,on_stage=barrier)
    assert seen==['paid']


@pytest.mark.parametrize('value', ['0.0000000000001','NaN','1e3',0.1,True,'1,000.00'])
def test_invalid_or_inexact_amounts_fail_before_processing(value):
    plan,inputs=base();inputs['orders'][0]['amount']=value
    with pytest.raises(WorkflowError):execute(plan,inputs)


def test_duplicate_or_missing_grain_cannot_be_silently_processed():
    plan,inputs=base();inputs['orders'][1]['id']='a'
    with pytest.raises(WorkflowError,match='grain'):execute(plan,inputs)
    inputs['orders'][1]['id']=None
    with pytest.raises(WorkflowError,match='grain'):execute(plan,inputs)


def test_unknown_future_cyclic_or_duplicate_stage_references_are_rejected():
    plan,_=base();plan['stages'][0]['input']='offices'
    with pytest.raises(ValidationError):WorkflowPlan.model_validate(plan)
    plan,_=base();plan['stages'][1]['id']='paid'
    with pytest.raises(ValidationError):WorkflowPlan.model_validate(plan)


def test_meaning_unit_period_and_additivity_do_not_disappear_during_aggregation():
    plan,inputs=base();plan['inputs']['orders']['columns'][2]['unit']='USD'
    with pytest.raises(WorkflowError,match='units'):execute(plan,inputs)
    plan,inputs=base();plan['inputs']['orders']['columns'][2]['period']='2026-01'
    with pytest.raises(WorkflowError,match='period'):execute(plan,inputs)
    plan,inputs=base();plan['stages'][1]['calculations'][0]['output']['aggregation']='non_additive'
    with pytest.raises(WorkflowError,match='additive'):execute(plan,inputs)


def test_complete_concatenation_preserves_every_row_in_stable_order():
    plan={'name':'Complete notes','inputs':{'notes':schema([col('id','integer',unit='1'),col('office'),col('text')],['id'])},
          'stages':[{'kind':'aggregate','id':'joined','description':'All notes per office','input':'notes','by':['office'],
                     'meaning':'Office notes','aggregations':[{'function':'concat','column':'text','output':col('all_notes'),
                                                              'order_by':['id'],'delimiter':'|'}]}], 'output':'joined'}
    rows=[{'id':i,'office':'East','text':f'note-{i}'} for i in reversed(range(125))]
    result=execute(plan,{'notes':rows})
    assert result['output']['rows'][0]['all_notes']=='|'.join('note-'+str(i) for i in range(125))
    assert result['stages'][0]['inputs'][0]['rows']==125
    assert result['output']['row_count']==1


def lookup_plan():
    plan={'name':'Crosswalk','inputs':{
        'orders':schema([col('id'),col('province')],['id']),
        'map':schema([col('id'),col('province'),col('office')],['id'],'Business province/office crosswalk')},
        'stages':[{'kind':'lookup','id':'aligned','description':'Declared many-to-one lookup','input':'orders','right':'map',
                   'left_key':'province','right_key':'province','outputs':{'office':col('office')}}], 'output':'aligned'}
    inputs={'orders':[{'id':'A','province':'Anhui'},{'id':'B','province':'Anhui'}],
            'map':[{'id':'1','province':'Anhui','office':'Hefei'}]}
    return plan,inputs


def test_lookup_preserves_all_left_rows_and_blocks_nonfunctional_province_office_mapping():
    plan,inputs=lookup_plan();r=execute(plan,inputs)
    assert [row['office'] for row in r['output']['rows']]==['Hefei','Hefei']
    inputs['map'].append({'id':'2','province':'Anhui','office':'Wuhu'})
    with pytest.raises(WorkflowError,match='one-to-many'):execute(plan,inputs)


def test_lookup_unmapped_values_require_explicit_policy_and_nullable_output():
    plan,inputs=lookup_plan();inputs['orders'][0]['province']='Unknown'
    with pytest.raises(WorkflowError,match='unmatched'):execute(plan,inputs)
    plan['stages'][0]['unmapped']='null'
    assert execute(plan,inputs)['output']['rows'][0]['office'] is None
    plan['stages'][0]['outputs']['office']['nullable']=False
    with pytest.raises(WorkflowError,match='missing'):execute(plan,inputs)


def test_reclassification_requires_complete_mapping_and_preserves_source():
    plan,inputs=base();plan['stages']=[{'kind':'reclassify','id':'types','description':'Reclassify status',
        'input':'orders','column':'status','output':col('category'),'mapping':{'paid':'recognized'}}];plan['output']='types'
    with pytest.raises(WorkflowError,match='unmapped'):execute(plan,inputs)
    plan['stages'][0]['mapping']['unpaid']='excluded'
    rows=execute(plan,inputs)['output']['rows']
    assert rows[0]['status']=='paid' and rows[0]['category']=='recognized'


def test_declared_rounding_required_for_nonterminating_division():
    plan={'name':'Division','inputs':{'table':schema([col('id'),col('a','decimal',unit='CNY'),col('b','integer',unit='1')],['id'])},
          'stages':[{'kind':'compute','id':'rate','description':'Per-item amount','input':'table','calculations':[
              {'left':'a','right':'b','operation':'divide','output':col('per_item','decimal',unit='CNY')}]}],'output':'rate'}
    inputs={'table':[{'id':'a','a':'1','b':3}]}
    with pytest.raises(WorkflowError,match='exactly'):execute(plan,inputs)
    plan['stages'][0]['calculations'][0]['rounding_scale']=4
    assert execute(plan,inputs)['output']['rows'][0]['per_item']=='0.3333'
    inputs['table'][0]['b']=0
    with pytest.raises(WorkflowError,match='zero'):execute(plan,inputs)


def test_select_drop_and_union_cannot_lose_grain_or_meaning():
    plan,inputs=base();plan['stages']=[{'kind':'select','id':'selected','description':'Keep useful columns','input':'orders',
                                    'columns':['office','amount']}];plan['output']='selected'
    with pytest.raises(WorkflowError,match='grain'):execute(plan,inputs)
    plan['stages'][0]['columns'].append('id')
    assert execute(plan,inputs)['output']['row_count']==3
    plan['inputs']['other']=copy.deepcopy(plan['inputs']['orders'])
    inputs['other']=[dict(inputs['orders'][0],id='D')]
    plan['stages']=[{'kind':'union_rows','id':'combined','description':'Append all details','inputs':['orders','other']}]
    plan['output']='combined'
    assert execute(plan,inputs)['output']['row_count']==4
    inputs['other'][0]['id']='a'
    with pytest.raises(WorkflowError,match='grain'):execute(plan,inputs)


@pytest.mark.parametrize('sql',[
    "select * from read_csv('/etc/passwd')", "select * from sqlite_scan('/tmp/a','b')",
    "select * from duckdb_tables()", "select current_setting('access_mode') from orders",
    "select getenv('SECRET') from orders", "select query('select 1') from orders",
    "select random() from orders", "select * from information_schema.tables",
    "select * from orders; drop table orders", "COPY orders TO '/tmp/x'",
    "with recursive a as (select 1 union all select * from a) select * from a",
    "select * from unnest([1,2])", "select * from other", "select * from main.orders",
])
def test_sql_cannot_escape_supplied_tables_or_call_dynamic_functions(sql):
    with pytest.raises(WorkflowError):validate_sql(sql,{'orders'})


def test_sql_cte_aggregation_and_window_run_with_declared_output_semantics():
    plan,inputs=base()
    plan['stages']=[{'kind':'sql','id':'sql_result','description':'Scoped complex SQL','inputs':['orders'],
        'sql':"WITH paid AS (SELECT * FROM orders WHERE status='paid') SELECT office, CAST(SUM(amount-refund) AS DECIMAL(38,12)) AS net_total FROM paid GROUP BY office ORDER BY office",
        'output_schema':schema([col('office'),col('net_total','decimal',unit='CNY',aggregation='additive')],['office'],'Revenue per office')}]
    plan['output']='sql_result'
    result=execute(plan,inputs)
    assert result['output']['rows']==[{'office':'East','net_total':'37.123456789012'}]
    plan['stages'][0]['sql']='select office, row_number() over () as net_total from orders'
    with pytest.raises(WorkflowError,match='ordering'):execute(plan,inputs)


def test_sql_does_not_return_partial_output_when_row_budget_exceeded():
    plan,inputs=base();plan['budget']={'max_rows':3}
    plan['stages']=[{'kind':'sql','id':'expanded','description':'Expansion requires a higher explicit budget','inputs':['orders'],
        'sql':'select a.id || b.id as id from orders a cross join orders b order by id',
        'output_schema':schema([col('id')],['id'])}];plan['output']='expanded'
    with pytest.raises(WorkflowError,match='row budget'):execute(plan,inputs)


def test_result_and_plan_keep_full_semantic_metadata_for_next_agent():
    plan,inputs=base();result=execute(plan,inputs)
    field=next(c for c in result['output']['schema']['columns'] if c['name']=='net_total')
    assert field['unit']=='CNY' and field['meaning']=='net_total' and field['aggregation']=='additive'
    assert result['stages'][-1]['lineage']['by']==['office']
    assert len(result['plan_sha256'])==64 and len(result['output']['sha256'])==64


def test_percentage_storage_and_display_metadata_survive_stages_and_handoff_schema():
    percent=col('share','decimal',unit='%',aggregation='non_additive',
                value_format='percent_fraction',number_format='0.00%',label='Revenue share')
    plan={'name':'Preserve percentage representation','inputs':{'table':schema([col('id'),percent],['id'])},
          'stages':[{'kind':'select','id':'selected','description':'Keep percentage with semantic metadata',
                     'input':'table','columns':['id','share']},
                    {'kind':'filter','id':'retained','description':'Apply a numeric fraction threshold',
                     'input':'selected','conditions':[{'column':'share','operator':'gte','value':'0.1'}]}],
          'output':'retained'}
    result=execute(plan,{'table':[{'id':'a','share':'0.15'},{'id':'b','share':'0.01'}]})
    field=result['output']['schema']['columns'][1]
    assert field['value_format']=='percent_fraction' and field['number_format']=='0.00%'
    assert field['label']=='Revenue share' and field['unit']=='%'
    assert result['output']['rows']==[{'id':'a','share':'0.15'}]
    assert result['stages'][0]['schema_sha256']==result['stages'][1]['schema_sha256']


def test_percentage_representation_must_be_numeric_and_bounded():
    from agent_service.data_workflows import Column
    with pytest.raises(ValidationError):Column.model_validate(col('note',value_format='percent_fraction'))
    with pytest.raises(ValidationError):Column.model_validate(col('x',number_format='0'*256))
    with pytest.raises(ValidationError):Column.model_validate(col('x',label='x'*201))


def test_arithmetic_cannot_mix_percentage_storage_representations_or_relabel_output():
    fraction=col('fraction','decimal',unit='%',value_format='percent_fraction')
    points=col('points','decimal',unit='%',value_format='percent_points')
    plan={'name':'Percentage semantics','inputs':{'table':schema([col('id'),fraction,points],['id'])},
          'stages':[{'kind':'compute','id':'calculated','description':'Representation must be compatible',
                     'input':'table','calculations':[{'left':'fraction','right':'points','operation':'add',
                        'output':col('total','decimal',unit='%',value_format='percent_fraction')}]}],
          'output':'calculated'}
    with pytest.raises(WorkflowError,match='percentage'):execute(plan,{'table':[{'id':'A','fraction':'0.15','points':'15'}]})
    plan['inputs']['table']['columns'][2]['value_format']='percent_fraction'
    plan['stages'][0]['calculations'][0]['output']['value_format']='native'
    with pytest.raises(WorkflowError,match='representation'):execute(plan,{'table':[{'id':'A','fraction':'0.15','points':'0.25'}]})


def test_numeric_aggregate_must_preserve_percentage_representation():
    plan={'name':'Percentage maximum','inputs':{'table':schema([col('id'),col('rate','decimal',unit='%',value_format='percent_fraction')],['id'])},
          'stages':[{'kind':'aggregate','id':'maximum','description':'Maximum rate is still a fraction','input':'table',
                     'by':[],'meaning':'Maximum rate','aggregations':[{'function':'max','column':'rate',
                        'output':col('maximum','decimal',unit='%',value_format='percent_points')}]}], 'output':'maximum'}
    with pytest.raises(WorkflowError):execute(plan,{'table':[{'id':'A','rate':'0.15'}]})
    plan['stages'][0]['aggregations'][0]['output']['value_format']='percent_fraction'
    assert execute(plan,{'table':[{'id':'A','rate':'0.15'}]})['output']['rows']==[{'maximum':'0.15'}]


def test_lookup_and_union_cannot_drop_or_change_numeric_value_format():
    plan={'name':'Keep mapped percentage representation','inputs':{
            'left_table':schema([col('id'),col('office')],['id']),
            'right_table':schema([col('office'),col('rate','decimal',unit='%',value_format='percent_fraction')],['office'])},
          'stages':[{'kind':'lookup','id':'mapped','description':'Mapping retains declared scale','input':'left_table','right':'right_table',
                     'left_key':'office','right_key':'office','outputs':{'rate':col('rate','decimal',unit='%',value_format='percent_points')}}],
          'output':'mapped'}
    inputs={'left_table':[{'id':'A','office':'East'}],'right_table':[{'office':'East','rate':'0.15'}]}
    with pytest.raises(WorkflowError):execute(plan,inputs)
    plan['stages'][0]['outputs']['rate']['value_format']='percent_fraction'
    assert execute(plan,inputs)['output']['rows'][0]['rate']=='0.15'
    plan={'name':'Union with representation conflict','inputs':{
            'a':schema([col('id'),col('rate','decimal',unit='%',value_format='percent_fraction')],['id']),
            'b':schema([col('id'),col('rate','decimal',unit='%',value_format='percent_points')],['id'])},
          'stages':[{'kind':'union_rows','id':'all_rows','description':'Cannot mix fractional and points rates','inputs':['a','b']}],
          'output':'all_rows'}
    with pytest.raises(WorkflowError):execute(plan,{'a':[{'id':'A','rate':'0.15'}],'b':[{'id':'B','rate':'15'}]})
