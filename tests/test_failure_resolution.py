"""Completion clears a failed request only after its actual equivalent retry."""
import copy

import pytest

from agent_service import agent,agent_context,workspace
from agent_service.store import get,patch,put,delete,uid
from test_opencode_agent_bridge import turn


def receipt(tool,arguments,status='failed',observation=None):
    return {'tool':tool,'arguments':arguments,'status':status,
            'observation':observation or ({'error':'safe failure'} if status=='failed' else {'checked':True})}


@pytest.mark.parametrize('later',[
    {'path':'notes/b.txt'},
    {'path':'notes/a.txt','offset':12000},
    {'path':'notes/a.txt','limit':6000},
])
def test_failed_file_or_range_is_not_cleared_by_another_successful_read(later):
    failed=receipt('read_file',{'path':'notes/a.txt'})
    success=receipt('read_file',later,'done')
    trace=[failed,success];frozen=copy.deepcopy(trace)
    assert agent._unresolved_failures(trace)==[failed]
    assert trace==frozen


@pytest.mark.parametrize('first,last',[
    ({'path':'notes/a.txt'},{'path':'notes/a.txt','offset':0,'limit':12000}),
    ({'path':'notes/a.txt','offset':0,'limit':12000},{'path':'notes/a.txt'}),
    ({'path':'notes/a.txt','offset':'0','limit':'12000'},{'path':'notes/a.txt'}),
])
def test_same_schema_normalized_request_with_default_arguments_is_resolved(first,last):
    assert not agent._unresolved_failures([receipt('read_file',first),receipt('read_file',last,'done')])


@pytest.mark.parametrize('tool,failed_args,other_args',[
    ('run_data_workflow',{'workflow_id':'wf_a'},{'workflow_id':'wf_b'}),
    ('publish_data_workflow_result',{'run_id':'run_a','expected_plan_sha256':'a'*64},
                                    {'run_id':'run_b','expected_plan_sha256':'a'*64}),
    ('publish_data_workflow_result',{'run_id':'run_a','expected_plan_sha256':'a'*64},
                                    {'run_id':'run_a','expected_plan_sha256':'b'*64}),
    ('publish_result',{'pipeline_id':'app_a'},{'pipeline_id':'app_b'}),
])
def test_financial_processing_and_publication_failure_require_the_same_object_and_version(tool,failed_args,other_args):
    failed=receipt(tool,failed_args)
    assert agent._unresolved_failures([failed,receipt(tool,other_args,'done')])==[failed]
    assert not agent._unresolved_failures([failed,receipt(tool,failed_args,'done')])


def test_prior_success_or_invalid_request_cannot_clear_a_later_failure():
    failure=receipt('read_file',{'path':'notes/a.txt'})
    assert agent._unresolved_failures([receipt('read_file',failure['arguments'],'done'),failure])==[failure]
    invalid=receipt('read_file',{'path':{'invalid':'value'}})
    assert agent._unresolved_failures([invalid,receipt('read_file',invalid['arguments'],'done')])==[invalid]


def test_authorized_historical_recovery_does_not_automatically_replace_current_source():
    failure=receipt('read_file',{'path':'notes/original.txt'})
    historical=receipt('read_tool_result',{'step_id':'old_original','source_turn_id':'prior_turn'},'done',
        {'tool':'read_file','content':'an authorized historical version'})
    assert agent._unresolved_failures([failure,historical])==[failure]


def test_unexecuted_guard_attempt_remains_excluded_without_ignoring_business_failures():
    guarded=receipt('read_file',{'path':'notes/a.txt'},observation={'executed':False,'code':'TOOL_NO_PROGRESS'})
    failed=receipt('run_data_workflow',{'workflow_id':'wf_failed'})
    assert agent._unresolved_failures([guarded,failed])==[failed]


@pytest.mark.parametrize('retry_args,expected',[
    ({'path':'notes/b.txt'},'partial'),
    ({'path':'notes/a.txt','offset':12000},'partial'),
    ({'path':'notes/a.txt','offset':0,'limit':12000},'verified_complete'),
])
def test_actual_finish_keeps_unrelated_read_failure_but_accepts_the_same_retry(turn,retry_args,expected):
    content='actual verified deliverable'
    workspace.write_file(turn['project_id'],'reports/native.md',content)
    trace=[receipt('read_file',{'path':'notes/a.txt'}),receipt('read_file',retry_args,'done'),
           receipt('write_file',{'path':'reports/native.md','content':content},'done')]
    patch('turn',turn['id'],agent_trace=trace)
    agent._execute_node({'turn_id':turn['id'],'step':{'kind':'final','status':'complete','answer':'报告已保存'}})
    current=get('turn',turn['id'])
    assert current['verification']['verified'] and current['outcome']==expected
    assert current.get('resume_available') is (expected=='partial')


def test_recovery_schema_description_and_actual_prompt_preserve_history_reference_owner(turn):
    definition=agent_context.definition('read_tool_result')
    description=definition['description']
    assert 'source_turn_id' in definition['arguments_schema']['properties']
    assert all(term in description for term in ('当前任务','恢复链','recover_with','source_turn_id','step_id','可见性','授权','撤回'))
    assert '不能跨任务' not in description
    messages,_=agent.planning_context(turn,native=False)
    assert '按recover_with原参数分页回读' in messages[0]['content']
    assert '历史回执同时传source_turn_id和step_id' in messages[0]['content']


@pytest.mark.parametrize('arguments',[
    {'sql':'SELECT 1'},
    {'pipeline_id':None,'sql':'SELECT 1'},
])
def test_unbound_dynamic_pipeline_defaults_cannot_clear_an_earlier_failure(arguments):
    failed=receipt('query_sql',arguments)
    done=receipt('query_sql',arguments,'done')
    assert agent._tool_request_key(failed) is None
    assert agent._unresolved_failures([failed,done])==[failed]
    # An explicit later target does not establish what an old implicit call
    # actually targeted. This intentionally retains a conservative partial.
    explicit=receipt('query_sql',{'pipeline_id':'app_a','sql':'SELECT 1'},'done')
    assert agent._unresolved_failures([failed,explicit])==[failed]


@pytest.mark.parametrize('implicit,expected',[(True,'partial'),(False,'verified_complete')])
def test_actual_authorized_focus_change_does_not_clear_a_failed_implicit_query(turn,monkeypatch,implicit,expected):
    from agent_service import api,conversation
    first='app_'+uid();second='app_'+uid();session='c_'+uid();sql='SELECT SUM(amount) FROM business.business_objects'
    for identity in (first,second):
        put('pipeline',{'project_id':turn['project_id'],'source_id':'s_'+identity,'name':identity},identity)
    contract=dict(turn['task_contract'],kind='query',requirements=['query'],application_ids=[first,second],acceptance='真实查询成功')
    put('conversation',{'project_id':turn['project_id'],'name':'Focus drift fixture','created_at':turn['created_at']},session)
    patch('project',turn['project_id'],pipeline_id=first,created_at=turn['created_at'])
    patch('turn',turn['id'],application_id=None,session_id=session,task_contract=contract,
        text='请查询两个应用的金额并重跑第二个应用',work_request='请查询两个应用的金额并重跑第二个应用')
    attempts=[];refreshes=[]
    def query(pipeline,value):
        attempts.append(pipeline)
        if len(attempts)==1:raise ValueError('Synthetic first application query failed')
        return {'rows':[{'total':'123.45'}],'columns':['total'],'sql':value.sql}
    def refresh(current):
        assert current['forced_decision']['action']=='refresh'
        assert get('project',turn['project_id'])['pipeline_id']==second
        refreshes.append(second)
        agent.observe(current,{'fixture_queue_completed':True})
    monkeypatch.setattr(api,'query',query)
    monkeypatch.setattr(conversation,'result_card',lambda pipeline:{'pipeline_id':pipeline,'run_id':'fixture_run'})
    monkeypatch.setattr(conversation,'legacy_handle',refresh)
    try:
        args={'sql':sql} if implicit else {'pipeline_id':first,'sql':sql}
        agent._execute_node({'turn_id':turn['id'],'step':{'kind':'tool','tool':'query_sql','arguments':args}})
        assert get('turn',turn['id'])['agent_trace'][-1]['status']=='failed'
        # Retain real authorization, target resolution and project-focus
        # mutation. Only the asynchronous refresh executor is a test fixture.
        agent._execute_node({'turn_id':turn['id'],'step':{'kind':'tool','tool':'refresh_data',
            'arguments':{'pipeline_id':second}}})
        assert refreshes==[second]
        agent._execute_node({'turn_id':turn['id'],'step':{'kind':'tool','tool':'query_sql','arguments':args}})
        assert attempts==[first,second if implicit else first]
        agent._execute_node({'turn_id':turn['id'],'step':{'kind':'final','status':'complete','answer':'查询已处理'}})
        current=get('turn',turn['id'])
        assert current['verification']['verified'] and current['outcome']==expected
    finally:
        delete('pipeline',first);delete('pipeline',second);delete('conversation',session)
