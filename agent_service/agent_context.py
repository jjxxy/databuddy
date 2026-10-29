"""Demand-loaded tools and recoverable context; summaries are never evidence."""
import hashlib,json,re
from pathlib import PurePosixPath
from pydantic import Field
from .models import StrictModel
from .store import get,patch

class DiscoverTools(StrictModel):
    query:str=Field(min_length=1,max_length=300)
    limit:int=Field(default=6,ge=1,le=8)

class ToolResult(StrictModel):
    step_id:str=Field(min_length=1,max_length=64)
    source_turn_id:str|None=Field(default=None,min_length=1,max_length=64,
        description='历史回执所属轮次；从read_context_original提供的引用逐字使用。省略时仅查本任务恢复链，不搜索其他任务。')
    offset:int=Field(default=0,ge=0,le=2000000)
    limit:int=Field(default=6000,ge=1,le=10000)

class InvalidRecoveryReference(ValueError):
    code='RECOVERY_REFERENCE_INVALID'

def recovery_page(kind,resource_id,text,offset,limit):
    content=text[offset:offset+limit];end=offset+len(content)
    return {'kind':kind,'resource_id':resource_id,
            'version_sha256':hashlib.sha256(text.encode()).hexdigest(),
            'offset_unit':'characters',
            'offset':offset,'returned_characters':len(content),'total_characters':len(text),
            'next_offset':end if end<len(text) else None}

class FileSearch(StrictModel):
    query:str=Field(min_length=1,max_length=300)
    path_glob:str=Field(default='*',max_length=100)
    offset:int=Field(default=0,ge=0,le=1000)
    limit:int=Field(default=20,ge=1,le=40)

class FileEdit(StrictModel):
    path:str=Field(min_length=1,max_length=200)
    expected_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    old_text:str=Field(min_length=1,max_length=64000)
    new_text:str=Field(max_length=64000)

CORE={'project_context','set_task_plan','load_workflow','business_rules','search_project_history','discover_tools','read_tool_result','list_data_applications','list_data_workflows','list_work_connectors','discover_skills','recall_memories','list_agent_peers'}
# A working set is model context, not an authorization grant. The complete
# allowed catalog remains discoverable, with unchanged server-side validators.
ESSENTIAL={'project_context','set_task_plan','business_rules','discover_tools','read_tool_result'}
OFFICE_WORKING_SET=ESSENTIAL|{'source_schema','read_source_table','write_file','render_office_artifact'}
INLINE_SCHEMA_CHARACTERS=2500
AUTO_SCHEMA_CHARACTERS=8000
DATA_WORKING_SET=('source_schema','inspect_source_tables','read_source_table','data_workflow_guide',
                  'write_file','read_file','register_data_workflow','run_data_workflow',
                  'read_data_workflow_result','export_data_workflow_result','list_data_workflows')
GROUPS=[
 (r'代码|编程|脚本|终端|Python|python|Node|node|运行|执行|图表|可视化|网页|工作环境',{'work_environment_status','run_work_command','list_work_files','read_work_file','publish_work_file'}),
 (r'协作|协同|委派|分工|复核|核对|独立检查|Agent|agent',{'list_agent_peers','delegate_agents','get_agent_task','cancel_agent_task'}),
 (r'记住|记忆|偏好|忘记|以后|约定',{'recall_memories','remember_memory','forget_memory'}),
 (r'上下文|压缩|之前|历史|继续|context',{'compact_work_context','read_context_original','read_context_checkpoint'}),
 (r'技能|SKILL|Skill|skill|方法|SOP',{'discover_skills','load_skill','read_skill_file'}),
 (r'本地|文件夹|电脑|工作空间',{'local_workspace_status','export_local_workspace'}),
 (r'语义|口径|语义包|数据定义|标准目录|字段含义|单位|粒度|semantic|catalog',{'semantic_catalog_guide','list_semantic_catalogs','inspect_semantic_catalog','propose_semantic_catalog','propose_catalog_alignment','inspect_semantic_proposal'}),
 (r'月报|经营|预算|汇报|小结|财经|财务|差异|同比|环比',{'list_business_reports','business_report_guide','inspect_business_report_input','render_business_report','list_data_workflows','data_workflow_guide','write_file','read_file'}),
 (r'非结构|文字|文档|PDF|pdf|图形|图表.*提取|缩进|数值.*提取',{'extract_indented_source','read_document_data_evidence','propose_extracted_data_facts','inspect_extracted_data_facts','align_extracted_data_facts'}),
 (r'网页|报表|报告.*(?:获取|导出|下载)|iframe|筛选器|浏览器',{'list_report_sources','acquire_report_data','read_report_acquisition'}),
 (r'表格|Excel|excel|页签|合并|公式|粒度|语义|加工|重分类|拼接|SQL|sql|底稿|工作流',{'inspect_source_tables','read_source_table','register_data_workflow','inspect_data_workflow','run_data_workflow','read_data_workflow_result','export_data_workflow_result','propose_semantic_alignment','propose_value_mapping','inspect_semantic_proposal','write_file','read_file'}),
 (r'血缘|资产|指标|口径|数据质量|验收|契约|失败|对账',{'data_asset_catalog','inspect_data_quality','inspect_column_lineage','inspect_metric','confirm_metric','read_file','write_file','source_schema'}),
 (r'Word|Excel|PPT|word|excel|ppt|文档|演示|幻灯片|工作簿|docx|xlsx|pptx',{'write_file','render_office_artifact','load_workflow','read_file'}),
 (r'系统|邮件|日历|会议|任务|工单|发送|检索|搜索|MCP|连接',{'list_work_connectors','list_work_tools','read_work_tool','prepare_work_action','read_work_action'}),
 (r'文件|报告|代码|脚本|\.md|\.py|搜索文件|查找文件',{'list_files','read_file','search_files','edit_file','write_file','delete_file','versions','run_python_file'}),
 (r'应用|分析|汇总|整理|筛选|看板|报表|数据',{'source_schema','source_preview','create_data_application','build_transform','revise_data_application','application_info','view_result','query_sql','ask_data_application'}),
 (r'重跑|刷新|重新运行',{'run_data_application','refresh_data','application_info','view_result'}),
 (r'发文|制度|内部.*文档|公司.*规定',{'company_search_status','search_company_documents','read_company_document'}),
 (r'每天|定时|调度|更新|暂停|每周|每月|工作日',{'schedule_updates','schedule_daily','pause_updates','project_health','schedule_data_workflow'}),
 (r'发布|确认',{'publish_result','view_result'}),
 (r'多少|合计|平均|排名|查询',{'application_info','query_sql','ask_data_application','view_result'}),
 (r'记住|约定|口径',{'remember_business_rule'}),
 (r'导出|下载',{'export_result'}),
 (r'改名|重命名',{'rename_project'}),
 (r'切换',{'switch_source'}),
]

def allowed(turn):
    from .agent import TOOLS
    from .isolation import status
    names={name for name,(_,writes,_) in TOOLS.items() if (turn.get('interaction_mode','work')=='work' or not writes) and (name!='run_python_file' or status()['configured'])}
    from .work_runtime import status as work_status, DEFINITIONS as WORK_RUNTIME_TOOLS
    if not work_status()['configured']:
        names -= set(WORK_RUNTIME_TOOLS) - {'work_environment_status'}
    from . import agent_gateway,agent_team
    return agent_team.allowed_tools(turn,agent_gateway.allowed_tools(turn,names))

def definition(name):
    from .agent import TOOLS
    schema,_,description=TOOLS[name]
    return {'name':name,'description':description,'arguments_schema':schema.model_json_schema()}

def selected(turn):
    text=turn.get('work_request',turn['text'])
    retained=set(turn.get('discovered_tools',[])[-6:])
    retained.update(s['tool'] for s in turn.get('agent_trace',[])[-8:])
    office_task=(re.search(r'Word|Excel|PPT|PowerPoint|docx|xlsx|pptx|工作簿|幻灯片|演示文稿',text,re.I)
                 and re.search(r'创建|生成|写|做|制作|导出|\b(?:create|generate|export|write|make)\b',text,re.I))
    data_task=re.search(r'汇总|对账|数据加工|工作流|底稿|库存|销售|人事|财务|金额|订单|CSV|SQL',text,re.I)
    document_output=re.search(r'(?:创建|生成|写|做|制作|导出|create|generate|export|write|make)[^。；;\n]{0,24}(?:Word|PPT|PowerPoint|docx|pptx|文档|幻灯片|演示文稿)',text,re.I)
    if office_task and (not data_task or document_output):
        # "Excel + report + source + verify" used to union unrelated application,
        # report-acquisition, semantic and delegation schemas (~40k characters).
        # Start with document/source tools; data-workflow/report capabilities are
        # still in the catalog and are loaded in full when the agent needs them.
        names=sorted(OFFICE_WORKING_SET)
    elif data_task:
        # A data task needs a small executable workflow frontier. Keywords such
        # as "口径、核对、报告、文件" must not union every unrelated subsystem.
        # The complete authorized catalog remains available through discovery.
        names=list(DATA_WORKING_SET)
    else:
        names=[]
        for pattern,group in GROUPS:
            if re.search(pattern,text):names.extend(sorted(group))
    permitted=allowed(turn)
    forced=ESSENTIAL|retained|{name for name in permitted if name in text}
    if turn.get('application_id'):forced.update({'application_info','view_result','ask_data_application'})
    selected_names=forced & permitted
    used=sum(len(json.dumps(definition(name),ensure_ascii=False)) for name in selected_names)
    for name in dict.fromkeys(names):
        if name in selected_names or name not in permitted:continue
        size=len(json.dumps(definition(name),ensure_ascii=False))
        if size<=INLINE_SCHEMA_CHARACTERS and used+size<=AUTO_SCHEMA_CHARACTERS:
            selected_names.add(name);used+=size
    return sorted(selected_names)

def discover(turn,value):
    from .agent import TOOLS
    query=value.query.lower();words=re.findall(r'[a-z_]{2,}|[\u4e00-\u9fff]{2}',query)
    names=allowed(turn)
    explicit=[(match.start(),name) for name in names
              if (match:=re.search(r'(?<![a-z0-9_])'+re.escape(name)+r'(?![a-z0-9_])',query))]
    if explicit:matches=[name for _,name in sorted(explicit)][:value.limit]
    else:
        ranked=sorted(names,key=lambda n:(sum(5 if w in n else 1 for w in words if w in n+' '+TOOLS[n][2]),n),reverse=True)
        matches=[n for n in ranked if any(w in n+' '+TOOLS[n][2] for w in words)][:value.limit]
        if not matches:matches=[n for n in ranked if n not in CORE][:value.limit]
    current=get('turn',turn['id'])
    focus=list(dict.fromkeys(current.get('discovered_tools',[])+matches))[-16:]
    patch('turn',turn['id'],discovered_tools=focus)
    return {'tools':[definition(n) for n in matches],'next_step':'下一步可调用这些工具；目录匹配不代表已经执行'}

def row_scope(value,tool):
    """Keep authoritative counts outside a potentially truncated row excerpt."""
    if tool not in ('source_schema','source_preview','read_source_table','inspect_source_tables') or not isinstance(value,dict):return None
    scope={}
    if value.get('source_id'):scope['source_id']=value['source_id']
    profile=value.get('profile')
    if isinstance(profile,dict) and type(profile.get('sample_rows')) is int:
        scope.update(profile_sample_rows=profile['sample_rows'],profile_statistics_scope='sample_only')
    if value.get('complete_source_read') is True and type(value.get('total')) is int:
        returned=len(value['rows']) if isinstance(value.get('rows'),list) else None
        scope.update(source_total_rows=value['total'],source_total_scope='complete_source_count',
                     preview_rows_returned=returned,preview_contains_all_rows=returned==value['total'])
    return scope if len(scope)>1 else None


def source_context(turn,clean):
    from .workbook_reader import LAYOUT_SELECTION_CODES
    entries={}
    for identifier in turn.get('source_ids',[]) or ([turn['source_id']] if turn.get('source_id') else []):
        try:source=get('source',identifier)
        except KeyError:continue
        if source.get('project_id')!=turn['project_id']:continue
        entries[identifier]={'source_id':identifier,'status':source.get('status'),
            'error_code':source.get('error_code'),
            'reading_boundary_required':source.get('status')=='failed' and source.get('error_code') in LAYOUT_SELECTION_CODES}
    for step in turn.get('agent_trace',[]):
        scope=row_scope(clean(step.get('observation')),step.get('tool'))
        if scope and scope.get('source_id'):
            entries.setdefault(scope['source_id'],{'source_id':scope['source_id']}).update(scope)
    return list(entries.values())


def _same_json(left,right):
    # Python equality alone conflates financial metadata values such as 1 and
    # True. Compare the exact model-facing JSON types as well as ordered values.
    encode=lambda value:json.dumps(value,ensure_ascii=False,sort_keys=True,default=str,separators=(',',':'))
    return encode(left)==encode(right)


def observation_view(value,tool):
    """Losslessly reference exactly repeated metadata; never summarize facts."""
    if not isinstance(value,dict):return value
    if tool in ('inspect_data_workflow','register_data_workflow') and isinstance(value.get('document'),dict):
        document=value['document'];plan=document.get('plan',{});inputs=value.get('inputs');view=dict(value)
        if (isinstance(inputs,dict) and set(inputs)==set(document.get('bindings',{}))==set(plan.get('inputs',{}))
                and all(isinstance(item,dict) and set(item)=={'binding','schema'}
                        and _same_json(item['binding'],document['bindings'][key])
                        and _same_json(item['schema'],plan['inputs'][key]) for key,item in inputs.items())):
            view.pop('inputs');view['inputs_reference']={'binding':'#/document/bindings/<alias>','schema':'#/document/plan/inputs/<alias>',
                'exact_values_already_present':True}
        if isinstance(value.get('steps'),list) and _same_json(value['steps'],plan.get('stages')):
            view.pop('steps');view['steps_reference']='#/document/plan/stages'
        return view
    if tool=='inspect_source_tables':
        sheets=value.get('sheets');candidates=value.get('candidate_regions')
        if (isinstance(sheets,list) and isinstance(candidates,list) and candidates
                and all(isinstance(sheet,dict) and isinstance(sheet.get('candidate_regions'),list) for sheet in sheets)):
            combined=[candidate for sheet in sheets for candidate in sheet['candidate_regions']]
            reference={'concatenate':['#/sheets/'+str(i)+'/candidate_regions' for i in range(len(sheets))],
                       'order':'sheet_order','exact_values_already_present':True}
            if (_same_json(candidates,combined)
                    and len(json.dumps(reference,ensure_ascii=False))<len(json.dumps(candidates,ensure_ascii=False,default=str))):
                view=dict(value);view.pop('candidate_regions')
                view['candidate_regions_reference']=reference
                return view
        return value
    if tool!='read_source_table':return value
    evidence=value.get('evidence')
    if isinstance(evidence,dict) and value.get('fields') and _same_json(evidence.get('fields'),value['fields']):
        evidence=dict(evidence);evidence.pop('fields')
        evidence['fields_reference']='#/fields'
        return dict(value,evidence=evidence)
    return value


def trace_context(turn,clean):
    trace=turn.get('agent_trace',[]);values=[]
    working={};versions={}
    for step in trace:
        raw=step.get('observation');r=raw.get('recovery') if isinstance(raw,dict) else None
        if step.get('status')=='done' and isinstance(r,dict) and raw.get('executed') is not False:
            versions[(r.get('kind'),r.get('resource_id'))]=r.get('version_sha256')
    for i,step in enumerate(trace):
        raw=step.get('observation');recovery=raw.get('recovery') if isinstance(raw,dict) else None
        if (step.get('status')=='done' and isinstance(recovery,dict) and raw.get('executed') is not False
                and recovery.get('version_sha256')==versions.get((recovery.get('kind'),recovery.get('resource_id')))):
            working[('recovery',recovery.get('kind'),recovery.get('resource_id'),recovery.get('version_sha256'),
                     recovery.get('offset'),recovery.get('returned_characters'))]=i
        if step.get('tool') in ('inspect_data_workflow','register_data_workflow') and isinstance(raw,dict) and raw.get('workflow_id'):
            working[('workflow',raw['workflow_id'])]=i
    retained=set(working.values())
    for i,step in enumerate(trace):
        sid=step.get('id','index_'+str(i));raw=clean(step.get('observation'))
        view=observation_view(raw,step['tool'])
        encoded=json.dumps(view,ensure_ascii=False,default=str)
        args=dict(step.get('arguments',{}))
        for key in ('content','old_text','new_text'):
            if key in args:args[key]={'characters':len(str(args[key])),'sha256':hashlib.sha256(str(args[key]).encode()).hexdigest(),'recover':'读取项目文件，不重复写入'}
        observation=view
        recent=i>=len(trace)-2
        # Canonical authoring schemas are reference material, not sampled
        # business data. Chopping a requested schema into JSON string fragments
        # caused the agent to re-read the same 17k guide in multiple 10k pages.
        canonical=step['tool'] in ('data_workflow_guide','business_report_guide','semantic_catalog_guide','discover_tools') and isinstance(raw,dict) and (isinstance(raw.get('schema'),dict)
            or isinstance(raw.get('tools'),list) and all(isinstance(t,dict) and 'arguments_schema' in t for t in raw['tools']))
        # A generic file write may only save notes or a plan. It cannot prove
        # that a requested authoring schema is no longer needed. Keep that
        # exact reference through this task; native token-window compaction
        # handles history when the actual model context eventually fills.
        # Structural table catalogs and explicit result pages are already
        # bounded. A second text cut hid their own original-step/next-offset
        # fields and pointed the model at a page-of-a-page recovery loop.
        bounded_structure=step['tool'] in ('inspect_source_tables','read_tool_result')
        # A small returned table stays a usable table: preserve every value,
        # field label, coordinate/formula/format warning and completeness flag.
        # Full backend observations remain unchanged and recoverable by ID.
        small_table=(step['tool']=='read_source_table' and isinstance(view,dict)
                     and isinstance(view.get('rows'),list)
                     and len(json.dumps(view['rows'],ensure_ascii=False,default=str))<=6000
                     and len(encoded)<=16000)
        small_observation=len(encoded)<=6000
        limit=len(encoded) if canonical or i in retained else 16000 if small_table else 6000 if small_observation else 24000 if recent and bounded_structure else 5000 if recent else 600
        if len(encoded)>limit:
            original=raw.get('step_id') if step['tool']=='read_tool_result' and isinstance(raw,dict) else sid
            observation={'context_excerpt':encoded[:limit],'omitted_characters':len(encoded)-limit,
                'recover_with':{'tool':'read_tool_result','step_id':original or sid,
                                'source_turn_id':raw.get('source_turn_id',turn['id']) if isinstance(raw,dict) else turn['id'],'offset':0},'not_full_result':True}
            if step['tool'] in ('read_context_original','read_context_checkpoint'):
                observation['recover_with']={'tool':step['tool'],**args}
        receipt={'step_id':sid,'tool':step['tool'],'arguments':args,'status':step['status'],'observation':observation}
        if step['tool']=='read_tool_result' and isinstance(raw,dict):
            receipt['original_result']={'step_id':raw.get('step_id') or args.get('step_id'),
                'offset':raw.get('offset',0),'next_offset':raw.get('next_offset'),
                'total_characters':raw.get('total_characters'),
                'offset_scope':'absolute original result; never the recovery-page envelope'}
        scope=row_scope(raw,step['tool'])
        if scope:receipt['row_scope']=scope
        values.append(receipt)
    return values

def read_result(turn,value):
    from .agent import public_observation
    target=value.step_id;aliases=[];owner=turn
    if value.source_turn_id:
        from .context_compaction import authorized_history_turn
        owner,_,active=authorized_history_turn(turn,value.source_turn_id)
        if not active:raise ValueError('历史内容已撤回或不适用于当前用户，不能恢复旧工具回执')
    for _ in range(16):
        if (owner['id'],target) in aliases:raise InvalidRecoveryReference('工具结果恢复引用形成循环，请使用原始工具步骤')
        current=owner;visited=set();step=None
        while current['id'] not in visited:
            visited.add(current['id'])
            if current['project_id']!=turn['project_id']:raise ValueError('工具结果不属于本项目')
            step=next((s for i,s in enumerate(current.get('agent_trace',[])) if s.get('id','index_'+str(i))==target),None)
            if step:break
            previous=current.get('resumed_from')
            if not previous or len(visited)>=50:break
            try:current=get('turn',previous)
            except KeyError:raise InvalidRecoveryReference('任务恢复链已不可用，请使用仍有效的来源引用') from None
        if not step:raise InvalidRecoveryReference('本任务及其恢复链没有该工具结果；不能读取其他任务或项目，历史回执须使用附带的source_turn_id')
        if step['tool']!='read_tool_result':break
        # A page is an alias, not another serialized financial result. Offsets
        # always address the original result; the same task/project and all
        # existing privacy filters still apply at every lookup.
        aliases.append((current['id'],target))
        reference=ToolResult(**dict(step.get('arguments',{}),offset=value.offset,limit=value.limit))
        target=reference.step_id;owner=current
        if reference.source_turn_id:
            from .context_compaction import authorized_history_turn
            owner,_,active=authorized_history_turn(turn,reference.source_turn_id)
            if not active:raise ValueError('历史内容已撤回或不适用于当前用户，不能恢复旧工具回执')
    else:raise InvalidRecoveryReference('工具结果恢复引用过深，请使用原始工具步骤')
    if current['id']!=turn['id']:
        from .context_compaction import authorized_history_turn
        _,_,active=authorized_history_turn(turn,current['id'])
        if not active:raise ValueError('历史内容已撤回或不适用于当前用户，不能恢复旧工具回执')
    if step['tool']=='read_context_original':
        from .context_compaction import read_original
        replay=read_original(turn,**step['arguments'])
        content=replay['content'][value.offset:value.offset+value.limit]
        recovery=dict(replay['recovery'],offset=replay['offset']+value.offset,returned_characters=len(content))
        end=recovery['offset']+len(content);recovery['next_offset']=end if end<recovery['total_characters'] else None
        return dict(replay,content=content,offset=recovery['offset'],next_offset=recovery['next_offset'],recovery=recovery,
            step_id=step.get('id'),source_turn_id=current['id'],requested_step_id=value.step_id,
            resolved_page_steps=[sid for _,sid in aliases],tool=step['tool'],status=step['status'],scope='saved_context_page_alias')
    if step['tool']=='read_context_checkpoint':
        from .context_compaction import read_checkpoint
        return dict(read_checkpoint(turn,**step['arguments']),step_id=step.get('id'),source_turn_id=current['id'],
            requested_step_id=value.step_id,resolved_page_steps=[sid for _,sid in aliases],tool=step['tool'],status=step['status'])
    if step['tool'] in ('remember_memory','forget_memory','recall_memories'):
        from .memory_service import context_for_turn
        observation=context_for_turn(turn)
    elif step['tool']=='compact_work_context':
        from .context_compaction import context_for_turn
        observation=context_for_turn(turn)
    else:observation=step.get('observation')
    text=json.dumps(public_observation(observation),ensure_ascii=False,default=str)
    end=value.offset+value.limit
    return {'step_id':target,'requested_step_id':value.step_id,'resolved_page_steps':[sid for _,sid in aliases],
        'tool':step['tool'],'status':step['status'],'format':'json','content':text[value.offset:end],
        'source_turn_id':current['id'],'total_characters':len(text),'offset':value.offset,'next_offset':end if end<len(text) else None,
        'recovery':recovery_page('tool_result','turn:'+current['id']+':step:'+target,text,value.offset,value.limit)}

def search_files(pid,value):
    from . import workspace
    from .provider import settings
    if not settings().get('share_data'):raise ValueError('搜索项目文件内容需启用模型项目内容读取')
    matches=[];scanned=0;truncated=False
    files=workspace.list_files(pid)
    for file in files:
        if not PurePosixPath(file['path']).match(value.path_glob):continue
        scanned+=1
        if scanned>100:truncated=True;break
        for line_number,line in enumerate(workspace.read_file(pid,file['path']).splitlines(),1):
            if value.query.casefold() in line.casefold():
                matches.append({'path':file['path'],'line':line_number,'text':line[:500]})
                if len(matches)>=value.offset+value.limit+1:truncated=True;break
        if len(matches)>=value.offset+value.limit+1:break
    return {'matches':matches[value.offset:value.offset+value.limit],'scanned_files':min(scanned,100),'complete_search':not truncated,'next_offset':value.offset+value.limit if len(matches)>value.offset+value.limit else None,'scope':'project_text_files; literal search, not semantic retrieval'}

def edit_file(pid,value,turn=None):
    from . import workspace
    from .provider import settings
    if not settings().get('share_data'):raise ValueError('精确修改前需允许模型读取项目文件')
    before=workspace.read_file(pid,value.path);digest=hashlib.sha256(before.encode()).hexdigest()
    effect_id=None
    if turn:
        from .store import put
        step_id=get('turn',turn['id'])['agent_trace'][-1]['id']
        effect_id='edit_'+hashlib.sha256((turn.get('agent_origin',turn['id'])+step_id).encode()).hexdigest()[:40]
        try:
            receipt=get('agent_effect',effect_id)
            if receipt['project_id']==pid and receipt['path']==value.path and digest==receipt['after_sha256']:return {k:receipt[k] for k in ('path','bytes','before_sha256','after_sha256')}
        except KeyError:pass
    if digest!=value.expected_sha256:raise ValueError('文件版本已变化，请重新读取后修改，未覆盖现有内容')
    start=before.find(value.old_text)
    if start<0 or before.find(value.old_text,start+1)>=0:raise ValueError('待替换片段必须在当前文件中唯一出现，请缩小并重新核对')
    result=before.replace(value.old_text,value.new_text,1)
    if effect_id:put('agent_effect',{'project_id':pid,'path':value.path,'bytes':len(result.encode()),'before_sha256':digest,'after_sha256':hashlib.sha256(result.encode()).hexdigest()},effect_id)
    receipt=workspace.write_file(pid,value.path,result)
    return dict(receipt,before_sha256=digest,after_sha256=hashlib.sha256(result.encode()).hexdigest())

def repetition(turn,step):
    signature=(step.tool,json.dumps(step.arguments,sort_keys=True,ensure_ascii=False))
    trace=turn.get('agent_trace',[])[turn.get('agent_budget_base',0):]
    count=0
    outcome=None
    for prior in reversed(trace):
        if (prior['tool'],json.dumps(prior['arguments'],sort_keys=True,ensure_ascii=False))!=signature:break
        if prior['status'] not in ('done','failed'):break
        observed=(prior['status'],prior.get('observation'))
        if count and observed!=outcome:break
        outcome=observed;count+=1
    if count>=2:return '同一操作和参数已连续执行两次，未再次执行。请读取已有结果、改变方法或说明阻碍'
    if step.tool=='discover_tools':return None
    # Discovery may change the model's information without changing business
    # state. Only identical, already-seen definitions/metadata are transparent;
    # a new schema or any other tool (read/load/write/input) breaks the streak.
    known={};last_metadata=None;count=0;failure=None
    for prior in trace:
        if prior.get('tool')=='discover_tools' and prior.get('status')=='done':
            value=prior.get('observation')
            if not isinstance(value,dict) or not isinstance(value.get('tools'),list):
                count=0;failure=None;continue
            if any(not isinstance(tool,dict) or not isinstance(tool.get('name'),str) for tool in value['tools']):
                count=0;failure=None;continue
            offered={tool['name']:json.dumps(tool,sort_keys=True,ensure_ascii=False,default=str) for tool in value['tools']}
            metadata=json.dumps({key:item for key,item in value.items() if key!='tools'},
                                sort_keys=True,ensure_ascii=False,default=str)
            novel=any(known.get(name)!=definition for name,definition in offered.items()) or metadata!=last_metadata
            known.update(offered);last_metadata=metadata
            if novel:count=0;failure=None
            continue
        prior_signature=(prior.get('tool'),json.dumps(prior.get('arguments',{}),sort_keys=True,ensure_ascii=False))
        value=prior.get('observation')
        if prior_signature==signature and prior.get('status')=='failed' and isinstance(value,dict) and value:
            count=count+1 if value==failure else 1
            failure=value
        else:count=0;failure=None
    if count>=2:
        return '同一操作和参数已返回相同失败，期间只重复查阅了未变化的工具说明；未再次执行。请修复输入或方案、改变方法，或说明当前阻碍'
    return None
