"""Explicit, version-bound section text revisions of generated Office files.

This creates a new specification, checkpoint and artifact. It never modifies the
original artifact, spreadsheet, structured financial tables or chart values.
"""
import hashlib
import json
import re
from contextlib import contextmanager
from typing import Literal

from fastapi import Depends, HTTPException, Request
from pydantic import Field, StrictInt, model_validator

from . import office, workspace, workspace_artifacts
from .actor_context import actor_from_identity
from .models import StrictModel
from .locks import FileLock, Timeout
from .config import DATA
from .store import get, put, now
from .workspace_lifecycle import _project_lock, assert_project_live


class EditInput(StrictModel):
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{16,80}$')
    expected_artifact_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_spec_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    section: int = Field(strict=True, ge=0, le=29)
    field: Literal['heading', 'body', 'bullet']
    bullet_index: int | None = Field(default=None, strict=True, ge=0, le=29)
    value: str = Field(max_length=20000)
    name: str = Field(min_length=1, max_length=91, pattern=r'^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff _-]{0,90}$')

    @model_validator(mode='after')
    def valid_edit(self):
        if (self.field == 'bullet') != (self.bullet_index is not None):
            raise ValueError('要点编辑必须指定一个现有要点')
        if self.field == 'heading' and len(self.value)>300:raise ValueError('标题最多300字')
        if self.field == 'bullet' and len(self.value)>2000:raise ValueError('要点最多2000字')
        if any(ord(ch)<32 and ch not in '\n\r\t' for ch in self.value):raise ValueError('文本含不支持的控制字符')
        return self


class SectionChange(StrictModel):
    section: int = Field(strict=True, ge=0, le=29)
    field: Literal['heading', 'body', 'bullet']
    bullet_index: int | None = Field(default=None, strict=True, ge=0, le=29)
    value: str = Field(max_length=20000)

    @model_validator(mode='after')
    def valid_change(self):
        if (self.field == 'bullet') != (self.bullet_index is not None):
            raise ValueError('要点编辑必须指定一个现有要点')
        if self.field == 'heading' and len(self.value)>300:raise ValueError('标题最多300字')
        if self.field == 'bullet' and len(self.value)>2000:raise ValueError('要点最多2000字')
        if any(ord(ch)<32 and ch not in '\n\r\t' for ch in self.value):raise ValueError('文本含不支持的控制字符')
        return self


class BatchEditInput(StrictModel):
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{16,80}$')
    expected_artifact_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_spec_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    changes: list[SectionChange] = Field(default_factory=list, max_length=100)
    section_order: list[StrictInt] = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=91, pattern=r'^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff _-]{0,90}$')

    @model_validator(mode='after')
    def valid_batch(self):
        keys=[(c.section,c.field,c.bullet_index) for c in self.changes]
        if len(set(keys))!=len(keys):raise ValueError('不能重复修改同一位置')
        if any(type(i) is not int for i in self.section_order):raise ValueError('章节顺序无效')
        return self


def _source(pid, aid):
    record=workspace_artifacts._record(pid,aid)
    if record.get('kind')!='office' or record.get('format') not in ('docx','pptx'):
        raise ValueError('仅支持本项目以可编辑规格生成的 Word 或 PPT')
    office_id=record.get('private',{}).get('office_id')
    if not office_id:raise ValueError('成果缺少原始生成规格')
    _,artifact=office.artifact(pid,office_id)
    if artifact.get('sha256')!=record.get('sha256'):raise ValueError('成果版本已变化')
    path=artifact.get('source_spec');checksum=artifact.get('source_sha256')
    if not isinstance(path,str) or not isinstance(checksum,str) or not re.fullmatch(r'[a-f0-9]{64}',checksum):
        raise ValueError('这份成果没有可用的项目规格，不能在此编辑')
    try:text=workspace.read_file(pid,path)
    except (OSError,ValueError):raise ValueError('原始规格不可用，请从项目历史恢复后再试') from None
    if hashlib.sha256(text.encode('utf-8')).hexdigest()!=checksum:
        raise ValueError('当前规格与成果版本不一致，请先从历史恢复原规格')
    spec=office._load_spec(text)
    if spec.kind not in ('document','presentation') or not spec.sections or spec.sheets:
        raise ValueError('该成果没有可编辑的章节规格')
    return record,artifact,spec


def inspect(pid,aid):
    with workspace.lock(pid),_project_lock(pid):
        assert_project_live(pid)
        record,artifact,spec=_source(pid,aid)
        return {'artifact_id':aid,'artifact_sha256':record['sha256'],'spec_sha256':artifact['source_sha256'],
                'kind':spec.kind,'title':spec.title,
                'sections':[{'index':i,'heading':section.heading,'body':section.body,'bullets':section.bullets,
                             'contains_table':section.table is not None,'contains_chart':section.chart is not None}
                            for i,section in enumerate(spec.sections)],
                'notice':'仅可修改章节标题、正文或已有要点；生成新文件，原成果和结构化表格、图表保持不变。内容与财务口径仍需核对。'}


def edit(pid,aid,actor,value):
    value=EditInput.model_validate(value)
    return _run_edit(pid,aid,actor,value)


def edit_batch(pid,aid,actor,value):
    value=BatchEditInput.model_validate(value)
    return _run_edit(pid,aid,actor,value)


def _run_edit(pid,aid,actor,value):
    key=hashlib.sha256(json.dumps([pid,actor,value.request_id],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    try:
        with FileLock(str(DATA/('office-edit-'+key+'.lock')),timeout=10):
            return _edit_locked(pid,aid,actor,value)
    except Timeout:
        raise ValueError('同一修改请求仍在处理中，请稍后使用同一请求重试') from None


def _edit_locked(pid,aid,actor,value):
    operation=value.model_dump();payload_hash=hashlib.sha256(json.dumps([pid,aid,actor,operation],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    ident='oe_'+hashlib.sha256(json.dumps([pid,actor,value.request_id],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()[:40]
    draft='reports/office-edits/'+ident+'.json'
    with workspace.lock(pid),_project_lock(pid):
        assert_project_live(pid)
        record,artifact,spec=_source(pid,aid)
        if record['sha256']!=value.expected_artifact_sha256 or artifact['source_sha256']!=value.expected_spec_sha256:
            raise ValueError('成果或规格版本已变化，请重新打开')
        changes=value.changes if isinstance(value,BatchEditInput) else [value]
        if isinstance(value,BatchEditInput):
            if sorted(value.section_order)!=list(range(len(spec.sections))):
                raise ValueError('章节顺序必须包含所有原章节且不能重复')
        changed=False
        for change in changes:
            if change.section>=len(spec.sections):raise ValueError('所选章节不存在')
            section=spec.sections[change.section]
            if change.field=='bullet':
                if change.bullet_index>=len(section.bullets):raise ValueError('所选要点不存在')
                if section.bullets[change.bullet_index]!=change.value:
                    section.bullets[change.bullet_index]=change.value;changed=True
            elif getattr(section,change.field)!=change.value:
                setattr(section,change.field,change.value);changed=True
        if isinstance(value,BatchEditInput) and value.section_order!=list(range(len(spec.sections))):
            spec.sections=[spec.sections[i] for i in value.section_order];changed=True
        if not changed:raise ValueError('内容没有变化')
        # Revalidate all limits after modifying a Pydantic model in place.
        revised=office.Spec.model_validate(spec.model_dump(mode='json'))
        content=json.dumps(revised.model_dump(mode='json'),ensure_ascii=False,indent=2)+'\n'
        content_hash=hashlib.sha256(content.encode('utf-8')).hexdigest()
        try:existing=get('office_edit',ident)
        except KeyError:existing=None
        if existing and (existing.get('project_id')!=pid or existing.get('actor')!=actor or existing.get('payload_sha256')!=payload_hash or existing.get('draft_sha256')!=content_hash):
            raise ValueError('请求编号已用于其他修改，请重新发起')
        if existing and existing.get('status')=='completed':
            saved=existing.get('result')
            if not isinstance(saved,dict) or saved.get('artifact_id')!=existing.get('artifact_id'):
                raise ValueError('已有成果回执不完整，请核对项目成果')
            try:current_draft=workspace.read_file(pid,draft)
            except (OSError,ValueError):raise ValueError('编辑方案已回退或不可用，请核对项目历史') from None
            if hashlib.sha256(current_draft.encode('utf-8')).hexdigest()!=content_hash:
                raise ValueError('编辑方案已变化，请核对项目历史')
            _,check=office.artifact(pid,saved['artifact_id'])
            if check.get('sha256')!=saved.get('sha256'):raise ValueError('已有成果校验失败')
            return saved
        if not existing:
            existing=put('office_edit',{'project_id':pid,'actor':actor,'payload_sha256':payload_hash,'draft':draft,
                               'draft_sha256':content_hash,'source_artifact_id':aid,'source_artifact_sha256':record['sha256'],
                               'source_spec_sha256':artifact['source_sha256'],'status':'pending','created_at':now()},ident)
        try:old=workspace.read_file(pid,draft)
        except FileNotFoundError:old=None
        if old is not None and old!=content:raise ValueError('编辑方案已变化，请检查项目历史')
        if old is None:workspace.write_file(pid,draft,content)
        if existing.get('draft_commit'):
            draft_commit=existing['draft_commit']
        else:
            draft_commit=workspace._checkpoint(pid,None,'编辑办公成果章节')['commit']
            existing=put('office_edit',dict(existing,draft_commit=draft_commit),ident)

    @contextmanager
    def publish_guard():
        # office.render owns workspace.lock; serialize its final replacement
        # and receipt with project deletion/restore after that lock.
        with _project_lock(pid):
            assert_project_live(pid)
            current=get('office_edit',ident)
            if current.get('payload_sha256')!=payload_hash or current.get('draft_sha256')!=content_hash:
                raise ValueError('编辑请求已变化，未生成成果')
            yield

    # Give each operation its own manifest name: two concurrent revisions
    # must never replace one another's named delivery receipt.
    output_name=value.name[:66]+'-'+ident[-24:]
    result=office.render({'project_id':pid,'interaction_mode':'work','text':'修改报告并生成交付文件'},draft,output_name,
                         expected_sha256=content_hash,publication_guard=publish_guard)
    with workspace.lock(pid),_project_lock(pid):
        assert_project_live(pid)
        try:current_draft=workspace.read_file(pid,draft)
        except (OSError,ValueError):raise ValueError('编辑方案已回退或不可用，请核对项目成果') from None
        if hashlib.sha256(current_draft.encode('utf-8')).hexdigest()!=content_hash:
            raise ValueError('编辑方案已变化，请核对项目成果')
        final_version=workspace._checkpoint(pid,None,'生成修改后的办公成果')
        current=get('office_edit',ident)
        if current.get('payload_sha256')!=payload_hash:raise ValueError('编辑请求已变化')
        response={**result,'original_artifact_id':aid,'original_artifact_sha256':record['sha256'],
                  'draft_path':draft,'draft_commit':draft_commit,'work_commit':final_version['commit'],
                  'notice':'已生成新文件；原文件、表格和图表未修改。修改的文字及财务内容仍需核对，未进行视觉验收。'}
        put('office_edit',dict(current,status='completed',artifact_id=result['artifact_id'],
                               artifact_sha256=result['sha256'],draft_commit=draft_commit,
                               output_commit=final_version['commit'],result=response,completed_at=now()),ident)
    return response


def install(app,auth):
    @app.get('/api/projects/{pid}/artifacts/{aid}/office-edit',dependencies=[Depends(auth)])
    def inspect_office_edit(pid:str,aid:str):
        try:return inspect(pid,aid)
        except KeyError:raise HTTPException(404,'成果不存在或不属于当前项目') from None
        except (ValueError,OSError) as error:raise HTTPException(409,str(error)[:500]) from None

    @app.post('/api/projects/{pid}/artifacts/{aid}/office-edit/batch',dependencies=[Depends(auth)])
    def apply_batch_office_edit(pid:str,aid:str,value:BatchEditInput,request:Request):
        try:return edit_batch(pid,aid,actor_from_identity(getattr(request.state,'identity',{})),value)
        except KeyError:raise HTTPException(404,'成果不存在或不属于当前项目') from None
        except (ValueError,OSError) as error:raise HTTPException(409,str(error)[:500]) from None

    @app.post('/api/projects/{pid}/artifacts/{aid}/office-edit',dependencies=[Depends(auth)])
    def apply_office_edit(pid:str,aid:str,value:EditInput,request:Request):
        try:return edit(pid,aid,actor_from_identity(getattr(request.state,'identity',{})),value)
        except KeyError:raise HTTPException(404,'成果不存在或不属于当前项目') from None
        except (ValueError,OSError) as error:raise HTTPException(409,str(error)[:500]) from None
