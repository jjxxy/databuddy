"""Agent-facing creation and inspection of versioned business-flow drafts.

Publishing remains an explicit user action through the authenticated UI/API.
"""
import hashlib
from pydantic import Field
from .models import StrictModel
from .business_flow_engine import BusinessFlow
from .store import get,patch


class CreateFlow(StrictModel):
    document: BusinessFlow


class FlowTarget(StrictModel):
    flow_id: str = Field(pattern=r'^bf_[a-f0-9]{40}$')


class UpdateFlow(FlowTarget):
    expected_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    document: BusinessFlow


class RunFlow(FlowTarget):
    expected_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    inputs: dict = Field(default_factory=dict,max_length=100)


class RunTarget(FlowTarget):
    run_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')


class SkillTarget(StrictModel):
    skill_id: str = Field(min_length=1,max_length=100)


class SourceTarget(StrictModel):
    source_id: str = Field(min_length=1,max_length=64)


class Empty(StrictModel):
    pass


DEFINITIONS={
    'create_business_flow':(CreateFlow,True,'根据用户的业务目标和分步要求创建版本化流程草稿；资料、范围或财务口径不明确时先询问，不猜测。创建不代表已运行或发布。用户要求执行时，可用 run_business_flow 运行并检查回执。'),
    'update_business_flow':(UpdateFlow,True,'按用户要求修改指定流程，传入查看到的候选版本摘要及完整新定义；只生成候选版本，不运行、不覆盖正式版。'),
    'list_business_flows':(Empty,False,'列出当前项目、当前用户的业务流程草稿与正式版本。'),
    'inspect_business_flow':(FlowTarget,False,'查看指定业务流程的真实版本、步骤和运行状态；不运行也不发布。'),
    'run_business_flow':(RunFlow,True,'用户要求执行时，按已查看的候选版本摘要和明确输入入队运行；仅支持本地读取、确定性加工和成果生成，不自动发布。模型、技能或其他可能产生外部影响的步骤不能通过此工具自动执行。入队不等于完成，应使用 inspect_business_flow_run 查询真实回执。'),
    'inspect_business_flow_run':(RunTarget,False,'读取指定业务流程运行的真实状态、成果和失败修复指引；根据实际回执核对并修复有依据的问题，缺少财务口径时询问。未知执行结果不得重试；执行完成不代表财务结果已核对。'),
    'inspect_business_flow_debug_run':(RunTarget,False,'读取指定业务流程调试运行的真实状态和失败修复指引；调试结果不可发布。'),
    'business_flow_guide':(Empty,False,'查看当前业务流程的实际节点、配置结构和数据连接方式，再据此创建可执行草稿。'),
    'prepare_business_flow_source':(SourceTarget,False,'取得本项目已就绪文件的固定版本和摘要，供业务流程准确引用；不读取全部业务数据。'),
    'prepare_business_flow_model':(Empty,False,'读取本轮已授权的模型目录版本，供业务流程模型步骤固定使用；不调用模型。'),
    'prepare_business_flow_skill':(SkillTarget,True,'固定已启用 Skill 的授权版本和摘要供业务流程使用；不执行包内脚本，不授予额外权限。'),
}


def execute(turn,name,args):
    from .actor_context import current_actor
    from . import business_flows,workflow_model_skill
    pid=turn['project_id'];actor=current_actor()
    if turn.get('actor_id','owner')!=actor:raise ValueError('任务不属于当前用户')
    selected=turn.get('selected_workflow')
    if (name in ('update_business_flow','run_business_flow') and selected and selected.get('kind')=='business'
            and selected.get('id')==args.get('flow_id') and selected.get('digest')!=args.get('expected_digest')):
        raise ValueError('所选流程版本已变化，请重新核对后继续')
    context={'project_id':pid,'actor_id':actor,'turn_id':turn['id'],'originating_turn_id':turn['id']}
    if name=='business_flow_guide':
        from .business_flow_data import SourceConfig,TableConfig
        from .business_flow_quality_gate import Config as QualityConfig
        from .business_flow_human_review import Config as HumanReviewConfig
        from .business_deliverables import DeliveryConfig,TableReportConfig,HTML_RENDERER
        from .office import RENDERER
        return {'flow_schema':BusinessFlow.model_json_schema(),
                'nodes':{'source_table':{'config_schema':SourceConfig.model_json_schema(),'output':'source_table_v1'},
                         'table_workflow':{'config_schema':TableConfig.model_json_schema(),'output':'table_workflow_v1'},
                         'quality_gate':{'config_schema':QualityConfig.model_json_schema(),'input':'table（table_workflow 或 quality_gate 输出）','output':'table_workflow_v1','notice':'只配置用户明确要求的字段和规则；失败阻断后续步骤，不改数据，不代表财务结果已核验。'},
                         'human_review':{'config_schema':HumanReviewConfig.model_json_schema(),'input':'table（已加工或已核对的数据表）','output':'table_workflow_v1','notice':'仅用户明确要求人工复核时添加；此节点暂停，明确确认后继续。'},
                         'table_report_spec':{'config_schema':TableReportConfig.model_json_schema(),'input':'table','optional_input':'commentary（仅 model 或 skill 输出，明确标注未经核验）','output':'table_report_spec_v1'},
                         'office_document':{'config_schema':DeliveryConfig.model_json_schema(),'renderer_version':RENDERER,'input':'spec'},
                         'html_report':{'config_schema':DeliveryConfig.model_json_schema(),'renderer_version':HTML_RENDERER,'input':'spec'},
                         'model':{'required_config':['model_id','catalog_revision','prompt'],'input':'content','nondeterministic':True},
                         'skill':{'required_config':['model_id','catalog_revision','prompt','pin_id','skill_id','skill_digest'],'input':'content','nondeterministic':True}},
                'notice':'仅使用已提供的实际节点及其输入输出。可用 input_specs 声明文本、整数、精确小数（字符串）、日期、布尔或选项参数；参数可绑定模型或技能的 content；数据加工只允许通过 parameter_bindings 明确绑定已声明筛选条件的值，不可改变字段或运算符。需要创建时用 discover_tools 查找 create_business_flow 并依据真实资料创建；用户要求执行时调用 run_business_flow，再调用 inspect_business_flow_run 核对状态和成果。入队不是完成，执行完成也不是财务验收；资料、期间、币种、汇率、含税及预算版本不明确时询问用户，不得猜测或模糊匹配。草稿尚未执行或发布；模型输出不是已核验的财经依据。'}
    if name=='prepare_business_flow_source':
        from .conversation import owned
        source=owned('source',args['source_id'],pid)
        if (source.get('kind')!='file' or source.get('status')!='ready' or source.get('archived')
                or type(source.get('revision')) is not int or not isinstance(source.get('content_hash'),str)):
            raise ValueError('资料未就绪或没有可固定的文件版本')
        return {'source_id':source['id'],'revision':source['revision'],'content_hash':source['content_hash'],'name':source.get('name')}
    if name=='prepare_business_flow_model':return workflow_model_skill.freeze_model(context)
    if name=='prepare_business_flow_skill':return workflow_model_skill.freeze_skill(context,args['skill_id'])
    if name=='list_business_flows':return business_flows.list_views(pid,actor)
    if name=='inspect_business_flow':return business_flows.get_view(pid,args['flow_id'],actor)
    if name=='inspect_business_flow_run':
        from .business_flow_repair import guidance
        run=business_flows.get_run(pid,args['flow_id'],args['run_id'],actor)
        return {**run,'repair_guidance':guidance(run)}
    if name=='inspect_business_flow_debug_run':
        from .business_flow_debug import status
        from .business_flow_repair import guidance
        run=status(pid,args['flow_id'],args['run_id'],actor)
        return {**run,'repair_guidance':guidance(run)}
    if name=='run_business_flow':
        from . import agent_context,agent_gateway,agent_team
        from .intents import require
        require(turn,'refresh');agent_gateway.assert_turn_allowed(turn)
        flow=business_flows.get_view(pid,args['flow_id'],actor)
        if flow.get('candidate_digest')!=args['expected_digest']:raise ValueError('流程版本已变化，请重新查看')
        doc=BusinessFlow.model_validate(flow.get('candidate_document'))
        permitted={'source_table','table_workflow','quality_gate','human_review','table_report_spec','html_report','office_document'}
        if any(node.kind not in permitted for node in doc.nodes):raise ValueError('该流程包含需单独确认的模型、技能或外部步骤，请在业务流程页面核对后运行')
        # Check the current turn as well as the original flow's execution
        # authority, which submit_run and the worker validate independently.
        names=agent_context.allowed(turn)
        required={'source_table':'read_source_table','table_workflow':'run_data_workflow','quality_gate':'run_data_workflow','human_review':'run_data_workflow',
                  'table_report_spec':'write_file','html_report':'write_file',
                  'office_document':'render_office_artifact'}
        for node in doc.nodes:
            tool=('render_office_artifact' if node.kind=='table_report_spec' and node.config.get('format')=='presentation' else required[node.kind])
            if tool not in names:raise ValueError('当前任务没有执行此步骤的权限')
            agent_team.assert_turn_allowed(turn,tool,{})
            if node.kind=='source_table':
                source_id=node.config.get('source_id')
                if not source_id and node.inputs.get('source'):
                    pin=args['inputs'].get(node.inputs['source'].name) or {}
                    source_id=pin.get('source_id') if isinstance(pin,dict) else None
                if not source_id:raise ValueError('需要明确提供本次使用的来源资料')
                agent_team.assert_turn_allowed(turn,'read_source_table',{'source_id':source_id})
        current=get('turn',turn['id']);trace=current.get('agent_trace') or []
        if not trace or trace[-1].get('status')!='started':raise ValueError('流程运行缺少当前执行回执')
        key=hashlib.sha256((turn['id']+'\0'+trace[-1]['id']).encode()).hexdigest()
        result=business_flows.submit_run(pid,args['flow_id'],actor,'agent_'+key,args['expected_digest'],args['inputs'])
        return {**result,'message':'任务已入队，尚未确认完成；请读取运行状态并核对来源和结果。'}
    if name in ('create_business_flow','update_business_flow'):
        current=get('turn',turn['id']);trace=current.get('agent_trace') or []
        if not trace or trace[-1].get('status')!='started':raise ValueError('流程创建缺少当前执行回执')
        # The identifier is derived from the durable tool step, not chosen by
        # the model. Recovery of the same step reuses exactly this draft.
        key=hashlib.sha256((turn['id']+'\0'+trace[-1]['id']).encode()).hexdigest()
        if name=='create_business_flow':
            result=business_flows.create(pid,actor,turn['id'],args['document'],'agent_'+key)
        else:
            result=business_flows.update(pid,args['flow_id'],actor,turn['id'],args['document'],'agent_'+key,args['expected_digest'])
        patch('turn',turn['id'],business_flow={'id':result['id'],'name':result['name']})
        return {'flow_id':result['id'],'name':result['name'],'candidate_digest':result['candidate_digest'],
                'candidate_commit':result['candidate_commit'],'state':'draft',
                'message':'业务流程草稿已保存，尚未运行或发布；如用户要求执行，可运行后读取真实回执并展示结果。'}
    raise ValueError('未知业务流程工具')
