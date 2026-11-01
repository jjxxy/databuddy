"""Explicit multi-stage work requires a real model-authored plan before writes."""
from agent_service import agent, workspace
from agent_service.store import get, patch
from test_opencode_agent_bridge import turn, write


def test_multistage_work_must_plan_before_mutation(turn):
    patch('turn',turn['id'],text='先读取项目资料，然后生成报告文件')
    reply=agent.opencode_step(turn['id'],write('结果'),'plan:early-write')
    current=get('turn',turn['id'])
    assert reply['verification_feedback']['code']=='WORK_PLAN_REQUIRED'
    assert current['state']=='agent' and not current.get('task_plan')
    assert not workspace.path_for(turn['project_id'],'reports/native.md').exists()
    plan={'kind':'tool','tool':'set_task_plan','arguments':{'steps':['读取项目资料','生成报告'], 'completed_steps':[]}}
    result=agent.opencode_step(turn['id'],plan,'plan:planned')
    assert result['observation']['tool']=='set_task_plan'
    assert get('turn',turn['id'])['task_plan']['steps']==['读取项目资料','生成报告']
    agent.opencode_step(turn['id'],write('结果'),'plan:write')
    assert workspace.read_file(turn['project_id'],'reports/native.md')=='结果'


def test_simple_work_and_chat_do_not_require_plan(turn):
    patch('turn',turn['id'],text='创建报告文件 reports/native.md')
    agent.opencode_step(turn['id'],write('一步完成'),'simple:write')
    assert workspace.read_file(turn['project_id'],'reports/native.md')=='一步完成'
    assert not get('turn',turn['id']).get('plan_required_rejections')
    assert not agent._requires_user_work_plan({'interaction_mode':'chat','text':'先读后回答'})


def test_initial_plan_cannot_claim_completed_steps_without_work_receipt(turn):
    patch('turn',turn['id'],text='读取文件并生成报告')
    result=agent.opencode_step(turn['id'],{'kind':'tool','tool':'set_task_plan','arguments':{'steps':['读取','生成'],'completed_steps':[0]}},'plan:premature')
    current=get('turn',turn['id'])
    assert not current.get('task_plan')
    assert current['agent_trace'][-1]['status']=='failed'
    assert '尚无实际工作回执' in str(result['observation'])


def test_read_only_inspection_is_allowed_before_plan(turn):
    patch('turn',turn['id'],text='先检查项目资料然后生成报告')
    result=agent.opencode_step(turn['id'],{'kind':'tool','tool':'project_context','arguments':{}},'plan:inspect')
    assert result['observation']['tool']=='project_context'
    assert get('turn',turn['id'])['state']=='agent'


def test_negative_constraints_are_not_counted_as_work_stages():
    request='请核对全部资料，保留9007199254740993.000000000001。不要发布，不要发送邮件。'
    assert not agent._requires_user_work_plan({'interaction_mode':'work','text':request})


def test_old_resumed_work_is_not_blocked_for_missing_new_plan(turn):
    patch('turn',turn['id'],text='先检查项目资料然后生成报告',resumed_from='old-turn')
    agent.opencode_step(turn['id'],write('已有任务继续'),'legacy:continued')
    assert workspace.read_file(turn['project_id'],'reports/native.md')=='已有任务继续'
    assert not get('turn',turn['id']).get('plan_required_rejections')
