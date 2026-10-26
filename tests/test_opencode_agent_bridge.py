"""OpenCode tool calls retain Atlas's actual authorization and durable effects."""
import json
import pytest
from agent_service import agent, provider, tasks, workspace, opencode_runtime
from agent_service.store import get, put, patch, delete, uid, now


@pytest.fixture
def turn(monkeypatch):
    pid='p_'+uid();tid='t_'+uid()
    put('project',{'name':'Native bridge验收'},pid)
    value=put('turn',{'project_id':pid,'text':'创建报告文件 reports/native.md',
                     'interaction_mode':'work','state':'agent','created_at':now()},tid)
    tasks.ensure(value)
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True,'base_url':'https://fixture.test/v1','model':'fixture','tool_protocol':'json'})
    yield get('turn',tid)
    delete('turn',tid);delete('project',pid)


def write(content='receipt'):
    return {'kind':'tool','tool':'write_file','arguments':{'path':'reports/native.md','content':content}}


def test_call_replay_never_creates_another_effect_or_accepts_changed_arguments(turn):
    agent.opencode_step(turn['id'],write(),'session:call-1')
    response=agent.opencode_step(turn['id'],write('changed'),'session:call-1')
    assert response['replayed'] is True
    assert workspace.read_file(turn['project_id'],'reports/native.md')=='receipt'
    assert len(get('turn',turn['id'])['agent_trace'])==1
    assert get('turn',turn['id'])['agent_trace'][0]['opencode_call_id']=='session:call-1'


def test_chat_cannot_write_through_opencode_and_invalid_input_is_not_reflected(turn):
    patch('turn',turn['id'],interaction_mode='chat')
    result=agent.opencode_step(turn['id'],write('FIXTURE-PRIVATE-CONTENT'),'session:call-1')
    assert result['observation']['error']
    assert 'FIXTURE-PRIVATE' not in json.dumps(result)
    assert not get('turn',turn['id']).get('agent_trace')
    assert not workspace.path_for(turn['project_id'],'reports/native.md').exists()


def test_schema_diagnostics_include_expected_structure_without_reflecting_values(turn):
    result=agent.opencode_step(turn['id'],{'kind':'tool','tool':'write_file',
        'arguments':{'path':{'secret':'FIXTURE-INJECTED-KEY'},'content':'hidden'}},'session:bad')
    observation=result['observation']
    assert observation['validation'] and observation['expected_arguments']['properties']['path']
    assert 'FIXTURE-' not in json.dumps(result)
    assert not get('turn',turn['id']).get('agent_trace')


def test_finish_requires_actual_receipts_and_terminal_turn_rejects_later_calls(turn):
    final={'kind':'final','answer':'报告已完成','status':'complete'}
    agent.opencode_step(turn['id'],final,'session:premature')
    assert get('turn',turn['id'])['state']=='agent'
    assert get('turn',turn['id'])['verification_feedback']
    agent.opencode_step(turn['id'],write(),'session:write')
    agent.opencode_step(turn['id'],final,'session:finished')
    assert get('turn',turn['id'])['outcome']=='verified_complete'
    response=agent.opencode_step(turn['id'],write('unexpected'),'session:late')
    assert response['terminal'] is True
    assert workspace.read_file(turn['project_id'],'reports/native.md')=='receipt'


@pytest.mark.parametrize('state',['waiting_run','waiting_data_tool','done','failed'])
def test_parallel_or_late_tools_stop_after_task_yields_ownership(turn,state):
    patch('turn',turn['id'],state=state)
    assert agent.opencode_step(turn['id'],write(),'session:late')['terminal'] is True
    assert not workspace.path_for(turn['project_id'],'reports/native.md').exists()


def test_interrupted_call_resumes_same_persisted_trace(turn):
    patch('turn',turn['id'],state='agent_tool',agent_trace=[{
        'id':'persisted-step','opencode_call_id':'session:write','tool':'write_file',
        'arguments':write()['arguments'],'status':'started','started_at':now()}])
    response=agent.opencode_step(turn['id'],write(),'session:write')
    current=get('turn',turn['id'])
    assert response['replayed'] and current['agent_trace'][0]['status']=='done'
    assert len(current['agent_trace'])==1 and current['agent_trace'][0]['id']=='persisted-step'
    assert workspace.read_file(turn['project_id'],'reports/native.md')=='receipt'


def test_native_tool_burst_is_bounded_even_when_one_model_reply_has_many_calls(turn):
    contract=get('turn',turn['id'])['task_contract'];contract['budget_policy']='bounded';contract['budgets']['model_calls']=2
    patch('turn',turn['id'],task_contract=contract)
    agent.opencode_step(turn['id'],write(),'session:first')
    response=agent.opencode_step(turn['id'],write('extra'),'session:second')
    assert response['terminal'] and get('turn',turn['id'])['outcome']=='paused'
    assert workspace.read_file(turn['project_id'],'reports/native.md')=='receipt'


def test_advance_uses_native_session_loop_when_enabled(turn,monkeypatch):
    monkeypatch.setattr(opencode_runtime,'is_enabled',lambda:True)
    def unexpected(*args,**kwargs):raise AssertionError('legacy planner must not run')
    monkeypatch.setattr(provider,'complete',unexpected)
    def session(value,prompt,info,callback):
        assert info['request']==turn['text'] and 'atlas_step' in prompt
        callback(write(),'session:one')
        callback({'kind':'final','answer':'报告已保存','status':'complete'},'session:two')
        return {'integration':'native_session_tool_loop','version':'2.1.2','model_requests':2}
    monkeypatch.setattr(opencode_runtime,'run_session',session)
    agent.advance(turn)
    current=get('turn',turn['id'])
    assert current['outcome']=='verified_complete'
    assert current['opencode_receipt']['integration']=='native_session_tool_loop'
