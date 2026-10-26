import math
import pytest
import duckdb
from agent_service.data_contracts import evaluate,validate

@pytest.fixture
def db():
    con=duckdb.connect();con.execute('create schema business');con.execute('create table business.business_objects(id int, region varchar, amount varchar)');con.execute("insert into business.business_objects values (1,'CN','10'),(1,'CN','bad'),(2,NULL,'-2')")
    yield con
    con.close()

def test_quality_gate_reports_duplicates_nulls_ranges_without_rows(db):
    r=evaluate(db,{'version':1,'min_rows':3,'max_rows':5,'required':['id'],'unique':[['id','region']],'not_null':['region'],'ranges':{'amount':{'min':0}}})
    assert not r['passed']
    assert {c['name']:c['failures'] for c in r['checks'] if c['status']=='fail'}=={'unique:id,region':1,'not_null:region':1,'range:amount':2}
    assert 'bad' not in str(r)

def test_missing_fields_fail_closed(db):
    r=evaluate(db,{'version':1,'required':['missing'],'ranges':{'missing':{'min':0}}})
    assert not r['passed']

def test_null_range_requires_separate_not_null(db):
    db.execute("update business.business_objects set amount=NULL")
    assert evaluate(db,{'version':1,'ranges':{'amount':{'min':0}}})['passed']

def test_identifiers_cannot_execute_sql(db):
    r=evaluate(db,{'version':1,'required':['x"; DROP TABLE business.business_objects;--']})
    assert not r['passed'] and db.execute('select count(*) from business.business_objects').fetchone()[0]==3

@pytest.mark.parametrize('contract',[{'version':True},{'version':1,'max_rows':True},{'version':1,'min_rows':4,'max_rows':2},{'version':1,'unique':[[]]},{'version':1,'ranges':{'amount':{'min':math.nan}}},{'version':1,'sql':'delete from x'}])
def test_invalid_contracts(contract):
    with pytest.raises(ValueError):validate(contract)

def test_digest_canonical_and_success(db):
    a=evaluate(db,{'version':1,'min_rows':1});b=evaluate(db,{'min_rows':1,'version':1})
    assert a['passed'] and a['contract_sha256']==b['contract_sha256']

def test_nonfinite_numbers_rejected(db):
    db.execute("update business.business_objects set amount='Infinity'")
    assert not evaluate(db,{'version':1,'ranges':{'amount':{'min':0}}})['passed']

def test_exact_decimal_reconciliation(db):
    db.execute("delete from business.business_objects")
    db.execute("insert into business.business_objects values (1,'CN','9999999999999999999999999.123456789012'),(2,'CN','0.000000000001')")
    r=evaluate(db,{'version':1,'totals':{'amount':{'expected':'9999999999999999999999999.123456789013','tolerance':'0'}}})
    assert r['passed']
    assert not evaluate(db,{'version':1,'totals':{'amount':{'expected':'9999999999999999999999999.123456789012'}}})['passed']

def test_exact_total_rejects_null_and_bad_values(db):
    assert not evaluate(db,{'version':1,'totals':{'amount':{'expected':'8'}}})['passed']
    db.execute('update business.business_objects set amount=NULL')
    assert not evaluate(db,{'version':1,'totals':{'amount':{'expected':'0'}}})['passed']

@pytest.mark.parametrize('bounds',[{'expected':0.1},{'expected':'NaN'},{'expected':'1','tolerance':'-1'},{'expected':'1e4'},{'expected':'0.1234567890123'}])
def test_exact_total_schema(bounds):
    with pytest.raises(ValueError):validate({'version':1,'totals':{'amount':bounds}})

def test_exact_total_rejects_silent_rounding(db):
    db.execute('delete from business.business_objects')
    db.execute("insert into business.business_objects values (1,'CN','0.0000000000001')")
    assert not evaluate(db,{'version':1,'totals':{'amount':{'expected':'0'}}})['passed']
