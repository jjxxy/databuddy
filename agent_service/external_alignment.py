"""Narrow authenticated-subject review of a submitted identical CSV.

This does not verify an external agent, the remote ancestry, or financial truth.
Only complete one-input, one-step select results are eligible. Every use
revalidates the authenticated upload, exact bytes, source, plan and result.
"""
import re,json
from . import project_data as data, workspace
from .actor_context import actor_scope,current_actor
from .data_workflows import digest, execute
from .handoff_anchor import check_anchor
from .jobs import get_job
from .result_delivery import read_all_handoff
from .store import get, put, seal, unseal, now, all_items
from .config import MAX_ROWS

WARNING='仅证明认证主体提交的文件与交接文件字节相同，口径为该主体的声明；不能证明提交者是真人、文件是原件、远端来源或祖先链真实，也不构成财务审批。'
KIND='external_alignment'


def preview(pid,rid,anchor_source_id):
    folder,meta=data.result_folder(pid,rid)
    run=get_job(rid)
    if run.get('status') not in ('validated','succeeded'):raise ValueError('仅可核对已验证结果')
    raw=run.get('workflow_document')
    if not isinstance(raw,dict) or digest(raw)!=meta['plan_sha256']:raise ValueError('冻结方案不可核对')
    doc=data.WorkflowDocument.model_validate(raw)
    target=data.owned(pid,'data_workflow',run['target'])
    if (rid not in (target.get('candidate_run'),target.get('published_run'))
            or digest(data.load_document(pid,target['path']).model_dump())!=meta['plan_sha256']):
        raise ValueError('当前方案或结果已变化，请重新运行并核对')
    if len(doc.bindings)!=1 or len(doc.plan.inputs)!=1 or len(doc.plan.stages)!=1 or doc.contract is not None or doc.reviewed_mapping_ids:
        raise ValueError('仅支持单一来源的完整字段直接选取；其他加工仍需原有复核')
    alias,binding=next(iter(doc.bindings.items()));stage=doc.plan.stages[0]
    if (not binding.source_id or binding.result_id or binding.options!=data.ReadOptions()
            or stage.kind!='select' or stage.input!=alias or doc.plan.output!=stage.id or set(doc.plan.inputs)!={alias}
            or any(k.startswith('__') for k in binding.column_map)):
        raise ValueError('仅支持完整原表选取，不支持筛选或其他加工')
    snapshot=run.get('source_snapshot');source=snapshot.get(binding.source_id) if isinstance(snapshot,dict) else None
    if not isinstance(source,dict):raise ValueError('来源快照缺失')
    current=data.owned(pid,'source',binding.source_id)
    local=data.owned(pid,'source',anchor_source_id)
    if (type(current.get('bytes')) is not int or current['bytes']>5*1024*1024
            or type(local.get('bytes')) is not int or local['bytes']!=current['bytes']
            or type(meta.get('records')) is not int or meta['records']>10000):
        raise ValueError('核对上限为 5 MB、10000 行；资料超出范围，当前不能自动核对，请分段处理并人工检查')
    for key in ('id','project_id','kind','revision','revision_id','content_hash','secret_config','origin_agent_handoff','external_provenance'):
        if source.get(key)!=current.get(key):raise ValueError('交接来源版本已变化')
    if any(key in source for key in ('origin_source_id','origin_source_revision','fact_review_id','visual_fact_review_id','browser_download_id','local_origin')):
        raise ValueError('派生、抽取和网页资料不可通过此核对')
    anchor=check_anchor(pid,binding.source_id,anchor_source_id)
    rows,fields,evidence,_=data.read_input(pid,binding,frozen_source=source)
    schema=doc.plan.inputs[alias];names=[c.name for c in schema.columns]
    if len(names)>50:raise ValueError('核对最多支持 50 个字段，不能省略字段后当作完整结果')
    if (not names or not schema.grain or [f['name'] for f in fields]!=names or stage.columns!=names
            or any(c.data_type in ('decimal','integer') and (not c.period or c.aggregation=='unknown') for c in schema.columns)):
        raise ValueError('需按原顺序保留全部字段，并明确粒度、数字字段期间及可加性')
    page=read_all_handoff(folder/'handoff',expected_manifest_sha256=meta['handoff_sha256'],max_rows=MAX_ROWS)
    ctx=page.get('context')
    if (not isinstance(ctx,dict) or ctx.get('project_id')!=pid or ctx.get('plan_sha256')!=meta['plan_sha256']
            or ctx.get('workflow_id')!=run['target'] or ctx.get('input_evidence',{}).get(alias)!=evidence
            or page['total']!=len(rows) or meta['records']!=len(rows)):
        raise ValueError('冻结输入证据与结果不一致')
    output=execute(doc.plan,{alias:rows})['output']
    if (page['rows']!=output['rows'] or page['schema']!=output['schema']['columns']
            or ctx.get('output_semantics')!=output['schema']):
        raise ValueError('完整输出与提交文件或字段语义不一致')
    content={'project_id':pid,'run_id':rid,'anchor_source_id':anchor_source_id,'anchor_revision':anchor['anchor_revision'],
             'source_id':binding.source_id,'source_sha256':anchor['external_content_sha256'],
             'plan_sha256':meta['plan_sha256'],'handoff_sha256':meta['handoff_sha256'],
             'fields':[dict(c.model_dump(),original_label=f.get('label',''),original_name=next((k for k,v in binding.column_map.items() if v==c.name),c.name)) for c,f in zip(schema.columns,fields)],
             'grain':schema.grain,'stage_summaries':[stage.model_dump()],
             'anchor_binding':anchor,'source_snapshot_sha256':digest(snapshot),
             'input_evidence_sha256':digest(ctx['input_evidence']),'result_sha256':output['sha256'],
             'notice':WARNING}
    return dict(content,preview_sha256=digest(content))


def _ident(pid,rid,preview_hash,actor):return 'ea_'+digest([pid,rid,preview_hash,actor])[:40]


def confirm(pid,rid,value,actor):
    from .job_authority import client_guard
    # Discover the lock from verified server-side evidence, never from the caller.
    with actor_scope(actor):
        first=preview(pid,rid,value.anchor_source_id)
        client=first['anchor_binding']['gateway_client_id']
        with client_guard(client),workspace.lock(pid):
            result=preview(pid,rid,value.anchor_source_id)
            if result['preview_sha256']!=value.expected_preview_sha256:raise ValueError('资料、方案或结果已变化，请重新核对')
            names=[field['name'] for field in result['fields']]
            if len(value.confirmed_fields)!=len(names) or len(set(value.confirmed_fields))!=len(names) or set(value.confirmed_fields)!=set(names):
                raise ValueError('必须逐项确认所有字段')
            if value.confirm_original is not True or value.confirm_plan is not True or len(value.note.strip())<4:
                raise ValueError('请确认文件、方案并填写核对依据')
            ident=_ident(pid,rid,result['preview_sha256'],actor)
            try: existing=get(KIND,ident)
            except KeyError: existing=None
            if existing is None:
                receipt={'version':1,'project_id':pid,'run_id':rid,'reviewer':actor,'anchor_source_id':value.anchor_source_id,
                         'preview_sha256':result['preview_sha256'],'confirmed_fields':names,'note':value.note.strip(),
                         'reviewed_at':now(),'scope':'independent_csv_full_select_only'}
                put(KIND,{'project_id':pid,'run_id':rid,'receipt':seal(receipt)},ident)
            else:
                receipt=unseal(existing['receipt'])
                if (existing.get('project_id')!=pid or existing.get('run_id')!=rid or receipt.get('reviewer')!=actor
                        or receipt.get('preview_sha256')!=result['preview_sha256'] or receipt.get('confirmed_fields')!=names):
                    raise ValueError('已有核对记录不一致')
            return {'run_id':rid,'confirmed':True,'preview_sha256':result['preview_sha256']}


def receipt_details(pid,rid):
    # Informational only: this never authorizes publishing, exporting or reuse.
    for item in all_items(KIND):
        try:
            if item.get('project_id')!=pid or item.get('run_id')!=rid:continue
            receipt=unseal(item['receipt'])
            if (receipt.get('version')!=1 or receipt.get('project_id')!=pid or receipt.get('run_id')!=rid
                    or receipt.get('scope')!='independent_csv_full_select_only'
                    or not isinstance(receipt.get('reviewer'),str)
                    or receipt.get('reviewer')!=current_actor()
                    or not re.fullmatch(r'owner|u_[a-f0-9]{64}',receipt['reviewer'])
                    or not isinstance(receipt.get('note'),str) or len(receipt['note'])<4):continue
            with actor_scope(receipt['reviewer']):
                result=preview(pid,rid,receipt['anchor_source_id'])
            if (receipt['preview_sha256']==result['preview_sha256']
                    and receipt['confirmed_fields']==[field['name'] for field in result['fields']]):
                return {'anchor_source_id':receipt['anchor_source_id'],'preview_sha256':receipt['preview_sha256']}
        except Exception:
            continue
    return None


def receipt_valid(pid,rid):return bool(receipt_details(pid,rid))


def local_candidate(pid,rid,anchor_source_id,expected,actor):
    """Create a separate ordinary-source run, never authorize the old result."""
    from .job_authority import client_guard
    with actor_scope(actor):
        first=preview(pid,rid,anchor_source_id)
        with client_guard(first['anchor_binding']['gateway_client_id']),workspace.lock(pid):
            reviewed=preview(pid,rid,anchor_source_id)
            if reviewed['preview_sha256']!=expected:raise ValueError('资料或方案已变化，请重新核对')
            ident=_ident(pid,rid,expected,actor)
            try:item=get(KIND,ident);receipt=unseal(item['receipt'])
            except Exception:raise ValueError('请先由当前认证主体逐字段确认') from None
            if (item.get('project_id')!=pid or item.get('run_id')!=rid or receipt.get('reviewer')!=actor
                    or receipt.get('project_id')!=pid or receipt.get('run_id')!=rid
                    or receipt.get('preview_sha256')!=expected or receipt.get('anchor_source_id')!=anchor_source_id
                    or receipt.get('confirmed_fields')!=[f['name'] for f in reviewed['fields']]):
                raise ValueError('当前主体的核对记录不可用')
            original=data.WorkflowDocument.model_validate(get_job(rid)['workflow_document']).model_dump()
            alias=next(iter(original['bindings']))
            original['bindings'][alias]['source_id']=anchor_source_id
            doc=data.WorkflowDocument.model_validate(original)
            plan_hash=digest(doc.model_dump())
            path='workflows/local-review-'+digest([pid,rid,expected,actor])[:40]+'.json'
            try:existing=workspace.read_file(pid,path)
            except FileNotFoundError:
                workspace.write_file(pid,path,json.dumps(doc.model_dump(),ensure_ascii=False,indent=2))
            else:
                if digest(data.WorkflowDocument.model_validate_json(existing).model_dump())!=plan_hash:
                    raise ValueError('本地候选方案已变化，请重新核对')
            wid=data.register(pid,path)['workflow_id']
            key='local_review_'+digest([pid,rid,expected,actor])
            job=data._enqueue_workflow(pid,wid,scheduled_key=key,expected_draft_sha256=plan_hash,
                expected_source_versions={anchor_source_id:(reviewed['anchor_revision'],reviewed['source_sha256'])},
                local_alignment_candidate={'source_run_id':rid,'review_sha256':expected,'reviewer':actor})
            snap=job.get('source_snapshot',{}).get(anchor_source_id,{})
            if (snap.get('revision')!=reviewed['anchor_revision'] or snap.get('content_hash')!=reviewed['source_sha256']
                    or job.get('commit')!=plan_hash):raise ValueError('本地候选版本已变化')
            return {'workflow_id':wid,'run_id':job['id'],'status':job['status'],'plan_sha256':plan_hash}
