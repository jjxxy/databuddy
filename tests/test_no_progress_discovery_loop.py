"""Repeated discovery cannot conceal identical failed actions as progress."""
import copy

import pytest

from agent_service import agent,agent_context as context,workspace
from agent_service.store import get,patch
from test_opencode_agent_bridge import turn


ARGS={'workflow_id':'wf_example'}
ERROR='本轮是在否定或讨论这项操作，未执行；请明确说明要做的变更'


def failed(error=ERROR,arguments=None):
    return {'tool':'run_data_workflow','arguments':dict(arguments or ARGS),'status':'failed','observation':{'error':error}}


def discovery(query='run_data_workflow',*,extra=False,revision=1):
    tools=[{'name':'run_data_workflow','description':'Run saved workflow',
            'arguments_schema':{'type':'object','revision':revision}}]
    if extra:tools.append({'name':'inspect_data_workflow','arguments_schema':{'type':'object'}})
    return {'tool':'discover_tools','arguments':{'query':query,'limit':6},'status':'done',
            'observation':{'tools':tools,'next_step':'下一步可调用这些工具；目录匹配不代表已经执行'}}


def step(arguments=None):
    return agent.Step(kind='tool',tool='run_data_workflow',arguments=arguments or ARGS)


def stalled():return [discovery(),failed(),discovery('参数完整说明'),failed(),discovery('重新查找同一工具')]


def test_same_failed_action_is_detected_across_changed_discovery_queries():
    assert context.repetition({'agent_trace':stalled()},step())
    assert not context.repetition({'agent_trace':stalled()},step({'workflow_id':'wf_changed'}))
    assert not context.repetition({'agent_trace':stalled(),'agent_budget_base':5},step())


def test_changed_failure_receipt_or_read_result_is_progress_not_repetition():
    trace=stalled();trace[3]=failed('源文件已变化，需要核对新版本')
    assert not context.repetition({'agent_trace':trace},step())
    reads=[{'tool':'read_file','arguments':{'path':'report.md'},'status':'done','observation':{'content':value}}
           for value in ('first version','new version')]
    assert not context.repetition({'agent_trace':reads},agent.Step(kind='tool',tool='read_file',arguments={'path':'report.md'}))


@pytest.mark.parametrize('last',[discovery(extra=True),discovery(revision=2)])
def test_new_discovered_tool_or_changed_schema_resets_the_failure_streak(last):
    assert not context.repetition({'agent_trace':stalled()[:-1]+[last]},step())


def test_reverted_schema_is_still_a_change_from_the_current_definition():
    trace=[discovery(revision=1),discovery(revision=2),failed(),discovery(revision=2),failed(),discovery(revision=1)]
    assert not context.repetition({'agent_trace':trace},step())


@pytest.mark.parametrize('tool',['write_file','load_workflow','read_source_table','read_file','inspect_source_tables','set_task_plan'])
def test_any_actual_intervening_repair_load_or_input_resets_the_streak(tool):
    repair={'tool':tool,'arguments':{'fixture':'changed'},'status':'done','observation':{'new':'evidence'}}
    assert not context.repetition({'agent_trace':stalled()[:-1]+[repair,discovery()]},step())


def test_malformed_or_failed_discovery_is_not_silently_treated_as_transparent():
    for replacement in ({'tool':'discover_tools','arguments':{},'status':'failed','observation':{'error':'unavailable'}},
                        {'tool':'discover_tools','arguments':{},'status':'done','observation':{'new_status':'changed'}}):
        assert not context.repetition({'agent_trace':stalled()[:-1]+[replacement]},step())


def test_execute_node_pauses_no_progress_and_preserves_prior_evidence(turn,monkeypatch):
    workspace.write_file(turn['project_id'],'workflows/saved.json','{"saved":"evidence"}')
    trace=stalled();patch('turn',turn['id'],agent_trace=copy.deepcopy(trace))
    executed=[]
    def execute(value,action):
        executed.append(action.tool)
        assert action.tool=='discover_tools','A third identical failed business action must not execute'
        return discovery()['observation']
    monkeypatch.setattr(agent,'execute',execute)
    agent._execute_node({'turn_id':turn['id'],'step':step().model_dump()})
    warned=get('turn',turn['id'])
    assert warned['state']=='agent' and warned['loop_attempts']==1
    assert warned['agent_trace'][:len(trace)]==trace and not executed
    assert warned['agent_trace'][-1]['observation']['executed'] is False
    assert warned['agent_trace'][-1]['observation']['code']=='TOOL_NO_PROGRESS'
    assert '未变化的工具说明' in warned['verification_feedback']['error']
    # One more description lookup must not reset the protected boundary.
    agent._execute_node({'turn_id':turn['id'],'step':{'kind':'tool','tool':'discover_tools',
        'arguments':{'query':'run_data_workflow 的完整参数','limit':6}}})
    agent._execute_node({'turn_id':turn['id'],'step':step().model_dump()})
    stopped=get('turn',turn['id'])
    assert stopped['state']=='done' and stopped['outcome']=='paused' and stopped['resume_available']
    assert stopped['agent_trace'][:len(trace)]==trace
    assert executed==['discover_tools'] and len(stopped['agent_trace'])==len(trace)+3
    assert workspace.read_file(turn['project_id'],'workflows/saved.json')=='{"saved":"evidence"}'


def test_long_task_with_real_progress_still_executes_after_seventy_steps(turn):
    trace=[{'tool':'read_file','arguments':{'path':f'notes/{index}.txt'},'status':'done',
            'observation':{'content':f'different evidence {index}'}} for index in range(70)]
    patch('turn',turn['id'],agent_trace=trace)
    agent._execute_node({'turn_id':turn['id'],'step':{'kind':'tool','tool':'write_file',
        'arguments':{'path':'reports/native.md','content':'Legitimate progress after seventy steps.'}}})
    current=get('turn',turn['id'])
    assert current['state']=='agent' and len(current['agent_trace'])==71
    assert current['agent_trace'][-1]['status']=='done'
    assert workspace.read_file(turn['project_id'],'reports/native.md')=='Legitimate progress after seventy steps.'
