"""Version-bound file parsing and typed table processing for business flows.

These handlers perform actual reads/calculations. A successful run proves only
that declared technical checks ran; source interpretation and finance approval
remain separate obligations carried in the output.
"""
from __future__ import annotations

import json
import base64
import hashlib
import hmac
import os
import secrets
import tempfile
from pathlib import Path
from typing import Literal

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


class ReadBasis(StrictModel):
    """The actual table selection and declared mapping, not business approval."""
    version: Literal[1]
    options: ReadOptions
    column_map: dict[str,str] = Field(max_length=200)
    selection: dict


def _read_basis(basis,expected_hash=None,source_hash=None):
    # Refuse oversized or altered lineage rather than silently dropping it.
    if not isinstance(basis,dict):raise ValueError('资料读取依据无效')
    raw=canonical(basis)
    if len(raw)>128*1024:raise ValueError('资料读取依据超出容量')
    checksum=hashlib.sha256(raw).hexdigest()
    if expected_hash is not None and checksum!=expected_hash:raise ValueError('资料读取依据摘要不一致')
    parsed=ReadBasis.model_validate(basis)
    selection=parsed.selection;kind=selection.get('kind')
    if kind=='workbook_table':
        if (selection.get('file_sha256')!=source_hash or not isinstance(selection.get('sheet'),str)
                or not isinstance(selection.get('range'),list) or len(selection['range'])!=4
                or not isinstance(selection.get('header_rows'),list) or not isinstance(selection.get('data_rows'),list)):
            raise ValueError('工作簿实际读取范围无效')
    elif kind=='html_table':
        if not isinstance(selection.get('table_id'),str) or not selection['table_id']:
            raise ValueError('网页表格读取依据无效')
    elif kind=='json_records':
        path=selection.get('records_path');method=selection.get('selection_method')
        if (not isinstance(path,str) or method not in ('explicit','inferred','root_array','root_object')
                or (method in ('explicit','inferred')) != bool(path)):
            raise ValueError('JSON 记录读取路径无效')
        context=selection.get('wrapper_context_fields',[])
        if not isinstance(context,list) or any(not isinstance(field,str) for field in context):
            raise ValueError('JSON 记录上下文无效')
        if selection.get('wrapper_context_status') not in (None,'not_recorded'):
            raise ValueError('JSON 记录上下文状态无效')
    elif kind!='whole_source_table':raise ValueError('资料读取类型无效')
    return checksum


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
_DEFAULT_ACTOR_MB=1024
_DEFAULT_PROJECT_MB=512
_PAGE_MAGIC=b'ATLASP2\0'
_PAGE_ROWS=256
_MAX_INDEX_BYTES=2*1024*1024


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


def _index_key():
    from .store import cipher
    from .config import DATA
    cipher()
    master=base64.urlsafe_b64decode((DATA/'connector.key').read_bytes())
    return HKDF(algorithm=hashes.SHA256(),length=32,salt=None,
                info=b'atlas-business-flow-owner-index-v1').derive(master)


def _owner_claim(context,scope,key):
    # Opaque keyed identities do not put account or project names on disk.
    actor=hmac.new(key,canonical(['actor',context['actor_id']]),hashlib.sha256).hexdigest()
    project=hmac.new(key,canonical(['project',context['actor_id'],context['project_id']]),hashlib.sha256).hexdigest()
    value={'version':1,'scope':scope,'actor':actor,'project':project}
    value['mac']=hmac.new(key,canonical(value),hashlib.sha256).hexdigest()
    return value


def _scope_bytes(folder):
    """Only regular files in an exact scope directory contribute to quota."""
    total=0
    for item in folder.iterdir():
        if item.is_symlink():raise ValueError('数据快照目录包含不可用的链接')
        if item.is_file():total+=item.stat().st_size
        elif item.is_dir():raise ValueError('数据快照目录结构无效')
    return total


def _indexed_usage(root,claim,key):
    """Report attributed usage; unindexed legacy bytes remain global-only."""
    actor_bytes=project_bytes=unindexed_bytes=0
    for folder in root.iterdir():
        if folder.is_symlink():raise ValueError('数据快照目录包含不可用的链接')
        if not folder.is_dir():continue
        size=_scope_bytes(folder);marker=folder/'.owner.json'
        if not marker.exists():
            unindexed_bytes+=size;continue
        try:
            if marker.is_symlink() or marker.stat().st_size>2048:raise ValueError()
            saved=json.loads(marker.read_bytes())
            if set(saved)!={'version','scope','actor','project','mac'} or saved['version']!=1 or saved['scope']!=folder.name:
                raise ValueError()
            msg={k:saved[k] for k in ('version','scope','actor','project')}
            actual=hmac.new(key,canonical(msg),hashlib.sha256).hexdigest()
            if not isinstance(saved['mac'],str) or not hmac.compare_digest(saved['mac'],actual):raise ValueError()
        except (ValueError,TypeError,KeyError,OSError):
            raise ValueError('数据快照归属索引损坏，请管理员核对；未删除数据') from None
        if saved['actor']==claim['actor']:
            actor_bytes+=size
            if saved['project']==claim['project']:project_bytes+=size
    return actor_bytes,project_bytes,unindexed_bytes


def _quota_mb(name,default):
    try:value=int(os.getenv(name,str(default)))
    except ValueError:raise ValueError('数据快照存储配额配置无效') from None
    if not 1<=value<=131072:raise ValueError('数据快照存储配额配置无效')
    return value


def snapshot_storage_usage(project_id,actor_id):
    """Caller must authenticate current actor and project before invoking."""
    from .config import DATA
    from .locks import FileLock
    if not all(isinstance(x,str) and x for x in (project_id,actor_id)):raise ValueError('数据快照身份无效')
    root=DATA/'business-flow-tables';key=_index_key()
    claim=_owner_claim({'project_id':project_id,'actor_id':actor_id},'0'*64,key)
    with FileLock(str(DATA/'business-flow-tables.lock'),timeout=10):
        if root.exists() and (root.is_symlink() or root.resolve()!=root.absolute()):
            raise ValueError('数据快照目录无效')
        actor,project,unindexed=_indexed_usage(root,claim,key) if root.is_dir() else (0,0,0)
        return {'actor_bytes':actor,'project_bytes':project,'has_unindexed_legacy':bool(unindexed),
                'actor_limit_bytes':_quota_mb('ATLAS_FLOW_SNAPSHOT_ACTOR_MB',_DEFAULT_ACTOR_MB)*1024*1024,
                'project_limit_bytes':_quota_mb('ATLAS_FLOW_SNAPSHOT_PROJECT_MB',_DEFAULT_PROJECT_MB)*1024*1024,
                'global_limit_bytes':_quota_mb('ATLAS_FLOW_SNAPSHOT_TOTAL_MB',_DEFAULT_TOTAL_MB)*1024*1024,
                'note':'未归属历史快照只计入全局配额；不会自动清除历史。'}


def _save_rows(context,rows,raw):
    if len(raw)>_MAX_TABLE_BYTES:
        raise FlowStepError('BYTE_BUDGET','完整数据超过当前步骤的安全容量，未截断或发布')
    folder,scope=_table_folder(context)
    sha=hashlib.sha256(raw).hexdigest()
    target=folder/(sha+'.enc')
    ref={'version':1,'sha256':sha,'bytes':len(raw),'row_count':len(rows),'scope':scope}
    from .locks import FileLock
    from .config import DATA
    total_mb=_quota_mb('ATLAS_FLOW_SNAPSHOT_TOTAL_MB',_DEFAULT_TOTAL_MB)
    actor_mb=_quota_mb('ATLAS_FLOW_SNAPSHOT_ACTOR_MB',_DEFAULT_ACTOR_MB)
    project_mb=_quota_mb('ATLAS_FLOW_SNAPSHOT_PROJECT_MB',_DEFAULT_PROJECT_MB)
    root=DATA/'business-flow-tables'
    # One cross-process lock serializes quota accounting and atomic publication,
    # including across workers when the configured shared filesystem/DB locks
    # are available. We never evict retained snapshots to make room.
    with FileLock(str(DATA/'business-flow-tables.lock'),timeout=10):
        key=_index_key();claim=_owner_claim(context,scope,key);marker=folder/'.owner.json'
        actor_used,project_used,_=_indexed_usage(root,claim,key)
        if marker.exists():
            if marker.is_symlink() or json.loads(marker.read_bytes())!=claim:
                raise ValueError('数据快照归属与当前用户或项目不一致')
        else:
            # Legacy scopes are attributed when next used with the exact run
            # identity. Other unindexed legacy scopes remain in the global
            # quota until they can be attributed; do not invent their owner.
            own_legacy=_scope_bytes(folder)
            actor_used+=own_legacy;project_used+=own_legacy
        if target.exists():
            # A corrupt or altered existing snapshot must not be silently reused.
            _read_rows(context,ref)
            if not marker.exists():_write_owner(marker,claim)
            return ref
        used=sum(p.stat().st_size for p in folder.iterdir() if p.is_file() and not p.is_symlink())
        if used+len(raw)+28>_MAX_RUN_BYTES:
            raise FlowStepError('BYTE_BUDGET','本次运行的完整数据快照超过安全容量，未截断或发布')
        additional=len(raw)+28+(0 if marker.exists() else len(canonical(claim)))
        if actor_used+additional>actor_mb*1024*1024:
            raise FlowStepError('BYTE_BUDGET','当前用户的数据快照容量不足；未删除历史结果')
        if project_used+additional>project_mb*1024*1024:
            raise FlowStepError('BYTE_BUDGET','当前项目的数据快照容量不足；未删除历史结果')
        total=0
        for parent,dirs,files in os.walk(root,followlinks=False):
            dirs[:]=[d for d in dirs if not (Path(parent)/d).is_symlink()]
            for name in files:
                item=Path(parent)/name
                if item.is_file() and not item.is_symlink():total+=item.stat().st_size
        if total+additional>total_mb*1024*1024:
            raise FlowStepError('BYTE_BUDGET','数据快照存储容量不足，请按保留策略处理空间后重试；未删除历史结果')
        if not marker.exists():_write_owner(marker,claim)
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


def _write_owner(path,claim):
    fd,name=tempfile.mkstemp(prefix='.owner-pending-',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'wb') as out:
            out.write(canonical(claim));out.flush();os.fsync(out.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def _read_rows(context,ref):
    if isinstance(ref,dict) and ref.get('version')==2:
        return _read_paged(context,ref,0,ref.get('row_count'),whole=True)
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


def _file_digest(stream):
    stream.seek(0);h=hashlib.sha256()
    for part in iter(lambda:stream.read(1024*1024),b''):h.update(part)
    return h.hexdigest()


def _paged_ref(context,ref):
    import re
    if (not isinstance(ref,dict) or set(ref)!={'version','sha256','bytes','row_count','scope','cipher_sha256','table_sha256'}
            or ref.get('version')!=2 or not re.fullmatch(r'[a-f0-9]{64}',str(ref.get('sha256','')))
            or not re.fullmatch(r'[a-f0-9]{64}',str(ref.get('cipher_sha256','')))
            or (ref.get('table_sha256') is not None and not re.fullmatch(r'[a-f0-9]{64}',str(ref.get('table_sha256',''))))
            or type(ref.get('bytes')) is not int or not 0<ref['bytes']<=_MAX_TABLE_BYTES
            or type(ref.get('row_count')) is not int or not 0<=ref['row_count']<=1000000
            or ref.get('scope')!=_table_scope(context)):
        raise ValueError('数据快照与当前运行不一致')
    folder,scope=_table_folder(context);path=folder/(ref['sha256']+'.enc')
    if path.is_symlink() or not path.is_file():raise ValueError('数据快照丢失')
    return path,scope


def _paged_index(stream,ref,scope):
    """Validate authenticated index and full ciphertext without loading it."""
    stream.seek(0,os.SEEK_END);size=stream.tell()
    if size<48 or size>_MAX_TABLE_BYTES+_MAX_INDEX_BYTES+1000000:
        raise ValueError('数据快照大小无效')
    if _file_digest(stream)!=ref['cipher_sha256']:raise ValueError('数据快照完整性校验失败')
    stream.seek(0)
    if stream.read(len(_PAGE_MAGIC))!=_PAGE_MAGIC:raise ValueError('数据快照格式无效')
    stream.seek(-8,os.SEEK_END);index_length=int.from_bytes(stream.read(8),'big')
    if not 1<=index_length<=_MAX_INDEX_BYTES or size-index_length-40<len(_PAGE_MAGIC):
        raise ValueError('数据快照索引无效')
    index_start=size-index_length-40;stream.seek(index_start)
    raw=stream.read(index_length);tag=stream.read(32)
    if not hmac.compare_digest(tag,hmac.new(_table_key(scope),raw,hashlib.sha256).digest()):
        raise ValueError('数据快照索引校验失败')
    try:meta=json.loads(raw)
    except (ValueError,UnicodeError):raise ValueError('数据快照索引损坏') from None
    if (not isinstance(meta,dict) or set(meta)!={'version','scope','sha256','bytes','row_count','chunks'}
            or meta['version']!=2 or any(meta[k]!=ref[k] for k in ('scope','sha256','bytes','row_count'))
            or not isinstance(meta['chunks'],list) or len(meta['chunks'])>4000):
        raise ValueError('数据快照索引与运行记录不一致')
    row=0;position=len(_PAGE_MAGIC)
    for item in meta['chunks']:
        if (not isinstance(item,dict) or set(item)!={'start','count','offset','length','sha256'}
                or any(type(item.get(k)) is not int for k in ('start','count','offset','length'))
                or item['start']!=row or not 1<=item['count']<=_PAGE_ROWS or item['offset']!=position
                or not 28<item['length']<=_MAX_TABLE_BYTES+28
                or not isinstance(item['sha256'],str) or len(item['sha256'])!=64):
            raise ValueError('数据快照分块索引无效')
        row+=item['count'];position+=item['length']
    if row!=ref['row_count'] or position!=index_start:raise ValueError('数据快照分块不完整')
    return meta


def _page_chunk(stream,scope,ref,index,item):
    stream.seek(item['offset']);encrypted=stream.read(item['length'])
    if hashlib.sha256(encrypted).hexdigest()!=item['sha256']:raise ValueError('数据快照分块校验失败')
    aad=canonical(['atlas-table-page-v2',scope,ref['sha256'],index,item['start'],item['count']])
    try:plain=AESGCM(_table_key(scope)).decrypt(encrypted[:12],encrypted[12:],aad);rows=json.loads(plain)
    except Exception:raise ValueError('数据快照分块无法解密') from None
    if not isinstance(rows,list) or len(rows)!=item['count'] or any(not isinstance(row,dict) for row in rows):
        raise ValueError('数据快照分块行数不一致')
    return rows


def _read_paged(context,ref,offset,limit,*,whole=False,verify_only=False):
    path,scope=_paged_ref(context,ref)
    if type(offset) is not int or offset<0 or type(limit) is not int or limit<0:raise ValueError('分页参数无效')
    with path.open('rb') as stream:
        index=_paged_index(stream,ref,scope)
        if verify_only:return None
        result=[]
        for number,item in enumerate(index['chunks']):
            start=item['start'];end=start+item['count']
            if end<=offset or start>=offset+limit:continue
            rows=_page_chunk(stream,scope,ref,number,item)
            result.extend(rows[max(0,offset-start):min(item['count'],offset+limit-start)])
    if len(result)!=max(0,min(limit,ref['row_count']-offset)):
        raise ValueError('数据快照分页不完整')
    if whole:
        raw=canonical(result)
        if len(raw)!=ref['bytes'] or hashlib.sha256(raw).hexdigest()!=ref['sha256']:
            raise ValueError('完整数据快照摘要不一致')
    return result


def verify_table_snapshot(context,value):
    """Verify an immutable table snapshot without materializing v2 rows."""
    if not isinstance(value,dict):raise ValueError('数据表无效')
    ref=value.get('rows_ref')
    if ref is None:return
    if not isinstance(ref,dict):raise ValueError('数据快照引用无效')
    if 'rows' in value:raise ValueError('数据表不能同时包含明细与快照引用')
    if value.get('kind')=='table_workflow_v1' and ref.get('version')==2:
        if (not isinstance(ref.get('table_sha256'),str) or type(value.get('row_count')) is not int or value['row_count']!=ref.get('row_count')
                or value.get('output_sha256')!=ref.get('table_sha256')):
            raise ValueError('数据结果与快照摘要不一致')
    if ref.get('version')==2:_read_paged(context,ref,0,0,verify_only=True)
    else:_read_rows(context,ref)


def get_table_page(context,value,offset,limit):
    """Return a verified slice; legacy v1 snapshots require full decryption."""
    if not isinstance(value,dict) or type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
        raise ValueError('数据分页参数无效')
    ref=value.get('rows_ref')
    if ref is None:
        rows=value.get('rows')
        if not isinstance(rows,list):raise ValueError('数据表无效')
        return rows[offset:offset+limit]
    if not isinstance(ref,dict):raise ValueError('数据快照引用无效')
    if 'rows' in value:raise ValueError('数据表不能同时包含明细与快照引用')
    if ref.get('version')==2:
        if value.get('kind')=='table_workflow_v1' and (not isinstance(ref.get('table_sha256'),str) or value.get('row_count')!=ref.get('row_count')
                or value.get('output_sha256')!=ref.get('table_sha256')):
            raise ValueError('数据结果与快照摘要不一致')
        return _read_paged(context,ref,offset,limit)
    return _read_rows(context,ref)[offset:offset+limit]


def _save_rows_paged(context,rows,raw,table_sha=None):
    """Atomically persist independently encrypted blocks with a signed index."""
    if len(raw)>_MAX_TABLE_BYTES:raise FlowStepError('BYTE_BUDGET','完整数据超过当前步骤的安全容量，未截断或发布')
    folder,scope=_table_folder(context);sha=hashlib.sha256(raw).hexdigest();target=folder/(sha+'.enc')
    from .locks import FileLock
    from .config import DATA
    count=(len(rows)+_PAGE_ROWS-1)//_PAGE_ROWS
    reserve=len(raw)+count*512+65536
    root=DATA/'business-flow-tables'
    with FileLock(str(DATA/'business-flow-tables.lock'),timeout=10):
        key=_index_key();claim=_owner_claim(context,scope,key);marker=folder/'.owner.json'
        actor_used,project_used,_=_indexed_usage(root,claim,key)
        if marker.exists():
            if marker.is_symlink() or json.loads(marker.read_bytes())!=claim:raise ValueError('数据快照归属与当前用户或项目不一致')
        else:
            own_legacy=_scope_bytes(folder);actor_used+=own_legacy;project_used+=own_legacy
        if target.exists():
            if target.is_symlink() or not target.is_file():raise ValueError('数据快照目录无效')
            with target.open('rb') as old:
                magic=old.read(len(_PAGE_MAGIC));old.seek(0)
                if magic==_PAGE_MAGIC:
                    ref={'version':2,'sha256':sha,'bytes':len(raw),'row_count':len(rows),'scope':scope,'cipher_sha256':_file_digest(old),'table_sha256':table_sha}
                else:ref={'version':1,'sha256':sha,'bytes':len(raw),'row_count':len(rows),'scope':scope}
            _read_rows(context,ref)
            if not marker.exists():_write_owner(marker,claim)
            return ref
        if _scope_bytes(folder)+reserve>_MAX_RUN_BYTES:
            raise FlowStepError('BYTE_BUDGET','本次运行的数据快照超过安全容量，未截断或发布')
        additional=reserve+(0 if marker.exists() else len(canonical(claim)))
        if actor_used+additional>_quota_mb('ATLAS_FLOW_SNAPSHOT_ACTOR_MB',_DEFAULT_ACTOR_MB)*1024*1024:
            raise FlowStepError('BYTE_BUDGET','当前用户的数据快照容量不足；未删除历史结果')
        if project_used+additional>_quota_mb('ATLAS_FLOW_SNAPSHOT_PROJECT_MB',_DEFAULT_PROJECT_MB)*1024*1024:
            raise FlowStepError('BYTE_BUDGET','当前项目的数据快照容量不足；未删除历史结果')
        total=0
        for item in root.iterdir():
            if item.is_symlink():raise ValueError('数据快照目录包含不可用的链接')
            if item.is_dir():total+=_scope_bytes(item)
            elif item.is_file():total+=item.stat().st_size
        if total+additional>_quota_mb('ATLAS_FLOW_SNAPSHOT_TOTAL_MB',_DEFAULT_TOTAL_MB)*1024*1024:
            raise FlowStepError('BYTE_BUDGET','数据快照存储容量不足；未删除历史结果')
        if not marker.exists():_write_owner(marker,claim)
        fd,name=tempfile.mkstemp(prefix='.pending-',dir=folder)
        try:
            os.fchmod(fd,0o600);entries=[];page_key=_table_key(scope)
            with os.fdopen(fd,'wb') as stream:
                stream.write(_PAGE_MAGIC)
                plain_hash=hashlib.sha256();plain_hash.update(b'[');plain_bytes=1
                for i in range(0,len(rows),_PAGE_ROWS):
                    page=rows[i:i+_PAGE_ROWS];nonce=secrets.token_bytes(12)
                    aad=canonical(['atlas-table-page-v2',scope,sha,len(entries),i,len(page)])
                    page_bytes=canonical(page)
                    if entries:plain_hash.update(b',');plain_bytes+=1
                    plain_hash.update(page_bytes[1:-1]);plain_bytes+=len(page_bytes)-2
                    encrypted=nonce+AESGCM(page_key).encrypt(nonce,page_bytes,aad)
                    entries.append({'start':i,'count':len(page),'offset':stream.tell(),'length':len(encrypted),
                                    'sha256':hashlib.sha256(encrypted).hexdigest()})
                    stream.write(encrypted)
                plain_hash.update(b']');plain_bytes+=1
                if plain_bytes!=len(raw) or plain_hash.hexdigest()!=sha:
                    raise ValueError('数据快照与处理结果不一致')
                meta={'version':2,'scope':scope,'sha256':sha,'bytes':len(raw),'row_count':len(rows),'chunks':entries}
                index=canonical(meta)
                if len(index)>_MAX_INDEX_BYTES:raise ValueError('数据快照索引超过安全容量')
                stream.write(index);stream.write(hmac.new(page_key,index,hashlib.sha256).digest());stream.write(len(index).to_bytes(8,'big'))
                stream.flush();os.fsync(stream.fileno())
            with open(name,'rb') as stream:cipher_sha=_file_digest(stream)
            ref={'version':2,'sha256':sha,'bytes':len(raw),'row_count':len(rows),'scope':scope,'cipher_sha256':cipher_sha,'table_sha256':table_sha}
            if os.path.getsize(name)>reserve:raise ValueError('数据快照超过预留容量')
            os.replace(name,target)
            return ref
        finally:
            if os.path.exists(name):os.unlink(name)


def _serialized_table_rows(rows):
    if not isinstance(rows,list):raise ValueError('数据快照行格式无效')
    # Enforce the bound while serializing instead of first allocating an
    # arbitrarily large canonical representation and checking afterward.
    from .data_workflows import _json_value
    encoder=json.JSONEncoder(ensure_ascii=False,sort_keys=True,separators=(',', ':'),
                             default=_json_value,allow_nan=False)
    data=bytearray()
    for fragment in encoder.iterencode(rows):
        chunk=fragment.encode('utf-8')
        if len(data)+len(chunk)>_MAX_TABLE_BYTES:
            raise FlowStepError('BYTE_BUDGET','完整数据超过当前步骤的安全容量，未截断或发布')
        data.extend(chunk)
    return bytes(data)


def _compact_table(context,value):
    rows=value.get('rows')
    raw=_serialized_table_rows(rows)
    if len(raw)<=_INLINE_ROWS_LIMIT:
        return json.loads(canonical(value))
    table_sha=None
    if value.get('kind')=='table_workflow_v1':
        from .data_workflows import digest
        table_sha=digest({'schema':value.get('schema'),'rows':rows})
        if table_sha!=value.get('output_sha256') or value.get('row_count')!=len(rows):
            raise ValueError('数据加工结果与回执摘要不一致')
    ref=_save_rows_paged(context,rows,raw,table_sha)
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
    # read_input only applies workbook options to XLSX and HTML table/header
    # options to HTML. Silently accepting a range or sheet on CSV would make
    # the report imply filtering that never occurred.
    filename=source['name'].lower()
    options=cfg.options.model_dump(mode='json',exclude_none=True)
    defaults=ReadOptions().model_dump(mode='json',exclude_none=True)
    if filename.endswith('.xlsx'):
        unsupported={'html_table'}
    elif filename.endswith(('.html','.htm')):
        unsupported=set(options)-{'html_table','header_rows'}
    else:unsupported=set(options)
    if any(options.get(key)!=defaults.get(key) for key in unsupported):
        raise ValueError('所选资料格式不支持这些读取选项，未执行；请调整读取范围或格式')
    binding=InputBinding(source_id=pin.source_id,options=cfg.options,column_map=cfg.column_map)
    rows,fields,evidence,provenance=read_input(turn['project_id'],binding,frozen_source=source)
    current=owned(turn['project_id'],'source',pin.source_id)
    if (current.get('revision')!=pin.revision or current.get('content_hash')!=pin.content_hash
            or current.get('archived') or current.get('status')!='ready'):
        raise FlowStepError('SOURCE_VERSION_CHANGED','读取期间资料版本已变化，请重新确认并运行')
    # Record the reader's resolved selection, not just the requested options:
    # automatic Excel layout discovery can choose a different effective range.
    if source['name'].lower().endswith('.xlsx'):
        if not isinstance(provenance.get('selection'),dict):raise ValueError('工作簿缺少实际读取范围')
        selection={'kind':'workbook_table',**provenance['selection']}
    elif source['name'].lower().endswith(('.html','.htm')):
        selection={'kind':'html_table','table_id':evidence.get('table_id')}
    elif source['name'].lower().endswith('.json'):
        path=evidence.get('records_path')
        if path:
            method='inferred' if evidence.get('records_path_inferred') is True else 'explicit'
        else:
            method='root_object' if evidence.get('root_object_record') is True else 'root_array'
        selection={'kind':'json_records','records_path':path or '', 'selection_method':method}
        if method=='inferred':
            # A conventional key is only a parser proposal, even if the
            # wrapper has no sibling metadata. The user still needs to verify
            # that this array is the intended business dataset.
            evidence['requires_source_review']=True
            evidence['requires_semantic_review']=True
        if 'wrapper_context_fields' in evidence:
            selection['wrapper_context_fields']=evidence['wrapper_context_fields']
        elif method=='explicit':
            # The legacy explicit-path parser records the selected path but
            # does not inspect sibling metadata (period, currency, etc.).
            # Absence of that evidence must not be presented as no context.
            selection['wrapper_context_status']='not_recorded'
            evidence['requires_source_review']=True
            evidence['requires_semantic_review']=True
    else:selection={'kind':'whole_source_table'}
    read_basis={'version':1,'options':cfg.options.model_dump(mode='json',exclude_none=True),
                'column_map':dict(cfg.column_map),'selection':selection}
    read_basis_sha256=_read_basis(read_basis,source_hash=pin.content_hash)
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
        'read_basis':read_basis,'read_basis_sha256':read_basis_sha256,
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
    source_refs=[];seen_sources=set();source_lineage_complete=True;selection_lineage_complete=True
    for alias in cfg.plan.inputs:
        value=inputs[alias]
        if (not isinstance(value,dict) or value.get('kind') not in ('source_table_v1','table_workflow_v1')
                or value.get('complete') is not True):
            raise ValueError('加工输入不是完整数据来源或已完成的数据步骤')
        value=materialize_table(context,value)
        if not isinstance(value.get('rows'),list):raise ValueError('加工输入的数据表不完整')
        # Carry the identity of the actual frozen inputs through chained
        # processing steps. Older outputs may lack lineage; do not present
        # their references as a complete audit trail.
        if value['kind']=='source_table_v1':
            refs=[{key:value.get(key) for key in ('source_id','revision','content_hash','read_basis','read_basis_sha256')}]
        else:
            refs=value.get('source_refs')
            if value.get('source_lineage_complete') is not True or not isinstance(refs,list) or not refs:
                source_lineage_complete=False
                selection_lineage_complete=False
                refs=refs if isinstance(refs,list) else []
            if value.get('selection_lineage_complete') is not True:selection_lineage_complete=False
        from .project_data import owned
        for ref in refs:
            if not isinstance(ref,dict):raise ValueError('资料来源引用无效')
            pin=SourcePin.model_validate({key:ref.get(key) for key in ('source_id','revision','content_hash')})
            agent_team.assert_turn_allowed(turn,'read_source_table',{'source_id':pin.source_id})
            source=owned(turn['project_id'],'source',pin.source_id)
            if (source.get('kind')!='file' or source.get('status')!='ready' or source.get('archived')
                    or source.get('revision')!=pin.revision or source.get('content_hash')!=pin.content_hash):
                raise FlowStepError('SOURCE_VERSION_CHANGED','资料版本已变化或不可用，请重新确认并运行')
            basis=ref.get('read_basis');basis_hash=ref.get('read_basis_sha256')
            if basis is None and basis_hash is None:
                selection_lineage_complete=False
            elif basis is None or not isinstance(basis_hash,str):
                raise ValueError('资料读取依据摘要不完整')
            else:_read_basis(basis,basis_hash,pin.content_hash)
            # Older JSON results claimed a whole source table even when the
            # parser had selected a nested array. Preserve the old receipt but
            # never certify its record-path lineage as complete.
            if (str(source.get('name') or '').lower().endswith('.json') and isinstance(basis,dict)
                    and isinstance(basis.get('selection'),dict)
                    and basis['selection'].get('kind')=='whole_source_table'):
                selection_lineage_complete=False
            key=(pin.source_id,pin.revision,pin.content_hash,basis_hash)
            if key not in seen_sources:
                if len(source_refs)>=200:raise ValueError('资料来源数量超过处理上限')
                seen_sources.add(key)
                source_ref={'source_id':pin.source_id,'revision':pin.revision,
                            'content_hash':pin.content_hash,'name':str(source.get('name') or pin.source_id)}
                if basis is not None:source_ref.update(read_basis=basis,read_basis_sha256=basis_hash)
                source_refs.append(source_ref)
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
    validated_plan=WorkflowPlan.model_validate(plan)
    from .data_workflows import dependencies_for,digest
    stages_by_id={stage.id:stage for stage in validated_plan.stages}
    ancestry=set();pending=[validated_plan.output]
    while pending:
        ident=pending.pop()
        if ident in ancestry:continue
        ancestry.add(ident)
        if ident in stages_by_id:pending.extend(dependencies_for(stages_by_id[ident]))
    evidence=[]
    def keep_fx_evidence(stage,schema,rows,receipt):
        # Only retain computed FX detail on the final output's own dependency
        # path. The final FX table itself is already directly pageable.
        if stage.id not in ancestry or stage.id==validated_plan.output:return
        raw=_serialized_table_rows(rows)
        checksum=digest({'schema':schema,'rows':rows})
        if checksum!=receipt['output_sha256'] or digest(schema)!=receipt['schema_sha256']:
            raise ValueError('换算明细与执行回执不一致')
        ref=_save_rows_paged(context,rows,raw,checksum)
        evidence.append({'stage_id':stage.id,'kind':'fx_convert','schema':schema,
                         'row_count':len(rows),'output_sha256':checksum,'rows_ref':ref})
    contribution=[]
    def keep_aggregate_input(stage,schema,rows,receipt):
        if not context.get('run_id'):return  # Direct engine calls do not have a durable run scope.
        # All rows in a group contribute to a sum or COUNT(*). Other
        # aggregates have different inclusion semantics and are deliberately
        # excluded from the row-contribution view.
        if any(a.function!='sum' and not (a.function=='count' and a.column is None)
               for a in stage.aggregations):return
        dependencies=receipt.get('inputs')
        checksum=digest({'schema':schema,'rows':rows})
        if (not isinstance(dependencies,list) or len(dependencies)!=1
                or dependencies[0].get('table')!=stage.input
                or dependencies[0].get('sha256')!=checksum
                or dependencies[0].get('rows')!=len(rows)):
            raise ValueError('汇总输入与执行回执不一致')
        raw=_serialized_table_rows(rows)
        ref=_save_rows_paged(context,rows,raw,checksum)
        contribution.append({'stage_id':stage.id,'input_id':stage.input,'schema':schema,
                             'row_count':len(rows),'output_sha256':checksum,'rows_ref':ref})
    result=execute(validated_plan,tables,on_evidence=keep_fx_evidence,
                   on_aggregate_input=keep_aggregate_input)
    value={'kind':'table_workflow_v1','complete':result['complete'],'rows':result['output']['rows'],
        'schema':result['output']['schema'],'row_count':result['output']['row_count'],
        'output_sha256':result['output']['sha256'],'plan_sha256':result['plan_sha256'],
        'input_sha256':result['input_sha256'],'stage_receipts':result['stages'],
        'source_refs':source_refs,'source_lineage_complete':source_lineage_complete,
        'selection_lineage_complete':selection_lineage_complete,
        'requires_source_review':source_review,
        'requires_semantic_review':semantic_review or result.get('requires_semantic_review') is True,
        'financial_evidence_status':'requires_business_review'}
    if evidence:
        value['evidence_tables']=evidence
    if contribution:
        value['contribution_evidence']=contribution[0]
    if evidence or contribution:
        value['workflow_output_stage']=validated_plan.output
    return _compact_table(context,value)
