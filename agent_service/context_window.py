"""Recoverable model-window projections, separate from task/cost budgets.

Only model-visible, authorized context is checkpointed. Files, durable receipts,
user instructions, permissions and financial approval gates are never rewritten.
"""
from __future__ import annotations

import copy
import hashlib
import json

from .store import get, patch, put, seal, now


def encoded(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def model_identity():
    from .provider import settings
    value=settings()
    return fingerprint({key:value.get(key) for key in ('base_url','model')})


def state_for(turn):
    state=turn.get('context_window') or {}
    return dict(state) if state.get('model_identity')==model_identity() else {}


def recover(turn, error):
    """Bound only repeated failures without progress, never total task length."""
    from .workspace_lifecycle import assert_turn_live
    from .memory_service import generation
    assert_turn_live(turn)
    current=get('turn',turn['id'])
    if current['state'] not in ('agent','agent_tool'):return False
    state=state_for(current)
    trace=current.get('agent_trace',[])
    boundary=fingerprint([[(step.get('id'),step.get('status')) for step in trace],generation(turn['project_id'])])
    attempts=state.get('attempts',0) if state.get('boundary')==boundary else 0
    if attempts>=3:return False
    size=error.characters or current.get('model_usage',{}).get('largest_opencode_request_characters') or current.get('context_metrics',{}).get('request_characters') or 64000
    previous=state.get('target_characters')
    target=int((error.limit if error.limit else size)*(.85 if error.limit else .6))
    if previous:target=min(target,int(previous*.65))
    target=max(1,target)
    state.update(model_identity=model_identity(),boundary=boundary,attempts=attempts+1,
                 target_characters=target,reason=error.source,needs_epoch=True,
                 recoveries=state.get('recoveries',0)+1)
    values={'context_window':state,'progress':'上下文较长，正在压缩并继续；原始资料与已完成步骤保留'}
    session=current.get('opencode_session_id')
    if session:
        # run_session has interrupted and closed the old bridge before returning
        # this typed error. Its original history remains in the native store.
        values.update(opencode_session_id=None,opencode_compaction_requested=False,
            opencode_retired_sessions=(current.get('opencode_retired_sessions',[])+[session])[-10:])
    patch('turn',turn['id'],**values)
    return True


def request_size(prompt, info, native_tools):
    return len(encoded({'messages':[{'role':'system','content':prompt},
        {'role':'user','content':json.dumps(info,ensure_ascii=False,default=str,separators=(',',':'))}],
        'tools':native_tools}))


def fit(turn, prompt, info, native_tools):
    """Project only when a real/explicit window requires it; no default cliff."""
    from .provider import settings, ContextWindowExceeded
    from .provider_compatibility import configured_input_character_limit
    from .memory_service import actor, generation
    from . import agent_context
    state=state_for(turn)
    cap=configured_input_character_limit(settings().get('model'))
    target=state.get('target_characters')
    before=request_size(prompt,info,native_tools)
    # An explicit transport limit is a trigger only after it is exceeded. No
    # fixed turn count/character threshold pre-emptively discards working facts.
    if target is None and (cap is None or before<=cap):return info,native_tools
    if cap is not None:target=min(target or cap,int(cap*.85))
    if target is None or before<=target:return info,native_tools

    original=copy.deepcopy(info)
    view=copy.deepcopy(info)
    native_tools=copy.deepcopy(native_tools)
    identity=fingerprint({'turn':turn['id'],'actor':actor(),'memory_generation':generation(turn['project_id']),
                          'info':original})
    checkpoint_id='ctxw_'+identity[:48]
    sections={}
    coverage=[]
    def checkpoint_ref(key,value):
        if key not in sections:
            start=len(coverage);text=encoded(value);checksum=hashlib.sha256(text.encode()).hexdigest()
            for offset in range(0,len(text),1000):
                coverage.append({'section':key,'format':'json_fragment','offset':offset,
                    'total_characters':len(text),'sha256':checksum,'content':text[offset:offset+1000]})
            sections[key]=start
        return {'context_omitted':True,'not_full_result':True,'checkpoint_id':checkpoint_id,
            'recover_with':{'tool':'read_context_checkpoint','checkpoint_id':checkpoint_id,'offset':sections[key],'limit':2}}

    allowed=agent_context.allowed(turn)
    if 'read_context_checkpoint' not in allowed:
        raise ContextWindowExceeded('当前授权未包含上下文回查工具，无法安全缩短请求；原任务已保留',limit=target,characters=before)
    # Load a read-only recovery schema; this is selection within existing grants.
    current=get('turn',turn['id'])
    if 'read_context_checkpoint' not in agent_context.selected(current):
        patch('turn',turn['id'],discovered_tools=list(dict.fromkeys(current.get('discovered_tools',[])+['read_context_checkpoint']))[-16:])
    recovery=agent_context.definition('read_context_checkpoint')
    if native_tools is None:
        if not any(tool['name']=='read_context_checkpoint' for tool in view.get('tools',[])):
            view.setdefault('tools',[]).append(recovery)
    elif not any(tool['function']['name']=='read_context_checkpoint' for tool in native_tools):
        native_tools.append({'type':'function','function':{'name':recovery['name'],'description':recovery['description'],'parameters':recovery['arguments_schema']}})
    view['window_checkpoint']={'id':checkpoint_id,'method':'recoverable_projection','originals_retained':True,
        'summary_is_financial_evidence':False,'notice':'已完成步骤和未知提交均不得重做。缺少原文时按引用分页读取；不能将省略项当作空值、零或已核验。'}

    def fits():return request_size(prompt,view,native_tools)<=target
    # First recover large historical/auxiliary sections. Explicit instructions,
    # exact critical denials, current memory and delegated scope stay verbatim.
    protected={'project','mode','request','continuation_request','task_contract','task_plan','memory_context',
        'specialist_assignment','work_actions','attached_sources','attached_source_ids','attached_source_id',
        'target_application_id','current_source_id','current_pipeline_id','context_compaction',
        'tools','response_schema','completed_steps','window_checkpoint','tool_catalog'}
    candidates=sorted((key for key in view if key not in protected),key=lambda key:len(encoded(view[key])),reverse=True)
    for key in candidates:
        if fits():break
        if len(encoded(view[key]))>500:
            view[key]=checkpoint_ref(key,original[key])
            if key=='conversation' and isinstance(original[key],list):
                # Preserve a useful work index and recent exchange after a real
                # overflow; full turns remain recoverable, with exact denials
                # separately retained in critical_constraints.
                view[key]['recent_verbatim']=original[key][-2:]
                view[key]['history_index']=[{'turn_id':item['turn_id'],'objective_excerpt':item.get('user','')[:160],
                    'recover_with':{'tool':'read_context_original','turn_id':item['turn_id'],'offset':0,'limit':2000}}
                    for item in original[key][-8:] if item.get('turn_id')]
                view[key]['index_is_evidence']=False
    compressed=view.get('context_compaction')
    if not fits() and isinstance(compressed,dict):
        view['context_compaction']={key:copy.deepcopy(compressed[key]) for key in
            ('critical_constraints','memory_generation','frozen_task','scope_note') if key in compressed}
        view['context_compaction']['history']=checkpoint_ref('context_compaction',original['context_compaction'])

    steps=view.get('completed_steps',[])
    # Keep the largest amount of usable recent evidence that fits, rather than
    # cutting every financial table at a fixed size or deleting all but two.
    ordered=sorted(range(len(steps)),key=lambda i:(i>=len(steps)-2,-len(encoded(steps[i]))))
    for index in ordered:
        if fits():break
        step=steps[index]
        observation=step.get('observation')
        if len(encoded(step))<700:continue
        ref=checkpoint_ref('completed_steps.'+str(index),original['completed_steps'][index])
        item={key:step[key] for key in ('step_id','tool','status','row_scope','original_result') if key in step}
        item['observation']=ref
        if step.get('step_id') and 'read_tool_result' in allowed:
            item['observation']['recover_with']={'tool':'read_tool_result','step_id':step['step_id'],
                'source_turn_id':turn['id'],'offset':0,'limit':min(6000,max(1,target//8))}
        if isinstance(observation,dict):
            item['execution_receipt']={key:observation[key] for key in
                ('executed','state','status','id','request_id','action_id','execution_id','job_id','artifact_id','sha256')
                if type(observation.get(key)) in (str,int,bool) and len(str(observation[key]))<=200}
        args=step.get('arguments',{})
        if len(encoded(args))<=500:item['arguments']=args
        steps[index]=item
    # Schemas are demand-loaded. All allowed names and validation remain intact;
    # keep discovery/recovery/final schemas and the most recent used tool.
    essential={'discover_tools','read_tool_result','read_context_checkpoint','finish_task'}
    if steps:essential.add(steps[-1].get('tool'))
    if not fits():
        if native_tools is not None:
            for tool in sorted(list(native_tools),key=lambda t:len(encoded(t)),reverse=True):
                if fits():break
                if tool['function']['name'] not in essential:native_tools.remove(tool)
        else:
            for tool in sorted(list(view.get('tools',[])),key=lambda t:len(encoded(t)),reverse=True):
                if fits():break
                if tool['name'] not in essential:view['tools'].remove(tool)
        if 'office_spec_schema' in view and not fits():
            view['office_spec_schema']=checkpoint_ref('office_spec_schema',original['office_spec_schema'])
    history=view.get('conversation')
    if not fits() and isinstance(history,dict) and history.get('context_omitted'):
        history.pop('recent_verbatim',None)
    if not fits() and len(steps)>4:
        ref=checkpoint_ref('completed_steps',original['completed_steps'])
        view['completed_steps']=steps[-4:]
        view['window_checkpoint']['earlier_steps']=ref
    after=request_size(prompt,view,native_tools)
    # Persist exact authorized sections, not a model-invented summary. Reads are
    # actor/project/memory-generation bound and paginated by the existing tool.
    if not coverage:checkpoint_ref('context',original)
    if coverage:
        put('context_checkpoint',{'project_id':turn['project_id'],'session_id':turn.get('session_id'),
            'actor_id':actor(),'memory_generation':generation(turn['project_id']),'source_turn_id':turn['id'],
            'kind':'working_context','coverage_sha256':identity,'created_at':now(),
            'secret_summary':seal({'coverage':coverage})},checkpoint_id)
    if after>target:
        raise ContextWindowExceeded('压缩后，当前需求、关键约束和必要工具仍超过所选模型容量；原任务与资料已保留，需要更大上下文的模型',
            limit=target,characters=after,source='irreducible')
    kept=[step['step_id'] for step in view.get('completed_steps',[]) if step.get('step_id') and
          not (isinstance(step.get('observation'),dict) and step['observation'].get('context_omitted'))]
    old=set(state.get('retained_step_ids',[]))
    epoch=turn.get('context_window_epoch',0)
    if state.get('needs_epoch') or not state.get('checkpoint_id') or old-set(kept):epoch+=1
    state.update(model_identity=model_identity(),target_characters=target,checkpoint_id=checkpoint_id,
        before_characters=before,after_characters=after,originals_retained=True,needs_epoch=False,
        retained_step_ids=kept[-256:],projections=state.get('projections',0)+1)
    patch('turn',turn['id'],context_window=state,context_window_epoch=epoch,
        progress='上下文已压缩，正在沿用已完成步骤继续')
    return view,native_tools
