"""Personal, project-scoped registrations of immutable PPTX source versions.

Registration is not a private copy: the source is still shared with the project.
Only a verified account can see or use its own registrations. Shared workspace
access tokens identify a single account. No organization-level template role is
implied by this registry.
"""
import hashlib
import json
from typing import Literal

from fastapi import Depends, Request
from pydantic import Field

from . import ppt_template, workspace
from .actor_context import actor_from_identity
from .models import StrictModel
from .store import all_items, get, now, put
from .workspace_lifecycle import assert_project_live, _project_lock

KIND = 'personal_ppt_template'


class Register(StrictModel):
    source_id: str = Field(min_length=1, max_length=64)
    revision: int = Field(strict=True, ge=1)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    title: str = Field(min_length=1, max_length=100)


class Remove(StrictModel):
    version: int = Field(strict=True, ge=1)


class Rename(Remove):
    title: str = Field(min_length=1, max_length=100)


class Fill(StrictModel):
    version: int = Field(strict=True, ge=1)
    name: str = Field(min_length=1, max_length=91, pattern=r'^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff _-]{0,90}$')
    replacements: list[ppt_template.Replacement] = Field(min_length=1, max_length=200)


def _ident(pid, actor, value):
    raw=json.dumps([pid,actor,value.source_id,value.revision,value.sha256],ensure_ascii=False,separators=(',',':')).encode()
    return 'pt_'+hashlib.sha256(raw).hexdigest()[:40]


def _owned(pid, tid, actor):
    entry=get(KIND,tid)
    if entry.get('project_id')!=pid or entry.get('owner_id')!=actor or entry.get('removed'):
        raise KeyError(tid)
    return entry


def _reference(entry):
    return dict(source_id=entry['source_id'],revision=entry['source_revision'],expected_sha256=entry['source_sha256'])


def _public(entry):
    return {key:entry[key] for key in ('id','title','source_id','source_revision','source_sha256','version','created_at','updated_at')}


def list_templates(pid,actor):
    with workspace.lock(pid),_project_lock(pid):
        assert_project_live(pid)
        entries=[x for x in all_items(KIND) if x.get('project_id')==pid and x.get('owner_id')==actor and not x.get('removed')]
        entries.sort(key=lambda x:(x.get('updated_at',''),x.get('id','')),reverse=True)
        result=[]
        for entry in entries[:500]:
            item=_public(entry)
            try:
                source=ppt_template.project_data.owned(pid,'source',entry['source_id'])
                item['available']=(not source.get('archived') and not source.get('deleted_at') and source.get('kind')=='file'
                                   and source.get('revision',1)==entry['source_revision'] and source.get('content_hash')==entry['source_sha256'])
            except (KeyError,ValueError):item['available']=False
            result.append(item)
        return {'items':result,'truncated':len(entries)>500,'notice':'这是当前账号在本项目的模板清单。源文件仍由项目共享，模板复用不代表财务内容已核对。'}


def eligible_sources(pid):
    with workspace.lock(pid),_project_lock(pid):
        assert_project_live(pid)
        records=[s for s in all_items('source') if s.get('project_id')==pid and s.get('kind')=='file'
                 and not s.get('archived') and not s.get('deleted_at') and s.get('name','').lower().endswith('.pptx')
                 and isinstance(s.get('content_hash'),str) and type(s.get('revision',1)) is int]
        records.sort(key=lambda x:(x.get('created_at',''),x.get('id','')),reverse=True)
        return {'items':[{'id':s['id'],'name':s['name'],'revision':s.get('revision',1),'sha256':s['content_hash']}
                         for s in records[:500]],'truncated':len(records)>500}


def register(pid,actor,value):
    title=value.title.strip()
    if not title:raise ValueError('请填写模板名称')
    ident=_ident(pid,actor,value)
    with workspace.lock(pid),_project_lock(pid):
        assert_project_live(pid)
        # Verify file bytes, version, type, and disallowed active content before saving.
        ppt_template._template(pid,value.source_id,value.revision,value.sha256)
        try:entry=get(KIND,ident)
        except KeyError:entry=None
        if entry:
            if entry.get('project_id')!=pid or entry.get('owner_id')!=actor:raise ValueError('模板身份不一致')
            if not entry.get('removed'):
                if entry.get('title')!=title:raise ValueError('该版本已登记，请使用已有模板')
                return _public(entry)
            entry=dict(entry,title=title,removed=False,version=entry['version']+1,updated_at=now())
        else:
            entry={'project_id':pid,'owner_id':actor,'source_id':value.source_id,'source_revision':value.revision,
                   'source_sha256':value.sha256,'title':title,'version':1,'removed':False,'created_at':now(),'updated_at':now()}
        return _public(put(KIND,entry,ident))


def remove(pid,tid,actor,version):
    with workspace.lock(pid),_project_lock(pid):
        assert_project_live(pid)
        entry=_owned(pid,tid,actor)
        if entry['version']!=version:raise ValueError('模板清单已变化，请刷新后再试')
        put(KIND,dict(entry,removed=True,version=version+1,updated_at=now()),tid)
        return {'removed':True,'source_preserved':True}


def rename(pid,tid,actor,value):
    title=value.title.strip()
    if not title:raise ValueError('请填写模板名称')
    with workspace.lock(pid),_project_lock(pid):
        assert_project_live(pid)
        entry=_owned(pid,tid,actor)
        if entry['version']!=value.version:raise ValueError('模板清单已变化，请刷新后再试')
        if entry['title']==title:return _public(entry)
        return _public(put(KIND,dict(entry,title=title,version=value.version+1,updated_at=now()),tid))


def inspect(pid,tid,actor):
    with workspace.lock(pid),_project_lock(pid):
        assert_project_live(pid)
        entry=_owned(pid,tid,actor)
        return {'template':_public(entry),'structure':ppt_template.inspect(pid,**_reference(entry))}


def render(pid,tid,actor,value):
    entry=_owned(pid,tid,actor)
    if entry['version']!=value.version:raise ValueError('模板清单已变化，请刷新后再试')
    def revalidate():
        assert_project_live(pid)
        fresh=_owned(pid,tid,actor)
        if fresh['version']!=value.version or _reference(fresh)!=_reference(entry):
            raise ValueError('模板版本已变化，请刷新后再试')
    return ppt_template.render_for_project(pid,**_reference(entry),name=value.name,
        replacements=[item.model_dump() for item in value.replacements],before_render=revalidate)


def install(app,auth):
    deps=[Depends(auth)]
    def actor(request):return actor_from_identity(getattr(request.state,'identity',{}))

    @app.get('/api/projects/{pid}/personal-ppt-templates',dependencies=deps)
    def get_templates(pid:str,request:Request):return list_templates(pid,actor(request))

    @app.get('/api/projects/{pid}/personal-ppt-template-sources',dependencies=deps)
    def get_sources(pid:str):return eligible_sources(pid)

    @app.post('/api/projects/{pid}/personal-ppt-templates',dependencies=deps)
    def add_template(pid:str,value:Register,request:Request):return register(pid,actor(request),value)

    @app.post('/api/projects/{pid}/personal-ppt-templates/{tid}/remove',dependencies=deps)
    def remove_template(pid:str,tid:str,value:Remove,request:Request):return remove(pid,tid,actor(request),value.version)

    @app.put('/api/projects/{pid}/personal-ppt-templates/{tid}',dependencies=deps)
    def rename_template(pid:str,tid:str,value:Rename,request:Request):return rename(pid,tid,actor(request),value)

    @app.get('/api/projects/{pid}/personal-ppt-templates/{tid}',dependencies=deps)
    def inspect_template(pid:str,tid:str,request:Request):return inspect(pid,tid,actor(request))

    @app.post('/api/projects/{pid}/personal-ppt-templates/{tid}/render',dependencies=deps)
    def render_template(pid:str,tid:str,value:Fill,request:Request):return render(pid,tid,actor(request),value)
