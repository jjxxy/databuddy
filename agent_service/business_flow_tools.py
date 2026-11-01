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


class SkillTarget(StrictModel):
    skill_id: str = Field(min_length=1,max_length=100)


class SourceTarget(StrictModel):
    source_id: str = Field(min_length=1,max_length=64)


class Empty(StrictModel):
    pass


DEFINITIONS={
    'create_business_flow':(CreateFlow,True,'创建项目内版本化业务流程草稿；必须给出已获准的实际节点和固定来源，先解释需求，创建不代表已试运行或发布。用户在页面核对后手动运行和确认发布。'),
    'update_business_flow':(UpdateFlow,True,'按用户要求修改指定流程，传入查看到的候选版本摘要及完整新定义；只生成候选版本，不运行、不覆盖正式版。'),
    'list_business_flows':(Empty,False,'列出当前项目、当前用户的业务流程草稿与正式版本。'),
    'inspect_business_flow':(FlowTarget,False,'查看指定业务流程的真实版本、步骤和运行状态；不运行也不发布。'),
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
    context={'project_id':pid,'actor_id':actor,'turn_id':turn['id'],'originating_turn_id':turn['id']}
    if name=='business_flow_guide':
        from .business_flow_data import SourceConfig,TableConfig
        from .business_deliverables import DeliveryConfig,TableReportConfig,HTML_RENDERER
        from .office import RENDERER
        return {'flow_schema':BusinessFlow.model_json_schema(),
                'nodes':{'source_table':{'config_schema':SourceConfig.model_json_schema(),'output':'source_table_v1'},
                         'table_workflow':{'config_schema':TableConfig.model_json_schema(),'output':'table_workflow_v1'},
                         'table_report_spec':{'config_schema':TableReportConfig.model_json_schema(),'input':'table','optional_input':'commentary（仅 model 或 skill 输出，明确标注未经核验）','output':'table_report_spec_v1'},
                         'office_document':{'config_schema':DeliveryConfig.model_json_schema(),'renderer_version':RENDERER,'input':'spec'},
                         'html_report':{'config_schema':DeliveryConfig.model_json_schema(),'renderer_version':HTML_RENDERER,'input':'spec'},
                         'model':{'required_config':['model_id','catalog_revision','prompt'],'input':'content','nondeterministic':True},
                         'skill':{'required_config':['model_id','catalog_revision','prompt','pin_id','skill_id','skill_digest'],'input':'content','nondeterministic':True}},
                'notice':'仅使用已提供的实际节点及其输入输出。草稿尚未执行或发布；模型输出不是已核验的财经依据。'}
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
                'message':'业务流程草稿已保存，尚未试运行或发布；请在业务流程页面核对。'}
    raise ValueError('未知业务流程工具')
