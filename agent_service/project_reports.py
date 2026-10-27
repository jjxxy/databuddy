"""Project-scoped configured report exports with frozen private execution state.

The Agent chooses a preconfigured connector, never a URL, authentication header,
principal scope or run identity. Completed exports become ordinary immutable file
sources so existing probe/processing workflows can consume the actual numbers.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from sqlalchemy import select, update

from .config import DATA
from .models import StrictModel
from .store import all_items, get, items, now, put, seal, transaction, uid, unseal
from . import jobs
from .report_acquisition import ReportError, ReportRequest, acquire_report, request_digest


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


class ReportVerification(StrictModel):
    expected_columns: list[str] = Field(default_factory=list,max_length=1000)
    expected_rows: int | None = Field(default=None,ge=0,le=10000000)
    expected_sha256: str | None = Field(default=None,pattern=r'^[a-f0-9]{64}$')
    filter_columns: dict[str,str] = Field(default_factory=dict,max_length=40)
    filter_echo_paths: dict[str,str] = Field(default_factory=dict,max_length=40)
    context_echo_paths: dict[str,str] = Field(default_factory=dict,max_length=30)
    json_rows_path: str = Field(default='',max_length=300)
    csv_encoding: Literal['utf-8-sig','utf-8','gb18030'] = 'utf-8-sig'
    xlsx_sheet: str | None = Field(default=None,max_length=100)
    xlsx_header_row: int = Field(default=1,ge=1,le=1000)
    allow_empty: bool = False
    max_bytes: int = Field(default=25*1024*1024,ge=1,le=500*1024*1024)
    max_rows: int = Field(default=100000,ge=1,le=10000000)
    max_columns: int = Field(default=1000,ge=1,le=16384)
    timeout_seconds: float = Field(default=30,ge=1,le=120)
    total_timeout_seconds: float = Field(default=120,ge=1,le=900)
    max_attempts: int = Field(default=3,ge=1,le=5)


class ReportConnectorInput(StrictModel):
    name: str = Field(min_length=1,max_length=100)
    url: SecretStr
    format: Literal['xlsx','csv','json'] = 'xlsx'
    headers: dict[str,SecretStr] = Field(default_factory=dict,max_length=40)
    verification: ReportVerification = Field(default_factory=ReportVerification)
    allowed_filter_keys: list[str] = Field(default_factory=list,max_length=40)
    default_filters: dict = Field(default_factory=dict,max_length=40)
    default_context: dict[str,str] = Field(default_factory=dict,max_length=30)
    enabled: bool = True

    @field_validator('allowed_filter_keys')
    @classmethod
    def validate_keys(cls,value):
        if len(value)!=len(set(value)) or any(not v or len(v)>100 for v in value):
            raise ValueError('允许的筛选参数为空、重复或过长')
        return value


class FetchReportInput(StrictModel):
    connector_id: str = Field(min_length=1,max_length=64)
    filters: dict = Field(default_factory=dict,max_length=40)
    context: dict[str,str] = Field(default_factory=dict,max_length=30)


class ReportFetchTarget(StrictModel):
    run_id: str = Field(min_length=1,max_length=64)


def public(connector):
    return {'id':connector['id'],'name':connector['name'],
            'status':'configured' if connector.get('enabled') else 'disabled',
            'formats':[connector['format']]}


def owned(pid,connector_id,*,require_enabled=True):
    c=get('report_connector',connector_id)
    if c.get('project_id')!=pid:raise ValueError('报告连接不属于当前项目')
    if require_enabled and not c.get('enabled'):raise ValueError('报告连接已停用')
    return c


def configure(pid,value,connector_id=None):
    """Owner API only. The full URL and authentication headers are sealed at rest."""
    get('project',pid)
    value=ReportConnectorInput.model_validate(value)
    old=owned(pid,connector_id,require_enabled=False) if connector_id else None
    cfg={'url':value.url.get_secret_value(),'format':value.format,
         'headers':{k:v.get_secret_value() for k,v in value.headers.items()},
         'verification':value.verification.model_dump(),
         'allowed_filter_keys':sorted(set(value.allowed_filter_keys)|set(value.default_filters)|
                                     set(value.verification.filter_columns)|set(value.verification.filter_echo_paths)),
         'default_filters':value.default_filters,'default_context':value.default_context}
    # Check shape using placeholders only for required-but-runtime filter values.
    probe_filters=dict(cfg['default_filters'])
    for key in set(value.verification.filter_columns)|set(value.verification.filter_echo_paths):probe_filters.setdefault(key,'')
    probe_context=dict(cfg['default_context'])
    for key in value.verification.context_echo_paths:probe_context.setdefault(key,'')
    try:
        ReportRequest(scope_id='owner_configuration',request_id='configuration',url=cfg['url'],format=cfg['format'],
                      filters=probe_filters,context=probe_context,**cfg['verification'])
        from .connectors import safe_api_url
        from .report_acquisition import _headers
        import httpx
        _headers(cfg['headers'],httpx.URL(cfg['url']))
        safe_api_url(cfg['url'])
    except Exception:
        raise ValueError('报告连接地址或核对规则无效；请检查网络允许范围、格式和参数配置') from None
    connector_id=connector_id or 'rc_'+uid()
    row={'project_id':pid,'name':value.name,'format':value.format,'enabled':value.enabled,
         'revision':uid(),'secret_config':seal(cfg),'updated_at':now(),
         'created_at':old.get('created_at',now()) if old else now()}
    return public(put('report_connector',row,connector_id))


def list_connectors(pid):
    get('project',pid)
    return [public(c) for c in sorted(all_items('report_connector'),key=lambda r:r['id']) if c.get('project_id')==pid]


def _frozen_request(pid,connector,filters,context,run_id):
    cfg=unseal(connector['secret_config'])
    requested_filters=dict(cfg.get('default_filters',{}));requested_filters.update(filters or {})
    if set(requested_filters)-set(cfg.get('allowed_filter_keys',[])):
        raise ValueError('获取请求包含未在此连接中批准的筛选参数')
    declared_context=dict(cfg.get('default_context',{}))
    for key,value in (context or {}).items():
        if key in declared_context and declared_context[key]!=value:
            raise ValueError('固定报告的语义上下文不能由获取请求修改')
        declared_context[key]=value
    scope_id='report_'+_digest([pid,connector['id'],connector['revision']])[:48]
    try:
        request=ReportRequest(scope_id=scope_id,request_id=run_id,url=cfg['url'],format=cfg['format'],
                              filters=requested_filters,context=declared_context,**cfg.get('verification',{}))
    except Exception:
        missing=(set(cfg.get('verification',{}).get('filter_columns',{}))|
                 set(cfg.get('verification',{}).get('filter_echo_paths',{})))-set(requested_filters)
        if missing:raise ValueError('缺少此报告要求的筛选参数：'+', '.join(sorted(missing))) from None
        raise ValueError('报告筛选、上下文或核对参数不完整；未发起获取') from None
    return cfg,request


def enqueue_fetch(pid,connector_id,filters=None,context=None,*,operation_id=None,originating_turn_id=None):
    get('project',pid);connector=owned(pid,connector_id)
    if filters is not None and not isinstance(filters,dict):raise ValueError('报告筛选必须为对象')
    if context is not None and not isinstance(context,dict):raise ValueError('报告上下文必须为对象')
    cfg,request=_frozen_request(pid,connector,filters,context,'queued_request')
    # No credentials/transport tokens enter this digest; actual run_id is server-generated.
    fingerprint=request_digest(request)
    snapshot={'project_id':pid,'connector_id':connector_id,'connector_revision':connector['revision'],
              'name':connector['name'],'config':cfg,'filters':request.filters,'context':request.context}
    duration=min(3600,math.ceil(request.total_timeout_seconds+request.timeout_seconds+30))
    key='report:'+_digest([pid,operation_id]) if operation_id is not None else None
    from .job_authority import for_turn
    if originating_turn_id is None:
        from .provider import current_turn_id
        originating_turn_id=current_turn_id()
    authority=for_turn(originating_turn_id)
    extra={**({'gateway_authority':authority} if authority else {}),
           'project_id':pid,'report_connector_revision':connector['revision'],
           'originating_turn_id':originating_turn_id,
           'report_request_sha256':fingerprint,'secret_report_snapshot':seal(snapshot),
           'runtime_snapshot':{'timeout_seconds':duration,'max_attempts':2}}
    job=jobs.enqueue('report_fetch',connector_id,commit=connector['revision'],scheduled_key=key,max_attempts=2,extra=extra)
    if job.get('kind')!='report_fetch' or job.get('project_id')!=pid or job.get('report_request_sha256')!=fingerprint:
        raise ValueError('该连接已有不同条件的获取任务；请等待结束后再发起新条件')
    return read_fetch(pid,job['id'])


def _public_receipt(receipt):
    columns=receipt.get('columns',[])
    column_count=receipt.get('column_count',len(columns))
    return {key:receipt.get(key) for key in ('format','rows','bytes','completion_verified','content_sha256',
            'filters_verified','context_verified','acquisition_id')} | {
            'columns':columns[:100],'columns_complete':column_count<=100 and receipt.get('columns_complete',True),
            'column_count':column_count,'scope':'download_integrity_and_configured_checks; not_automatic_business_semantics'}


def read_fetch(pid,run_id):
    job=jobs.get_job(run_id)
    if job.get('kind')!='report_fetch' or job.get('project_id')!=pid:raise ValueError('报告获取任务不属于当前项目')
    value={key:job.get(key) for key in ('id','status','stage','created_at','finished_at','error_code','source_id')}
    value.update(connector_id=job['target'],connector_revision=job.get('report_connector_revision'))
    if job.get('report_receipt'):value['receipt']=_public_receipt(job['report_receipt'])
    return value


def _copy_verified(source,destination,expected_hash,checkpoint):
    if source.is_symlink() or destination.is_symlink():raise ValueError('报告文件不能是符号链接')
    def file_hash(path):
        digest=hashlib.sha256()
        with path.open('rb') as f:
            for chunk in iter(lambda:f.read(1024*1024),b''):digest.update(chunk)
        return digest.hexdigest()
    if destination.exists():
        if file_hash(destination)!=expected_hash:raise ValueError('获取任务的来源文件内容已变化，未覆盖')
        return
    fd,name=tempfile.mkstemp(prefix='.report-source-',dir=destination.parent)
    temporary=Path(name)
    try:
        from .platform_files import chmod_descriptor,fsync_directory,atomic_replace
        chmod_descriptor(fd);digest=hashlib.sha256()
        with os.fdopen(fd,'wb') as out,source.open('rb') as inp:
            for chunk in iter(lambda:inp.read(1024*1024),b''):
                checkpoint();digest.update(chunk);out.write(chunk)
            out.flush();os.fsync(out.fileno())
        if digest.hexdigest()!=expected_hash:raise ValueError('已下载报告在登记前发生变化，未创建来源')
        checkpoint();atomic_replace(temporary,destination)
        fsync_directory(destination.parent)
    finally:temporary.unlink(missing_ok=True)


def _register_and_complete(run,snapshot,receipt,stored_name,checkpoint):
    """Lease-fenced source, source revision, probe queue and completion in one txn."""
    from .job_authority import guard
    with guard(run):
        return _register_and_complete_locked(run,snapshot,receipt,stored_name,checkpoint)


def _register_and_complete_locked(run,snapshot,receipt,stored_name,checkpoint):
    from .errors import Cancelled
    source_id='rs_'+_digest([run['project_id'],run['id']])[:30]
    revision_id='rv_'+_digest([source_id,run['id']])[:30]
    config={'stored_name':stored_name,'report_receipt':receipt}
    verification=snapshot['config'].get('verification',{})
    if snapshot['config']['format']=='json' and verification.get('json_rows_path'):
        config['records_path']=verification['json_rows_path']
    if snapshot['config']['format']=='xlsx':
        options={'header_rows':[verification.get('xlsx_header_row',1)]}
        if verification.get('xlsx_sheet'):options['sheet']=verification['xlsx_sheet']
        config['xlsx_options']=options
    source={'id':source_id,'project_id':run['project_id'],'name':snapshot['name'][:110]+'.'+snapshot['config']['format'],
            'kind':'file','secret_config':seal(config),'bytes':receipt['bytes'],'content_hash':receipt['content_sha256'],
            'created_at':now(),'status':'queued','goal':'','revision':1,'revision_id':revision_id,
            'report_connector_id':snapshot['connector_id'],'report_connector_revision':snapshot['connector_revision'],
            'report_fetch_run':run['id'],'report_acquisition_id':receipt['acquisition_id']}
    checkpoint()
    with transaction() as c:
        row=c.execute(select(jobs.jobs).where(jobs.jobs.c.id==run['id']).with_for_update()).mappings().first()
        if not row or row['status']!='running' or row['lease_token']!=run['lease_token'] or (row['lease_until'] or 0)<time.time():
            raise RuntimeError('任务租约已失效，禁止创建报告来源')
        job=jobs.render(row)
        if job.get('cancel_requested'):raise Cancelled()
        existing=c.execute(select(items.c.body).where(items.c.id==source_id,items.c.kind=='source')).first()
        if existing:
            old=json.loads(existing[0])
            if old.get('project_id')!=run['project_id'] or old.get('report_fetch_run')!=run['id'] or old.get('content_hash')!=receipt['content_sha256']:
                raise ValueError('报告来源与本次获取任务不一致')
            source=old
        else:
            c.execute(items.insert().values(id=source_id,kind='source',body=json.dumps(source,ensure_ascii=False)))
            version=dict(source,id=revision_id,source_id=source_id,recorded_at=now())
            c.execute(items.insert().values(id=revision_id,kind='source_version',body=json.dumps(version,ensure_ascii=False)))
        # A lost process cannot leave a queued source without its actual probe job.
        probe=c.execute(select(jobs.jobs.c.id).where(jobs.jobs.c.target==source_id,
                        jobs.jobs.c.kind=='probe',jobs.jobs.c.status.in_(jobs.ACTIVE))).first()
        if source.get('status')!='ready' and not probe:
            probe_id='probe_'+_digest([source_id,run['id']])[:30]
            old_probe=c.execute(select(jobs.jobs.c.id).where(jobs.jobs.c.id==probe_id)).first()
            if not old_probe:
                t=time.time();body={'id':probe_id,'kind':'probe','target':source_id,'status':'queued','created_at':now(),
                                    'stage':'等待探查导出报告','cancel_requested':False}
                c.execute(jobs.jobs.insert().values(id=probe_id,kind='probe',target=source_id,commit_sha=None,status='queued',
                          dedup_key=None,attempts=0,max_attempts=3,available_at=t,created=t,updated=t,body=json.dumps(body,ensure_ascii=False)))
        if row['lease_until']<time.time():raise RuntimeError('任务租约已失效，禁止登记报告来源')
        job.update(status='succeeded',stage='报告下载完整，已登记来源并排队探查',finished_at=now(),
                   source_id=source_id,report_receipt=_public_receipt(receipt),records=receipt['rows'])
        c.execute(update(jobs.jobs).where(jobs.jobs.c.id==run['id']).values(status='succeeded',updated=time.time(),body=json.dumps(job,ensure_ascii=False)))
    return source_id


def execute_job(run,folder):
    """Called by the leased Worker; all authentication stays inside frozen state."""
    from .execution import checkpoint,patch_run
    from .errors import TaskError
    checkpoint(run)
    try:
        owned(run['project_id'],run['target'])
    except Exception:
        raise TaskError('报告连接已停用或不属于当前项目，未发起获取','REPORT_CONNECTOR_UNAVAILABLE') from None
    try:
        snapshot=unseal(run['secret_report_snapshot'])
        if snapshot['project_id']!=run['project_id'] or snapshot['connector_id']!=run['target'] or snapshot['connector_revision']!=run['commit']:
            raise ValueError('snapshot mismatch')
        cfg=snapshot['config']
        request=ReportRequest(scope_id='report_'+_digest([run['project_id'],run['target'],snapshot['connector_revision']])[:48],
                    request_id=run['id'],url=cfg['url'],format=cfg['format'],filters=snapshot['filters'],
                    context=snapshot['context'],**cfg.get('verification',{}))
        if request_digest(request.model_copy(update={'request_id':'queued_request'}))!=run['report_request_sha256']:
            raise ValueError('request digest mismatch')
    except Exception:
        raise TaskError('冻结的报告配置不完整，未发起获取','REPORT_SNAPSHOT_INVALID') from None
    stop=threading.Event()
    def beat():
        while not stop.wait(3):
            try:
                if not jobs.heartbeat(run['id'],run['lease_token']):return
            except Exception:return
    thread=threading.Thread(target=beat,daemon=True);thread.start()
    try:
        patch_run(run,stage='获取配置的报告导出；核对完整性与筛选证据')
        directory=DATA/'report-downloads'/request.scope_id/run['id']
        try:
            receipt=acquire_report(request,directory,headers=cfg.get('headers',{}))
        except ReportError as exc:
            code=exc.code if isinstance(exc.code,str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}',exc.code) else 'REPORT_FAILED'
            raise TaskError('报告获取或核对未完成，请检查连接、筛选、导出格式及响应内容',code,exc.retryable) from None
        except Exception:
            raise TaskError('报告获取未完成，请检查已配置连接及网络状态','REPORT_FAILED') from None
        checkpoint(run)
        name=receipt.get('file','');downloaded=directory/name
        if not name or Path(name).name!=name or not downloaded.resolve().is_relative_to(directory.resolve()):
            raise TaskError('报告下载回执路径无效','REPORT_PATH_INVALID')
        uploads=DATA/'uploads';uploads.mkdir(parents=True,exist_ok=True)
        stored_name='report_'+_digest([run['project_id'],run['id']])[:32]+'.'+request.format
        _copy_verified(downloaded,uploads/stored_name,receipt['content_sha256'],lambda:checkpoint(run))
        # The on-disk run folder contains safe evidence only, never frozen credentials.
        proof=Path(folder)/'report-receipt.json'
        proof.write_text(json.dumps(_public_receipt(receipt),ensure_ascii=False,indent=2))
        proof.chmod(0o600)
        _register_and_complete(run,snapshot,receipt,stored_name,lambda:checkpoint(run))
    finally:stop.set();thread.join(timeout=4)
