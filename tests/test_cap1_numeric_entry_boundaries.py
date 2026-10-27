"""CAP1: exact finance policy applies to queries, typed rules and frozen reruns."""
import json
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from agent_service import analysis,codegen,runner
from agent_service.api import app
from agent_service.config import TOKEN,DATA
from agent_service.data_workflows import WorkflowError,validate_sql
from agent_service.errors import TaskError
from agent_service.execution import process_once
from agent_service.jobs import list_jobs
from agent_service.rules import filter_sql,fixed_filter_number,computation_sql,NUMERIC_POLICY
from agent_service.store import get,patch,all_items


@pytest.fixture
def warehouse(tmp_path):
    with duckdb.connect(str(tmp_path/'warehouse.duckdb')) as con:
        con.execute('CREATE SCHEMA business')
        con.execute('CREATE TABLE business.business_objects(id VARCHAR, amount DECIMAL(38,12), legacy DOUBLE)')
        con.execute('INSERT INTO business.business_objects VALUES (?,?,?), (?,?,?)',
                    ['a','9007199254740993.01',9007199254740992.0,'b','-0.01',1.0])
    return tmp_path


@pytest.mark.parametrize('expression',[
    'CAST(amount / 1 AS DECIMAL(38,12))','CAST(AVG(amount) OVER (ORDER BY id) AS DECIMAL(38,12))',
    'CAST(CAST(amount AS DOUBLE) AS DECIMAL(38,12))','CAST(amount * 1e0 AS DECIMAL(38,12))',
    'CAST(SQRT(amount) AS DECIMAL(38,12))','CAST(POWER(amount,1) AS DECIMAL(38,12))',
])
def test_published_query_shares_the_complete_workflow_numeric_and_function_policy(warehouse,expression):
    with pytest.raises(WorkflowError) as error:
        analysis.read_query(warehouse,'SELECT '+expression+' AS result FROM business.business_objects')
    assert error.value.code in ('SQL_INEXACT_NUMERIC','UNSAFE_SQL')


@pytest.mark.parametrize('sql',[
    'SELECT legacy FROM business.business_objects',
    'SELECT CAST(legacy AS DECIMAL(38,12)) FROM business.business_objects',
    'SELECT * FROM business.business_objects',
    'SELECT b.* FROM business.business_objects b',
    "SELECT CAST(t['legacy'] AS DECIMAL(38,12)) FROM business.business_objects t",
    "SELECT CAST(business_objects['legacy'] AS DECIMAL(38,12)) FROM business.business_objects",
    "SELECT CAST((t)['legacy'] AS DECIMAL(38,12)) FROM business.business_objects t",
    'SELECT t FROM business.business_objects t',
    "SELECT t[id] FROM business.business_objects t",
    'WITH prior AS (SELECT legacy AS renamed FROM business.business_objects) '
    'SELECT CAST(renamed AS DECIMAL(38,12)) FROM prior',
])
def test_historical_float_values_cannot_be_relabelled_exact_by_cast_or_cte(warehouse,sql):
    with pytest.raises(ValueError,match='历史FLOAT/DOUBLE'):
        analysis.read_query(warehouse,sql)


def test_nested_historical_float_column_cannot_hide_behind_struct_member(warehouse):
    with duckdb.connect(str(warehouse/'warehouse.duckdb')) as con:
        con.execute('ALTER TABLE business.business_objects ADD payload STRUCT(n DOUBLE)')
        con.execute('UPDATE business.business_objects SET payload = {n: legacy}')
    with pytest.raises(ValueError,match='历史FLOAT/DOUBLE'):
        analysis.read_query(warehouse,'SELECT CAST(payload.n AS DECIMAL(38,12)) FROM business.business_objects')


def test_historical_summary_decimal_type_does_not_hide_float_source_pollution(warehouse):
    with duckdb.connect(str(warehouse/'warehouse.duckdb')) as con:
        con.execute('CREATE TABLE business.summary AS SELECT SUM(CAST(legacy AS DECIMAL(38,12))) amount_total '
                    'FROM business.business_objects')
        assert con.execute('SELECT typeof(amount_total) FROM business.summary').fetchone()[0]=='DECIMAL(38,12)'
    with pytest.raises(ValueError,match='FLOAT/DOUBLE'):
        analysis.read_query(warehouse,'SELECT amount_total FROM business.summary')
    assert analysis.read_query(warehouse,'SELECT COUNT(*) FROM business.business_objects')['rows']==[[2]]


def test_new_summary_from_exact_business_columns_remains_queryable(tmp_path):
    with duckdb.connect(str(tmp_path/'warehouse.duckdb')) as con:
        con.execute('CREATE SCHEMA business')
        con.execute('CREATE TABLE business.business_objects(amount DECIMAL(38,12))')
        con.execute("INSERT INTO business.business_objects VALUES ('9007199254740993.01')")
        con.execute('CREATE TABLE business.summary AS SELECT SUM(amount) amount_total FROM business.business_objects')
    answer=analysis.read_query(tmp_path,'SELECT amount_total FROM business.summary')
    assert Decimal(answer['rows'][0][0])==Decimal('9007199254740993.01')
    # A row STRUCT is a legitimate query result. FastAPI's default encoder
    # converts an untreated nested Decimal to binary float, so sanitize before
    # crossing that boundary, even when the source contains no FLOAT column.
    from fastapi.encoders import jsonable_encoder
    nested=jsonable_encoder(analysis.read_query(tmp_path,'SELECT t FROM business.business_objects t'))
    assert nested['rows'][0][0]['amount']=='9007199254740993.010000000000'
    assert analysis._exact_query_value({'a':[(Decimal('0.130000000001'),None,3)]})=={'a':[['0.130000000001',None,3]]}
    assert analysis._exact_query_value({'a':[(9007199254740993,-9007199254740993,2,True)]})=={
        'a':[['9007199254740993','-9007199254740993',2,True]]}


def test_safe_query_count_exact_columns_aggregation_and_cte_still_work(warehouse):
    assert analysis.read_query(warehouse,'SELECT COUNT(*) n FROM business.business_objects')['rows']==[[2]]
    result=analysis.read_query(warehouse,'SELECT SUM(amount) total FROM business.business_objects')
    assert Decimal(result['rows'][0][0])==Decimal('9007199254740993')
    member=analysis.read_query(warehouse,"SELECT t['amount'] FROM business.business_objects t ORDER BY id")
    assert Decimal(member['rows'][0][0])==Decimal('9007199254740993.01')
    result=analysis.read_query(warehouse,'WITH exact AS (SELECT id, amount FROM business.business_objects) '
                               'SELECT id, CAST(amount AS DECIMAL(38,12)) FROM exact ORDER BY id')
    assert result['rows'][0][0]=='a' and Decimal(result['rows'][0][1])==Decimal('9007199254740993.01')
    with pytest.raises(WorkflowError):
        validate_sql('SELECT amount FROM business.business_objects',{'business_objects'})


def test_grounded_summary_fallback_does_not_restate_a_cast_legacy_float_metric(warehouse,monkeypatch):
    from agent_service import provider
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'','model':'','api_key':''})
    monkeypatch.setattr(provider,'complete',lambda *a,**k:pytest.fail('No model call is needed for this regression'))
    manifest={'run_id':'synthetic','commit':'a'*40,'records':2,'spec':{'amount_field':'legacy','metric_status':'draft'},
              'summary':[{'category':'synthetic','amount_total':'9007199254740992','record_count':2}]}
    answer=analysis.ask(warehouse,manifest,'查看汇总')
    assert answer['mode']=='needs_input' and '9007199254740992' not in answer['answer']
    manifest['spec']['amount_field']='amount';manifest['summary'][0]['amount_total']='9007199254740993'
    exact=analysis.ask(warehouse,manifest,'查看汇总')
    assert exact['mode']=='grounded_rules' and '9007199254740993' in exact['answer']


@pytest.mark.parametrize(('value','expected'),[
    ('1e15','1000000000000000.000000000000'),('1e-12','0.000000000001'),
    ('0E+1000000000','0'),('0E-1000000000','0'),('0.1000000000000','0.100000000000'),
])
def test_numeric_thresholds_are_bounded_fixed_point_literals(value,expected):
    assert fixed_filter_number(value)==expected
    assert len(expected)<40 and 'e' not in expected.lower()


@pytest.mark.parametrize('value',['1e1000000000','1e-1000000000','1e-13','1.0000000000001','NaN','Infinity','9'*2001,0.13,True])
def test_invalid_or_extreme_thresholds_fail_before_large_fixed_point_allocation(value):
    with pytest.raises(ValueError):fixed_filter_number(value)


def test_scientific_threshold_comparison_does_not_lose_fractional_amount():
    predicate=filter_sql({'field':'amount','data_type':'number','operator':'gt','value':'1e15'})
    with duckdb.connect() as con:
        con.execute('CREATE TABLE source(amount VARCHAR)')
        con.executemany('INSERT INTO source VALUES (?)',[['1000000000000000.01'],['1000000000000001.00']])
        assert con.execute('SELECT count(*) FROM source WHERE '+predicate).fetchone()[0]==2


def test_legacy_frozen_divide_rejected_before_reading_any_source(tmp_path,monkeypatch):
    code=tmp_path/'code';run=tmp_path/'run';code.mkdir();run.mkdir()
    (code/'plan.json').write_text(json.dumps({'spec':{'computations':[{'operation':'divide'}]}}))
    monkeypatch.setattr(runner,'read_source',lambda *a,**k:pytest.fail('Rejected plan must not read sources'))
    with pytest.raises(TaskError) as error:runner.ingest(run,code)
    assert error.value.code=='SQL_INEXACT_NUMERIC' and 'compute' in str(error.value)
    with pytest.raises(ValueError,match='compute'):
        computation_sql({'operation':'divide','left_field':'amount','right_field':'basis'})


def test_actual_http_probe_dbt_exact_filter_query_and_frozen_rerun_boundaries():
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    text='id,office,amount,basis\nA,office-a,1000000000000000.01,1\nB,office-b,1000000000000001.00,1\n'
    uploaded=client.post('/api/upload',headers=headers,files={'file':('cap1.csv',text.encode(),'text/csv')})
    assert uploaded.status_code==200,uploaded.text
    assert process_once();source=get('source',uploaded.json()['id'])
    spec=dict(source['proposed_spec'],primary_key='c_1',group_by='c_2',amount_field='m_net',
              filters=[{'field':'c_3','data_type':'number','operator':'gt','value':'1e15'}],
              computations=[{'name':'m_net','label':'Exact subtraction','operation':'subtract','left_field':'c_3','right_field':'c_4'}])
    response=client.post('/api/pipelines',headers=headers,json={'source_id':source['id'],'name':'CAP1 exact numeric pipeline',
        'spec':spec,'release_mode':'manual','runtime':{'timeout_seconds':180,'max_attempts':1,'freshness_minutes':30}})
    assert response.status_code==200,response.text
    pid=response.json()['id'];assert process_once();pipeline=get('pipeline',pid)
    baseline=list_jobs(target=pid)[0];assert baseline['status']=='validated',baseline
    folder=DATA/baseline['artifact_dir']
    with duckdb.connect(str(folder/'warehouse.duckdb'),read_only=True) as con:
        assert con.execute('SELECT count(*) FROM business.business_objects').fetchone()[0]==2
        total=con.execute('SELECT sum(m_net) FROM business.business_objects').fetchone()[0]
        assert total==Decimal('1999999999999999.01')
        assert con.execute('SELECT typeof(m_net) FROM business.business_objects LIMIT 1').fetchone()[0]=='DECIMAL(38,12)'
    assert pipeline['published_run'] is None
    assert client.post('/api/pipelines/'+pid+'/publish',headers=headers,json={'run_id':baseline['id']}).status_code==200
    exact=client.post('/api/pipelines/'+pid+'/query',headers=headers,json={'sql':'SELECT sum(m_net) total FROM business.business_objects'})
    assert exact.status_code==200 and Decimal(exact.json()['rows'][0][0])==total
    nested=client.post('/api/pipelines/'+pid+'/query',headers=headers,json={
        'sql':'SELECT t FROM business.business_objects t ORDER BY c_1'})
    assert nested.status_code==200,nested.text
    nested_amount=nested.json()['rows'][0][0]['m_net']
    assert isinstance(nested_amount,str) and Decimal(nested_amount)==Decimal('999999999999999.01')
    integers=client.post('/api/pipelines/'+pid+'/query',headers=headers,json={
        'sql':'SELECT CAST(9007199254740993 AS BIGINT) large, COUNT(*) n, TRUE flag FROM business.business_objects'})
    assert integers.status_code==200 and integers.json()['rows']==[['9007199254740993',2,True]]
    unsafe=client.post('/api/pipelines/'+pid+'/query',headers=headers,json={
        'sql':'SELECT CAST(m_net / 1 AS DECIMAL(38,12)) amount FROM business.business_objects'})
    assert unsafe.status_code==400 and 'compute' in unsafe.text
    # New unsafe candidates fail before repository creation or queue admission.
    before={p['id'] for p in all_items('pipeline')}
    divided=json.loads(json.dumps(spec));divided['computations'][0]['operation']='divide'
    rejected=client.post('/api/pipelines',headers=headers,json={'source_id':source['id'],'name':'CAP1 rejected divide','spec':divided})
    assert rejected.status_code==400 and 'compute' in rejected.text
    assert {p['id'] for p in all_items('pipeline')}==before
    # Real archived Git plans bypass new generation validation; runner must
    # independently reject them before collecting sources or invoking dbt.
    repo=Path(pipeline['repository']);plan_file=repo/'plan.json';plan=json.loads(plan_file.read_text())
    assert plan['numeric_policy']==NUMERIC_POLICY
    prepared=repo/'dbt/models/prepared_objects.sql';business=repo/'dbt/models/business_objects.sql'
    original_prepared=prepared.read_text();original_business=business.read_text()
    for historical in ('divide','scientific_filter'):
        frozen=json.loads(json.dumps(plan));frozen.pop('numeric_policy',None)
        if historical=='divide':
            frozen['spec']['computations'][0]['operation']='divide'
            prepared.write_text(original_prepared.replace(' - ',' / '))
            business.write_text(original_business)
        else:
            prepared.write_text(original_prepared)
            business.write_text(original_business.replace(fixed_filter_number('1e15'),'1E+15'))
        plan_file.write_text(json.dumps(frozen));codegen.git(repo,'add','.')
        codegen.git(repo,'commit','-m','Synthetic archived '+historical)
        patch('pipeline',pid,commit=codegen.git(repo,'rev-parse','HEAD'),spec=frozen['spec'])
        queued=client.post('/api/pipelines/'+pid+'/run',headers=headers);assert queued.status_code==200,queued.text
        assert process_once();failed=list_jobs(target=pid)[0]
        assert failed['status']=='failed' and failed['error_code']=='SQL_INEXACT_NUMERIC',failed
        assert not failed.get('validated') and get('pipeline',pid)['published_run']==baseline['id']
