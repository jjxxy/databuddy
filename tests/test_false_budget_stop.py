"""A model-authored round-budget excuse cannot terminate unlimited work."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from agent_service import agent,conversation,provider,task_budget,workspace,api
from agent_service.config import TOKEN
from agent_service.store import get,patch,put
from test_opencode_agent_bridge import turn,write
from test_agent_team import parent,create

CLAIM='本轮模型预算已用完，已有成果已保存；可以缩小目标后继续'


@pytest.mark.parametrize('mode',['chat','work'])
def test_native_model_cannot_stop_an_unlimited_conversation_with_old_budget_excuse(turn,mode):
    current=patch('turn',turn['id'],interaction_mode=mode,text='请解释收入与利润的区别')
    reply=agent.opencode_step(current['id'],{'kind':'final','answer':CLAIM,'status':'blocked'},'native:old-cap')
    saved=get('turn',turn['id'])
    assert saved['state']=='agent' and not saved.get('answer')
    assert reply['verification_feedback']['code']=='UNSUPPORTED_ROUND_BUDGET_STOP'
    assert not reply.get('terminal')
    assert '缩小目标' in reply['verification_feedback']['error']


def test_corrected_native_final_reuses_existing_write_and_finishes(turn):
    agent.opencode_step(turn['id'],write('original artifact'),'native:write')
    agent.opencode_step(turn['id'],{'kind':'final','answer':CLAIM,'status':'blocked'},'native:bad-final')
    agent.opencode_step(turn['id'],{'kind':'final','answer':'报告已保存。','status':'complete'},'native:good-final')
    current=get('turn',turn['id'])
    assert current['outcome']=='verified_complete'
    assert len(current['agent_trace'])==1
    assert workspace.read_file(turn['project_id'],'reports/native.md')=='original artifact'


def test_legacy_planner_receives_correction_and_executes_original_goal(turn,monkeypatch):
    monkeypatch.setattr('agent_service.opencode_runtime.is_enabled',lambda:False)
    steps=iter([{'kind':'final','answer':CLAIM,'status':'blocked'},write(),
                {'kind':'final','answer':'原任务已完成。','status':'complete'}])
    sent=[]
    def complete(messages,**kwargs):
        sent.append(json.loads(messages[1]['content']))
        return json.dumps(next(steps))
    monkeypatch.setattr(provider,'complete',complete)
    for _ in range(3):agent.advance(get('turn',turn['id']))
    assert get('turn',turn['id'])['outcome']=='verified_complete'
    assert sent[1]['verification_feedback']['code']=='UNSUPPORTED_ROUND_BUDGET_STOP'
    assert all(item['request']==turn['text'] for item in sent)


def test_repeated_invalid_stopping_has_honest_failure_instead_of_infinite_retry(turn):
    for i in range(2):agent.opencode_step(turn['id'],{'kind':'final','answer':CLAIM,'status':'blocked'},'bad:'+str(i))
    current=get('turn',turn['id'])
    assert current['state']=='done' and current['outcome']=='blocked' and current['resume_available']
    assert '模型连续返回与当前运行策略不符' in current['answer']
    assert '预算已用完' not in current['answer']


@pytest.mark.parametrize('answer',[
    '“本轮模型预算已用完”是历史错误提示。',
    '本轮模型预算已用完是什么意思？',
    '工作空间今日模型请求预算已用完',
    '模型服务返回 429 Too Many Requests',
    '本次预算已经用完，请核对财务明细',
])
def test_guard_does_not_suppress_error_explanations_finance_or_service_errors(answer):
    assert not task_budget.unsupported_round_stop({'interactive_unlimited':True},answer)


def test_actual_bounded_contract_is_not_changed_or_suppressed():
    value={'task_contract':{'budget_policy':'bounded','budgets':{'model_calls':3}}}
    original=copy.deepcopy(value)
    assert not task_budget.unsupported_round_stop(value,CLAIM)
    assert value==original and task_budget.effective_limits(value)['model_calls']==3


def test_historical_message_is_preserved_and_labelled_as_old_policy(turn):
    old=patch('turn',turn['id'],session_id='fixture-session',interaction_mode='work',
              state='done',outcome='paused',answer=CLAIM,resume_available=True)
    assert conversation.public_turn(old)['obsolete_budget_pause'] is True
    from agent_service.context_compaction import _visible
    projected=_visible(old,set(),set(),{})
    assert projected['agent']==CLAIM and '当前任务' in projected['runtime_policy_note']
    assert get('turn',old['id'])['answer']==CLAIM


def test_linked_old_local_specialist_inherits_owner_policy_without_rewriting_audit(parent):
    record=create(parent)
    legacy=copy.deepcopy(get('turn',record['child_turn_id']))
    legacy.pop('collaboration_budget_inherited')
    legacy['task_contract']['budgets']=dict(task_budget.LEGACY_SPECIALIST)
    legacy['task_contract'].pop('budget_policy',None)
    put('turn',legacy,legacy['id'])
    assert all(value is None for value in task_budget.effective_limits(legacy).values())
    assert get('turn',legacy['id'])['task_contract']['budgets']==task_budget.LEGACY_SPECIALIST
    from agent_service import collaboration_budget
    for _ in range(8):totals=collaboration_budget.admit(legacy,150000,32000)
    assert totals['used']['calls']==8 and totals['used']['input_characters']==1200000
    # A forged relationship or an explicit policy must keep finite grants.
    unrelated=dict(legacy,collaboration_task_id='missing-task')
    assert task_budget.effective_limits(unrelated)['model_calls']==5
    bounded=dict(legacy,task_contract={**legacy['task_contract'],'budget_policy':'bounded'})
    assert task_budget.effective_limits(bounded)['model_calls']==5
    explicit=dict(legacy,collaboration_budget_inherited=False)
    assert task_budget.effective_limits(explicit)['model_calls']==5
    foreign=dict(legacy,actor_id='another-owner')
    assert task_budget.effective_limits(foreign)['model_calls']==5


def test_runtime_revision_and_uncached_composer_are_delivered_by_actual_http(monkeypatch):
    monkeypatch.setattr(api,'_requests',{})
    client=TestClient(api.app)
    from agent_service.release_info import RUNTIME_REVISION
    for path in ('/','/index.html','/app.js'):
        response=client.get(path)
        assert response.status_code==200
        assert response.headers['cache-control']=='no-store'
        assert response.headers['X-Atlas-Runtime-Revision']==RUNTIME_REVISION
    assert client.get('/api/runtime-info').status_code==401
    info=client.get('/api/runtime-info',headers={'Authorization':'Bearer '+TOKEN}).json()
    assert info['revision']==RUNTIME_REVISION and info['interactive_task_totals']=='unlimited'
    assert info['multiple_attachments'] is True
