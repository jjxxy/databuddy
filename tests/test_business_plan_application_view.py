"""Version selection never substitutes a different run or stale candidate."""
import copy
import pytest

from agent_service import applications, business_plan, run_provenance


def fixture(monkeypatch):
    p={'id':'app1','project_id':'project1','commit':'c'*40,'candidate_run':'new',
       'active_commit':'a'*40,'published_run':'old'}
    runs={'new':{'id':'new','target':'app1','commit':'c'*40},
          'old':{'id':'old','target':'app1','commit':'a'*40},
          'history':{'id':'history','target':'app1','commit':'b'*40}}
    plans={'a'*40:{'origin':'generated','rules':{'group_by':'office'},'identity_policy':'legacy','sources':[],'application':{}},
           'b'*40:{'origin':'derived_from_frozen_plan','rules':{'group_by':'country'},'identity_policy':'legacy','sources':[],'application':{}},
           'c'*40:{'origin':'generated','rules':{'group_by':'office'},'identity_policy':'preserve','sources':[],'application':{}}}
    monkeypatch.setattr(applications,'owned',lambda project,app: p if (project,app)==('project1','app1') else (_ for _ in ()).throw(ValueError('应用不属于当前项目')))
    monkeypatch.setattr(applications,'get_job',lambda rid:copy.deepcopy(runs[rid]))
    monkeypatch.setattr(applications,'manifest_for',lambda run:(None,{'run_id':run['id']}))
    monkeypatch.setattr(applications,'snapshot',lambda project,app,preview,rid:(p,copy.deepcopy(runs[rid]),None,{},{}))
    monkeypatch.setattr(business_plan,'for_commit',lambda pipeline,commit:copy.deepcopy(plans[commit]))
    monkeypatch.setattr(run_provenance,'describe',lambda pipeline,run,manifest:{'run_id':run['id'],'commit':run['commit']})
    return p,runs,plans


def test_selected_run_is_bound_to_its_frozen_plan_and_comparison(monkeypatch):
    fixture(monkeypatch)
    result=applications.business_plan_view('project1','app1',run_id='new')
    assert result['selected']['role']=='candidate'
    assert result['selected']['commit']=='c'*40
    assert result['selected']['run_evidence']['run_id']=='new'
    assert result['published']['commit']=='a'*40
    assert any(row['field']=='identity_policy' for row in result['changes'])


def test_same_candidate_and_published_pointer_is_not_a_second_candidate(monkeypatch):
    p,runs,_=fixture(monkeypatch)
    p.update(commit='a'*40,candidate_run='old')
    result=applications.business_plan_view('project1','app1',run_id='old')
    assert result['selected']['role']=='published' and result['candidate'] is None


def test_broken_unrelated_candidate_does_not_hide_historical_plan(monkeypatch):
    _,_,plans=fixture(monkeypatch)
    del plans['c'*40]
    result=applications.business_plan_view('project1','app1',run_id='history')
    assert result['selected']['role']=='history' and result['selected']['commit']=='b'*40
    assert result['candidate'] is None and result['warnings'] and result['changes']==[]
    with pytest.raises(KeyError):applications.business_plan_view('project1','app1')


def test_run_commit_mismatch_and_cross_project_rejected(monkeypatch):
    p,runs,_=fixture(monkeypatch)
    runs['new']['commit']='d'*40
    with pytest.raises(ValueError,match='版本不一致'):
        applications.business_plan_view('project1','app1')
    with pytest.raises(ValueError,match='不属于'):
        applications.business_plan_view('other','app1')
