import pytest

from agent_service.intents import denied


@pytest.mark.parametrize(('text', 'action'), [
    ('请生成报告，不要运行', 'run_python_file'),
    ('请生成报告，不要运行任何代码', 'run_python_file'),
    ('请生成报告，不要重新处理数据', 'run_data_workflow'),
    ('请生成报告，不要修改源数据', 'write_file'),
    ('请生成报告，不要分析退款', 'build_transform'),
    ('请生成报告，不要导出到本地', 'export_local_workspace'),
])
def test_deliverable_request_does_not_override_operation_veto(text, action):
    assert denied(text, action)


@pytest.mark.parametrize('text', [
    '请生成报告，无法可靠计算的数字不要生成',
    '请生成报告，不能将未经确认的解释写成结论',
])
@pytest.mark.parametrize('action', ['write_file', 'build_transform'])
def test_content_quality_restrictions_do_not_veto_entire_deliverable(text, action):
    assert not denied(text, action)


def test_quality_exception_does_not_cancel_separate_operation_veto():
    assert denied('请生成报告，无法可靠计算的数字不要生成，不要修改源数据', 'write_file')


@pytest.mark.parametrize('text', [
    '请解释“请生成报告”是什么意思',
    '请说明“请生成报告”怎么做',
    '如果用户说请生成报告，该怎么办',
    '假如我说请生成报告，你会做什么',
])
def test_quoted_or_hypothetical_deliverable_does_not_authorize_writes(text):
    assert denied(text, 'write_file')


@pytest.mark.parametrize('text', [
    '如果资料不完整请先确认，否则生成报告',
    '如果资料不完整请先确认，否则请生成报告',
])
def test_condition_on_data_completeness_does_not_veto_the_actual_request(text):
    assert not denied(text, 'write_file')
