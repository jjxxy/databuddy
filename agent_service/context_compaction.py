"""Recoverable extractive checkpoints for long business conversations.

Summaries index work and references. They are not new financial evidence; full
source/results and original turns remain authoritative and recoverable.
"""
from __future__ import annotations
import hashlib,json,re
from pydantic import Field
from .models import StrictModel
from .store import get,put,all_items,seal,unseal,now
from .memory_service import actor,generation,revoked_turn_ids,sensitive

RECENT_TURNS=6
SUMMARY_BUDGET=7000
CRITICAL_BUDGET=14000
_RECOVERY_TRAVERSAL=frozenset({'read_tool_result','read_context_original','read_context_checkpoint','compact_work_context'})

class ReadOriginal(StrictModel):
    turn_id:str=Field(min_length=1,max_length=64)
    offset:int=Field(default=0,ge=0,le=2000000)
    limit:int=Field(default=4000,ge=1,le=10000)
class CompressContext(StrictModel):force:bool=True
class ReadCheckpoint(StrictModel):
    checkpoint_id:str=Field(min_length=1,max_length=64)
    offset:int=Field(default=0,ge=0,le=100000)
    limit:int=Field(default=20,ge=1,le=50)

def _digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()

def _session(turn):
    from .conversation import default_session
    return turn.get('session_id') or default_session(turn['project_id'])

def _history(turn):
    from .workspace_lifecycle import assert_turn_live
    pid=turn['project_id'];sid=_session(turn)
    assert_turn_live(turn)
    return sorted((value for value in all_items('turn') if value.get('project_id')==pid and _session(value)==sid and value['id']!=turn['id'] and value.get('created_at','')<turn.get('created_at','9999') and value.get('state') in ('done','failed')),key=lambda x:(x.get('created_at',''),x['id']))

def _foreign_personal_sources():
    result=set()
    for memory in all_items('user_memory'):
        if memory.get('scope')!='personal' or memory.get('owner_id')==actor():continue
        result.update(memory.get('revoked_source_turn_ids',[]))
        identity=memory.get('provenance',{}).get('source_turn_id')
        if identity:result.add(identity)
    return result

def _active_memory_versions(pid):
    from .memory_service import list_memories
    return {item['id']:item['version'] for item in list_memories(pid,include_expired=False)}

def _visible(value,revoked,foreign_personal_sources=None,memory_versions=None,*,include_historical_answer=False):
    from .company_search import visible_answer
    from .conversation import redact
    if value['id'] in revoked:
        return {'turn_id':value['id'],'user':'[本轮曾保存的记忆已被用户撤回；不再注入原偏好，原始对话可手动回查]','agent':'','revoked_memory':True,'outcome':value.get('outcome')}
    # A colleague's personal-memory instructions are not project policy. Shared
    # project work remains visible, but other actors' explicit personal-memory
    # turns never become the current user's preferences.
    foreign=value.get('actor_id','owner')!=actor()
    text=value.get('text','')
    if foreign_personal_sources is None:foreign_personal_sources=_foreign_personal_sources()
    if value['id'] in foreign_personal_sources or (foreign and re.search(r'记住|个人偏好|忘记.*记忆|删除.*记忆|\b(?:remember|forget|personal preference|my preference|i prefer)\b',text,re.I)):
        return {'turn_id':value['id'],'user':'[其他用户的个人偏好轮次，不注入当前用户上下文]','agent':'','personal_other_actor':True,'outcome':value.get('outcome')}
    text=redact(text)
    if memory_versions is None:memory_versions=_active_memory_versions(value['project_id'])
    used=value.get('memory_context_versions',{})
    stale=any(memory_versions.get(identity)!=version for identity,version in used.items())
    if stale:
        return {'turn_id':value['id'],'user':text,'agent':'[此前回答使用的记忆已变更；请回读来源与结果，原回答可在界面手动查看]',
                'stale_memory_context':True,'outcome':value.get('outcome'),'state':value.get('state')}
    answer=visible_answer(value,model=True)
    answer=redact(answer) if sensitive(answer) else answer
    item={'turn_id':value['id'],'user':text,'agent':answer,'outcome':value.get('outcome'),'state':value.get('state')}
    from .task_budget import unsupported_round_stop
    if unsupported_round_stop(value,answer):
        item['runtime_policy_note']='这是历史停止回执，不能作为当前任务的额度依据；普通对话无固定轮次预算，继续原目标并复用成功回执。'
        if not include_historical_answer:
            # The original is retained and available through explicit history
            # recovery, but routinely feeding it back can cause the model to
            # repeat an obsolete stopping message on a new task.
            item['agent']='[此前任务记录了暂停提示；原始回执可按引用回查]'
    return item

def _refs(value):
    fields={key:value[key] for key in ('source_id','source_ids','attached_sources','application_id','run_id','model_id','model_catalog_revision','work_version') if value.get(key) is not None}
    refs=[]
    for step in value.get('agent_trace',[]):
        if step.get('tool') in _RECOVERY_TRAVERSAL:continue
        obs=step.get('observation') or {}
        if not isinstance(obs,dict):continue
        identity={k:obs[k] for k in ('run_id','result_id','artifact_id','workflow_id','report_id','plan_sha256','manifest_sha256','sha256','commit') if obs.get(k) is not None}
        refs.append({'step_id':step.get('id'),'tool':step.get('tool'),'status':step.get('status'),
                     'result_refs':identity,'recover_with':{'tool':'read_tool_result','source_turn_id':value['id'],
                                                           'step_id':step.get('id'),'offset':0}})
    return {'turn_id':value['id'],'input_and_versions':fields,'tool_receipts':refs}

def _critical(history,visible,*,bounded=True):
    result=[];seen=set();length=0
    for value,item in zip(history,visible):
        if item.get('revoked_memory') or item.get('personal_other_actor'):continue
        for line in re.split(r'\n+|(?<=[。；;])',item['user']):
            quote=line.strip()
            if not quote or not re.search(r'不要|不得|禁止|不允许|不能|只统计|仅统计|仅限|口径|单位|币种|期间|截至|截止|必须|确认指标|记住|忘记|更正|不含税|含税|\b(?:do not|don.t|never|must not|exclude|only|must|currency|period|deadline|remember|forget|correction|tax)\b',quote,re.I):continue
            key=(value['id'],quote)
            if key in seen:continue
            seen.add(key);length+=len(quote)
            result.append({'source_turn_id':value['id'],'quote':quote,'verbatim':True})
        if value.get('outcome') in ('needs_input','blocked','partial','paused'):
            result.append({'source_turn_id':value['id'],'unresolved_outcome':value.get('outcome'),
                           'recover_with':{'tool':'read_context_original','turn_id':value['id']}})
    if bounded and (length>CRITICAL_BUDGET or len(json.dumps(result,ensure_ascii=False))>CRITICAL_BUDGET):
        raise ValueError('历史关键约束超过安全压缩预算，请明确本次适用的约束或在新会话继续；未静默丢弃否定条件')
    return result

def context_for_turn(turn,recent_turns=None,*,force=False,window_managed=False):
    from .workspace_lifecycle import assert_turn_live
    assert_turn_live(turn)
    from .provider import settings
    pid=turn['project_id'];get('project',pid)
    history=_history(turn) if recent_turns is None else sorted(recent_turns,key=lambda x:(x.get('created_at',''),x['id']))
    sid=_session(turn);revoked=revoked_turn_ids(pid);memory_generation=generation(pid)
    allowed=bool(settings().get('share_data'))
    foreign_personal_sources=_foreign_personal_sources()
    memory_versions=_active_memory_versions(pid)
    visible=[_visible(value,revoked,foreign_personal_sources,memory_versions) for value in history] if allowed else [
        {'turn_id':value['id'],'user':'[模型业务内容读取未获批准]','agent':'','outcome':value.get('outcome')} for value in history]
    keep=2 if force else max(1,len(history)) if window_managed else RECENT_TURNS
    older=history[:-keep] if len(history)>keep else []
    old_visible=visible[:-keep] if older else []
    recent=visible[-keep:]
    total_chars=sum(len(item['user'])+len(item['agent']) for item in visible)
    compress=bool(older and (force or len(history)>RECENT_TURNS or total_chars>16000))
    critical=_critical(history,visible,bounded=not window_managed) if allowed else []
    coverage=[{'turn_id':value['id'],'sha256':_digest({'text':value.get('text'),'answer':value.get('answer'),'trace':value.get('agent_trace'),'outcome':value.get('outcome'),'model_revision':value.get('model_catalog_revision')})} for value in older]
    coverage_hash=_digest({'coverage':coverage,'memory_generation':memory_generation,'actor':actor(),'content_allowed':allowed})
    checkpoint_id='ctx_'+coverage_hash[:48]
    summary=None
    if compress:
        try:
            stored=get('context_checkpoint',checkpoint_id)
            if stored['project_id']!=pid or stored.get('actor_id')!=actor() or stored.get('session_id')!=sid:raise ValueError('压缩检查点范围不匹配')
            summary=unseal(stored['secret_summary'])
        except KeyError:
            entries=[];used=0
            for original,item in zip(older,old_visible):
                entry={'turn_id':original['id'],'objective_excerpt':item['user'][:320],
                       'answer_excerpt':item['agent'][:180] if original.get('outcome') not in ('needs_input','blocked','partial','paused') else '',
                       'outcome':original.get('outcome'),'state':original.get('state'),
                       'recover_with':{'tool':'read_context_original','turn_id':original['id']}}
                size=len(json.dumps(entry,ensure_ascii=False))
                if used+size<=SUMMARY_BUDGET:entries.append(entry);used+=size
            # Retain every covered turn ID/hash even if its excerpt did not fit.
            # No compressed numeric excerpt is accepted as a calculation result.
            summary={'method':'extractive_work_index_v1','entries':entries,'omitted_excerpts':len(older)-len(entries),
                     'critical_constraints':_critical(older,old_visible) if allowed else [],'coverage':coverage,
                     'result_references':[_refs(value) for value in older[-12:]],
                     'summary_is_financial_evidence':False,
                     'policy':'摘要仅用于定位工作与原文；数值、字段及业务结论必须回读完整结果和冻结来源，不能依据摘要重新计算或批准口径'}
            put('context_checkpoint',{'project_id':pid,'session_id':sid,'actor_id':actor(),'memory_generation':memory_generation,
                'coverage_sha256':coverage_hash,'covered_turn_ids':[value['id'] for value in older],
                'created_at':now(),'secret_summary':seal(summary)},checkpoint_id)
    # Bound the injected projection, while the durable checkpoint retains the
    # full coverage list. Recent material is labelled/paged if it does not fit.
    recent_budget=10000
    for item in ([] if window_managed and not force else reversed(recent)):
        for field in ('user','agent'):
            allowance=min(3500,max(0,recent_budget))
            if len(item[field])>allowance:
                item[field]=item[field][:allowance]
                item.setdefault('recover_with',{})[field]={'tool':'read_context_original','turn_id':item['turn_id'],'offset':0}
                item['recent_excerpted']=True
            recent_budget-=len(item[field])
    if summary:
        summary=dict(summary)
        full_coverage=summary.pop('coverage',[])
        summary.pop('critical_constraints',None)
        summary['coverage']=(full_coverage[:2]+full_coverage[-2:]) if len(full_coverage)>4 else full_coverage
        summary['coverage_is_complete']=len(full_coverage)<=4
        summary['covered_turns']=len(full_coverage)
        summary['coverage_sha256']=coverage_hash
        summary['recover_coverage_with']={'tool':'read_context_checkpoint','checkpoint_id':checkpoint_id,'offset':0,'limit':20}
        refs=summary.get('result_references',[])[-4:]
        summary['result_references']=[dict(item,tool_receipts=item.get('tool_receipts',[])[-4:]) for item in refs]
    return {'summary':summary,'summary_id':checkpoint_id if compress else None,'recent_verbatim':recent,
            'critical_constraints':critical,'memory_generation':memory_generation,
            'frozen_task':{key:turn[key] for key in ('id','model_id','model_catalog_revision','source_id','source_ids') if turn.get(key) is not None}|{'state':turn.get('state'),'task_contract_sha256':_digest(turn.get('task_contract')),'task_plan_sha256':_digest(turn.get('task_plan')),'verification_feedback_present':bool(turn.get('verification_feedback'))},
            'compression':{'active':compress,'method':'extractive_work_index_v1','source_turns':len(history),'covered_older_turns':len(older) if compress else 0,
                'recent_turns':len(recent),'coverage_sha256':coverage_hash,'source_characters':total_chars,
                'context_characters':len(json.dumps({'summary':summary,'recent':recent,'critical':critical},ensure_ascii=False,default=str)),
                'lossy':compress,'originals_retained':True,'content_allowed':allowed},
            'scope_note':'当前项目会话；个人偏好按当前用户隔离。摘要不是财务证据，原文和完整结果可回查'}

def authorized_history_turn(turn,turn_id,*,include_historical_answer=False):
    try:target=get('turn',turn_id)
    except KeyError:
        from .agent_context import InvalidRecoveryReference
        raise InvalidRecoveryReference('历史上下文不存在或已删除，不能恢复该引用') from None
    if target.get('project_id')!=turn['project_id']:raise ValueError('不能读取其他项目的上下文')
    from .workspace_lifecycle import assert_turn_live,visible_turn
    assert_turn_live(turn);assert_turn_live(target)
    if not visible_turn(target) and target.get('session_id')!=turn.get('session_id'):raise ValueError('不能读取内部协作对话')
    from .provider import settings
    if not settings().get('share_data'):raise ValueError('模型业务内容读取未获批准，请管理员配置')
    item=_visible(target,revoked_turn_ids(turn['project_id']),include_historical_answer=include_historical_answer)
    # Manual/API users can retrieve originals even after memory withdrawal;
    # agent recovery preserves the withdrawal and never restores old preference.
    active=not item.get('revoked_memory') and not item.get('personal_other_actor') and not item.get('stale_memory_context')
    return target,item,active

def _original_snapshot(turn,turn_id):
    target,item,active=authorized_history_turn(turn,turn_id,include_historical_answer=True)
    from .agent_context import trace_context
    from .agent import public_observation
    # Reading an original is traversal, not a new business fact. Including the
    # read's own new step ID/page would make a self-read expand forever and
    # manufacture a new resource version on every request.
    business=dict(target,agent_trace=[step for step in target.get('agent_trace',[])
                                    if step.get('tool') not in _RECOVERY_TRAVERSAL])
    text=json.dumps({'turn':item,'references':_refs(target),'task_contract':target.get('task_contract') if active else None,
                     'task_plan':target.get('task_plan') if active else None,'agent_trace':trace_for_model(business,trace_context(business,public_observation),rehydrate_recovery=False) if active else []},ensure_ascii=False,default=str)
    return text,bool(item.get('revoked_memory'))

def _original_page(turn_id,text,withdrawn,offset,limit):
    from .agent_context import recovery_page
    return {'turn_id':turn_id,'content':text[offset:offset+limit],'offset':offset,
            'next_offset':offset+limit if offset+limit<len(text) else None,'total_characters':len(text),
            'recovery':recovery_page('context_original','turn:'+turn_id,text,offset,limit),
            'scope':'original_turn_and_receipt_references','memory_withdrawal_applied':withdrawn}

def read_original(turn,turn_id,offset=0,limit=4000):
    text,withdrawn=_original_snapshot(turn,turn_id)
    return _original_page(turn_id,text,withdrawn,offset,limit)

def status(pid,session_id=None,force=False):
    from .conversation import default_session
    get('project',pid)
    current={'id':'context_inspection','project_id':pid,'session_id':session_id or default_session(pid),'created_at':'9999','text':''}
    return context_for_turn(current,force=force)

CONTEXT_TOOLS={
 'read_context_checkpoint':(ReadCheckpoint,False,'分页读取当前项目压缩检查点的完整覆盖清单与原始轮次定位，不把摘要当作财务结果'),
 'read_context_original':(ReadOriginal,False,'分页恢复当前项目的原始对话与工具回执，压缩摘要不是财务证据；已撤回记忆不会重新注入'),
 'compact_work_context':(CompressContext,False,'生成持久、可回查的上下文检查点，保留关键否定约束、待办和结果版本；不是删除历史'),
}

def tool(turn,name,args):
    if name=='read_context_checkpoint':return read_checkpoint(turn,**args)
    if name=='read_context_original':return read_original(turn,**args)
    if name=='compact_work_context':return context_for_turn(turn,force=args.get('force',True))
    raise KeyError(name)


def filter_history_matches(pid,entries):
    """Apply the selected model's content grant before any history injection."""
    from .provider import settings
    from .workspace_lifecycle import visible_turn
    if not settings().get('share_data'):return []
    revoked=revoked_turn_ids(pid);result=[]
    for value in entries:
        identity=value.get('turn_id')
        if identity in revoked:continue
        if identity:
            try:original=get('turn',identity)
            except KeyError:continue
            if original.get('project_id')!=pid:continue
            if not visible_turn(original):continue
            visible=_visible(original,revoked)
            if visible.get('personal_other_actor') or visible.get('stale_memory_context'):continue
            if visible.get('runtime_policy_note'):
                value={**value,'answer':visible['agent'],'runtime_policy_note':visible['runtime_policy_note']}
        result.append(value)
    return result


def trace_for_model(turn,values,*,rehydrate_recovery=True):
    """A receipt remains stored, but stale personal content is not re-injected."""
    current_generation=generation(turn['project_id'])
    revised=[];snapshots={};seen_pages={}
    def recovery_key(item):
        tool=item.get('tool');args=item.get('arguments',{})
        raw=next((step.get('observation') for step in turn.get('agent_trace',[]) if step.get('id')==item.get('step_id')),{}) or {}
        if item.get('status')!='done' or isinstance(raw,dict) and raw.get('executed') is False:return None
        descriptor=raw.get('recovery') if isinstance(raw,dict) else None
        if isinstance(descriptor,dict):return (descriptor.get('kind'),descriptor.get('resource_id'))
        if tool=='read_context_original':return ('context_original',args.get('turn_id'))
        if tool=='read_context_checkpoint':return ('context_checkpoint',args.get('checkpoint_id'))
        if tool=='read_tool_result' and (args.get('source_turn_id') or not rehydrate_recovery):return ('tool_result',args.get('source_turn_id'),args.get('step_id'))
        return None
    for item in values:
        value=dict(item)
        key=recovery_key(item)
        if key:
            args=dict(item.get('arguments',{}));tool=item.get('tool')
            if rehydrate_recovery:
                try:
                    if tool=='read_context_original':
                        target=args['turn_id']
                        if target not in snapshots:snapshots[target]=_original_snapshot(turn,target)
                        text,withdrawn=snapshots[target]
                        page=_original_page(target,text,withdrawn,args.get('offset',0),args.get('limit',4000))
                    elif tool=='read_context_checkpoint':page=read_checkpoint(turn,**args)
                    else:
                        from .agent_context import read_result,ToolResult
                        page=read_result(turn,ToolResult.model_validate(args))
                    r=page.get('recovery',{});identity=tuple(r.get(field) for field in
                        ('kind','resource_id','version_sha256','offset_unit','offset','returned_characters'))
                    if r and identity in seen_pages:
                        value['observation']={'recovery':r,'duplicate_result_view':True,
                            'context_reference':{'step_id':seen_pages[identity],'in_current_context':True,
                                'scope':'exact same authorized resource version and returned range; values already present'}}
                    else:
                        value['observation']=page
                        if r:seen_pages[identity]=value.get('step_id')
                except (ValueError,KeyError):
                    value['observation']={'recovery_unavailable':True,'notice':'历史恢复内容当前不可用；请核对来源引用、授权或已撤回内容'}
            else:
                if tool=='read_tool_result':args.setdefault('source_turn_id',turn['id'])
                value['observation']={'notice':'已读取的历史页；按原资源定位回读，不恢复旧缓存',
                    'recover_with':{'tool':tool,**args}}
            revised.append(value);continue
        if value.get('tool') in ('remember_memory','forget_memory','recall_memories','compact_work_context'):
            # Call arguments may themselves contain a remembered quote, even if
            # the old observation has already been replaced.
            value['arguments']={'memory_operation_recorded':True}
            value['observation']={'memory_context_generation':current_generation,
                'notice':'记忆操作已有持久回执；最新有效记忆仅以当前memory_context为准，旧副本不再注入',
                'recover_with':{'tool':'recall_memories','query':''}}
        revised.append(value)
    return revised


def read_checkpoint(turn,checkpoint_id,offset=0,limit=20):
    try:record=get('context_checkpoint',checkpoint_id)
    except KeyError:
        from .agent_context import InvalidRecoveryReference
        raise InvalidRecoveryReference('压缩检查点已不存在，请使用当前有效的上下文引用') from None
    if record.get('project_id')!=turn['project_id'] or record.get('actor_id')!=actor():raise ValueError('压缩检查点不属于当前项目和用户')
    if record.get('memory_generation')!=generation(turn['project_id']):raise ValueError('检查点含已变更的记忆，请重新压缩当前上下文')
    from .provider import settings
    if not settings().get('share_data'):raise ValueError('模型业务内容读取未获批准')
    if record.get('kind')=='working_context':
        authorized_history_turn(turn,record['source_turn_id'])
    summary=unseal(record['secret_summary']);coverage=summary.get('coverage',[])
    page=coverage[offset:offset+limit]
    from .agent_context import recovery_page
    lines=[json.dumps(item,ensure_ascii=False,sort_keys=True,default=str)+'\n' for item in coverage]
    text=''.join(lines);start=sum(map(len,lines[:offset]));length=sum(map(len,lines[offset:offset+limit]))
    return {'checkpoint_id':checkpoint_id,'coverage_sha256':record['coverage_sha256'],'total':len(coverage),
            'recovery':dict(recovery_page('context_checkpoint','checkpoint:'+checkpoint_id,text,start,length),pagination_unit='items'),
            'items':page if record.get('kind')=='working_context' else [dict(item,recover_with={'tool':'read_context_original','turn_id':item['turn_id']}) for item in page],
            'next_offset':offset+limit if offset+limit<len(coverage) else None,'summary_is_financial_evidence':False}
