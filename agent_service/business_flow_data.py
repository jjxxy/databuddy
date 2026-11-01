"""Version-bound file parsing and typed table processing for business flows.

These handlers perform actual reads/calculations. A successful run proves only
that declared technical checks ran; source interpretation and finance approval
remain separate obligations carried in the output.
"""
from __future__ import annotations

import json
import base64
import hashlib
import os
import secrets
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from pydantic import Field, model_validator

from .models import StrictModel
from .business_flow_engine import FlowStepError
from .project_data import InputBinding, ReadOptions
from .data_workflows import WorkflowPlan, canonical


class SourceConfig(StrictModel):
    source_id: str | None = Field(default=None,min_length=1,max_length=64)
    revision: int | None = Field(default=None,ge=1,strict=True)
    content_hash: str | None = Field(default=None,pattern=r'^[a-f0-9]{64}$')
    options: ReadOptions = Field(default_factory=ReadOptions)
    column_map: dict[str,str] = Field(default_factory=dict,max_length=200)

    @model_validator(mode='after')
    def complete_pin(self):
        fields=(self.source_id,self.revision,self.content_hash)
        if any(v is not None for v in fields) and any(v is None for v in fields):
            raise ValueError('固定来源必须同时包含资料、版本和内容摘要')
        return self


class SourcePin(StrictModel):
    source_id: str = Field(min_length=1,max_length=64)
    revision: int = Field(ge=1,strict=True)
    content_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


class FilterParameterBinding(StrictModel):
    stage_id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,63}$')
    condition_index: int = Field(ge=0,le=49,strict=True)


class TableConfig(StrictModel):
    plan: WorkflowPlan
    parameter_bindings: dict[str,FilterParameterBinding] = Field(default_factory=dict,max_length=50)

    @model_validator(mode='after')
    def validate_bindings(self):
        import re
        seen=set();stages={stage.id:stage for stage in self.plan.stages}
        for alias,binding in self.parameter_bindings.items():
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',alias) or alias in self.plan.inputs:
                raise ValueError('筛选参数名称无效或与数据输入冲突')
            key=(binding.stage_id,binding.condition_index)
            if key in seen:raise ValueError('同一筛选条件不能绑定多个参数')
            seen.add(key)
            stage=stages.get(binding.stage_id)
            if not stage or stage.kind!='filter' or stage.input not in self.plan.inputs or binding.condition_index>=len(stage.conditions):
                raise ValueError('筛选参数只能绑定原始输入表中已声明的筛选条件')
            condition=stage.conditions[binding.condition_index]
            if condition.operator in ('in','not_in','is_null','not_null'):
                raise ValueError('该筛选操作不支持单值参数')
            columns={column.name:column for column in self.plan.inputs[stage.input].columns}
            if condition.column not in columns:raise ValueError('筛选参数引用的字段不存在')
            from .data_workflows import _coerce
            _coerce(condition.value,columns[condition.column])
        return self


def validate_source(config):SourceConfig.model_validate(config)
def validate_table(config):TableConfig.model_validate(config)


def _authorized_turn(context):
    # Reuse the model/Skill adapter's actual project, actor, task and gateway
    # authorization checks, including revoked/cancelled turn boundaries.
    from .workflow_model_skill import _turn
    return _turn(context)



# Large table rows are not copied into the encrypted database checkpoint or the
# model prompt. The typed engine is still in-memory and deliberately bounded;
# these references solve checkpoint size, not streaming or unlimited data.
_INLINE_ROWS_LIMIT=1024*1024
_MAX_TABLE_BYTES=64*1024*1024
_MAX_RUN_BYTES=256*1024*1024
_DEFAULT_TOTAL_MB=2048


def _table_scope(context):
    fields=('project_id','actor_id','turn_id','run_id')
    if any(not isinstance(context.get(k),str) or not context[k] for k in fields):
        raise ValueError('大数据步骤缺少可恢复的运行身份')
    return hashlib.sha256(canonical({k:context[k] for k in fields})).hexdigest()


def _table_folder(context):
    from .config import DATA
    scope=_table_scope(context)
    root=DATA/'business-flow-tables'
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    if root.is_symlink() or root.resolve()!=root.absolute():raise ValueError('数据快照目录无效')
    folder=root/scope
    folder.mkdir(mode=0o700,exist_ok=True)
    if folder.is_symlink() or folder.resolve()!=folder.absolute():raise ValueError('数据快照目录无效')
    return folder,scope


def _table_key(scope):
    from .store import cipher
    from .config import DATA
    cipher() # Exclusive key initialization shared with encrypted checkpoints.
    master=base64.urlsafe_b64decode((DATA/'connector.key').read_bytes())
    return HKDF(algorithm=hashes.SHA256(),length=32,salt=bytes.fromhex(scope),
                info=b'atlas-business-flow-table-v1').derive(master)


def _save_rows(context,rows,raw):
    if len(raw)>_MAX_TABLE_BYTES:
        raise FlowStepError('BYTE_BUDGET','完整数据超过当前步骤的安全容量，未截断或发布')
    folder,scope=_table_folder(context)
    sha=hashlib.sha256(raw).hexdigest()
    target=folder/(sha+'.enc')
    ref={'version':1,'sha256':sha,'bytes':len(raw),'row_count':len(rows),'scope':scope}
    from .locks import FileLock
    from .config import DATA
    try:total_mb=int(os.getenv('ATLAS_FLOW_SNAPSHOT_TOTAL_MB',str(_DEFAULT_TOTAL_MB)))
    except ValueError:raise ValueError('数据快照存储配额配置无效') from None
    if not 1<=total_mb<=131072:raise ValueError('数据快照存储配额配置无效')
    root=DATA/'business-flow-tables'
    # One cross-process lock serializes quota accounting and atomic publication,
    # including across workers when the configured shared filesystem/DB locks
    # are available. We never evict retained snapshots to make room.
    with FileLock(str(DATA/'business-flow-tables.lock'),timeout=10):
        if target.exists():
            # A corrupt or altered existing snapshot must not be silently reused.
            _read_rows(context,ref)
            return ref
        used=sum(p.stat().st_size for p in folder.iterdir() if p.is_file() and not p.is_symlink())
        if used+len(raw)+28>_MAX_RUN_BYTES:
            raise FlowStepError('BYTE_BUDGET','本次运行的完整数据快照超过安全容量，未截断或发布')
        total=0
        for parent,dirs,files in os.walk(root,followlinks=False):
            dirs[:]=[d for d in dirs if not (Path(parent)/d).is_symlink()]
            for name in files:
                item=Path(parent)/name
                if item.is_file() and not item.is_symlink():total+=item.stat().st_size
        if total+len(raw)+28>total_mb*1024*1024:
            raise FlowStepError('BYTE_BUDGET','数据快照存储容量不足，请按保留策略处理空间后重试；未删除历史结果')
        nonce=secrets.token_bytes(12)
        encrypted=nonce+AESGCM(_table_key(scope)).encrypt(nonce,raw,scope.encode())
        fd,name=tempfile.mkstemp(prefix='.pending-',dir=folder)
        try:
            os.fchmod(fd,0o600)
            with os.fdopen(fd,'wb') as out:
                out.write(encrypted);out.flush();os.fsync(out.fileno())
            # Lock serializes writers for this run and id; publishing is atomic.
            os.replace(name,target)
        finally:
            if os.path.exists(name):os.unlink(name)
    return ref


def _read_rows(context,ref):
    if not isinstance(ref,dict) or set(ref)!={'version','sha256','bytes','row_count','scope'} or ref.get('version')!=1:
        raise ValueError('数据快照引用无效')
    import re
    if (not isinstance(ref.get('sha256'),str) or not re.fullmatch('[a-f0-9]{64}',ref['sha256'])
            or type(ref.get('bytes')) is not int or not 0<ref['bytes']<=_MAX_TABLE_BYTES
            or type(ref.get('row_count')) is not int or not 0<=ref['row_count']<=1000000
            or ref.get('scope')!=_table_scope(context)):
        raise ValueError('数据快照与当前运行不一致')
    folder,scope=_table_folder(context);path=folder/(ref['sha256']+'.enc')
    if path.is_symlink() or not path.is_file() or path.stat().st_size!=ref['bytes']+28:
        raise ValueError('数据快照丢失或大小不一致')
    try:
        encrypted=path.read_bytes()
        raw=AESGCM(_table_key(scope)).decrypt(encrypted[:12],encrypted[12:],scope.encode())
        if hashlib.sha256(raw).hexdigest()!=ref['sha256']:raise ValueError('数据快照摘要不一致')
        rows=json.loads(raw)
    except Exception:
        raise ValueError('数据快照无法解密或完整性校验失败') from None
    if not isinstance(rows,list) or len(rows)!=ref['row_count'] or any(not isinstance(row,dict) for row in rows):
        raise ValueError('数据快照行数或格式不一致')
    return rows


def _compact_table(context,value):
    rows=value.get('rows')
    if not isinstance(rows,list):raise ValueError('数据快照行格式无效')
    raw=canonical(rows)
    if len(raw)<=_INLINE_ROWS_LIMIT:
        return json.loads(canonical(value))
    ref=_save_rows(context,rows,raw)
    result=dict(value);del result['rows'];result['rows_ref']=ref
    return json.loads(canonical(result))


def materialize_table(context,value):
    if not isinstance(value,dict):raise ValueError('数据表无效')
    if 'rows_ref' not in value:return value
    if 'rows' in value:raise ValueError('数据表不能同时包含明细与快照引用')
    result=dict(value);result['rows']=_read_rows(context,result.pop('rows_ref'))
    return result


def source_table(context, config, inputs):
    turn=_authorized_turn(context);cfg=SourceConfig.model_validate(config)
    if cfg.source_id is None:
        if not isinstance(inputs,dict) or set(inputs)!={'source'}:raise ValueError('运行时需要明确固定的资料版本')
        pin=SourcePin.model_validate(inputs['source'])
    else:
        if inputs:raise ValueError('固定资料不能再指定其他来源')
        pin=SourcePin(source_id=cfg.source_id,revision=cfg.revision,content_hash=cfg.content_hash)
    from . import agent_context,agent_gateway,agent_team
    agent_gateway.assert_turn_allowed(turn)
    agent_team.assert_turn_allowed(turn,'read_source_table',{'source_id':pin.source_id})
    if 'read_source_table' not in agent_context.allowed(turn):raise ValueError('当前任务无权读取来源资料')
    from .project_data import owned,read_input
    source=owned(turn['project_id'],'source',pin.source_id)
    if (source.get('kind')!='file' or source.get('status')!='ready' or source.get('archived')
            or source.get('revision')!=pin.revision or source.get('content_hash')!=pin.content_hash):
        raise FlowStepError('SOURCE_VERSION_CHANGED','资料版本已变化或不可用，请重新确认并运行')
    binding=InputBinding(source_id=pin.source_id,options=cfg.options,column_map=cfg.column_map)
    rows,fields,evidence,provenance=read_input(turn['project_id'],binding,frozen_source=source)
    current=owned(turn['project_id'],'source',pin.source_id)
    if (current.get('revision')!=pin.revision or current.get('content_hash')!=pin.content_hash
            or current.get('archived') or current.get('status')!='ready'):
        raise FlowStepError('SOURCE_VERSION_CHANGED','读取期间资料版本已变化，请重新确认并运行')
    # Full file reads preserve duplicate transactions. A snapshot-scoped row
    # coordinate gives the typed engine a unique grain without silently
    # identifying equal-valued transactions as duplicates. It is not a
    # business key and is not stable if a later source snapshot is reordered.
    if any('atlas_row' in row for row in rows) or any(f.get('name')=='atlas_row' for f in fields):
        raise ValueError('来源字段与内部行标识冲突，请调整字段映射')
    rows=[dict(row,atlas_row=str(index)) for index,row in enumerate(rows,1)]
    fields=list(fields)+[{'name':'atlas_row','label':'本次资料行号','numeric':False}]
    # canonical encodes Decimal values exactly as strings and rejects binary
    # floats for numeric execution through the existing typed workflow engine.
    return _compact_table(context,{'kind':'source_table_v1','source_id':pin.source_id,
        'revision':pin.revision,'content_hash':pin.content_hash,'rows':rows,'fields':fields,
        'row_identity':'snapshot_scoped_ordinal_not_business_key',
        'complete':True,'requires_source_review':bool(evidence.get('requires_source_review')),
        'requires_semantic_review':bool(evidence.get('requires_semantic_review')),
        'evidence':evidence,'provenance':provenance})


def table_workflow(context, config, inputs):
    turn=_authorized_turn(context);cfg=TableConfig.model_validate(config)
    from . import agent_context,agent_gateway,agent_team
    agent_gateway.assert_turn_allowed(turn)
    agent_team.assert_turn_allowed(turn,'run_data_workflow',{})
    if 'run_data_workflow' not in agent_context.allowed(turn):raise ValueError('当前任务无权运行数据加工')
    if not isinstance(inputs,dict) or set(inputs)!=(set(cfg.plan.inputs)|set(cfg.parameter_bindings)):
        raise ValueError('加工输入与已确认的数据步骤不一致')
    tables={};source_review=False;semantic_review=False
    for alias in cfg.plan.inputs:
        value=inputs[alias]
        if (not isinstance(value,dict) or value.get('kind') not in ('source_table_v1','table_workflow_v1')
                or value.get('complete') is not True):
            raise ValueError('加工输入不是完整数据来源或已完成的数据步骤')
        value=materialize_table(context,value)
        if not isinstance(value.get('rows'),list):raise ValueError('加工输入的数据表不完整')
        tables[alias]=value['rows']
        source_review |= value.get('requires_source_review') is True
        semantic_review |= value.get('requires_semantic_review') is True
    # User parameters can replace only the declared scalar filter values,
    # never columns, operators, formulas, source identity or SQL. Validate
    # against the fixed table schema even if the table happens to contain no
    # matching rows, and re-validate the complete plan before execution.
    from .data_workflows import execute,_coerce
    plan=cfg.plan.model_dump(mode='json')
    stages={stage['id']:stage for stage in plan['stages']}
    for alias,binding in cfg.parameter_bindings.items():
        stage=stages[binding.stage_id];condition=stage['conditions'][binding.condition_index]
        columns={column.name:column for column in cfg.plan.inputs[stage['input']].columns}
        _coerce(inputs[alias],columns[condition['column']])
        condition['value']=inputs[alias]
    result=execute(WorkflowPlan.model_validate(plan),tables)
    return _compact_table(context,{'kind':'table_workflow_v1','complete':result['complete'],'rows':result['output']['rows'],
        'schema':result['output']['schema'],'row_count':result['output']['row_count'],
        'output_sha256':result['output']['sha256'],'plan_sha256':result['plan_sha256'],
        'input_sha256':result['input_sha256'],'stage_receipts':result['stages'],
        'requires_source_review':source_review,
        'requires_semantic_review':semantic_review or result.get('requires_semantic_review') is True,
        'financial_evidence_status':'requires_business_review'})
