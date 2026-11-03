"""GAP uses complete adopted data, exact decimals, declared units and periods."""
import pytest
from fastapi.testclient import TestClient
from agent_service import project_data as data
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.gap_analysis import analyze, evaluate
from test_project_data import project,source,registered,run,column


def fixture():
    pid=project();s=source(pid,b'branch,base,actual\nEast,9007199254740993.123456789012,9007199254740993.123456789013\nWest,5,2\nZero,0,1\n')
    schema={'columns':[column('branch'),column('base','decimal','CNY','additive')|{'period':'2026-09'},column('actual','decimal','CNY','additive')|{'period':'2026-09'}],
            'grain':['branch'],'meaning':'one record per branch'}
    doc={'version':1,'plan':{'name':'gap','inputs':{'detail':schema},'stages':[{'id':'keep','kind':'select','description':'all rows','input':'detail','columns':['branch','base','actual']}], 'output':'keep'},
         'bindings':{'detail':{'source_id':s['id'],'column_map':{'c_1':'branch','c_2':'base','c_3':'actual'}}}}
    wid=registered(pid,doc);job=run(pid,wid);sha=data.result_folder(pid,job['id'])[1]['handoff_sha256']
    body={'expected_handoff_sha256':sha,'baseline_column':'base','current_column':'actual','baseline_period':'2026-09',
          'current_period':'2026-09','group_by':['branch'],'ratio_decimals':2}
    return pid,job,body


def test_real_full_data_exact_difference_zero_baseline_and_chart():
    pid,job,body=fixture();data.publish_result(pid,job['id'],job['commit'])
    value=analyze(pid,job['id'],body);east,west,zero=value['groups']
    assert value['total_rows']==3 and value['unit']=='CNY'
    assert east['baseline']=='9007199254740993.123456789012' and east['delta']=='0.000000000001'
    assert east['chart_basis_points']==1 and east['chart_direction']=='increase'
    assert west['delta']=='-3' and west['relative_percent']=='-60.00' and west['chart_basis_points']==10000
    assert zero['relative_percent'] is None and '基准为0' in zero['ratio_unavailable_reason']
    assert '不证明业务原因' in value['notice'] and '近似比例' in value['chart_notice']


def test_http_rejects_unauthorized_candidate_and_stale():
    pid,job,body=fixture();client=TestClient(app);path=f'/api/projects/{pid}/data-results/{job["id"]}/gap-analysis';headers={'Authorization':'Bearer '+TOKEN}
    assert client.post(path,json=body).status_code==401
    assert client.post(path,json=body,headers=headers).status_code==400
    data.publish_result(pid,job['id'],job['commit'])
    good=client.post(path,json=body,headers=headers);assert good.status_code==200,good.text
    stale={**body,'expected_handoff_sha256':'0'*64};assert client.post(path,json=stale,headers=headers).status_code==400


def test_missing_values_incompatible_period_unit_or_non_additive_rejected():
    schema={'columns':[column('id'),column('base','decimal','CNY','additive')|{'period':'2026-09'},column('actual','decimal','CNY','additive')|{'period':'2026-09'}], 'grain':['id'],'meaning':'per id'}
    table={'schema':schema,'rows':[{'id':'A','base':'1','actual':None}], 'total':1,'complete_result_available':True}
    body={'expected_handoff_sha256':'a'*64,'baseline_column':'base','current_column':'actual','baseline_period':'2026-09','current_period':'2026-09','ratio_decimals':2}
    with pytest.raises(ValueError,match='空值'):evaluate(table,body)
    table['rows'][0]['actual']='2';schema['columns'][2]['unit']='USD'
    with pytest.raises(ValueError,match='单位不一致'):evaluate(table,body)
    schema['columns'][2]['unit']='CNY';schema['columns'][2]['period']='2026-08'
    with pytest.raises(ValueError,match='期间'):evaluate(table,body)
    schema['columns'][2]['period']='2026-09';schema['columns'][2]['aggregation']='unknown'
    with pytest.raises(ValueError,match='可加性'):evaluate(table,body)
    schema['columns'][2]['aggregation']='additive';table['rows'][0]['actual']=1.5
    with pytest.raises(ValueError):evaluate(table,body)


def test_zero_rows_bad_precision_and_unverifiable_grain_rejected():
    schema={'columns':[column('id'),column('base','decimal','CNY','additive')|{'period':'2026-09'},column('actual','decimal','CNY','additive')|{'period':'2026-09'}],'grain':['id'],'meaning':'each id'}
    table={'schema':schema,'rows':[{'id':'A','base':'1','actual':'2'},{'id':'A','base':'2','actual':'3'}], 'total':2,'complete_result_available':True}
    body={'expected_handoff_sha256':'a'*64,'baseline_column':'base','current_column':'actual','baseline_period':'2026-09','current_period':'2026-09','ratio_decimals':2}
    with pytest.raises(ValueError,match='粒度重复'):evaluate(table,body)
    with pytest.raises(ValueError):evaluate(table,{**body,'ratio_decimals':9})
    with pytest.raises(ValueError,match='有效日历'):evaluate(table,{**body,'baseline_period':'0000-01'})
    table['rows']=[];table['total']=0
    with pytest.raises(ValueError,match='没有数据'):evaluate(table,body)


def test_ratio_uses_declared_half_even_and_rejects_unknown_units():
    schema={'columns':[column('id'),column('base','decimal','CNY','additive')|{'period':'2026-09'},column('actual','decimal','CNY','additive')|{'period':'2026-09'}],'grain':['id'],'meaning':'each id'}
    table={'schema':schema,'rows':[{'id':'A','base':'100','actual':'101.245'},{'id':'B','base':'100','actual':'101.255'}],'total':2,'complete_result_available':True}
    body={'expected_handoff_sha256':'a'*64,'baseline_column':'base','current_column':'actual','baseline_period':'2026-09','current_period':'2026-09','ratio_decimals':2,'group_by':['id']}
    result=evaluate(table,body)['groups'];assert [x['relative_percent'] for x in result]==['1.24','1.26']
    schema['columns'][2]['value_format']='percent_fraction'
    with pytest.raises(ValueError,match='原始数值'):evaluate(table,body)


def test_delete_waits_until_gap_reads_snapshot(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from agent_service import gap_analysis as module,workspace_lifecycle as lifecycle
    pid,job,body=fixture();data.publish_result(pid,job['id'],job['commit'])
    entered=threading.Event();release=threading.Event();deleted=threading.Event();original=module.read_all_handoff
    def slow(*args,**kwargs):
        value=original(*args,**kwargs);entered.set();assert release.wait(5);return value
    monkeypatch.setattr(module,'read_all_handoff',slow)
    def remove():lifecycle.delete_project(pid);deleted.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        analysis=pool.submit(analyze,pid,job['id'],body);assert entered.wait(5)
        removal=pool.submit(remove);assert not deleted.wait(.1);release.set()
        assert analysis.result(timeout=10)['total_rows']==3
        removal.result(timeout=10)
    with pytest.raises(ValueError,match='回收站'):analyze(pid,job['id'],body)


def test_rejects_oversized_or_excessive_groups_without_partial_results(monkeypatch):
    from agent_service import gap_analysis as module
    pid,job,body=fixture();data.publish_result(pid,job['id'],job['commit'])
    monkeypatch.setattr(module,'MAX_ANALYSIS_BYTES',1)
    with pytest.raises(ValueError,match='没有截断'):analyze(pid,job['id'],body)
    monkeypatch.setattr(module,'MAX_ANALYSIS_BYTES',20*1024*1024);monkeypatch.setattr(module,'MAX_GROUPS',1)
    with pytest.raises(ValueError,match='没有截断'):analyze(pid,job['id'],body)


def test_negative_baseline_is_mathematical_only_with_explicit_warning():
    schema={'columns':[column('id'),column('base','decimal','CNY','additive')|{'period':'2026-09'},column('actual','decimal','CNY','additive')|{'period':'2026-09'}],'grain':['id'],'meaning':'per entity'}
    table={'schema':schema,'rows':[{'id':'A','base':'-10','actual':'-5'}],'total':1,'complete_result_available':True}
    body={'expected_handoff_sha256':'a'*64,'baseline_column':'base','current_column':'actual','baseline_period':'2026-09','current_period':'2026-09','ratio_decimals':2}
    value=evaluate(table,body);result=value['groups'][0]
    assert result['delta']=='5' and result['relative_percent']=='-50.00'
    assert '不代表改善或恶化' in result['ratio_interpretation_warning']
    assert '单位相同不证明业务口径相同' in value['notice']
