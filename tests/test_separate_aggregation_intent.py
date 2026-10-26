"""The character 别 in 分别/识别 is not an instruction to refuse processing."""
import pytest

from agent_service import agent,intents,tasks
from agent_service.jobs import get_job,request_cancel
from agent_service.store import get,patch
from test_agent_contracts import task
from test_project_data import project,source,document,registered


@pytest.mark.parametrize('text',[
    '收入和成本明细分别汇总后再连接预算。',
    '请分别处理收入和成本，再汇总。',
    '请识别字段类型后生成报告。',
    '请区别处理不同来源，再生成候选。',
    '请特别生成一份核对报告。',
    '按类别生成报表，按级别汇总。',
])
def test_word_internal_bie_is_not_an_analysis_veto(text):
    assert not intents.denied(text,'analyze')
    intents.require(task(text),'analyze')


@pytest.mark.parametrize('text,action',[
    ('收入和成本请你别汇总。','analyze'),
    ('请你别分别处理这些数据。','run_data_workflow'),
    ('千万别运行流程。','refresh'),
    ('收入和成本分别汇总，但别发布。','publish_result'),
    ('请识别字段，但不要修改原始文件。','write_file'),
    ('请区别对待来源，别删除文件。','delete_file'),
    ('先不要分析，只列出文件名。','analyze'),
    ('只解释分别汇总是什么意思。','analyze'),
    ('解释为什么要分别汇总。','analyze'),
])
def test_real_veto_and_discussion_remain_enforced(text,action):
    with pytest.raises(ValueError):intents.require(task(text),action)


def test_official_finance_prompt_queues_real_workflow_without_granting_publication():
    from acceptance.depth.cases.finance_monthly import official_prompts
    primary,_=official_prompts();assert '收入和成本明细分别汇总后再连接预算' in primary
    pid=project();value=source(pid);wid=registered(pid,document(value['id']))
    turn=task(primary);patch('turn',turn['id'],project_id=pid,state='agent');turn=get('turn',turn['id'])
    tasks.ensure(turn);job_id=None
    try:
        for action in ('analyze','refresh','run_data_workflow'):intents.require(turn,action)
        step={'kind':'tool','tool':'run_data_workflow','arguments':{'workflow_id':wid}}
        agent.validate_step(turn,step,selected_only=True)
        agent._execute_node({'turn_id':turn['id'],'step':step})
        waiting=get('turn',turn['id']);assert waiting['state']=='waiting_data_workflow'
        job_id=waiting['data_workflow_job'];run=get_job(job_id)
        assert run['kind']=='data_workflow' and run['project_id']==pid and run['target']==wid and run['status']=='queued'
        assert not tasks.verify(waiting)['verified']
        with pytest.raises(ValueError,match='用户本轮明确提出'):agent.authorize(waiting,'publish_result',{})
        assert get('data_workflow',wid)['published_run'] is None
        assert get('turn',turn['id'])['text']==primary
    finally:
        if job_id:request_cancel(job_id)
        patch('turn',turn['id'],state='done',outcome='cancelled')
