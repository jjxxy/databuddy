"""Chat-facing tools. Only the authenticated review UI can approve semantics."""
import json,re
from typing import Literal
from pydantic import Field
from .models import StrictModel
from . import project_data as data
from .store import get,patch,all_items


def _wait_for_job(turn,**values):
    from .job_authority import turn_guard
    # Admission releases its lock before this link is saved. Stop may win in
    # between; never resurrect its cancelled turn as a waiting task.
    with turn_guard(turn['id']):
        current=get('turn',turn['id'])
        if current.get('state') in ('done','failed') or current.get('outcome')=='cancelled':return
        patch('turn',turn['id'],**values)


class SourceId(StrictModel):source_id:str=Field(min_length=1,max_length=64)
class ResultPage(data.RunTarget):
    offset:int=Field(default=0,ge=0)
    limit:int=Field(default=25,ge=1,le=100)
class Proposal(StrictModel):
    source:dict
    catalog:list[dict]=Field(min_length=1,max_length=500)
    model_candidates:list[dict]=Field(default_factory=list,max_length=100)
class Mapping(StrictModel):mapping:dict
class Id(StrictModel):proposal_id:str=Field(min_length=1,max_length=64)
class WorkflowSchedule(data.WorkflowTarget):
    watch_sources:bool=False
class Empty(StrictModel):pass
class BusinessBrief(data.RunTarget):
    recipe_path:str=Field(min_length=1,max_length=200)
class SemanticCatalogPage(StrictModel):
    status:Literal['draft','approved','retired']|None='approved'
    offset:int=Field(default=0,ge=0)
    limit:int=Field(default=20,ge=1,le=40)
class SemanticCatalogTarget(StrictModel):
    catalog_id:str=Field(min_length=1,max_length=64)
class SemanticCatalogProposal(StrictModel):
    definition:dict
class CatalogAlignment(SemanticCatalogTarget):
    source:dict
    expected_catalog_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    as_of_date:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    model_candidates:list[dict]=Field(default_factory=list,max_length=100)

DEFINITIONS={
 'data_workflow_guide':(Empty,False,'按需获取加工方案完整参数结构与可验证示例，由Agent编写代码逻辑；初次编写方案前读取，不能让用户填写JSON'),
 'list_data_workflows':(Empty,False,'查找当前业务空间可复用的加工任务、正式结果与刷新状态，优先复用已存在的业务口径'),
 'business_report_guide':(Empty,False,'获取财经简报可复用方案参数结构；由Agent编写方案，用户无需填写代码；数值来自完整已验证结果'),
 'inspect_business_report_input':(data.RunTarget,False,'核对财经简报完整结果的字段、单位、期间和语义摘要，返回绑定方案所需的schema_sha256'),
 'render_business_report':(BusinessBrief,True,'按项目中的可复用简报方案计算完整数值，生成带来源的汇报材料；金额和比率由确定性代码计算，不靠模型心算'),
 'list_business_reports':(Empty,False,'查看当前个人业务空间内已生成的简报及其来源结果，可复用原报告方案'),
 'inspect_source_tables':(SourceId,False,'查看当前项目资料的多页签、命名表格、合并区域、字段和公式读取边界；不能将首次样本当完整数据'),
 'read_source_table':(data.SourceTable,False,'按明确页签和header_rows读取有坐标证据的表格预览；需模型内容读取授权；完整数据由后续流程直接读取'),
 'register_data_workflow':(data.RegisterWorkflow,True,'校验并登记项目JSON加工方案：version=1,plan为类型化输入/依赖步骤/output,bindings绑定source_id或result_id，可选contract；不等于已执行'),
 'inspect_data_workflow':(data.WorkflowTarget,False,'查看可编辑加工方案、正式与草稿版本及latest_run.workflow_failure安全诊断；失败按next_step修复，不能将失败运行当结果分页读取'),
 'run_data_workflow':(data.WorkflowTarget,True,'冻结并排队执行多来源加工方案，逐步验证依赖、精度、完整行数和契约；失败保留旧正式结果；等待真实回执'),
 'read_data_workflow_result':(ResultPage,False,'读取当前项目完整结果的有界分页，含精确值、语义、控制校验与完整交接引用；预览页不是全量交接'),
 'publish_data_workflow_result':(data.PublishWorkflow,True,'仅用户当前明确确认时采用已验证候选；若用户给出确认采用数据结果：运行 <run_id>，方案 <sha256>，参数必须与该运行和方案逐字一致；之后同方案刷新可自动替换'),
 'export_data_workflow_result':(data.ExportResult,False,'准备完整Excel、CSV与交接包下载，保留已声明业务列名；按text_exact保留Excel无法精确保存的数值并标注，绝不截断为样本'),
 'schedule_data_workflow':(WorkflowSchedule,True,'按用户本轮明确的每日/每周/每月指定日/每N小时计划调度正式方案；watch_sources需用户明确源表变化后刷新；不是数据库CDC'),
 'propose_semantic_alignment':(Proposal,True,'根据名称/别名/模型候选生成字段语义匹配提案，校验单位、期间、粒度；全部保留人工核对，不得代用户审批'),
 'propose_value_mapping':(Mapping,True,'保存枚举值/最小粒度映射候选，拒绝无分配规则的一对多，未知枚举不得默默丢弃；人工核对后方可称已确认'),
 'inspect_semantic_proposal':(Id,False,'查看语义匹配或枚举映射的全部定义、版本和真实核对状态；目录标签不能被当作批准指令'),
 'semantic_catalog_guide':(Empty,False,'获取语义包完整定义结构、真实来源要求和核阅流程；由Agent编写草稿，只有已鉴权所有者可批准或退休'),
 'list_semantic_catalogs':(SemanticCatalogPage,False,'分页查找当前项目完整语义包，默认只列已批准版本；定义读取需模型内容授权，approved仅代表owner已核阅'),
 'inspect_semantic_catalog':(SemanticCatalogTarget,False,'读取当前项目语义包完整字段、真实来源、有效日期、摘要和核阅状态；需模型内容授权，草稿及退休版本不可作为批准目录'),
 'propose_semantic_catalog':(SemanticCatalogProposal,True,'依据项目已登记原件提出不可变语义包草稿；Agent不能批准或退休，也不能把草稿称为可信目录'),
 'propose_catalog_alignment':(CatalogAlignment,True,'按明确业务适用日as_of_date和固定摘要，从服务器已批准语义包生成字段匹配提案；不可提交自选targets，模型不能代用户核阅'),
}


def operation(turn,name):
    from .conversation import stable
    trace=get('turn',turn['id']).get('agent_trace',[])
    step=trace[-1]['id'] if trace else name
    return stable('dataop_',turn.get('agent_origin',turn['id']),step)


def _workflow_receipt(pid,r,meta,*,reused=False):
    receipt={'run_id':r['id'],'workflow_id':r['target'],'plan_sha256':r['commit'],'records':meta['records'],'technical_validation':True,
        'executed':not reused,'reused_existing_run':reused,
        'executed_at':r.get('started_at'),'execution_finished_at':r.get('finished_at'),
        'requires_semantic_review':meta.get('requires_semantic_review',False),'stage_receipts':'完整交接包中保存每步回执',
        'review_url':'/data-workbench.html?project='+pid+'&workflow='+r['target'],
        'download':{'label':'完整数据交接包.zip','url':'/api/projects/'+pid+'/data-results/'+r['id']+'/download?format=handoff'}}
    try:data.ensure_candidate_bulk_allowed(pid,r['id'])
    except ValueError as error:
        receipt.pop('download',None)
        receipt['download_blocked_reason']=str(error)
        receipt['next_step']='候选结果仅供分页核对；业务语义未对齐前不能下载完整结果或当作正式成果。'
    else:
        if reused:receipt['next_step']='本任务已有此方案和来源版本的完整已验证结果；需要时读取该run_id、导出或交由用户明确确认采用，不必再次运行相同方案。'
    return receipt


def execute(turn,name,args):
    pid=turn['project_id']
    from . import provider
    if name=='semantic_catalog_guide':
        from .semantic_catalog import CatalogDefinition
        return {'schema':CatalogDefinition.model_json_schema(),
                'instructions':'先读取本项目已登记文件，使用真实source_id、原件content_hash和locator；语义字段需明确含义、单位、粒度、期间与聚合。创建只产生draft，由已鉴权owner在核阅界面批准固定摘要。匹配时as_of_date必须来自明确业务期间的适用日，不明时询问用户，不能默认今天；日期须落在包有效期内。批准或退休不是Agent工具能力。'}
    if name in ('list_semantic_catalogs','inspect_semantic_catalog','propose_catalog_alignment'):
        if not provider.settings().get('share_data'):raise ValueError('读取语义包完整定义需要启用模型读取项目内容')
        from . import semantic_catalog
        if name=='list_semantic_catalogs':return semantic_catalog.list_catalogs(pid,**SemanticCatalogPage.model_validate(args).model_dump())
        if name=='inspect_semantic_catalog':return semantic_catalog.get_catalog(pid,args['catalog_id'])
        from .semantic_service import propose_from_catalog
        from .intents import require
        require(turn,name)
        return propose_from_catalog(pid,args['source'],args['catalog_id'],args['expected_catalog_sha256'],
                                    args.get('model_candidates',[]),as_of_date=args['as_of_date'])
    if name=='propose_semantic_catalog':
        from .semantic_catalog import propose
        from .intents import require
        require(turn,name)
        return propose(pid,args['definition'],'agent_proposal')
    if name=='business_report_guide':
        from .business_reports import ReportRecipe
        return {'schema':ReportRecipe.model_json_schema(),'instructions':'先inspect_business_report_input获得真实schema_sha256与字段，Agent编写reports/*.json。用户说明业务问题即可。value要求唯一一行；sum要求可加字段；ratio/variance需明确比较字段、零分母和舍入。'}
    if name=='inspect_business_report_input':
        from .business_reports import inspect_result
        return inspect_result(pid,args['run_id'])
    if name=='list_business_reports':
        from .business_reports import list_reports
        return {'items':list_reports(pid)}
    if name=='render_business_report':
        from .business_reports import render_saved
        result=render_saved(pid,args['run_id'],args['recipe_path'])
        url=result['download_url']
        patch('turn',turn['id'],agent_download={'label':'业务简报.docx','url':url+'?format=docx'})
        return dict(result,downloads=[{'label':'业务简报.docx','url':url+'?format=docx'},
                                     {'label':'汇报材料.pptx','url':url+'?format=pptx'},
                                     {'label':'数值依据.json','url':url+'?format=manifest'}])
    if name=='data_workflow_guide':
        from .workflow_guidance import guide
        return guide()
    if name=='list_data_workflows':
        from .workflow_guidance import catalog
        return catalog(pid)
    if name=='inspect_source_tables':return data.inspect_source(pid,args['source_id'])
    if name=='read_source_table':
        if not provider.settings().get('share_data'):raise ValueError('需要启用模型读取项目内容，才能读取表格行值或公式证据')
        return data.preview_source(pid,args)
    if name=='register_data_workflow':return data.register(pid,args['path'])
    if name=='inspect_data_workflow':return data.inspect_workflow(pid,args['workflow_id'])
    if name=='run_data_workflow':
        from .intents import require
        require(turn,'analyze');require(turn,'refresh')
        r=data.enqueue_workflow(pid,args['workflow_id'],scheduled_key=operation(turn,name),originating_turn_id=turn['id'])
        if r.get('reused_verified_result') is True:
            _,meta=data.result_folder(pid,r['id'])
            return _workflow_receipt(pid,r,meta,reused=True)
        _wait_for_job(turn,state='waiting_data_workflow',data_workflow_job=r['id'],progress='正在读取完整来源并逐步校验加工结果')
        return None
    if name=='read_data_workflow_result':
        if not provider.settings().get('share_data'):raise ValueError('需要启用模型读取项目内容才能向模型传递结果行值')
        from .jobs import get_job
        run=get_job(args['run_id'])
        if run.get('project_id')!=pid or run.get('kind')!='data_workflow':raise ValueError('结果不属于当前项目')
        if run['status'] in ('failed','cancelled'):
            diagnostic=data.workflow_run_diagnostic(pid,run['target'],run['id'])
            raise ValueError('该运行没有已验证结果，不能按结果分页读取；请用inspect_data_workflow查看流程 '+diagnostic['workflow_id']+' 的latest_run.workflow_failure，修复草稿后重新登记运行')
        return data.read_result(pid,args['run_id'],args['offset'],args['limit'])
    if name=='publish_data_workflow_result':
        bound=re.fullmatch(r'确认采用数据结果[：:]\s*运行\s+([A-Za-z0-9_-]{1,64})[，,]\s*方案\s+([a-f0-9]{64})[。！!\s]*',turn['text'])
        if bound:
            if bound.groups()!=(args['run_id'],args['expected_plan_sha256']):
                raise ValueError('本轮确认的运行或方案版本与待采用结果不一致，请重新核对')
        elif not re.fullmatch(r'确认采用数据结果[。！!\s]*',turn['text']):
            raise ValueError('请先打开结果核对页并点击采用，或明确回复确认采用数据结果')
        return data.publish_result(pid,args['run_id'],args['expected_plan_sha256'])
    if name=='export_data_workflow_result':
        data.export_result(pid,args['run_id'],args['precision_policy'])
        return {'run_id':args['run_id'],'download':{'label':'完整数据结果.xlsx','url':'/api/projects/'+pid+'/data-results/'+args['run_id']+'/download?format=xlsx'},
                'csv_download':{'label':'完整数据结果.csv','url':'/api/projects/'+pid+'/data-results/'+args['run_id']+'/download?format=csv'},
                'complete_handoff':'/api/projects/'+pid+'/data-results/'+args['run_id']+'/download?format=handoff'}
    if name=='schedule_data_workflow':
        from .cadence import parse
        cadence=parse(turn.get('work_request') or turn['text'],embedded=True)
        watch=args['watch_sources']
        if watch and not re.search(r'源表.*(?:变化|刷新|更新)|数据.*变化.*刷新',turn['text']):raise ValueError('需用户明确要求源表变化后刷新')
        if not cadence and not watch:raise ValueError('需要明确刷新计划或源表变化后的刷新要求')
        return data.schedule(pid,args['workflow_id'],cadence['cron'] if cadence else None,watch)
    if name=='propose_semantic_alignment':
        from .semantic_service import propose
        return propose(pid,args['source'],args['catalog'],args['model_candidates'])
    if name=='propose_value_mapping':
        from .semantic_service import propose_mapping
        return propose_mapping(pid,args['mapping'])
    if name=='inspect_semantic_proposal':
        from .semantic_service import get_proposal
        if not provider.settings().get('share_data'):raise ValueError('完整定义可能含原始枚举值，需启用模型读取项目内容')
        return get_proposal(pid,args['proposal_id'])
    raise ValueError('未知数据工作工具')


def resume(turn):
    if turn.get('state')!='waiting_data_workflow':return False
    from .jobs import get_job,ACTIVE
    from .agent import observe
    r=get_job(turn['data_workflow_job'])
    if r.get('project_id')!=turn['project_id'] or r.get('kind')!='data_workflow':raise ValueError('加工任务不属于当前项目')
    if r['status'] in ACTIVE:return True
    if r['status'] not in ('validated','succeeded'):
        pending=(turn.get('agent_trace') or [{}])[-1]
        expected=pending.get('arguments',{}).get('workflow_id')
        if pending.get('tool')!='run_data_workflow' or expected!=r.get('target'):
            raise ValueError('加工任务与当前待处理步骤不一致，未返回诊断')
        diagnostic=data.workflow_run_diagnostic(turn['project_id'],expected,r['id'])
        observe(turn,dict(diagnostic,error='数据加工未完成验收，之前正式结果保留',error_code=diagnostic['workflow_failure']['code']),True);return True
    folder,meta=data.result_folder(turn['project_id'],r['id'])
    observe(turn,_workflow_receipt(turn['project_id'],r,meta))
    return True

from .project_reports import FetchReportInput,ReportFetchTarget
DEFINITIONS.update({
 'list_report_sources':(Empty,False,'查看本项目管理员已配置的授权报表导出源；不返回密钥、登录态或任意网站操作能力'),
 'acquire_report_data':(FetchReportInput,True,'按本轮明确筛选条件获取已配置报告的完整导出，校验完成/格式/行数/筛选证据；后台排队，不能把请求成功等同导出成功'),
 'read_report_acquisition':(ReportFetchTarget,False,'查看真实报表获取任务与来源登记回执，区分requested/verified筛选与上下文，不访问其他项目'),
})
_previous_execute=execute

def execute(turn,name,args):
    if name=='list_report_sources':
        from .project_reports import list_connectors
        return {'items':list_connectors(turn['project_id'])}
    if name=='acquire_report_data':
        from .project_reports import enqueue_fetch
        from .intents import require
        require(turn,'connect');require(turn,'refresh')
        result=enqueue_fetch(turn['project_id'],args['connector_id'],args['filters'],args['context'],
            operation_id=operation(turn,name),originating_turn_id=turn['id'])
        _wait_for_job(turn,state='waiting_report_fetch',report_fetch_job=result['id'],progress='正在获取并校验完整报表导出')
        return None
    if name=='read_report_acquisition':
        from .project_reports import read_fetch
        return read_fetch(turn['project_id'],args['run_id'])
    return _previous_execute(turn,name,args)

_previous_resume=resume

def resume(turn):
    if turn.get('state')!='waiting_report_fetch':return _previous_resume(turn)
    from .project_reports import read_fetch
    from .jobs import ACTIVE
    from .agent import observe
    value=read_fetch(turn['project_id'],turn['report_fetch_job'])
    if value['status'] in ACTIVE:return True
    if value['status']=='succeeded' and value.get('source_id'):observe(turn,value)
    else:observe(turn,{'error':'报告获取未完成，未登记不完整数据','error_code':value.get('error_code'),'run_id':value['id']},True)
    return True


class DocumentRead(SourceId):
    offset:int=Field(default=0,ge=0)
    limit:int=Field(default=12000,ge=1,le=20000)
class FactProposal(SourceId):facts:list[dict]=Field(min_length=1,max_length=500)
class FactTarget(StrictModel):facts_id:str=Field(min_length=1,max_length=64)
class FactAlignment(FactTarget):
    catalog:list[dict]=Field(min_length=1,max_length=500)
    aggregation_by_measure:dict=Field(default_factory=dict)

DEFINITIONS.update({
 'read_document_data_evidence':(DocumentRead,False,'读取项目文本、静态HTML或文字层PDF的可核对原文偏移和摘要；需内容授权；图片/图表没有OCR时不能编造数值'),
 'propose_extracted_data_facts':(FactProposal,True,'提交数值/含义/单位/期间/维度及逐字原文偏移，检查完整数字证据后保存待核对事实；不等于已验证语义或已批准'),
 'inspect_extracted_data_facts':(FactTarget,False,'读取含原文证据的数值事实提案和真实核对状态，需模型内容读取授权'),
 'align_extracted_data_facts':(FactAlignment,False,'将已核对数值事实与已有语义目录提出对齐候选，保留单位/期间/维度和出处；不能把候选当已审批'),
})
_prior_execute_with_reports=execute

def execute(turn,name,args):
    if name in ('read_document_data_evidence','propose_extracted_data_facts','inspect_extracted_data_facts','align_extracted_data_facts'):
        from .provider import settings
        if not settings().get('share_data'):raise ValueError('原文与数值证据需要启用模型读取项目内容')
        from . import fact_service as facts
        pid=turn['project_id']
        if name=='read_document_data_evidence':return facts.read_document(pid,args['source_id'],args['offset'],args['limit'])
        if name=='propose_extracted_data_facts':return facts.propose_facts(pid,args['source_id'],args['facts'])
        if name=='inspect_extracted_data_facts':return facts.get_facts(pid,args['facts_id'])
        return facts.align_facts(pid,args['facts_id'],args['catalog'],args['aggregation_by_measure'])
    return _prior_execute_with_reports(turn,name,args)

DEFINITIONS['extract_indented_source']=(data.IndentedSource,True,'按明确列名提取制表符/多空格分隔的缩进文本表，完整保留层级候选与行号，创建可加工来源；布局层级仍需核对业务语义')
_execute_with_facts=execute

def execute(turn,name,args):
    if name=='extract_indented_source':
        from .provider import settings
        if not settings().get('share_data'):raise ValueError('需要启用模型读取项目内容')
        return data.extract_indented_source(turn['project_id'],args['source_id'],args['columns'])
    return _execute_with_facts(turn,name,args)
