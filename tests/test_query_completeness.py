"""A query preview must never be mistaken for a complete financial result."""
import json
from decimal import Decimal

import duckdb
import pytest

from agent_service import analysis, provider, tasks, work_tools


@pytest.fixture
def warehouse(tmp_path):
    with duckdb.connect(str(tmp_path/'warehouse.duckdb')) as con:
        con.execute('CREATE SCHEMA business')
        con.execute('CREATE TABLE business.business_objects(id INTEGER, amount DECIMAL(38,12))')
        con.execute('INSERT INTO business.business_objects SELECT i, CAST(i AS DECIMAL(38,12)) FROM range(350) t(i)')
    return tmp_path


@pytest.mark.parametrize(('limit','expected','truncated'),[(0,0,False),(199,199,False),(200,200,False),(201,200,True),(350,200,True)])
def test_complete_and_truncated_query_results_are_distinguished(warehouse,limit,expected,truncated):
    result=analysis.read_query(warehouse,f'SELECT id, amount FROM business.business_objects WHERE id < {limit} ORDER BY id')
    assert result['returned_rows']==expected==len(result['rows'])
    assert result['limit']==200 and result['truncated'] is truncated
    assert result['result_complete'] is (not truncated)
    assert result['completeness_scope']=='executed_sql_result'
    if expected:
        assert result['rows'][0][0]==0 and Decimal(result['rows'][-1][1])==Decimal(expected-1)
    assert 'total' not in result  # The exact total was not counted.


def test_sql_limit_is_the_explicit_query_scope_and_aggregate_is_complete(warehouse):
    limited=analysis.read_query(warehouse,'SELECT id FROM business.business_objects ORDER BY id LIMIT 200')
    assert limited['result_complete'] and len(limited['rows'])==200
    total=analysis.read_query(warehouse,'SELECT SUM(amount) AS total FROM business.business_objects')
    assert total['result_complete'] and Decimal(total['rows'][0][0])==sum(range(350))


def test_model_answer_includes_real_completeness_warning(warehouse,monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://fixture.invalid','model':'fixture','api_key':'unused'})
    monkeypatch.setattr(provider,'complete',lambda *a,**k:json.dumps({'sql':'SELECT id FROM business.business_objects ORDER BY id','explanation':'查询结果','limitations':[]}))
    manifest={'run_id':'r','commit':'x','records':350,'fields':[{'name':'id','label':'编号'}],
              'spec':{'amount_field':'amount'},'summary':[]}
    result=analysis.ask(warehouse,manifest,'列出所有编号')
    assert result['mode']=='llm_sql' and result['truncated'] and not result['result_complete']
    assert '仅返回前200条' in result['limitations'][0]


@pytest.mark.parametrize('tool',['query_sql','ask_data_application','data_evidence'])
@pytest.mark.parametrize(('complete','verified'),[(True,True),(False,False),(None,False)])
def test_partial_or_unknown_query_never_satisfies_full_query_receipt(monkeypatch,tool,complete,verified):
    monkeypatch.setattr(work_tools,'actions',lambda pid:[])
    obs={'rows':[[1]],'mode':'llm_sql','sql':'SELECT id FROM business.business_objects','completeness_scope':'executed_sql_result'}
    if complete is not None:obs.update(result_complete=complete,truncated=not complete)
    contract={'kind':'query','requirements':['query'],'acceptance':'complete query'}
    trace=[] if tool=='data_evidence' else [{'tool':tool,'status':'done','arguments':{'sql':obs['sql']},'observation':obs}]
    turn={'id':'t_test','project_id':'p_test','text':'查询数据','task_contract':contract,'agent_trace':trace,
          **({'data_evidence':obs} if tool=='data_evidence' else {})}
    monkeypatch.setattr(tasks,'patch',lambda *a,**k:None)
    result=tasks.verify(turn)
    assert result['verified'] is verified
    assert ('query' in result['missing']) is not verified


def test_contradictory_completeness_flags_do_not_satisfy_query(monkeypatch):
    monkeypatch.setattr(work_tools,'actions',lambda pid:[])
    monkeypatch.setattr(tasks,'patch',lambda *a,**k:None)
    turn={'id':'t_test','project_id':'p_test','text':'查询数据',
          'task_contract':{'kind':'query','requirements':['query'],'acceptance':'完整查询'},
          'agent_trace':[{'tool':'query_sql','status':'done','arguments':{'sql':'SELECT id'},
                          'observation':{'rows':[[1]],'result_complete':True,'truncated':True,
                                         'completeness_scope':'executed_sql_result'}}]}
    assert not tasks.verify(turn)['verified']
