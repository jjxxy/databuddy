"""Operator-published, version-pinned PPTX catalog for one OIDC deployment.

Scope is the authorized users of this service instance, not a cross-workspace
organization identity platform. Personal copies survive catalog withdrawal.
"""
import hashlib, json, os, re, tempfile
from pathlib import Path
from fastapi import Depends, Request
from pydantic import Field
from sqlalchemy import select, update
from . import identity, ppt_template, project_templates, workspace
from .actor_context import actor_from_identity
from .config import DATA
from .locks import FileLock, assert_owned
from .models import StrictModel
from .platform_files import assert_plain_path, open_regular_read, create_regular_exclusive, atomic_replace, fsync_directory
from .store import all_items, get, now, seal, transaction, uid
from .workspace_lifecycle import _project_lock, assert_project_live

KIND='organization_ppt_template'

class Publish(StrictModel):
    source_project_id:str=Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
    source_id:str=Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
    revision:int=Field(strict=True,ge=1)
    sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    title:str=Field(min_length=1,max_length=100)
    confirm_full_document:bool=Field(strict=True)

class Withdrawal(StrictModel):
    version:int=Field(strict=True,ge=1)

class Copy(StrictModel):
    version:int=Field(strict=True,ge=1)
    sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    request_id:str=Field(pattern=r'^[A-Za-z0-9_-]{16,80}$')


def _enabled():
    identity.validate_config()
    if identity.mode()!='oidc' or identity._compatible_setting('REQUIRE_SINGLE_OIDC_SUBJECT','0')=='1':
        raise ValueError('实例模板库仅在多主体 OIDC 服务中启用；独立个人工作空间及共享口令不能代表组织权限')


def _lock():return FileLock(str(DATA/'organization-template-catalog.lock'),timeout=10)


def _path(digest):
    if not re.fullmatch(r'[a-f0-9]{64}',digest):raise ValueError('模板摘要无效')
    path=DATA/'organization-templates'/(digest+'.pptx')
    assert_plain_path(path)
    return path


def _read(digest):
    path=_path(digest)
    try:
        with os.fdopen(open_regular_read(path),'rb') as stream:raw=stream.read(ppt_template.MAX_BYTES+1)
    except OSError:raise ValueError('组织模板快照不可用') from None
    if len(raw)>ppt_template.MAX_BYTES or hashlib.sha256(raw).hexdigest()!=digest:
        raise ValueError('组织模板快照完整性校验失败')
    return raw


def _store(raw,digest):
    path=_path(digest);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True);assert_plain_path(path)
    if path.exists():
        if _read(digest)!=raw:raise ValueError('已保存的模板快照不一致')
        return
    # The catalog lock serializes writers; a temporary file is never served.
    fd,name=tempfile.mkstemp(prefix='.template-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as out:
            out.write(raw);out.flush();os.fsync(out.fileno())
        os.chmod(name,0o600);assert_owned();atomic_replace(name,path);fsync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def _record(ident,*,active=True):
    if not isinstance(ident,str) or not re.fullmatch(r'ot_[a-f0-9]{40}',ident):raise KeyError(ident)
    item=get(KIND,ident)
    if active and item.get('state')!='active':raise KeyError(ident)
    return item


def _public(item):
    return {k:item[k] for k in ('id','title','version','sha256','source_revision','created_at','state')}


def list_catalog():
    _enabled()
    with _lock():
        entries=[x for x in all_items(KIND) if x.get('state')=='active']
        entries.sort(key=lambda x:(x.get('created_at',''),x.get('id','')),reverse=True)
        return {'items':[_public(x) for x in entries[:500]],'truncated':len(entries)>500,
                'scope':'authorized_users_of_this_instance','notice':'管理员发布的固定模板；不保证财务内容或版式正确。独立工作空间不共享此清单。'}


def _append_audit(connection,action,subject,details):
    from . import audit
    from .store import items
    # Same sentinel as the rest of the application; keep audit event and
    # mutation in a single transaction and never include template content.
    sentinel=connection.execute(select(items.c.id).where(items.c.id=='audit-lock').with_for_update()).first()
    if not sentinel:raise ValueError('审计服务不可用，操作未完成')
    previous=connection.execute(select(audit.events.c.event_hash).order_by(audit.events.c.sequence.desc()).limit(1)).first()
    previous=previous[0] if previous else '0'*64;timestamp=now()
    body=json.dumps(details,ensure_ascii=False,sort_keys=True,default=str)
    signature=audit.digest(timestamp,action,subject,body,previous)
    connection.execute(audit.events.insert().values(created=timestamp,action=action,subject=subject,
                       body=body,previous_hash=previous,event_hash=signature))


def publish(value,actor):
    from . import store,audit
    from .store import cipher
    _enabled();value=Publish.model_validate(value);title=value.title.strip()
    if not title or any(ord(c)<32 for c in title):raise ValueError('模板名称无效')
    if value.confirm_full_document is not True:raise ValueError('发布前须确认完整模板包含的文字、隐藏对象和备注可共享')
    key=json.dumps([value.source_project_id,value.source_id,value.revision,value.sha256],separators=(',',':'))
    ident='ot_'+hashlib.sha256(key.encode()).hexdigest()[:40]
    cipher()
    with workspace.lock(value.source_project_id),_project_lock(value.source_project_id),_lock():
        assert_project_live(value.source_project_id)
        raw=ppt_template._template(value.source_project_id,value.source_id,value.revision,value.sha256)
        # Ensure the presentation is readable, including group/shape limits.
        ppt_template.inspect(value.source_project_id,value.source_id,value.revision,value.sha256)
        _store(raw,value.sha256)
        with transaction() as c:
            row=c.execute(select(store.items.c.body).where(store.items.c.id==ident,store.items.c.kind==KIND).with_for_update()).first()
            if row:
                old=json.loads(row[0])
                if old.get('state')!='active':raise ValueError('此固定版本已撤下，请发布新的来源版本')
                if old.get('title')!=title:raise ValueError('此固定版本已登记，请使用现有模板名称')
                return _public(old)
            item={'id':ident,'title':title,'state':'active','version':1,'sha256':value.sha256,
                  'source_revision':value.revision,'created_at':now()}
            c.execute(store.items.insert().values(id=ident,kind=KIND,body=store._encode(KIND,item,c)))
            _append_audit(c,'organization_template.published',ident,{'actor':actor,'sha256':value.sha256,'source_revision':value.revision})
            assert_owned()
        return _public(item)


def withdraw(ident,version,actor):
    from . import store,audit
    from .store import cipher
    _enabled();cipher()
    with _lock(),transaction() as c:
        row=c.execute(select(store.items.c.body).where(store.items.c.id==ident,store.items.c.kind==KIND).with_for_update()).first()
        if not row:raise KeyError(ident)
        item=json.loads(row[0])
        if item.get('state')!='active' or item.get('version')!=version:raise ValueError('模板状态已变化，请刷新')
        item.update(state='withdrawn',version=version+1,withdrawn_at=now())
        c.execute(update(store.items).where(store.items.c.id==ident).values(body=store._encode(KIND,item,c)))
        _append_audit(c,'organization_template.withdrawn',ident,{'actor':actor,'sha256':item['sha256']})
        assert_owned()
    return {'id':ident,'withdrawn':True,'existing_copies_preserved':True}


def copy_to_project(pid,ident,value,actor):
    from . import store,audit
    from .store import cipher
    from .conversation import stable
    from .execution import enqueue
    _enabled();value=Copy.model_validate(value);cipher()
    if not isinstance(actor,str) or not re.fullmatch(r'u_[a-f0-9]{64}',actor):raise ValueError('组织模板复制需要已认证的独立账号')
    sid=stable('s_','organization-template-copy',pid,actor,value.request_id)
    with workspace.lock(pid),_project_lock(pid),_lock():
        assert_project_live(pid);entry=_record(ident)
        if entry['version']!=value.version or entry['sha256']!=value.sha256:raise ValueError('模板版本已变化，请刷新')
        raw=_read(entry['sha256'])
        uploads=DATA/'uploads';assert_plain_path(uploads);uploads.mkdir(mode=0o700,parents=True,exist_ok=True)
        filename='org-'+sid[2:]+'.pptx';destination=uploads/filename;assert_plain_path(destination)
        if destination.exists():
            with os.fdopen(open_regular_read(destination),'rb') as f:old=f.read(ppt_template.MAX_BYTES+1)
            if hashlib.sha256(old).hexdigest()!=entry['sha256']:raise ValueError('已有复制文件不一致，未覆盖')
        else:
            fd=create_regular_exclusive(destination)
            with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
            fsync_directory(destination.parent)
        with transaction() as c:
            row=c.execute(select(store.items.c.body).where(store.items.c.id==sid,store.items.c.kind=='source').with_for_update()).first()
            if row:
                source=store._decode(row[0])
                if (source.get('project_id')!=pid or source.get('organization_template_id')!=ident
                        or source.get('organization_template_owner')!=actor or source.get('content_hash')!=value.sha256):
                    raise ValueError('此请求编号已经用于其他资料')
            else:
                created=now();revision_id=uid()
                base={'id':sid,'project_id':pid,'name':'组织模板-'+entry['sha256'][:12]+'.pptx','kind':'file',
                      'secret_config':seal({'stored_name':filename}),'content_hash':value.sha256,'bytes':len(raw),
                      'status':'queued','goal':'','created_at':created,'skip_model_planning':True,
                      'organization_template_id':ident,'organization_template_owner':actor}
                source=dict(base,revision=1,revision_id=revision_id)
                old_version=dict(base,id=revision_id,source_id=sid,revision=1,recorded_at=created)
                for kind,item in (('source',source),('source_version',old_version)):
                    c.execute(store.items.insert().values(id=item['id'],kind=kind,body=store._encode(kind,item,c)))
                _append_audit(c,'organization_template.copied',sid,{'actor':actor,'template_id':ident,'sha256':value.sha256})
                assert_owned()
        # The source is a project-owned copy; normal personal registration
        # ensures existing UI, file checks, and optimistic version semantics.
    if source.get('status')!='ready':enqueue('probe',sid)
    registration=project_templates.Register(source_id=sid,revision=source.get('revision',1),sha256=value.sha256,title=entry['title'])
    personal=project_templates.register(pid,actor,registration)
    return {'template':personal,'source_id':sid,'organization_template_id':ident,
            'notice':'复制的是固定哈希模板；撤下组织模板不会撤回已经复制的项目文件。'}


def install(app,auth,operator_auth):
    user=[Depends(auth)];admin=[Depends(operator_auth)]
    def actor(request):return actor_from_identity(getattr(request.state,'identity',{}))
    @app.get('/api/organization/ppt-templates',dependencies=user)
    def catalog():return list_catalog()
    @app.post('/api/organization/ppt-templates',dependencies=admin)
    def add(value:Publish,request:Request):return publish(value,actor(request))
    @app.post('/api/organization/ppt-templates/{ident}/withdraw',dependencies=admin)
    def remove(ident:str,value:Withdrawal,request:Request):return withdraw(ident,value.version,actor(request))
    @app.post('/api/projects/{pid}/organization-ppt-templates/{ident}/copy',dependencies=user)
    def copy(pid:str,ident:str,value:Copy,request:Request):return copy_to_project(pid,ident,value,actor(request))
