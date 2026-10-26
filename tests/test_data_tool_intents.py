"""Business-language tool choices never override the current user's veto."""
import pytest

from agent_service import agent
from agent_service.data_tools import DEFINITIONS
from agent_service.intents import ALIASES, VERBS, require
from agent_service.store import put,delete,uid,now


MUTATING_TOOLS=[name for name,(_,mutates,_) in DEFINITIONS.items() if mutates]


@pytest.fixture
def project():
    row=put('project',{'name':'Business intent authorization','created_at':now()},'p_'+uid())
    yield row['id']
    delete('project',row['id'])


def test_every_data_mutation_has_an_intent_category():
    assert MUTATING_TOOLS
    assert all(ALIASES.get(name,name) in VERBS for name in MUTATING_TOOLS)


@pytest.mark.parametrize('name',MUTATING_TOOLS)
@pytest.mark.parametrize('prior_key',['work_request','workflow_goal'])
def test_current_explanation_only_request_overrides_saved_processing_goal(name,prior_key,project):
    turn={'project_id':project,'text':'先只解释处理逻辑，暂时不要做任何操作','interaction_mode':'work',
          prior_key:'帮我获取报表并加工数据，生成核对表与财经简报'}
    with pytest.raises(ValueError,match='本轮是在否定或讨论'):
        agent.authorize(turn,name,{})


@pytest.mark.parametrize('name,text',[
    ('register_data_workflow','先不要登记加工方案'),
    ('run_data_workflow','不要执行这个加工流程'),
    ('publish_data_workflow_result','不要采用数据结果'),
    ('schedule_data_workflow','不要定时刷新数据'),
    ('propose_semantic_alignment','先别对齐字段'),
    ('propose_value_mapping','不要映射省份与办事处'),
    ('acquire_report_data','不要获取报告数据'),
    ('acquire_report_data','不要下载报表'),
    ('propose_extracted_data_facts','不要提取报告中的数值'),
    ('extract_indented_source','不要解析这张缩进表'),
    ('render_business_report','不要生成业务简报'),
    ('render_business_report','不要制作汇报材料'),
])
def test_operation_specific_veto_blocks_despite_prior_permission(name,text,project):
    turn={'project_id':project,'text':text,'interaction_mode':'work','work_request':'获取报告，按办事处汇总并生成报告'}
    with pytest.raises(ValueError,match='本轮是在否定或讨论'):
        agent.authorize(turn,name,{})


@pytest.mark.parametrize('name,text',[
    ('register_data_workflow','帮我按办事处汇总收入，保存可复用方案'),
    ('run_data_workflow','按刚才核对过的口径处理完整数据'),
    ('propose_semantic_alignment','帮我对齐这两张表的字段，生成提案并说明依据'),
    ('propose_value_mapping','帮我建立省份到办事处的映射，未知值请单独列出'),
    ('acquire_report_data','获取2026年9月华东区报表并核对行数'),
    ('propose_extracted_data_facts','提取报告中的收入金额并保留原文证据'),
    ('extract_indented_source','解析这张缩进表，保留行号和层级'),
    ('render_business_report','生成收入简报并解释变化原因'),
])
def test_real_business_processing_does_not_require_internal_tool_vocabulary(name,text,project):
    agent.authorize({'project_id':project,'text':text,'interaction_mode':'work'},name,{})


@pytest.mark.parametrize('name',[
    'register_data_workflow','run_data_workflow','propose_semantic_alignment',
    'propose_value_mapping','acquire_report_data','propose_extracted_data_facts',
    'extract_indented_source','render_business_report',
])
def test_continue_retains_authorized_multistep_processing(name,project):
    agent.authorize({'project_id':project,'text':'继续','interaction_mode':'work',
                     'work_request':'帮我获取报表，按办事处汇总收入并生成核对表和简报'},name,{})


def test_download_veto_does_not_block_saving_a_plan_or_running_an_existing_workflow():
    text='不要下载报表，先保存现有加工方案并运行'
    require({'text':text},'register_data_workflow')
    require({'text':text},'run_data_workflow')
    with pytest.raises(ValueError):require({'text':text},'acquire_report_data')
