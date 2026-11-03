"""Explore only complete, pinned and reviewed results using exact arithmetic."""
import pytest
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service import project_data as data
from agent_service.interactive_analysis import explore, MAX_GROUPS
from test_project_data import project, source, registered, run, column


def adopted(raw=None):
    pid=project();s=source(pid,raw or b'id,office,month,amount\nA,East,2026-09,9007199254740993.123456789012\nB,East,2026-09,0.000000000001\nC,West,2026-10,3.4\n')
    schema={'columns':[column('id'),column('office'),column('month'),column('amount','decimal','CNY','additive')],
            'grain':['id'],'meaning':'one row per order'}
    doc={'version':1,'plan':{'name':'detail','inputs':{'detail':schema},'stages':[{'id':'keep','kind':'select','description':'keep rows','input':'detail','columns':['id','office','month','amount']}],'output':'keep'},
         'bindings':{'detail':{'source_id':s['id'],'column_map':{'c_1':'id','c_2':'office','c_3':'month','c_4':'amount'}}}}
    wid=registered(pid,doc);job=run(pid,wid)
    data.publish_result(pid,job['id'],job['commit'])
    return pid,wid,job,data.result_folder(pid,job['id'])[1]['handoff_sha256']


def req(sha, **kwargs):
    return {'expected_handoff_sha256':sha,**({'group_by':['office']} if 'group_by' not in kwargs else {}),**kwargs}


def test_full_exact_aggregation_cut_month_and_drilldown():
    pid,wid,job,sha=adopted()
    result=explore(pid,job['id'],req(sha,metric='sum',measure='amount',period_column='month',period='2026-09'))
    assert result['complete'] is True and result['total_rows']==3 and result['selected_rows']==2
    assert result['groups']==[{'values':['East'],'count':2,'amount':'9007199254740993.123456789013','unit':'CNY','period':'2026-09'}]
    drill=explore(pid,job['id'],req(sha,group_by=['month'],filters=[{'column':'office','value':'East'}]))
    assert drill['groups']==[{'values':['2026-09'],'count':2}]


def test_rejects_stale_versions_unapproved_runs_unknown_period_and_bad_metric():
    pid,wid,job,sha=adopted()
    with pytest.raises(ValueError,match='版本已变化'):explore(pid,job['id'],req('0'*64))
    with pytest.raises(ValueError,match='数值期间'):explore(pid,job['id'],req(sha,metric='sum',measure='amount'))
    with pytest.raises(ValueError,match='只能汇总'):explore(pid,job['id'],req(sha,metric='sum',measure='office'))
    with pytest.raises(ValueError):explore(project(),job['id'],req(sha))
    second=registered(pid,data.load_document(pid,'workflows/orders.json').model_dump(),path='workflows/second.json')
    candidate=run(pid,second)
    # A candidate is not a business-confirmed result.
    with pytest.raises(ValueError,match='先采用'):explore(pid,candidate['id'],req(data.result_folder(pid,candidate['id'])[1]['handoff_sha256']))


def test_api_requires_auth_and_does_not_accept_extra_sql():
    pid,wid,job,sha=adopted();client=TestClient(app)
    path=f'/api/projects/{pid}/data-results/{job["id"]}/explore'
    assert client.post(path,json=req(sha)).status_code==401
    response=client.post(path,json=req(sha),headers={'Authorization':'Bearer '+TOKEN})
    assert response.status_code==200,response.text
    assert response.json()['groups']==[{'values':['East'],'count':2},{'values':['West'],'count':1}]
    assert client.post(path,json=req(sha,sql='select secret'),headers={'Authorization':'Bearer '+TOKEN}).status_code==422


def test_missing_filter_values_and_period_are_not_silently_excluded():
    pid,wid,job,sha=adopted(b'id,office,month,amount\nA,East,2026-09,1\nB,,2026-09,2\n')
    with pytest.raises(ValueError,match='空值'):
        explore(pid,job['id'],req(sha,filters=[{'column':'office','value':'East'}]))
    selected=explore(pid,job['id'],req(sha,filters=[{'column':'office','value':''}]))
    assert selected['selected_rows']==1 and selected['groups']==[{'values':[''],'count':1}]
    pid,wid,job,sha=adopted(b'id,office,month,amount\nA,East,2026-09,1\nB,West,,2\n')
    with pytest.raises(ValueError,match='期间字段'):
        explore(pid,job['id'],req(sha,metric='sum',measure='amount',period_column='month',period='2026-09'))


def test_no_matching_rows_is_explicit_and_no_fabricated_zero_group():
    pid,wid,job,sha=adopted()
    value=explore(pid,job['id'],req(sha,filters=[{'column':'office','value':'North'}]))
    assert value['selected_rows']==0 and value['groups']==[] and value['total_rows']==3


def test_capacity_limits_stop_instead_of_truncating(monkeypatch):
    from agent_service import interactive_analysis as module
    pid,wid,job,sha=adopted()
    monkeypatch.setattr(module,'MAX_ANALYSIS_BYTES',1)
    with pytest.raises(ValueError,match='没有截断计算'):
        explore(pid,job['id'],req(sha))
    monkeypatch.setattr(module,'MAX_ANALYSIS_BYTES',20*1024*1024)
    monkeypatch.setattr(module,'MAX_GROUPS',1)
    with pytest.raises(ValueError,match='没有截断计算'):
        explore(pid,job['id'],req(sha))


def test_month_filter_accepts_valid_iso_dates_but_rejects_ambiguous_dates():
    pid,wid,job,sha=adopted(b'id,office,month,amount\nA,East,2026-09-10,1\nB,West,2026-10-01,2\n')
    selected=explore(pid,job['id'],req(sha,metric='sum',measure='amount',period_column='month',period='2026-09'))
    assert selected['selected_rows']==1 and selected['groups'][0]['amount']=='1'
    pid,wid,job,sha=adopted(b'id,office,month,amount\nA,East,2026-09-31,1\nB,West,2026-10-01,2\n')
    with pytest.raises(ValueError,match='期间字段'):
        explore(pid,job['id'],req(sha,metric='sum',measure='amount',period_column='month',period='2026-09'))


def test_cross_scope_duplicate_grain_is_not_hidden_by_month_or_department_filter(monkeypatch):
    # Current execution prevents this input. Simulate an old authenticated
    # result with an invalid declared grain to verify this consumer's guard.
    from agent_service import interactive_analysis as module
    pid,wid,job,sha=adopted();read=module.read_all_handoff
    def legacy(*args,**kwargs):
        table=read(*args,**kwargs);table['rows'][1]['id']=table['rows'][0]['id']
        table['rows'][1]['office']='West';table['rows'][1]['month']='2026-10'
        return table
    monkeypatch.setattr(module,'read_all_handoff',legacy)
    with pytest.raises(ValueError,match='粒度重复'):
        explore(pid,job['id'],req(sha,metric='sum',measure='amount',period_column='month',period='2026-09'))
    with pytest.raises(ValueError,match='粒度重复'):
        explore(pid,job['id'],req(sha,metric='sum',measure='amount',period_column='month',period='2026-09',filters=[{'column':'office','value':'East'}]))


def test_deletion_waits_until_snapshot_analysis_finishes(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from agent_service import interactive_analysis as module, workspace_lifecycle as lifecycle
    pid,wid,job,sha=adopted();entered=threading.Event();release=threading.Event();deleted=threading.Event()
    original=module.read_all_handoff
    def slow_read(*args,**kwargs):
        result=original(*args,**kwargs);entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(module,'read_all_handoff',slow_read)
    def remove():
        lifecycle.delete_project(pid);deleted.set()
    with ThreadPoolExecutor(max_workers=2) as executor:
        analysis=executor.submit(explore,pid,job['id'],req(sha))
        assert entered.wait(5)
        removal=executor.submit(remove)
        assert not deleted.wait(.1)
        release.set()
        assert analysis.result(timeout=10)['complete'] is True
        removal.result(timeout=10)
    with pytest.raises(ValueError,match='回收站'):
        explore(pid,job['id'],req(sha))
