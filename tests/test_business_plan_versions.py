import json
import subprocess
from pathlib import Path

import pytest

from agent_service import business_plan, codegen
from agent_service.store import put, uid


def test_generated_plans_follow_frozen_rule_and_git_version(monkeypatch, tmp_path):
    monkeypatch.setattr(codegen, 'DATA', tmp_path)
    pid='p_'+uid(); put('project',{'name':'收入','created_at':'2026-01-01'},pid)
    sid='s_'+uid()
    source=put('source',{'project_id':pid,'name':'收入明细.xlsx','kind':'file','status':'ready','revision':3,
        'profile':{'fields':[{'name':'c_1','label':'订单号','numeric':False},
                            {'name':'c_2','label':'办事处','numeric':False},
                            {'name':'c_3','label':'收入','numeric':True}]}},sid)
    spec={'primary_key':'c_1','group_by':'c_2','amount_field':'c_3',
          'metric_definition':'收入按办事处汇总','currency':'CNY',
          'filters':[{'field':'c_2','operator':'eq','value':'合肥'}]}
    pipeline=codegen.generate(source,'收入分析',spec,project_id=pid,origin_turn='op_example')
    assert pipeline['origin_turn']=='op_example' and pipeline['origin_commit']==pipeline['commit']
    old=pipeline['commit']; first=business_plan.for_commit(pipeline,old)
    frozen=json.loads(codegen.git(pipeline['repository'],'show',old+':plan.json'))
    assert first['rules']==frozen['spec']
    assert first['sources'][0]=={'id':sid,'name':'收入明细.xlsx','design_revision':3,'role':'主来源'}
    assert '合肥' in ' '.join(first['steps'][2]['details'])
    assert 'secret' not in json.dumps(first,ensure_ascii=False)
    updated=codegen.generate(source,'收入分析',dict(spec,filters=[{'field':'c_2','operator':'eq','value':'上海'}]),
                            existing=pipeline,project_id=pid)
    second=business_plan.for_commit(updated,updated['commit'])
    assert updated['commit']!=old
    assert updated.get('origin_turn') is None and updated.get('origin_commit') is None
    assert '上海' in ' '.join(second['steps'][2]['details'])
    assert business_plan.for_commit(updated,old)==first, '历史版本不能读取当前可变配置'
    assert first['rules']!=second['rules']
    with pytest.raises(ValueError,match='不一致'):
        business_plan.for_commit(dict(updated,project_id='p_other'),updated['commit'])
    with pytest.raises(ValueError,match='标识'):
        business_plan.for_commit(updated,'HEAD')


def test_frozen_plan_protects_description_and_legacy_versions(monkeypatch, tmp_path):
    monkeypatch.setattr(codegen, 'DATA', tmp_path)
    pid='p_'+uid(); put('project',{'name':'报表','created_at':'2026-01-01'},pid)
    source=put('source',{'project_id':pid,'name':'报表.csv','kind':'file','status':'ready','revision':1,
        'profile':{'fields':[{'name':'c_1','label':'金额','numeric':True}]}},'s_'+uid())
    pipeline=codegen.generate(source,'报表',{},project_id=pid)
    repo=Path(pipeline['repository']); plan_file=repo/'business-plan.json'
    original=business_plan.for_commit(pipeline,pipeline['commit'])
    assert any('保留所有明细' in item for item in original['warnings'])
    changed=json.loads(plan_file.read_text());changed['steps'][2]['details']=['假装没有筛选']
    plan_file.write_text(json.dumps(changed,ensure_ascii=False))
    codegen.git(repo,'add','business-plan.json');codegen.git(repo,'commit','-m','tamper description')
    tampered=codegen.git(repo,'rev-parse','HEAD')
    with pytest.raises(ValueError,match='不一致'):business_plan.for_commit(pipeline,tampered)
    plan_file.unlink();codegen.git(repo,'add','-A');codegen.git(repo,'commit','-m','missing plan')
    with pytest.raises(ValueError,match='缺少配套'):business_plan.for_commit(pipeline,codegen.git(repo,'rev-parse','HEAD'))
    legacy_file=repo/'plan.json';old_plan=json.loads(legacy_file.read_text())
    old_plan.pop('business_plan_version');old_plan.pop('identity_policy')
    legacy_file.write_text(json.dumps(old_plan,ensure_ascii=False))
    codegen.git(repo,'add','-A');codegen.git(repo,'commit','-m','simulate legacy version')
    legacy=business_plan.for_commit(pipeline,codegen.git(repo,'rev-parse','HEAD'))
    frozen=json.loads(codegen.git(repo,'show','HEAD:plan.json'))
    assert legacy['origin']=='derived_from_frozen_plan' and legacy['rules']==frozen['spec']
    assert any('仅保留一条' in item for item in legacy['warnings'])


def test_only_same_project_redacted_turn_text_is_frozen(monkeypatch,tmp_path):
    monkeypatch.setattr(codegen,'DATA',tmp_path)
    pid='p_'+uid();put('project',{'name':'需求','created_at':'2026-01-01'},pid)
    source=put('source',{'project_id':pid,'name':'收入.csv','kind':'file','status':'ready','revision':1,
        'profile':{'fields':[{'name':'c_1','label':'金额','numeric':True}]}},'s_'+uid())
    tid='t_'+uid();put('turn',{'state':'done','project_id':pid,'text':'按照办事处汇总收入','secret_text':'must-not-leak'},tid)
    pipeline=codegen.generate(source,'收入',{},project_id=pid,request_turn_id=tid)
    plan=business_plan.for_commit(pipeline,pipeline['commit'])
    assert plan['user_request']=='按照办事处汇总收入' and plan['request_turn_id']==tid
    assert 'must-not-leak' not in json.dumps(plan,ensure_ascii=False)
    foreign='t_'+uid();put('turn',{'state':'done','project_id':'p_other','text':'另一项目隐私'},foreign)
    other=codegen.generate(source,'收入',{},existing=pipeline,project_id=pid,request_turn_id=foreign)
    assert business_plan.for_commit(other,other['commit'])['user_request'] is None
    secret='t_'+uid();put('turn',{'state':'done','project_id':pid,'text':'api_key = private-value'},secret)
    next_version=codegen.generate(source,'收入',{},existing=other,project_id=pid,request_turn_id=secret)
    assert business_plan.for_commit(next_version,next_version['commit'])['user_request'] is None


def test_continue_request_resolves_only_authorized_origin():
    pid='p_'+uid();cid='c_'+uid();actor='owner'
    root=put('turn',{'state':'done','project_id':pid,'session_id':cid,'actor_id':actor,'text':'按办事处计算收入'},'t_'+uid())
    continuation=put('turn',{'state':'done','project_id':pid,'session_id':cid,'actor_id':actor,'text':'继续',
        'resumed_from':root['id'],'agent_origin':root['id'],'work_request':'不可信覆盖'},'t_'+uid())
    assert codegen._safe_user_request(pid,continuation['id'])==('按办事处计算收入',root['id'],'继续')
    foreign=put('turn',{'state':'done','project_id':pid,'session_id':'c_other','actor_id':actor,'text':'他人原始需求'},'t_'+uid())
    bad=put('turn',{'state':'done','project_id':pid,'session_id':cid,'actor_id':actor,'text':'继续',
        'resumed_from':foreign['id'],'agent_origin':foreign['id']},'t_'+uid())
    assert codegen._safe_user_request(pid,bad['id'])==(None,None,'继续')
    own_memory=put('turn',{'state':'done','project_id':pid,'session_id':cid,'actor_id':actor,'text':'记住我的个人偏好'},'t_'+uid())
    assert codegen._safe_user_request(pid,own_memory['id'])==(None,None,None)


def test_approved_source_snapshots_and_target_commit_are_not_replaced(monkeypatch,tmp_path):
    import copy
    from agent_service.store import patch
    monkeypatch.setattr(codegen,'DATA',tmp_path)
    pid='p_'+uid();put('project',{'name':'固定方案','created_at':'2026-01-01'},pid)
    main=put('source',{'project_id':pid,'name':'主.csv','kind':'file','status':'ready','revision':1,
        'profile':{'fields':[{'name':'c_1','label':'关联键','numeric':False}]}},'s_'+uid())
    joined=put('source',{'project_id':pid,'name':'关联.csv','kind':'file','status':'ready','revision':1,
        'profile':{'fields':[{'name':'c_1','label':'键','numeric':False}]}},'s_'+uid())
    snapshots={main['id']:copy.deepcopy(main),joined['id']:copy.deepcopy(joined)}
    patch('source',joined['id'],revision=2,name='已替换.csv')
    spec={'joins':[{'source_id':joined['id'],'left_field':'c_1','right_field':'c_1','label':'关联'}]}
    pipeline=codegen.generate(main,'冻结来源',spec,project_id=pid,frozen_sources=snapshots,expected_commit=None)
    frozen=json.loads(codegen.git(pipeline['repository'],'show',pipeline['commit']+':plan.json'))
    assert frozen['joins'][0]['source_revision']==1
    with pytest.raises(ValueError,match='已被创建'):
        codegen.generate(main,'覆盖',spec,existing=pipeline,project_id=pid,frozen_sources=snapshots,expected_commit=None)
    with pytest.raises(ValueError,match='应用版本已发生变化'):
        codegen.generate(main,'覆盖',spec,existing=pipeline,project_id=pid,frozen_sources=snapshots,expected_commit='0'*40)
    with pytest.raises(ValueError,match='快照缺失'):
        codegen.generate(main,'缺失',spec,project_id=pid,frozen_sources={main['id']:main},expected_commit=None)


def test_business_plan_displays_executable_source_cleanup_filter_and_output_options():
    plan={'source_id':'s_1','source_revision':1,'identity_policy':'preserve_all_rows_occurrence_v1',
        'fields':[{'name':'c_1','label':'部门'},{'name':'c_2','label':'金额'}],
        'joins':[],'application':{'title':'汇总','columns':['c_1','c_2'],'layout':'summary',
            'chart':'bar','default_sort':'c_2','descending':True,'page_size':25},
        'spec':{'primary_key':None,'allow_empty_source':False,'trim_fields':['c_1'],
            'filters':[{'field':'c_2','operator':'gt','data_type':'number','value':0}],
            'computations':[],'group_by':'c_1','amount_field':'c_2','metric_definition':'收入','currency':'元',
            'metric_status':'draft','min_rows':1}}
    out=business_plan.build(plan,'a_1','p_1','汇总');text='\n'.join(d for s in out['steps'] for d in s['details'])
    for expected in ('不允许空来源','首尾空白','数值','大于','按 部门 汇总 金额','待确认','默认排序','降序','每页显示'):
        assert expected in text
