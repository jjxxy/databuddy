"""Owner conversations have no fixed totals; scoped grants remain finite."""
import copy

import pytest

from agent_service import agent,opencode_runtime,tasks,task_budget,workspace
from agent_service.store import get,patch,now
from test_opencode_agent_bridge import turn,write


def test_new_owner_contract_explicitly_has_no_fixed_task_totals(turn):
    contract=get('turn',turn['id'])['task_contract']
    assert contract['budget_policy']=='unlimited'
    assert contract['budgets']==dict.fromkeys(task_budget.KEYS,None)
    limits=task_budget.effective_limits(get('turn',turn['id']))
    assert task_budget.exceeded_reason(limits,{'calls':200,'input_characters':10000000,
        'output_tokens_budgeted':1000000,'duration_ms':86400000},input_characters=250000,
        output_tokens=3200,elapsed_ms=100000) is None


@pytest.mark.parametrize('defaults',[task_budget.LEGACY_DEFAULTS,task_budget.LEGACY_DEEP])
def test_resumed_historical_owner_default_does_not_reintroduce_old_limits(defaults):
    contract={'kind':'build','budgets':copy.deepcopy(defaults),'goal':'原来的月结'}
    original=copy.deepcopy(contract)
    value={'id':'continued','resumed_from':'old','task_contract':contract}
    assert task_budget.effective_limits(value)==dict.fromkeys(task_budget.KEYS,None)
    assert task_budget.tool_step_limit(value) is None
    assert contract==original,'Original evidence contract must not be rewritten'


def test_explicit_custom_limits_remain_finite_and_dimension_specific():
    limits=task_budget.effective_limits({'task_contract':{'budget_policy':'bounded',
        'budgets':{'model_calls':4,'input_characters':1000,'output_tokens':500,'duration_ms':10000}}})
    assert task_budget.exceeded_reason(limits,{'calls':4})=='model_calls'
    assert task_budget.exceeded_reason(limits,{},input_characters=1001)=='input_characters'
    assert task_budget.exceeded_reason(limits,{},output_tokens=501)=='output_tokens'
    assert task_budget.exceeded_reason(limits,{'duration_ms':9900},elapsed_ms=100)=='duration_ms'
    assert task_budget.exceeded_reason(limits,{'calls':3},input_characters=1000,output_tokens=500,elapsed_ms=9999) is None
    # A pre-policy custom administrator contract is an explicit choice too.
    assert task_budget.effective_limits({'task_contract':{'budgets':{'model_calls':2}}})['model_calls']==2


@pytest.mark.parametrize('scope',[
    {'gateway_client_id':'external','gateway_limits':{'model_calls':2}},
    {'gateway_limits':{'model_calls':2}},
    {'collaboration_parent_turn_id':'coordinator'},
])
def test_unlimited_owner_policy_cannot_expand_a_scoped_grant(scope):
    limits=task_budget.effective_limits({**scope,'task_contract':{'budget_policy':'unlimited',
        'budgets':dict.fromkeys(task_budget.KEYS,None)}})
    assert all(type(value) is int and value>0 for value in limits.values())
    assert task_budget.exceeded_reason(limits,{'calls':limits['model_calls']})=='model_calls'


@pytest.mark.parametrize('bad',[0,-1,True,'18'])
def test_invalid_explicit_limits_are_not_silently_treated_as_unlimited(bad):
    with pytest.raises(ValueError):
        task_budget.effective_limits({'task_contract':{'budget_policy':'bounded','budgets':{'model_calls':bad}}})


def test_unknown_policy_fails_closed():
    with pytest.raises(ValueError):task_budget.effective_limits({'task_contract':{'budget_policy':'almost-infinite'}})


def _long_trace():
    return [{'id':'saved-'+str(index),'tool':'read_file','arguments':{'path':'notes/'+str(index)+'.txt'},
             'status':'done','observation':{'text':'saved result '+str(index)},'started_at':now()}
            for index in range(70)]


def test_native_owner_can_execute_after_both_old_18_and_64_step_boundaries(turn):
    patch('turn',turn['id'],agent_trace=_long_trace())
    reply=agent.opencode_step(turn['id'],write('continued after step 70'),'new-session:new-step')
    current=get('turn',turn['id'])
    assert current['state']=='agent' and not reply.get('terminal')
    assert len(current['agent_trace'])==71
    assert workspace.read_file(turn['project_id'],'reports/native.md')=='continued after step 70'


def test_legacy_owner_advance_reaches_planning_after_old_step_boundaries(turn,monkeypatch):
    patch('turn',turn['id'],agent_trace=_long_trace())
    calls=[]
    monkeypatch.setattr(opencode_runtime,'is_enabled',lambda:False)
    monkeypatch.setattr(agent.agent_graph,'invoke',lambda value:calls.append(value))
    agent.advance(get('turn',turn['id']))
    assert calls==[{'turn_id':turn['id']}]
    assert get('turn',turn['id'])['state']=='agent'


def test_stop_and_unchanged_repetition_protection_do_not_depend_on_a_total_budget(turn):
    from agent_service import agent_context
    repeated={'tool':'read_file','arguments':{'path':'same.txt'},'status':'done'}
    value=dict(turn,agent_trace=[repeated,repeated])
    assert agent_context.repetition(value,agent.Step(kind='tool',tool='read_file',arguments={'path':'same.txt'}))
    patch('turn',turn['id'],state='done',outcome='cancelled')
    result=agent.opencode_step(turn['id'],write('should not run'),'stopped:new-step')
    assert result['terminal'] and result['outcome']=='cancelled'
    assert not workspace.path_for(turn['project_id'],'reports/native.md').exists()


def test_server_marked_ordinary_conversation_does_not_inherit_old_custom_round_caps():
    legacy={'budget_policy':'bounded','budgets':{'model_calls':12,'input_characters':250000,'output_tokens':16000,'duration_ms':120000}}
    assert all(value is None for value in task_budget.effective_limits({'interactive_unlimited':True,'task_contract':legacy}).values())
    bounded=task_budget.effective_limits({'interactive_unlimited':True,'gateway_limits':{'model_calls':3},'task_contract':legacy})
    assert bounded['model_calls']==3,'ordinary-chat policy cannot enlarge an external grant'


def test_old_session_task_is_unlimited_before_a_new_message_or_resume():
    old={'session_id':'original-session','interaction_mode':'work',
         'task_contract':{'budget_policy':'bounded','budgets':{'model_calls':12,'input_characters':250000}}}
    assert task_budget.tool_step_limit(old) is None
    assert all(value is None for value in task_budget.effective_limits(old).values())
