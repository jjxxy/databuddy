"""Build an explicit append-only candidate from two adopted result snapshots.

This is not source CDC: both inputs must already be processed, and the caller
must establish that the supplied delta is complete. The output is materialized
as a new candidate, not silently published or automatically scheduled.
"""
import hashlib
import json
from pydantic import Field

from .models import StrictModel
from .data_workflows import TableSchema, digest
from .interactive_analysis import _checked_grain, MAX_ANALYSIS_BYTES
from .result_delivery import read_all_handoff
from .config import MAX_ROWS


class AppendInput(StrictModel):
    baseline_run_id: str = Field(min_length=1,max_length=64)
    baseline_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    delta_run_id: str = Field(min_length=1,max_length=64)
    delta_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{16,80}$')
    confirm_append_only: bool = Field(strict=True)
    note: str = Field(min_length=4,max_length=500)


def _verified(pid,rid,checksum):
    from . import project_data as data
    from .jobs import get_job
    run=get_job(rid)
    if run.get('project_id')!=pid or run.get('kind')!='data_workflow' or run.get('status')!='succeeded':
        raise ValueError('仅能使用当前项目已采用的正式结果')
    parent=data.owned(pid,'data_workflow',run['target'])
    if parent.get('published_run')!=rid:raise ValueError('输入结果已不是当前正式版本，请刷新')
    folder,meta=data.result_folder(pid,rid)
    if meta['handoff_sha256']!=checksum:raise ValueError('输入结果版本已变化，请刷新')
    data.ensure_candidate_bulk_allowed(pid,rid)
    root=folder/'handoff'
    if sum((root/name).stat().st_size for name in ('rows.jsonl','provenance.jsonl','schema.json','context.json','stages.json'))>MAX_ANALYSIS_BYTES:
        raise ValueError('追加核验单个输入上限为20 MiB，未截断；请缩小范围')
    page=read_all_handoff(root,expected_manifest_sha256=checksum,max_rows=MAX_ROWS)
    if page['total']!=len(page['rows']) or page['total']!=meta['records'] or not page.get('complete_result_available'):
        raise ValueError('输入结果不完整，不能追加')
    data.require_safe_numeric_lineage(pid,rid,root_meta=meta,root_page=page)
    schema=TableSchema.model_validate(page.get('context',{}).get('output_semantics') or page['schema'])
    if not schema.grain:raise ValueError('追加合并要求明确且非空的业务唯一粒度')
    return run,meta,page,schema


def create(pid,actor,value):
    from . import workspace,project_data as data
    from .workspace_lifecycle import _project_lock,assert_project_live
    value=AppendInput.model_validate(value)
    if not value.confirm_append_only or len(value.note.strip())<4:raise ValueError('请确认这是完整的新增明细并填写核对依据')
    if value.baseline_run_id==value.delta_run_id:raise ValueError('历史结果和新增结果不能相同')
    with workspace.lock(pid),_project_lock(pid):
        assert_project_live(pid)
        base,bmeta,bpage,bschema=_verified(pid,value.baseline_run_id,value.baseline_sha256)
        delta,dmeta,dpage,dschema=_verified(pid,value.delta_run_id,value.delta_sha256)
        # source_ref denotes lineage, not business semantics; all other
        # declarations, including units, period, labels and grain, must match.
        bshape=bschema.model_dump();dshape=dschema.model_dump()
        bshape.pop('source_ref',None);dshape.pop('source_ref',None)
        if bshape!=dshape:raise ValueError('两份结果字段、期间、单位或业务粒度不一致，不能追加')
        if bpage['total']+dpage['total']>MAX_ROWS:raise ValueError('合并超过当前行数上限；未截断数据')
        if not dpage['rows']:raise ValueError('新增结果为空，不能确认本次追加')
        _checked_grain(bpage['rows']+dpage['rows'],bschema)
        plan={'version':1,'plan':{'name':'追加已核对明细','inputs':{'history':bschema.model_dump(),'delta':dschema.model_dump()},
              'stages':[{'id':'combined','kind':'union_rows','description':'保留历史明细并追加已核对新增明细','inputs':['history','delta']}],
              'output':'combined'},'bindings':{'history':{'result_id':value.baseline_run_id},'delta':{'result_id':value.delta_run_id}},
              'contract':{'version':1,'not_null':bschema.grain,'unique':[bschema.grain]}}
        document=data.WorkflowDocument.model_validate(plan);plan_hash=digest(document.model_dump())
        # One immutable request identity: repeating is safe; reusing its ID
        # with different versions, scope or attestation is rejected.
        pair=[pid,actor,value.baseline_run_id,value.baseline_sha256,value.delta_run_id,value.delta_sha256]
        ident=hashlib.sha256(json.dumps(pair,separators=(',',':')).encode()).hexdigest()
        request_key='ar_'+hashlib.sha256(json.dumps([pid,actor,value.request_id],separators=(',',':')).encode()).hexdigest()[:40]
        path='workflows/append-'+ident[:40]+'.json'
        attestation={'version':1,'baseline_run_id':value.baseline_run_id,'baseline_sha256':value.baseline_sha256,
                     'delta_run_id':value.delta_run_id,'delta_sha256':value.delta_sha256,'actor':actor,'note':value.note.strip()}
        from .store import get,put,seal,unseal
        try:previous=get('append_request',request_key)
        except KeyError:previous=None
        if previous is not None:
            try:old_request=unseal(previous['sealed'])
            except Exception:raise ValueError('追加请求记录无法核验') from None
            if (previous.get('project_id')!=pid or old_request!={'pair':pair,'note':value.note.strip()}):
                raise ValueError('请求编号已用于不同版本或核对依据')
        else:put('append_request',{'project_id':pid,'sealed':seal({'pair':pair,'note':value.note.strip()})},request_key)
        # Bind the attestation to a separate workspace file so changes are
        # versioned and the original facts are not inserted into the plan.
        receipt_path='workflows/append-'+ident[:40]+'.review.json'
        for filename,body in ((path,document.model_dump()),(receipt_path,attestation)):
            serialized=json.dumps(body,ensure_ascii=False,indent=2)
            try:old=workspace.read_file(pid,filename)
            except FileNotFoundError:workspace.write_file(pid,filename,serialized)
            else:
                try:matching=digest(json.loads(old))==digest(body)
                except (ValueError,TypeError):matching=False
                if not matching:raise ValueError('请求编号已用于不同版本或核对依据')
        workflow=data.register(pid,path)
        if workflow['draft_sha256']!=plan_hash:raise ValueError('追加方案已变化，请重新核对')
        # Fixed result IDs and hashes remain part of the input evidence. Never
        # reuse a key for another run/version, even after an interrupted call.
        job=data._enqueue_workflow(pid,workflow['workflow_id'],scheduled_key='append_'+ident,
                                   expected_draft_sha256=plan_hash)
        version=workspace._checkpoint(pid,None,'建立追加明细做法')
        return {'workflow_id':workflow['workflow_id'],'run_id':job['id'],'status':job['status'],
                'plan_sha256':plan_hash,'workspace_version':version['commit'],
                'baseline_run_id':value.baseline_run_id,'baseline_sha256':value.baseline_sha256,
                'delta_run_id':value.delta_run_id,'delta_sha256':value.delta_sha256,
                'baseline_rows':bpage['total'],'delta_rows':dpage['total'],'total_rows':bpage['total']+dpage['total'],
                'grain':bschema.grain,'notice':'将选中的固定历史结果与新增结果追加为待核对候选；输入可能已更新，本次不会自动切换版本。仍会完整写出结果，不是 CDC 或自动增量采集。新增数据是否完整由提交者确认。'}
