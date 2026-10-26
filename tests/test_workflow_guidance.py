import copy,json
from agent_service import workflow_guidance,project_data,agent
from agent_service.data_workflows import execute
from test_project_data import project,source,document,registered


def test_agent_guide_has_executable_example_and_project_scoped_reuse():
    guide=workflow_guidance.guide()
    example=copy.deepcopy(guide['example'])
    project_data.WorkflowDocument.model_validate(example)
    assert 'WorkflowPlan' in guide['schema']['$defs']
    result=execute(example['plan'],{'detail':[{'record_id':'A','amount':'10.01'},
                                            {'record_id':'B','amount':'0'},
                                            {'record_id':'C','amount':'-2'}]})
    assert result['complete'] and result['output']['rows']==[{'record_id':'A','amount':'10.01'}]
    pid=project();sid=source(pid)['id'];wid=registered(pid,document(sid))
    other=project()
    assert [w['id'] for w in workflow_guidance.catalog(pid)['items']]==[wid]
    assert not workflow_guidance.catalog(other)['items']
    assert agent.context(pid)['data_workflows']['items'][0]['id']==wid


def test_month_end_cadence_is_explicit_and_short_months_are_disclosed():
    from agent_service.cadence import parse
    from croniter import croniter
    from datetime import datetime
    rule=parse('每月5日上午9点刷新')
    assert rule['cron']=='0 9 5 * *'
    end=parse('每月31日晚上8点更新')
    assert '无此日期' in end['label']
    assert croniter(end['cron'],datetime(2026,1,31,21)).get_next(datetime)==datetime(2026,3,31,20)
    assert parse('不要每月5日9点刷新') is None
    assert parse('每月5日9点刷新吗？') is None
