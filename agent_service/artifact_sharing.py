"""Version-pinned, explicitly reviewed sharing of report and Office artifacts.

A share ID is never a credential. HTML is returned only as text for a sandboxed
viewer; Office previews are content extraction, not a fidelity or finance check.
"""
import hashlib, json, os, re, secrets, tempfile
from datetime import timedelta
from pathlib import Path
from typing import Literal
from fastapi import Depends, HTTPException, Request, Response
from pydantic import Field
from sqlalchemy import select, update
from .models import StrictModel
from .store import get, items, put, transaction
from .config import DATA
from . import data_sharing as identity, workspace, workspace_artifacts as artifacts
from .workspace_lifecycle import _project_lock, assert_project_live
from .platform_files import open_regular_read

KIND='artifact_share'
MAX_BYTES=25*1024*1024
MAX_HTML_BYTES=512*1024

class ShareInput(StrictModel):
    artifact_id:str=Field(pattern=r'^wa_[a-f0-9]{44}$')
    expected_artifact_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    format:Literal['pptx','docx','md','html','htm']
    allowed_subjects:list[str]=Field(min_length=1,max_length=100)
    expires_hours:int=Field(default=168,strict=True,ge=1,le=720)
    allow_download:bool=Field(default=False,strict=True)
    allow_comments:bool=Field(default=False,strict=True)
    confirm_content:bool=Field(strict=True)
    request_id:str=Field(pattern=r'^[A-Za-z0-9_-]{16,80}$')
    expected_owner_subject:str|None=Field(default=None,min_length=1,max_length=2000)


class CommentInput(StrictModel):
    text:str=Field(min_length=1,max_length=2000)
    request_id:str=Field(pattern=r'^[A-Za-z0-9_-]{16,80}$')
    expected_content_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    expected_viewer_hash:str=Field(pattern=r'^[a-f0-9]{64}$')


def _subject_list(value):
    if (len(set(value))!=len(value) or any(not isinstance(s,str) or not s or s!=s.strip()
        or len(s)>2000 or any(ord(c)<32 for c in s) or s=='*' for s in value)):
        raise ValueError('分享对象必须是明确且不重复的企业账号主体')


def _public(row):
    result={key:row[key] for key in ('id','artifact_id','artifact_sha256','format','name','content_sha256','content_bytes',
        'state','created_at','expires_at','allowed_subject_count','allow_download')}
    result['allow_comments']=row.get('allow_comments') is True
    return result


def create(pid,value,actor):
    value=ShareInput.model_validate(value)
    if value.confirm_content is not True:raise ValueError('请先核对分享文件和接收对象')
    _subject_list(value.allowed_subjects);identity._require_oidc()
    owner=identity._actor(actor)
    ident='as_'+identity._hash(json.dumps([pid,owner,value.request_id],separators=(',',':')))[:40]
    body=value.model_dump();body.pop('request_id');body_hash=identity._hash(json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':')))
    legacy_body=dict(body);legacy_body.pop('allow_comments',None)
    legacy_hash=identity._hash(json.dumps(legacy_body,ensure_ascii=False,sort_keys=True,separators=(',',':')))
    with workspace.lock(pid),_project_lock(pid):
        assert_project_live(pid)
        try:old=get(KIND,ident)
        except KeyError:old=None
        if old:
            if old.get('project_id')!=pid or old.get('owner_hash')!=owner or old.get('request_sha256') not in ([body_hash,legacy_hash] if not value.allow_comments and 'allow_comments' not in old else [body_hash]):
                raise ValueError('请求编号已用于不同的分享')
            return _public(old)
        rec=artifacts._record(pid,value.artifact_id)
        if rec.get('sha256')!=value.expected_artifact_sha256:raise ValueError('成果版本已变化，请重新选择')
        kind=rec.get('kind');fmt=value.format
        if not ((kind=='office' and fmt in ('pptx','docx') and rec.get('format')==fmt)
                or (kind=='report' and fmt in ('pptx','docx','md'))
                or (kind=='code' and fmt in ('html','htm') and rec.get('format')==fmt)):
            raise ValueError('仅支持项目内已生成的 PPT、Word、Markdown 报告或 HTML 页面')
        if fmt in ('html','htm') and value.allow_download:
            raise ValueError('HTML 分享只支持安全预览，不提供可执行原文件下载')
        path,meta=artifacts.file(pid,value.artifact_id,fmt)
        if meta['download_bytes']>MAX_BYTES or (fmt in ('html','htm') and meta['download_bytes']>MAX_HTML_BYTES):
            raise ValueError('分享文件超过当前安全容量，请缩小文件')
        # This path is the content-addressed immutable copy returned by file().
        relative=str(path.relative_to(DATA));created=identity._utcnow();issuer=os.environ['OIDC_ISSUER']
        row={'project_id':pid,'artifact_id':value.artifact_id,'artifact_sha256':value.expected_artifact_sha256,
             'format':fmt,'name':meta['name'],'relative_path':relative,'content_sha256':meta['download_sha256'],
             'content_bytes':meta['download_bytes'],'state':'active','version':1,'created_at':created.isoformat(),
             'expires_at':(created+timedelta(hours=value.expires_hours)).isoformat(),'owner_hash':owner,
             'issuer':issuer,'audience':os.environ['OIDC_AUDIENCE'],
             'allowed_subject_hashes':[identity._subject_hash(s,issuer) for s in value.allowed_subjects],
             'allowed_subject_count':len(value.allowed_subjects),'allow_download':value.allow_download,'allow_comments':value.allow_comments,'request_sha256':body_hash}
        return _public(put(KIND,row,ident))


def list_shares(pid):
    from .store import all_items
    assert_project_live(pid)
    rows=[row for row in all_items(KIND) if row.get('project_id')==pid]
    rows.sort(key=lambda x:(x.get('created_at',''),x.get('id','')),reverse=True)
    return {'items':[_public(row) for row in rows[:500]],'truncated':len(rows)>500}


def revoke(pid,sid,actor='owner'):
    actor_hash=identity._actor(actor)
    with workspace.lock(pid),_project_lock(pid):
        assert_project_live(pid)
        with transaction() as connection:
            row=connection.execute(select(items.c.body).where(items.c.kind==KIND,items.c.id==sid).with_for_update()).first()
            if not row:raise KeyError(sid)
            record=json.loads(row[0])
            if record.get('project_id')!=pid:raise KeyError(sid)
            if record.get('state')!='revoked':
                record.update(state='revoked',revoked_at=identity._utcnow().isoformat(),
                              revoked_by_hash=actor_hash,version=record.get('version',0)+1)
                connection.execute(update(items).where(items.c.kind==KIND,items.c.id==sid).values(body=json.dumps(record,ensure_ascii=False)))
            return _public(record)


def _check(row,subject):
    from datetime import datetime
    try:
        expiry=datetime.fromisoformat(row['expires_at'])
        valid=(expiry.tzinfo is not None and expiry>identity._utcnow() and row['state']=='active'
               and row['issuer']==os.environ['OIDC_ISSUER'] and row['audience']==os.environ['OIDC_AUDIENCE'])
        subject_hash=identity._subject_hash(subject,row['issuer'])
        if not valid or not any(secrets.compare_digest(subject_hash,s) for s in row['allowed_subject_hashes']):raise ValueError()
    except (ValueError,TypeError,KeyError):raise HTTPException(403,'分享已失效或当前账号无权访问') from None


def _capture(request,sid,*,download=False,materialize=None):
    identity._require_oidc(request_context=True);subject=identity.verified_subject(request)
    try:first=get(KIND,sid)
    except KeyError:raise HTTPException(404,'分享不存在') from None
    pid=first.get('project_id')
    if not isinstance(pid,str):raise HTTPException(403,'分享不可用')
    with workspace.lock(pid),_project_lock(pid):
        try:assert_project_live(pid)
        except (ValueError,KeyError):raise HTTPException(410,'项目或分享已不可用') from None
        if identity.verified_subject(request)!=subject:raise HTTPException(401,'身份凭据已变化')
        with transaction() as connection:
            row=connection.execute(select(items.c.body).where(items.c.kind==KIND,items.c.id==sid).with_for_update()).first()
            if not row:raise HTTPException(404,'分享不存在')
            record=json.loads(row[0]);_check(record,subject)
            if record.get('project_id')!=pid:raise HTTPException(410,'分享已不可用')
            if download and record.get('allow_download') is not True:raise HTTPException(403,'分享者未允许下载')
            try:
                artifact=artifacts._record(pid,record['artifact_id'])
                if artifact.get('sha256')!=record['artifact_sha256']:raise ValueError('changed')
                path=DATA/record['relative_path']
                if not path.resolve().is_relative_to(artifacts._root().resolve()):raise ValueError('invalid path')
                snapshot=tempfile.SpooledTemporaryFile(max_size=1024*1024,mode='w+b');digest=hashlib.sha256();size=0
                try:
                    with os.fdopen(open_regular_read(path),'rb') as f:
                        while chunk:=f.read(65536):
                            size+=len(chunk)
                            if size>MAX_BYTES:raise ValueError('too large')
                            digest.update(chunk);snapshot.write(chunk)
                    if size!=record['content_bytes'] or not secrets.compare_digest(digest.hexdigest(),record['content_sha256']):raise ValueError('changed')
                    snapshot.seek(0)
                    record['_viewer_hash']=identity._hash(json.dumps(['artifact-share-viewer',sid,record['issuer'],subject],ensure_ascii=False,separators=(',',':')))
                    value=materialize(record,connection,subject) if materialize else None
                    _check(record,subject);assert_project_live(pid)
                    if identity.verified_subject(request)!=subject:raise HTTPException(401,'身份凭据已变化')
                    if hasattr(request,'state'):
                        request.state.identity={'authenticated':True,'mode':'oidc','subject':subject,
                                                'scope':'shared_artifact_recipient'}
                    if materialize:
                        snapshot.close()
                        return value
                    return snapshot,record
                except BaseException:snapshot.close();raise
            except (KeyError,ValueError,OSError) as e:raise HTTPException(410,'固定成果已变化或不可读取') from None


def preview(request,sid,offset=0,limit=50):
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:raise HTTPException(400,'分页参数无效')
    snapshot,record=_capture(request,sid)
    try:
        fmt=record['format'];raw=snapshot.read()
        common={'name':record['name'],'format':fmt,'sha256':record['content_sha256'],'allow_download':record['allow_download'],
                'allow_comments':record.get('allow_comments') is True,'viewer_hash':record['_viewer_hash'],
                'notice':'仅为固定文件的安全预览，不构成财务审批；已查看或下载的内容无法撤回。'}
        if fmt in ('html','htm','md'):
            try:text=raw.decode('utf-8-sig')
            except UnicodeError:raise HTTPException(409,'文本不是有效的 UTF-8 编码') from None
            if fmt in ('html','htm'):
                return dict(common,render='html',text=text,notice=common['notice']+' 页面脚本和外部资源将被禁用。')
            if offset>len(text):raise HTTPException(400,'分页位置无效')
            end=min(len(text),offset+20000)
            return dict(common,render='text',text=text[offset:end],offset=offset,total=len(text),next_offset=end if end<len(text) else None)
        from .document_reader import read_document
        # A temporary immutable byte copy is used for parsing; names are not trusted.
        temp=None
        try:
            with tempfile.NamedTemporaryFile(suffix='.'+fmt,delete=False) as f:
                temp=Path(f.name);f.write(raw)
            document=read_document(temp,max_file_bytes=MAX_BYTES,max_text_bytes=2*1024*1024,max_blocks=10000)
            blocks=artifacts._document_blocks(document);end=min(len(blocks),offset+limit)
            if offset>len(blocks):raise HTTPException(400,'分页位置无效')
            safe=[]
            for b in blocks[offset:end]:
                if b.get('type')=='paragraph':safe.append({'type':'paragraph','text':b.get('text',''),'slide_index':b.get('slide_index')})
                elif b.get('type')=='table':safe.append({'type':'table','columns':b.get('columns',[]),'rows':b.get('rows',[])})
            return dict(common,render='document',blocks=safe,offset=offset,total=len(blocks),next_offset=end if end<len(blocks) else None,
                        notice=common['notice']+' 仅提取文字和表格，不还原页面版式、图片或图表。')
        finally:
            if temp is not None:temp.unlink(missing_ok=True)
    finally:snapshot.close()



COMMENT_KIND='artifact_share_comment'
MAX_COMMENTS_PER_SHARE=1000


def _comment_prefix(sid):return 'ac_'+identity._hash(sid)[:20]+'_'


def _comment_rows(connection,sid):
    prefix=_comment_prefix(sid);pattern=prefix.replace('_',r'\_')+'%'
    rows=connection.execute(select(items.c.body).where(items.c.kind==COMMENT_KIND,items.c.id.like(pattern,escape='\\')).limit(MAX_COMMENTS_PER_SHARE+1)).all()
    if len(rows)>MAX_COMMENTS_PER_SHARE:raise HTTPException(409,'评论超过容量，未返回不完整记录')
    try:return [json.loads(row[0]) for row in rows]
    except (ValueError,TypeError):raise HTTPException(409,'评论记录完整性校验失败') from None


def _comment_public(item,record):
    from .store import unseal
    try:
        payload=unseal(item['sealed'])
        if (not isinstance(payload,dict) or payload.get('id')!=item.get('id') or payload.get('share_id')!=record['id']
            or payload.get('content_sha256')!=record['content_sha256'] or not isinstance(payload.get('author'),str)
            or not isinstance(payload.get('text'),str) or not isinstance(payload.get('created_at'),str)):
            raise ValueError('inconsistent comment')
        return {k:payload[k] for k in ('id','author','text','created_at','content_sha256')}
    except Exception:raise HTTPException(409,'评论记录完整性校验失败') from None


def list_comments(request,sid,offset=0,limit=50):
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:raise HTTPException(400,'评论分页参数无效')
    def capture(record,connection,subject):
        if record.get('allow_comments') is not True:raise HTTPException(403,'此分享未开放评论')
        rows=[_comment_public(item,record) for item in _comment_rows(connection,sid)]
        rows.sort(key=lambda x:(x['created_at'],x['id']))
        return {'items':rows[offset:offset+limit],'offset':offset,'total':len(rows),'has_more':offset+limit<len(rows),
                'content_sha256':record['content_sha256'],'viewer_hash':record['_viewer_hash'],
                'notice':'评论对获准访问此分享的账号可见，仅供交流，不构成财务审批。'}
    return _capture(request,sid,materialize=capture)


def list_owner_comments(pid,sid,actor,offset=0,limit=50):
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:raise HTTPException(400,'评论分页参数无效')
    owner=identity._actor(actor)
    with workspace.lock(pid),_project_lock(pid):
        assert_project_live(pid)
        with transaction() as connection:
            row=connection.execute(select(items.c.body).where(items.c.kind==KIND,items.c.id==sid).with_for_update()).first()
            if not row:raise HTTPException(404,'分享不存在')
            record=json.loads(row[0])
            if record.get('project_id')!=pid or not secrets.compare_digest(record.get('owner_hash',''),owner):raise HTTPException(404,'分享不存在')
            if record.get('allow_comments') is not True:raise HTTPException(403,'此分享未开放评论')
            comments=[_comment_public(item,record) for item in _comment_rows(connection,sid)]
            comments.sort(key=lambda x:(x['created_at'],x['id']))
            return {'items':comments[offset:offset+limit],'offset':offset,'total':len(comments),'has_more':offset+limit<len(comments),
                    'content_sha256':record['content_sha256'],'notice':'历史评论仅供交流，不构成财务审批。撤销后分享者仍可查看留存记录。'}


def add_comment(request,sid,value):
    from .store import cipher,seal,unseal,now
    from .actor_context import actor_from_identity
    from . import audit
    value=CommentInput.model_validate(value);content=value.text.strip()
    if not content or any(ord(c)<32 and c not in '\n\r\t' for c in content):raise HTTPException(400,'评论内容须为1至2000字的普通文本')
    cipher()
    def capture(record,connection,subject):
        if record.get('allow_comments') is not True:raise HTTPException(403,'此分享未开放评论')
        if record['content_sha256']!=value.expected_content_sha256 or not secrets.compare_digest(record['_viewer_hash'],value.expected_viewer_hash):
            raise HTTPException(409,'成果版本或登录账号已变化，未提交评论')
        actor=actor_from_identity({'authenticated':True,'mode':'oidc','subject':subject,'issuer':record['issuer']})
        ident=_comment_prefix(sid)+identity._hash(json.dumps([sid,actor,value.request_id],separators=(',',':')))[:44]
        existing=connection.execute(select(items.c.body).where(items.c.id==ident,items.c.kind==COMMENT_KIND)).first()
        if existing:
            try:item=json.loads(existing[0]);payload=unseal(item['sealed'])
            except Exception:raise HTTPException(409,'评论记录完整性校验失败') from None
            if (payload.get('share_id')!=sid or payload.get('actor')!=actor or payload.get('content_sha256')!=value.expected_content_sha256 or payload.get('text')!=content):
                raise HTTPException(409,'请求编号已用于不同评论')
            return _comment_public(item,record)
        if len(_comment_rows(connection,sid))>=MAX_COMMENTS_PER_SHARE:raise HTTPException(409,'评论数量达到当前服务容量')
        stamp=now();author='用户 '+identity._hash(sid+'\x00'+actor)[:12]
        payload={'id':ident,'share_id':sid,'content_sha256':record['content_sha256'],'actor':actor,'author':author,'text':content,'created_at':stamp}
        item={'id':ident,'project_id':record['project_id'],'share_id':sid,'sealed':seal(payload)}
        connection.execute(items.insert().values(id=ident,kind=COMMENT_KIND,body=json.dumps(item,ensure_ascii=False)))
        sentinel=connection.execute(select(items.c.id).where(items.c.id=='audit-lock',items.c.kind=='system').with_for_update()).first()
        if not sentinel:raise HTTPException(503,'审计服务不可用，评论未写入')
        previous=connection.execute(select(audit.events.c.event_hash).order_by(audit.events.c.sequence.desc()).limit(1)).first()
        previous=previous[0] if previous else '0'*64;action='artifact_share.comment.created'
        details={'share_id':sid,'comment_id':ident,'author':author,'content_sha256':record['content_sha256']}
        body=json.dumps(details,ensure_ascii=False,sort_keys=True,default=str)
        signature=audit.digest(stamp,action,ident,body,previous)
        connection.execute(audit.events.insert().values(created=stamp,action=action,subject=ident,body=body,previous_hash=previous,event_hash=signature))
        return _comment_public(item,record)
    return _capture(request,sid,materialize=capture)


def _same_origin(request):
    from urllib.parse import urlsplit
    from .activity import public_origin
    origin=request.headers.get('origin');site=request.headers.get('sec-fetch-site')
    if site and site not in ('same-origin','none'):raise HTTPException(403,'评论需要同源请求')
    if origin:
        try:
            parsed=urlsplit(origin);expected=urlsplit(public_origin() or (request.url.scheme+'://'+request.headers.get('host','')))
            valid=(parsed.scheme in ('http','https') and parsed.hostname and not parsed.username and not parsed.password
                   and parsed.path in ('','/') and not parsed.query and not parsed.fragment and expected.scheme==parsed.scheme
                   and expected.hostname and expected.hostname.lower()==parsed.hostname.lower()
                   and (expected.port or (443 if expected.scheme=='https' else 80))==(parsed.port or (443 if parsed.scheme=='https' else 80)))
        except ValueError:valid=False
        if not valid:raise HTTPException(403,'评论需要同源请求')

def install(app,auth):
    from .actor_context import actor_from_identity
    from .api import _SnapshotResponse
    deps=[Depends(auth)]
    @app.post('/api/projects/{pid}/artifact-shares',dependencies=deps)
    def add(pid:str,value:ShareInput,request:Request):
        if value.expected_owner_subject is not None and value.expected_owner_subject!=getattr(request.state,'identity',{}).get('subject'):
            raise HTTPException(409,'当前账号与分享请求的提交账号不同')
        actor=actor_from_identity(getattr(request.state,'identity',{}))
        return create(pid,value,actor)
    @app.get('/api/projects/{pid}/artifact-shares',dependencies=deps)
    def listing(pid:str):return list_shares(pid)
    @app.delete('/api/projects/{pid}/artifact-shares/{sid}',dependencies=deps)
    def remove(pid:str,sid:str,request:Request):return revoke(pid,sid,actor_from_identity(getattr(request.state,'identity',{})))
    @app.get('/api/projects/{pid}/artifact-shares/{sid}/comments',dependencies=deps)
    def owner_comments(pid:str,sid:str,request:Request,response:Response,offset:int=0,limit:int=50):
        response.headers['Cache-Control']='private, no-store'
        return list_owner_comments(pid,sid,actor_from_identity(getattr(request.state,'identity',{})),offset,limit)
    @app.get('/api/shared-artifacts/{sid}')
    def read(sid:str,request:Request,offset:int=0,limit:int=50):return preview(request,sid,offset,limit)
    @app.get('/api/shared-artifacts/{sid}/comments')
    def comments(sid:str,request:Request,response:Response,offset:int=0,limit:int=50):
        response.headers['Cache-Control']='private, no-store'
        return list_comments(request,sid,offset,limit)
    @app.post('/api/shared-artifacts/{sid}/comments')
    def comment(sid:str,value:CommentInput,request:Request,response:Response):
        _same_origin(request);response.headers['Cache-Control']='private, no-store'
        return add_comment(request,sid,value)
    @app.get('/api/shared-artifacts/{sid}/download')
    def download(sid:str,request:Request):
        snap,row=_capture(request,sid,download=True)
        response=_SnapshotResponse(snap,filename=row['name'],size=row['content_bytes'])
        response.media_type='application/octet-stream';response.headers['Content-Type']='application/octet-stream'
        response.headers['Cache-Control']='private, no-store'
        return response
