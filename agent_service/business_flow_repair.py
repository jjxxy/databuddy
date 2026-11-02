"""Advice derived solely from an authorized workflow receipt.

This is guidance to the agent, not an assertion that a change or retry occurred.
"""

_RULES={
    'SCHEMA_MISMATCH':('inspect_source_and_plan','对照原始字段和已保存流程；只有字段对应关系有明确依据时才修改，含税、币种和期间不能猜测。'),
    'TYPE_MISMATCH':('inspect_source_and_plan','检查原始数据类型和转换规则；日期格式或金额单位不明确时先询问。'),
    'NULL_VALUE':('inspect_source_and_plan','定位缺失字段和缺失范围；仅在已有明确规则时处理缺失值。'),
    'UNMAPPED_VALUE':('inspect_source_and_plan','查找未匹配记录和有效映射；缺少明确映射时询问用户，不能模糊匹配。'),
    'LOOKUP_CARDINALITY':('inspect_source_and_plan','检查关联键及重复来源，不能擅自去重或扩大关联。'),
    'GRAIN_DUPLICATE':('inspect_source_and_plan','核对业务粒度和原始明细，不能擅自删除重复记录。'),
    'UNIT_MISMATCH':('ask_business_rule','核对源数据单位与业务口径；缺少依据时询问，不能自行换算。'),
    'FX_CURRENCY_MISMATCH':('ask_business_rule','核对币种和换算方向；缺少依据时询问，不能猜测。'),
    'FX_RATE_TYPE_MISMATCH':('ask_business_rule','核对汇率类型与生效期间；缺少依据时询问。'),
    'DECIMAL_PRECISION':('ask_business_rule','核对小数精度和舍入规则；缺少依据时询问。'),
    'SOURCE_VERSION_CHANGED':('confirm_source','核对原资料及已固定版本；不要静默替换为新版本。'),
    'BYTE_BUDGET':('inspect_capacity','检查容量与完整数据范围；不能截断结果冒充完整交付。'),
    'ROW_BUDGET':('inspect_capacity','检查数据量和执行容量；不能截断结果冒充完整交付。'),
    'CHECK_REQUIRED_FAILED':('inspect_source_and_plan','检查失败字段的来源和缺失范围；只有已确认规则允许时才能补值，不能猜测财务数据。'),
    'CHECK_UNIQUE_FAILED':('inspect_source_and_plan','检查组合键、重复行和业务粒度；不得擅自删除重复记录。'),
    'CHECK_RANGE_FAILED':('inspect_source_and_plan','核对字段、单位和已设置的数值范围；不能为了通过检查擅自改变数据或阈值。'),
}


def guidance(run):
    """Return a bounded diagnosis for the receipt; unknown effects block retry."""
    if not isinstance(run,dict):raise ValueError('运行记录无效')
    if run.get('status')=='unknown' or any(isinstance(s,dict) and s.get('status')=='unknown' for s in run.get('steps',[])):
        return {'status':'blocked_unknown','retry_allowed':False,'next_step':'执行结果未知，请先核对外部系统；不要自动重试或修改已发生的操作。'}
    failed=[s for s in run.get('steps',[]) if isinstance(s,dict) and s.get('status')=='failed']
    if run.get('status')!='failed' or not failed:
        return {'status':'not_diagnosed','retry_allowed':False,'next_step':'当前没有可诊断的失败步骤，请核对运行状态。'}
    step=failed[0];code=step.get('error_code') if isinstance(step.get('error_code'),str) else ''
    action,notice=_RULES.get(code,('inspect_failure','先核对失败回执、原始资料和已保存流程；无法确定原因时说明阻碍，不猜测业务口径。'))
    return {'status':'diagnosis_available','node_id':step.get('node_id'),'error_code':code,
            'action':action,'retry_allowed':False,'next_step':notice,
            'guard':'仅修改有证据的流程版本；不自动发布。不得盲目重试原运行，完成修复后可对已确认无外部副作用的步骤新建调试运行，并核对财务结果。'}


def verify_identical_source_revision(old,current,project_id,source_id,expected_hash,uploads,decode_config):
    """Check a proposed source rebind without writing or starting any task.

    Fail closed on absent historical metadata, changing interpretation, an
    archived source or missing bytes. This helper does not authorize applying
    the change; applying it additionally needs the project version lock.
    """
    import hashlib,re
    from pathlib import Path
    if not isinstance(old,dict) or not isinstance(current,dict) or not re.fullmatch(r'[a-f0-9]{64}',expected_hash or ''):return None
    if (old.get('source_id')!=source_id or current.get('id')!=source_id or
        old.get('project_id')!=project_id or current.get('project_id')!=project_id or
        old.get('kind')!='file' or current.get('kind')!='file' or old.get('status')!='ready' or current.get('status')!='ready' or
        old.get('archived') or current.get('archived') or old.get('content_hash')!=expected_hash or current.get('content_hash')!=expected_hash):return None
    a,b=old.get('revision'),current.get('revision')
    if type(a) is not int or type(b) is not int or a<1 or b<=a:return None
    if not isinstance(old.get('profile'),dict) or not old['profile'] or old.get('profile')!=current.get('profile'):return None
    ignore={'id','source_id','revision','revision_id','created_at','updated_at','recorded_at','secret_config'}
    if {k:v for k,v in old.items() if k not in ignore}!={k:v for k,v in current.items() if k not in ignore}:return None
    try:
        left=decode_config(old['secret_config']);right=decode_config(current['secret_config'])
        if not isinstance(left,dict) or not isinstance(right,dict):return None
        left=dict(left);right=dict(right);paths=[left.pop('stored_name',None),right.pop('stored_name',None)]
        if left!=right:return None
        root=Path(uploads).resolve(strict=True)
        for name in paths:
            if not isinstance(name,str) or not name or Path(name).name!=name or '/' in name or '\\' in name:return None
            path=root/name
            if path.is_symlink() or path.resolve(strict=True).parent!=root:return None
            h=hashlib.sha256()
            with path.open('rb') as data:
                for chunk in iter(lambda:data.read(1024*1024),b''):h.update(chunk)
            if h.hexdigest()!=expected_hash:return None
    except (OSError,ValueError,KeyError,TypeError):return None
    return b


def rebind_identical_source_debug(job):
    """Prepare and run an isolated copy when a file revision is byte-identical.

    This narrow repair never modifies or publishes the original workflow.
    Every ambiguous condition fails closed. Only a debug result can establish
    success; the returned job is merely queued.
    """
    import copy,hashlib
    from .actor_context import current_actor
    from . import business_flow_debug as debug,business_flows,workspace
    from .business_flow_engine import digest
    from .config import DATA
    from .store import all_items,get,put,unseal
    pid=job['project_id'];actor=current_actor();fid=job.get('flow_id');rid=job['id']
    if job.get('kind')!='business_flow_debug' or actor!=job.get('actor_id') or job.get('status')!='failed' or job.get('repair_of'):return None
    original,payload=debug._read_job(pid,fid,rid,actor)
    envelope=unseal(original.get('secret_debug_checkpoint',''));state=envelope.get('state') or {};steps=state.get('steps')
    if state.get('status')!='failed' or not isinstance(steps,list):return None
    failed=[s for s in steps if isinstance(s,dict) and s.get('status')=='failed']
    if len(failed)!=1 or failed[0].get('error_code')!='SOURCE_VERSION_CHANGED' or steps[-1] is not failed[0]:return None
    if envelope.get('kind')!='business_flow_debug' or envelope.get('publishable') is not False:return None
    doc=debug._document(pid,fid,job['project_commit'],job['commit'])
    if doc!=payload['document'] or envelope.get('scope',{}).get('flow_sha256')!=digest(doc):return None
    # A failed checkpoint is never resumed by run_debug; it verifies the
    # immutable full plan, selected subset and frozen inputs before returning.
    try:
        checked=debug.run_debug(doc,payload['inputs'],debug._handlers(),{},payload['selected_nodes'],
            allowed_kinds=debug.ALLOWED,checkpoint=envelope,persist=lambda _:None)
        if checked['state']!=state:return None
    except Exception:return None
    nodes=[n for n in doc['nodes'] if n['id']==failed[0].get('node_id')]
    if len(nodes)!=1 or nodes[0].get('kind')!='source_table':return None
    node=nodes[0];cfg=node['config'];inputs=copy.deepcopy(payload['inputs']);document=copy.deepcopy(doc)
    if node.get('depends_on') or any(ref.get('source')!='input' for ref in node.get('inputs',{}).values()):return None
    try:
        arguments={key:inputs[ref['name']] for key,ref in node.get('inputs',{}).items()}
        if failed[0].get('input_sha256')!=digest({'config':cfg,'inputs':arguments}):return None
    except (KeyError,TypeError):return None
    if cfg.get('source_id'):
        pin={'source_id':cfg.get('source_id'),'revision':cfg.get('revision'),'content_hash':cfg.get('content_hash')}
    else:
        ref=node.get('inputs',{}).get('source',{})
        if ref.get('source')!='input' or not isinstance(inputs.get(ref.get('name')),dict):return None
        pin=inputs[ref['name']]
    from .business_flow_data import SourcePin
    try:pin=SourcePin.model_validate(pin).model_dump()
    except Exception:return None
    refs=[v for v in all_items('source_version') if v.get('source_id')==pin['source_id'] and v.get('revision')==pin['revision'] and v.get('project_id')==pid]
    if len(refs)!=1:return None
    try:current=get('source',pin['source_id'])
    except KeyError:return None
    revision=verify_identical_source_revision(refs[0],current,pid,pin['source_id'],pin['content_hash'],DATA/'uploads',unseal)
    if revision is None:return None
    changed=False
    for entry in document['nodes']:
        c=entry.get('config',{})
        if entry.get('kind')=='source_table' and all(c.get(k)==v for k,v in pin.items()):c['revision']=revision;changed=True
    for value in inputs.values():
        if isinstance(value,dict) and value==pin:value['revision']=revision;changed=True
    if not changed:return None
    ident=hashlib.sha256((pid+'\0'+actor+'\0'+rid).encode()).hexdigest()
    document['name']=document['name'][:185]+'（自动修复副本）'
    candidate=business_flows.create(pid,actor,job['flow_turn_id'],document,'rfix_'+ident)
    with workspace.lock(pid):
        old=business_flows._owned(pid,fid,actor)
        if old.get('candidate_digest')!=job['commit'] or old.get('candidate_commit')!=job['project_commit']:return None
        # Source and authority are checked again before enqueue and once more
        # during the actual debug run; no original or published version changes.
        current=get('source',pin['source_id'])
        if verify_identical_source_revision(refs[0],current,pid,pin['source_id'],pin['content_hash'],DATA/'uploads',unseal)!=revision:return None
        request=debug.DebugInput(request_id='rfix_'+ident,expected_digest=candidate['candidate_digest'],selected_nodes=payload['selected_nodes'],inputs=inputs)
        result=debug.submit(pid,candidate['id'],actor,request,repair_of=rid)
        put('business_flow_auto_repair',{'project_id':pid,'actor_id':actor,'original_run_id':rid,
            'flow_id':candidate['id'],'run_id':result['run_id']},'bfr_'+ident)
    return {'flow_id':candidate['id'],'run_id':result['run_id'],'status':result['status'],'publishable':False}


def enqueue_diagnostic(job):
    """Idempotently start a restricted, visible diagnosis for a manual failure."""
    import hashlib
    from . import business_flows, conversation
    from .actor_context import current_actor
    from .store import get
    pid=job['project_id'];actor=current_actor();kind=job.get('kind');fid=job.get('flow_id') if kind=='business_flow_debug' else job['target'];rid=job['id']
    if actor!=job.get('actor_id') or kind not in ('business_flow','business_flow_debug') or job.get('schedule_snapshot'):
        raise ValueError('运行不属于当前用户或不是手动业务流程')
    if kind=='business_flow_debug':
        from .business_flow_debug import status
        run=status(pid,fid,rid,actor);tool='inspect_business_flow_debug_run'
    else:
        run=business_flows.get_run(pid,fid,rid,actor);tool='inspect_business_flow_run'
    if guidance(run)['status']!='diagnosis_available':return None
    flow=business_flows.get_view(pid,fid,actor)
    if flow.get('candidate_digest')!=run.get('plan_digest') or flow.get('candidate_commit')!=run.get('commit'):
        return None  # Never bind old errors to a changed plan.
    origin=get('turn',job['flow_turn_id'])
    if origin.get('project_id')!=pid or origin.get('actor_id','owner')!=actor:raise ValueError('原始对话不可用')
    request='diagnose_'+hashlib.sha256((pid+'\0'+actor+'\0'+rid).encode()).hexdigest()
    text=('业务流程调试失败。请调用 '+tool+' 核对流程 '+fid+' 的运行 '+rid+'；'
          '分析已保存的错误及业务依据，明确可安全修复的内容。缺少财务口径、权限或事实时请询问用户。'
          '本次是只读诊断，不能执行外部动作、重跑、发布或声称已经修复。')
    value=conversation.TurnInput(text=text,session_id=origin.get('session_id'),interaction_mode='work',request_id=request,
        workflow_context=conversation.SelectedWorkflow(kind='business',id=fid,digest=run['plan_digest']))
    return conversation.submit(pid,value,trusted_fields={'auto_diagnostic':True})
