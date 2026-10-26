"""Human no-code requirements must not veto the Agent's own processing files."""
import pytest

from agent_service import agent,intents,workspace
from agent_service.store import get
from test_agent_contracts import task


@pytest.mark.parametrize('human_requirement',[
    '用户不需要写代码或修脚本',
    '业务用户不用自己编写代码',
    '我不必亲自修改脚本',
    '无需用户写代码',
    '不要让我写代码',
    '别要求业务用户调试脚本',
])
def test_user_coding_burden_is_not_an_agent_file_write_veto(human_requirement):
    text='请处理全部业务底稿，生成可审查的加工方案。'+human_requirement+'。先给候选，不要发布。'
    value=task(text)
    assert not intents.denied(text,'write_file')
    assert not intents.denied(text,'register_data_workflow')
    intents.require(value,'write_file')
    assert intents.denied(text,'publish_result')


def test_official_finance_prompt_can_really_write_a_processing_file_but_cannot_publish():
    from acceptance.depth.cases.finance_monthly import official_prompts
    primary,_=official_prompts();assert '用户不需要写代码或修脚本' in primary
    value=task(primary)
    step=agent.validate_step(value,{'kind':'tool','tool':'write_file',
        'arguments':{'path':'workflows/test.json','content':'{"authorization_regression_only":true}'}},selected_only=True)
    agent.authorize(value,step.tool,step.arguments)
    receipt=agent.execute(value,step)
    assert receipt['path']=='workflows/test.json'
    assert workspace.read_file(value['project_id'],'workflows/test.json')=='{"authorization_regression_only":true}'
    assert get('turn',value['id'])['text']==primary
    with pytest.raises(ValueError,match='用户本轮明确提出'):
        agent.authorize(value,'publish_result',{})
    publish=agent.validate_step(value,{'kind':'tool','tool':'publish_data_workflow_result',
        'arguments':{'run_id':'not-an-accepted-run','expected_plan_sha256':'a'*64}})
    with pytest.raises(ValueError,match='先打开结果核对页'):
        agent.execute(value,publish)


@pytest.mark.parametrize('text',[
    '用户不需要写代码，你不要修改任何文件。',
    '用户不需要写代码也不能修改任何文件。',
    '用户不需要写代码，我没让你保存文件。',
    '你不需要写代码，请只给已有数据的预览。',
    '不用写代码，请先讨论。',
    '不允许用户写代码，也不允许你写文件。',
    '不要让用户修改原始文件。',
    '用户不需要写代码，请解释如何修改加工方案。',
    '用户不需要写代码，只解释，不做任何修改。',
    '解释“用户不需要写代码或修脚本”是什么意思。',
    '“用户不需要写代码”',
    '用户不需要写代码，讨论“写一份报告”。',
])
def test_actual_veto_discussion_or_quoted_instruction_remains_a_write_veto(text):
    with pytest.raises(ValueError):intents.require(task(text),'write_file')


def test_human_no_code_requirement_does_not_change_other_action_authority_or_new_veto():
    original='请处理业务底稿，用户不需要写代码或修脚本。不要删除文件，不要发布，不要定时更新。'
    value=task(original)
    for action in ('delete_file','publish_result','schedule_updates'):
        with pytest.raises(ValueError):intents.require(value,action)
    with pytest.raises(ValueError):intents.require(task('用户不用删除代码文件。'),'delete_file')
    with pytest.raises(ValueError,match='聊天模式'):
        intents.require(dict(value,interaction_mode='chat'),'write_file')
    with pytest.raises(ValueError):
        intents.require(dict(value,text='先不要修改文件',work_request=original),'write_file')
    with pytest.raises(ValueError):
        intents.require(dict(value,text='继续',work_request='不要修改文件'),'write_file')
