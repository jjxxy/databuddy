"""Bounded, read-only specialist sessions using the real Atlas Agent runtime.

The coordinator owns mutations and final delivery. Specialists get a distinct
session and a narrower resource scope; their opinions never approve financial
semantics or substitute for deterministic source/result verification.
"""
import hashlib
import json
import time
from datetime import datetime, timezone

from .config import DATA
from .locks import FileLock, Timeout, LockLost
from .store import get, put, patch, all_items, now

ROLES={
 'local_data_checker':('数据核对 Agent','核对来源范围、页签/表头、合并单元格坐标、字段单位、期间、行数、重复键、关联基数、未匹配和控制数。预览不是全量，缺完整证据明确说明；不得用猜测补空值。'),
 'local_finance_reviewer':('财经复核 Agent','独立检查含税/不含税、元/万元、币种、累计/当期、可加性、分母、先汇总再关联和十进制对账。已登记指标以实际确认记录为准；不能批准口径或从摘要复算财务数字。'),
 'local_report_reviewer':('报告审阅 Agent','核对报告的数字、期间、单位和来源版本是否对应完整结果，区分事实、假设、因果解释和待确认事项。读取实际方案/结果，不得把未生成或未检查的文件称作已交付。'),
}
TERMINAL={'succeeded','failed','input_required','auth_required','cancelled','unknown','rejected'}
READ_TOOLS={
 'project_context','discover_tools','read_tool_result','set_task_plan','load_workflow','business_rules',
 'source_schema','source_preview','read_source_text','inspect_source_tables','read_source_table','read_document_data_evidence',
 'list_files','read_file','search_files','versions','list_data_applications','application_info',
 'data_asset_catalog','inspect_data_quality','inspect_column_lineage','inspect_metric','view_result','query_sql','ask_data_application',
 'list_data_workflows','inspect_data_workflow','read_data_workflow_result','data_workflow_guide',
 'list_business_reports','inspect_business_report_input','business_report_guide',
 'inspect_semantic_proposal','list_semantic_catalogs','inspect_semantic_catalog','semantic_catalog_guide',
 'discover_skills','load_skill','read_skill_file','recall_memories',
}
EVIDENCE_TOOLS={'source_schema','source_preview','read_source_text','inspect_source_tables','read_source_table','read_document_data_evidence',
 'read_file','search_files','application_info','inspect_data_quality','inspect_column_lineage','inspect_metric',
 'view_result','query_sql','ask_data_application','inspect_data_workflow','read_data_workflow_result','inspect_business_report_input'}
APP_TOOLS={'application_info','inspect_data_quality','inspect_column_lineage','inspect_metric','view_result','query_sql','ask_data_application'}
SOURCE_TOOLS={'source_schema','source_preview','read_source_text','inspect_source_tables','read_source_table','read_document_data_evidence'}
RESULT_READ_TOOLS={'view_result','query_sql','ask_data_application','read_data_workflow_result','inspect_business_report_input'}


def builtin_peers(turn_or_pid=None):
    return [{'id':identity,'name':name,'description':description,'transport':'local','enabled':True,
             'revision':'atlas-specialists-v1','readonly':True,'capabilities':['read','analyze','review'],
             'requires_configuration':False,'financial_approval':False} for identity,(name,description) in ROLES.items()]


def _deadline(task):
    value=task.get('deadline_at')
    if value is None:return None
    if isinstance(value,(int,float)) and not isinstance(value,bool):return float(value)
    try:
        parsed=datetime.fromisoformat(value)
        if parsed.tzinfo is None:raise ValueError()
        return parsed.timestamp()
    except (ValueError,TypeError):raise ValueError('协同任务截止时间无效') from None


def _child(turn):return bool(turn.get('collaboration_parent_turn_id'))


def allowed_tools(turn,names):
    return set(names)&READ_TOOLS if _child(turn) else set(names)


def assert_turn_allowed(turn,name=None,args=None):
    if not _child(turn):return
    current=get('turn',turn['id']);parent=get('turn',current['collaboration_parent_turn_id'])
    task=get('collab_task',current['collaboration_task_id'])
    if (current.get('interaction_mode')!='chat' or current.get('collaboration_readonly') is not True
            or current.get('collaboration_depth')!=1 or parent.get('collaboration_parent_turn_id')):
        raise ValueError('协同子任务必须在只读、单层委派范围运行')
    if (parent.get('project_id')!=current['project_id'] or task.get('project_id')!=current['project_id']
            or parent.get('actor_id','owner')!=current.get('actor_id','owner')
            or task.get('actor_id','owner')!=current.get('actor_id','owner')
            or task.get('parent_turn_id')!=parent['id'] or task.get('child_turn_id')!=current['id']):
        raise ValueError('协同任务的身份、项目或父子关系不一致')
    if parent.get('state') in ('done','failed') or parent.get('outcome')=='cancelled' or task.get('state') in TERMINAL|{'cancel_requested'}:
        raise ValueError('主任务或协同任务已停止，未继续访问资料')
    deadline=_deadline(task)
    if deadline is not None and time.time()>=deadline:raise ValueError('协同任务已超过截止时间，已有记录保留')
    if any(current.get(key)!=parent.get(key) for key in ('model_id','model_catalog_revision')):
        raise ValueError('协同子任务必须沿用主任务冻结的模型版本')
    from .model_catalog import settings_for_turn
    settings_for_turn(current)
    from .conversation import validate_attachment_versions
    validate_attachment_versions(current)
    if name is None:return
    if name not in READ_TOOLS:raise ValueError('协同子任务不能修改项目、调用外部系统或继续委派')
    args=args or {}
    if name in SOURCE_TOOLS or args.get('source_id'):
        source_id=args.get('source_id') or current.get('source_id')
        if not source_id or source_id not in current.get('collaboration_source_ids',[]):
            raise ValueError('该来源不在本次子任务的明确输入范围')
    for key in ('run_id','result_id'):
        if args.get(key) and args[key] not in current.get('collaboration_result_ids',[]):
            raise ValueError('该结果版本不在本次子任务的明确输入范围')
    application=args.get('application_id') or args.get('pipeline_id')
    if name in APP_TOOLS:application=application or current.get('application_id')
    if application:
        if application not in current.get('collaboration_application_ids',[]):raise ValueError('该应用不在子任务范围')
        original=current.get('collaboration_application_snapshots',{}).get(application)
        if original:
            actual=get('pipeline',application)
            if any(actual.get(k)!=v for k,v in original.items()):raise ValueError('应用版本在复核中发生变化，请重新委派；未混用版本')
            if name in {'view_result','query_sql','ask_data_application'} and current.get('collaboration_result_ids'):
                selected=(original.get('candidate_run') or original.get('published_run')) if name=='view_result' else original.get('published_run')
                if selected not in current['collaboration_result_ids']:raise ValueError('该读取将访问未分配的结果版本，未继续执行')
    if name in APP_TOOLS and not application:raise ValueError('本次子任务没有明确的应用目标')


def create_local(parent_turn,collab_task,assignment):
    from .actor_context import current_actor,actor_scope
    from .conversation import TurnInput,SessionInput,create_session,submit,turn_identity,validate_attachment_versions
    from .model_catalog import settings_for_turn
    from .collaboration_budget import initialize,current_totals
    parent=get('turn',parent_turn['id']);task=get('collab_task',collab_task['id'])
    if parent.get('collaboration_parent_turn_id') or parent.get('collaboration_depth',0):raise ValueError('只读协同子任务不能继续委派')
    if parent.get('interaction_mode','work')!='work' or parent.get('state') in ('done','failed'):
        raise ValueError('只有仍在执行的工作任务可以创建协同复核')
    if parent.get('actor_id','owner')!=current_actor():raise ValueError('只能为自己的任务创建协同复核')
    if task.get('project_id')!=parent['project_id'] or task.get('parent_turn_id')!=parent['id'] or task.get('actor_id','owner')!=current_actor():
        raise ValueError('协同任务不属于当前主任务')
    peer=task.get('peer_id')
    if peer not in ROLES:raise ValueError('未知本地协同角色')
    if not parent.get('model_id') or not parent.get('model_catalog_revision') or settings_for_turn(parent) is None:
        raise ValueError('主任务缺少可用的冻结模型，未改用默认模型')
    validate_attachment_versions(parent)
    initialize(parent)
    with FileLock(str(DATA/('team-create-'+hashlib.sha256(task['id'].encode()).hexdigest()[:32]+'.lock')),timeout=10):
        task=get('collab_task',task['id'])
        if task.get('child_turn_id'):
            try:child=get('turn',task['child_turn_id'])
            except KeyError:pass  # Crash before the atomic conversation insert: same ID is safe to retry.
            else:
                if child.get('collaboration_task_id')!=task['id']:raise ValueError('已有子任务回执不一致')
                return task
        siblings=[t for t in all_items('turn') if t.get('collaboration_parent_turn_id')==parent['id']]
        if len(siblings)>=4:raise ValueError('每个工作任务最多创建4个协同子任务')
        sources=list(assignment.get('source_ids') or [])
        results=list(assignment.get('result_ids') or [])
        if len(sources)>20 or len(set(sources))!=len(sources) or len(results)>20 or len(set(results))!=len(results):raise ValueError('协同输入过多或重复')
        for identity in sources:
            source=get('source',identity)
            if source.get('project_id')!=parent['project_id'] or source.get('archived'):raise ValueError('协同来源不属于当前项目')
        applications=list(dict.fromkeys(([parent['application_id']] if parent.get('application_id') else [])+parent.get('task_contract',{}).get('application_ids',[])))
        for identity in results:
            from .jobs import get_job
            result=get_job(identity)
            result_project=result.get('project_id')
            if not result_project and result.get('kind')=='pipeline':result_project=get('pipeline',result['target']).get('project_id')
            if result_project!=parent['project_id'] or not result.get('validated') or result.get('status') not in ('validated','succeeded'):
                raise ValueError('协同结果不是当前项目已验证的版本')
            if result.get('kind')=='pipeline' and result['target'] not in applications:applications.append(result['target'])
        snapshots={}
        for identity in applications:
            application=get('pipeline',identity)
            if application.get('project_id')!=parent['project_id'] or application.get('archived'):raise ValueError('协同应用不属于当前项目')
            snapshots[identity]={key:application.get(key) for key in ('commit','active_commit','published_run','candidate_run')}
        goal=assignment.get('goal','')
        if not isinstance(goal,str) or not goal.strip() or len(goal)>8000:raise ValueError('子任务目标须为8000字以内的明确目标')
        from .context_compaction import context_for_turn as parent_context
        from .provider import task_scope
        with task_scope(parent['id']):critical=parent_context(parent).get('critical_constraints',[])
        context={'role_id':peer,'role':ROLES[peer][0],'root_turn_id':parent['id'],'task_id':task['id'],
            'parent_request_verbatim':parent.get('work_request',parent['text']),
            'parent_critical_constraints':critical,'source_ids':sources,'result_ids':results,
            'policy':'只读复核；原文约束不得反转；子任务意见不等于财务证据/人工审批；缺依据明确说明'}
        ledger=current_totals(parent)
        child_budget=dict(ledger['remaining'])
        if any(v is not None and v<=0 for v in child_budget.values()):raise ValueError('协同剩余预算不足')
        contract={'version':1,'kind':'answer','requirements':[],'goal':goal,'application_ids':applications,
            'created_at':now(),'acceptance':'实际读取指定来源/版本后提出有依据的复核意见，缺依据不得冒充通过',
            'budgets':child_budget,'initial_applications':applications}
        fields={'collaboration_parent_turn_id':parent['id'],'collaboration_root_turn_id':parent['id'],
            'collaboration_task_id':task['id'],'collaboration_peer_id':peer,'collaboration_depth':1,
            'collaboration_readonly':True,'collaboration_budget_inherited':True,'collaboration_source_ids':sources,'collaboration_result_ids':results,
            'collaboration_application_ids':applications,'collaboration_application_snapshots':snapshots,
            'collaboration_deadline_at':task.get('deadline_at'),'collaboration_context':context,'task_contract':contract,
            **{key:parent[key] for key in ('model_id','model_catalog_revision','model_name') if key in parent}}
        request='team_'+hashlib.sha256(task['id'].encode()).hexdigest()[:48]
        child_id=turn_identity(parent['project_id'],request,parent.get('actor_id','owner'))
        # Publish the deterministic relationship before the child is runnable;
        # otherwise the dedicated lane could observe an unlinked child row.
        patch('collab_task',task['id'],child_turn_id=child_id,state='queued',updated_at=now())
        name,description=ROLES[peer]
        text='你是'+name+'。'+description+'\n本次只读复核目标：'+goal+'\n明确输入来源：'+','.join(sources)+'；结果版本：'+','.join(results)+'。只返回已核对事实、发现的问题、缺失信息及证据定位。'
        if sources:
            # Explicitly requested tools enter the existing filtered working set.
            # Do not add a new submission field or bypass read-only authorization.
            text+='\n指定来源先用 source_schema 核对版本与字段，再按需要读取实际内容。'
        with actor_scope(parent.get('actor_id','owner')):
            session=create_session(parent['project_id'],SessionInput(name=name,request_id=request),internal=True)
            child=submit(parent['project_id'],TurnInput(text=text,request_id=request,session_id=session['id'],
                interaction_mode='chat',model_id=parent['model_id'],source_ids=sources,
                application_id=parent.get('application_id'),clear_application=not bool(parent.get('application_id'))),trusted_fields=fields)
        return patch('collab_task',task['id'],child_turn_id=child['id'],state='working',updated_at=now(),
            local_role=peer,model_id=parent['model_id'],model_catalog_revision=parent['model_catalog_revision'])


def context_for_turn(turn):
    if not _child(turn):return None
    assert_turn_allowed(turn)
    return dict(turn.get('collaboration_context',{}),readonly=True,source_scope=turn.get('collaboration_source_ids',[]),
                result_scope=turn.get('collaboration_result_ids',[]),financial_approval=False)


def _stop(child,reason):
    if child.get('state') in ('done','failed'):return
    patch('turn',child['id'],state='done',outcome='cancelled',answer=reason,finished_at=now(),progress='',
          resume_available=False,secret_text=None)


def reconcile_local(task):
    task=get('collab_task',task['id'])
    if task.get('transport') not in (None,'local') or task.get('state') in TERMINAL:return task
    if not task.get('child_turn_id'):return task
    try:child=get('turn',task['child_turn_id'])
    except KeyError:
        deadline=_deadline(task)
        if deadline is not None and time.time()>=deadline:
            return patch('collab_task',task['id'],state='failed',updated_at=now(),error='协同创建未完成且已超时')
        return task
    parent=get('turn',task['parent_turn_id'])
    deadline=_deadline(task)
    if task.get('state')=='cancel_requested' or parent.get('state') in ('done','failed') or parent.get('outcome')=='cancelled':
        _stop(child,'主任务或协同任务已停止，已读取的证据和回执保留。')
        return patch('collab_task',task['id'],state='cancelled',updated_at=now(),error='主任务或协同任务已停止')
    if deadline is not None and time.time()>=deadline:
        _stop(child,'协同截止时间已到，未继续访问资料。')
        return patch('collab_task',task['id'],state='failed',updated_at=now(),error='协同截止时间已到')
    if child.get('state') not in ('done','failed'):return task
    outcome=child.get('outcome');trace=child.get('agent_trace',[])
    evidence=[{'step_id':step['id'],'tool':step['tool'],'arguments_sha256':hashlib.sha256(json.dumps(step.get('arguments',{}),sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
               'observation_sha256':hashlib.sha256(json.dumps(step.get('observation'),sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()}
              for step in trace if step.get('status')=='done' and step.get('tool') in EVIDENCE_TOOLS]
    has_inputs=bool(child.get('collaboration_source_ids') or child.get('collaboration_result_ids'))
    source_reads=set();result_reads=set()
    for step in trace:
        if step.get('status')!='done' or step.get('tool') not in EVIDENCE_TOOLS:continue
        args=step.get('arguments') or {};obs=step.get('observation') or {}
        if step.get('tool') in SOURCE_TOOLS and args.get('source_id'):source_reads.add(args['source_id'])
        if step.get('tool') not in RESULT_READ_TOOLS:continue
        for key in ('run_id','result_id'):
            if args.get(key):result_reads.add(args[key])
            if isinstance(obs,dict) and obs.get(key):result_reads.add(obs[key])
        if step.get('tool') in {'view_result','query_sql','ask_data_application'}:
            application=args.get('application_id') or args.get('pipeline_id') or child.get('application_id')
            snapshot=child.get('collaboration_application_snapshots',{}).get(application,{})
            selected=(snapshot.get('candidate_run') or snapshot.get('published_run')) if step['tool']=='view_result' else snapshot.get('published_run')
            if selected:result_reads.add(selected)
    missing_sources=sorted(set(child.get('collaboration_source_ids',[]))-source_reads)
    missing_results=sorted(set(child.get('collaboration_result_ids',[]))-result_reads)
    complete_inputs=not missing_sources and not missing_results
    if child['state']=='failed':state='failed'
    elif outcome=='cancelled':state='cancelled'
    elif outcome in ('needs_input','blocked','partial','paused'):state='input_required'
    elif outcome in ('answered','verified_complete') and complete_inputs and (evidence or not has_inputs):state='succeeded'
    else:state='input_required'
    from .company_search import visible_answer
    answer=visible_answer(child)
    result={'answer':answer,'child_turn_id':child['id'],'outcome':outcome,'evidence':evidence,
            'opinion_is_financial_evidence':False,'requires_coordinator_verification':True,
            'source_ids':child.get('collaboration_source_ids',[]),'result_ids':child.get('collaboration_result_ids',[]),
            'input_read_coverage':{'all_assigned_inputs_read':complete_inputs,'missing_source_ids':missing_sources,'missing_result_ids':missing_results}}
    from .store import seal
    return patch('collab_task',task['id'],state=state,updated_at=now(),secret_result=seal(result),
        result_sha256=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
        evidence_count=len(evidence),error='子任务缺少指定输入的真实读取证据' if has_inputs and (not evidence or not complete_inputs) else None)


def runnable_children(limit=2):
    limit=max(0,min(int(limit),4));rows=[]
    for child in sorted(all_items('turn'),key=lambda x:(x.get('created_at',''),x['id'])):
        if not _child(child) or child.get('state') in ('done','failed'):continue
        if child.get('model_retry_at',0) and child['model_retry_at']>time.time():continue
        rows.append(child['id'])
        if len(rows)>=limit:break
    return rows if limit else []


def run_child(turn_id):
    """A separate read-only lane; never acquires a project's mutation lock.

    The scheduler bounds concurrent invocations. A per-child shared lock fences
    replicas and permits a crashed worker's same persisted turn to resume.
    """
    from .actor_context import actor_scope
    from .provider import task_scope
    from .maintenance import guard
    from .conversation import handle
    from .runtime import INSTANCE
    try:
        with guard(),FileLock(str(DATA/('team-child-'+hashlib.sha256(turn_id.encode()).hexdigest()[:32]+'.lock')),timeout=0):
            child=get('turn',turn_id)
            if not _child(child) or child.get('state') in ('done','failed'):return False
            try:
                with actor_scope(child.get('actor_id','owner')),task_scope(child['id']):
                    assert_turn_allowed(child)
                    patch('turn',child['id'],execution_node=INSTANCE)
                    handle(get('turn',child['id']))
            except LockLost:raise
            except Exception as exc:
                current=get('turn',turn_id)
                if current.get('outcome')!='cancelled':
                    from .conversation import redact
                    message=str(exc)[:500] if isinstance(exc,ValueError) and redact(str(exc))==str(exc) else '协同复核暂时未完成，已有证据保留'
                    patch('turn',turn_id,state='failed',outcome='blocked',answer=message,finished_at=now(),
                          resume_available=False,secret_text=None,progress='')
            task=get('collab_task',child['collaboration_task_id']);reconcile_local(task)
            return True
    except (Timeout,BlockingIOError):return False


def cancel_children(parent):
    root_id=parent if isinstance(parent,str) else parent['id'];cancelled=[]
    for task in all_items('collab_task'):
        if task.get('parent_turn_id')!=root_id or task.get('transport')!='local' or task.get('state') in TERMINAL:continue
        patch('collab_task',task['id'],state='cancel_requested',updated_at=now())
        reconcile_local(get('collab_task',task['id']));cancelled.append(task['id'])
    return {'cancelled_task_ids':cancelled}
