"""Read-only sharing of one immutable data result to explicit OIDC subjects.

A share ID is an object identifier, never a bearer capability. Recipient JWTs
use the fixed issuer/JWKS/audience but bypass the workspace owner subject list
only on dedicated result routes. All owner APIs keep owner authentication.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException
from sqlalchemy import select, update

from . import identity
from .store import all_items, get, items, put, transaction, uid

KIND = 'data_share'


def _utcnow():
    return datetime.now(timezone.utc)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _subject_hash(subject, issuer):
    return _hash(json.dumps([issuer, subject], ensure_ascii=False, separators=(',', ':')))


def _require_oidc(request_context=False):
    try:
        if identity.mode() != 'oidc':
            raise ValueError('限定用户范围分享需要已配置并验签的OIDC身份，不能使用工作空间公共口令')
        identity.validate_config()
    except ValueError:
        if request_context:
            raise HTTPException(401, '此分享需要有效的OIDC身份配置') from None
        raise


def _same_identity_domain(record):
    issuer=os.getenv('OIDC_ISSUER');audience=os.getenv('OIDC_AUDIENCE')
    return bool(issuer and audience and record.get('issuer')==issuer and record.get('audience')==audience)


def verified_subject(request):
    """Verify a recipient JWT without granting owner-workspace membership."""
    _require_oidc(request_context=True)
    token = request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
    if not token and os.getenv('OIDC_ACCEPT_PROXY_HEADER') == '1':
        token = request.headers.get('X-Forwarded-Access-Token', '')
    if not token or len(token) > 16000:
        raise HTTPException(401, '身份凭据无效')
    try:
        header = jwt.get_unverified_header(token)
        if header.get('alg') not in ('RS256', 'ES256') or not isinstance(header.get('kid'), str):
            raise ValueError('Unapproved JWT header')
        selected = [key for key in identity.keys() if key.get('kid') == header['kid']]
        if not selected:
            selected = [key for key in identity.keys(True) if key.get('kid') == header['kid']]
        if len(selected) != 1:
            raise ValueError('Missing or ambiguous key')
        key = selected[0]
        if key.get('alg') not in (None, header['alg']) or key.get('use') not in (None, 'sig'):
            raise ValueError('Key not intended for this signature')
        if key.get('key_ops') is not None and 'verify' not in key['key_ops']:
            raise ValueError('Key not permitted for verification')
        value = jwt.decode(token, jwt.PyJWK.from_dict(key).key, algorithms=[header['alg']],
                           audience=os.environ['OIDC_AUDIENCE'], issuer=os.environ['OIDC_ISSUER'],
                           leeway=30, options={'require': ['exp', 'iss', 'aud', 'sub']})
        subject = value['sub']
        if not isinstance(subject, str) or not subject or len(subject) > 2000:
            raise ValueError('Invalid subject')
        # Do not call identity.principal(): only owner APIs require membership in
        # OIDC_ALLOWED_SUBJECTS. Share recipients do not receive that authority.
        return subject
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, '身份凭据无效或已过期') from None


def _actor(actor):
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 2000:
        raise ValueError('分享操作需要已鉴权的所有者身份')
    return _hash(actor)


def _public(record):
    return {key: value for key, value in record.items()
            if key not in ('allowed_subject_hashes', 'issuer', 'audience')}


def _require_published_result(project_id,result_id):
    """A pageable share exposes all rows, so require the current adopted version."""
    from . import project_data
    from .jobs import get_job
    run=get_job(result_id)
    if run.get('project_id')!=project_id or run.get('kind')!='data_workflow' or run.get('status')!='succeeded':
        raise ValueError('仅可分享当前已采用的正式结果')
    workflow=project_data.owned(project_id,'data_workflow',run['target'])
    if workflow.get('published_run')!=result_id:
        raise ValueError('仅可分享当前已采用的正式结果；版本变更后需重新分享')
    project_data.ensure_candidate_bulk_allowed(project_id,result_id)


def create_share(project_id, result_id, allowed_subjects, actor,
                 expires_hours=168, allow_download=False, allow_comments=False):
    """Owner API only; no invitations or messages are sent by this function."""
    from . import workspace
    from .workspace_lifecycle import _project_lock, assert_project_live
    # Match restore's workspace -> lifecycle order and prevent a share from
    # appearing after project removal has completed its revocation scan.
    with workspace.lock(project_id), _project_lock(project_id):
        assert_project_live(project_id)
        return _create_share(project_id,result_id,allowed_subjects,actor,expires_hours,allow_download,allow_comments)


def _create_share(project_id, result_id, allowed_subjects, actor, expires_hours, allow_download, allow_comments):
    _require_oidc()
    actor_hash = _actor(actor)
    get('project', project_id)
    if type(expires_hours) is not int or not 1 <= expires_hours <= 720:
        raise ValueError('分享有效期必须为1至720小时')
    if type(allow_download) is not bool:
        raise ValueError('下载权限必须是明确的布尔值')
    if type(allow_comments) is not bool:
        raise ValueError('评论权限必须是明确的布尔值')
    if not isinstance(allowed_subjects, list) or not 1 <= len(allowed_subjects) <= 100:
        raise ValueError('必须明确指定1至100个OIDC主体')
    if any(not isinstance(s, str) or not s or s != s.strip() or len(s) > 2000
           or any(ord(c) < 32 for c in s) for s in allowed_subjects):
        raise ValueError('分享对象必须是明确的OIDC主体，不能使用空白、通配符或工作空间口令')
    if '*' in allowed_subjects or len(set(allowed_subjects)) != len(allowed_subjects):
        raise ValueError('分享对象不能重复或使用通配符')
    from .project_data import result_folder
    _, metadata = result_folder(project_id, result_id)
    # A recipient can page through the complete result. Creating a share must
    # therefore enforce the same source/semantic gate as a bulk export.
    _require_published_result(project_id, result_id)
    issuer = os.environ['OIDC_ISSUER']
    created = _utcnow()
    record = {'project_id': project_id, 'result_id': result_id,
              'schema_version': 1, 'version': 1, 'state': 'active',
              'created_at': created.isoformat(),
              'expires_at': (created + timedelta(hours=expires_hours)).isoformat(),
              'created_by_hash': actor_hash, 'revoked_at': None, 'revoked_by_hash': None,
              'issuer': issuer, 'audience': os.environ['OIDC_AUDIENCE'],
              'allowed_subject_hashes': [_subject_hash(s, issuer) for s in allowed_subjects],
              'allowed_subject_count': len(allowed_subjects), 'allow_download': allow_download,
              'allow_comments': allow_comments,
              'plan_sha256': metadata['plan_sha256'], 'handoff_sha256': metadata['handoff_sha256'],
              'scope': 'one_immutable_result; '+('read_and_comment' if allow_comments else 'read_only')+'; signed_oidc_identity_required'}
    return _public(put(KIND, record, 'share_' + uid()))


def list_shares(project_id):
    _require_oidc()
    get('project', project_id)
    records = [r for r in all_items(KIND) if r.get('project_id') == project_id and _same_identity_domain(r)]
    records.sort(key=lambda r: (r['created_at'], r['id']), reverse=True)
    return [_public(r) for r in records]


def revoke(project_id, share_id, actor):
    _require_oidc()
    get('project', project_id)
    actor_hash = _actor(actor)
    with transaction() as connection:
        row = connection.execute(select(items.c.body).where(items.c.id == share_id,
                                 items.c.kind == KIND).with_for_update()).first()
        if row is None:
            raise KeyError(share_id)
        record = json.loads(row[0])
        if record.get('project_id') != project_id:
            raise ValueError('分享不属于当前项目')
        if not _same_identity_domain(record):
            raise KeyError(share_id)
        if record['state'] != 'revoked':
            record.update(state='revoked', revoked_at=_utcnow().isoformat(),
                          revoked_by_hash=actor_hash, version=record['version'] + 1)
            connection.execute(update(items).where(items.c.id == share_id, items.c.kind == KIND)
                               .values(body=json.dumps(record, ensure_ascii=False)))
    return _public(record)


def _authorize_record(record, subject):
    try:
        expires = datetime.fromisoformat(record['expires_at'])
        if expires.tzinfo is None:
            raise ValueError('Expiration timezone missing')
        valid = (record['state'] == 'active' and expires > _utcnow()
                 and record['issuer'] == os.environ['OIDC_ISSUER']
                 and record['audience'] == os.environ['OIDC_AUDIENCE'])
        target = _subject_hash(subject, record['issuer'])
        allowed = any(secrets.compare_digest(target, saved)
                      for saved in record['allowed_subject_hashes'])
        if not valid or not allowed:
            raise ValueError('Share not accessible')
    except (KeyError, TypeError, ValueError):
        raise HTTPException(403, '分享已失效或此账号无访问权限') from None
    # Verify that the exact immutable snapshot remains available. A newer result
    # is never silently substituted for the originally shared one.
    from .project_data import result_folder
    from .workspace_lifecycle import assert_project_live
    try:
        assert_project_live(record['project_id'])
        _, metadata = result_folder(record['project_id'], record['result_id'])
        if metadata['plan_sha256'] != record['plan_sha256'] or metadata['handoff_sha256'] != record['handoff_sha256']:
            raise ValueError('Shared snapshot changed')
    except (KeyError, ValueError, OSError):
        raise HTTPException(410, '所分享的固定结果版本已不可用') from None
    # Recheck every access, including shares issued before this guard existed.
    # A change to a source, ancestry or review state must not silently keep a
    # previously created share open.
    try:
        _require_published_result(record['project_id'], record['result_id'])
    except (KeyError, ValueError, OSError):
        raise HTTPException(403, '共享结果不是当前正式版本，或来源、业务语义尚未通过核对') from None
    return record


def authorize(request, share_id):
    _require_oidc(request_context=True)
    subject = verified_subject(request)
    try:
        record = get(KIND, share_id)
    except KeyError:
        raise HTTPException(404, '分享不存在') from None
    return _authorize_record(record, subject)


def _locked_snapshot(request, share_id, materialize):
    """Authorize and capture one response while revocation and restore wait.

    Only the captured response is returned after the transaction. A later
    revocation cannot recall bytes already captured for an in-flight request.
    """
    from . import workspace
    from .jobs import get_job, jobs
    from .workspace_lifecycle import _project_lock, assert_project_live
    _require_oidc(request_context=True)
    subject=verified_subject(request)
    try: initial=get(KIND,share_id)
    except KeyError:raise HTTPException(404,'分享不存在') from None
    pid=initial.get('project_id')
    if not isinstance(pid,str):raise HTTPException(403,'分享记录无效')
    with workspace.lock(pid),_project_lock(pid):
        try:assert_project_live(pid)
        except (KeyError,ValueError):raise HTTPException(410,'所分享的固定结果版本已不可用') from None
        # Lock waits or CSV generation can outlast a short-lived token.
        # Validate the identity again before locking and capturing data.
        if verified_subject(request)!=subject:raise HTTPException(401,'身份凭据无效或已过期')
        try:initial=get(KIND,share_id);run=get_job(initial['result_id'])
        except (KeyError,ValueError):raise HTTPException(410,'所分享的固定结果版本已不可用') from None
        target=run.get('target')
        if initial.get('project_id')!=pid or not isinstance(target,str):
            raise HTTPException(410,'所分享的固定结果版本已不可用')
        # Lock the same rows modified by revoke() and publish_result(). SQLite
        # uses BEGIN IMMEDIATE; PostgreSQL uses row-level FOR UPDATE. Acquiring
        # workspace/lifecycle before the transaction matches restore/delete.
        with transaction() as connection:
            row=connection.execute(select(items.c.body).where(items.c.id==share_id,
                                   items.c.kind==KIND).with_for_update()).first()
            if row is None:raise HTTPException(404,'分享不存在')
            record=json.loads(row[0])
            workflow=connection.execute(select(items.c.body).where(items.c.id==target,
                                        items.c.kind=='data_workflow').with_for_update()).first()
            if workflow is None:raise HTTPException(410,'所分享的固定结果版本已不可用')
            locked=json.loads(workflow[0])
            job=connection.execute(select(jobs.c.target,jobs.c.status).where(
                                   jobs.c.id==record.get('result_id')).with_for_update()).first()
            if (record.get('project_id')!=pid or record.get('result_id')!=initial.get('result_id')
                    or locked.get('project_id')!=pid or locked.get('published_run')!=record.get('result_id')
                    or job is None or job.target!=target or job.status!='succeeded'):
                raise HTTPException(403,'共享结果不是当前正式版本，或来源、业务语义尚未通过核对')
            _authorize_record(record,subject)
            # Shared recipients are not the workspace owner. Run any
            # actor-scoped reads under the verified issuer+subject identity.
            from .actor_context import actor_scope,actor_from_identity
            actor=actor_from_identity({'authenticated':True,'mode':'oidc','subject':subject,
                                       'issuer':record['issuer']})
            with actor_scope(actor):value=materialize(record,connection,actor)
            # Check again before releasing either lock; no path is opened by
            # the HTTP response after the authorization boundary.
            _authorize_record(record,subject)
            # The generic API audit middleware otherwise labels this verified
            # recipient as the shared owner token. This is request-local only;
            # owner endpoints still run their own identity.principal check.
            if hasattr(request,'state'):
                request.state.identity={'authenticated':True,'mode':'oidc','subject':subject,
                                        'scope':'shared_result_recipient'}
            return value


def read_shared(request,share_id,offset=0,limit=100):
    from .project_data import read_result
    def capture(record,connection,actor):
        page=read_result(record['project_id'],record['result_id'],offset,limit)
        # Only the shared result and its version/pagination are public to a
        # recipient. Owner-only alignment receipts, workflow context, source
        # identifiers, and future internal fields must not leak by default.
        allowed=('result_id','run_id','plan_sha256','data_as_of','rows','schema','offset','limit','total',
                 'next_offset','complete_result_available','page_is_complete_result','manifest_sha256',
                 'decimal_representation')
        return {key:page[key] for key in allowed if key in page}
    return _locked_snapshot(request,share_id,capture)


def download_shared(request,share_id):
    """Return a private verified result CSV snapshot, never a deferred FileResponse."""
    import hashlib,os,tempfile
    from .data_workflows import digest
    from .platform_files import open_regular_read
    from .project_data import result_download
    # Export creation writes its own durable receipt. Do it before the locked
    # transaction, then reauthorize and validate the bytes under the locks.
    first=authorize(request,share_id)
    if first.get('allow_download') is not True:raise HTTPException(403,'该共享不允许下载')
    # The internal handoff archive also contains raw provenance and source
    # values. A result share grants only the final result, so export CSV and
    # never include the internal handoff archive.
    path=result_download(first['project_id'],first['result_id'],'csv',expected_handoff_sha256=first['handoff_sha256'])
    snapshot=tempfile.SpooledTemporaryFile(max_size=1024*1024,mode='w+b')
    try:
        def capture(record,connection,actor):
            if record.get('allow_download') is not True:raise HTTPException(403,'该共享不允许下载')
            if (record.get('project_id')!=first['project_id'] or record.get('result_id')!=first['result_id']
                    or record.get('handoff_sha256')!=first['handoff_sha256']):
                raise HTTPException(410,'所分享的固定结果版本已不可用')
            key='dc_'+digest([record['project_id'],record['result_id'],record['handoff_sha256'],'csv-literal-v2'])[:40]
            receipt=get('data_export',key)
            if (receipt.get('project_id')!=record['project_id'] or receipt.get('run_id')!=record['result_id']
                    or receipt.get('source_manifest')!=record['handoff_sha256'] or receipt.get('state')!='committed'
                    or not isinstance(receipt.get('sha256'),str) or len(receipt['sha256'])!=64):
                raise ValueError('共享文件回执不可用')
            hasher=hashlib.sha256()
            with os.fdopen(open_regular_read(path),'rb') as stream:
                while chunk:=stream.read(64*1024):
                    snapshot.write(chunk);hasher.update(chunk)
            if not secrets.compare_digest(hasher.hexdigest(),receipt.get('sha256','')):
                raise ValueError('共享文件内容与回执不一致')
            size=snapshot.tell();snapshot.seek(0)
            return snapshot,size
        return _locked_snapshot(request,share_id,capture)
    except BaseException:
        snapshot.close()
        raise


COMMENT_KIND='data_share_comment'
MAX_COMMENTS_PER_SHARE=1000


def _comment_prefix(share_id):
    # Hex-only prefix is safe for SQL LIKE and does not expose the share ID.
    return 'sc_'+_hash(share_id)[:16]+'_'


def _comment_public(item,record):
    from .store import unseal
    try:
        payload=unseal(item['sealed'])
        if (not isinstance(payload,dict) or payload.get('id')!=item.get('id')
                or payload.get('share_id')!=record['id'] or payload.get('result_id')!=record['result_id']
                or payload.get('handoff_sha256')!=record['handoff_sha256']
                or not isinstance(payload.get('author'),str) or not isinstance(payload.get('text'),str)
                or not isinstance(payload.get('created_at'),str)):
            raise ValueError('inconsistent comment')
        return {k:payload[k] for k in ('id','author','text','created_at','handoff_sha256')}
    except Exception:
        raise HTTPException(409,'评论记录完整性校验失败，请联系管理员') from None


def _comment_rows(connection,share_id):
    prefix=_comment_prefix(share_id)
    # Bound the number of stored comments examined and fail instead of
    # returning an apparently complete truncated discussion.
    pattern=prefix.replace('_',r'\_')+'%'
    rows=connection.execute(select(items.c.body).where(items.c.kind==COMMENT_KIND,
                            items.c.id.like(pattern,escape='\\')).limit(MAX_COMMENTS_PER_SHARE+1)).all()
    if len(rows)>MAX_COMMENTS_PER_SHARE:
        raise HTTPException(409,'评论数量超过当前服务容量，未返回不完整记录')
    try:return [json.loads(row[0]) for row in rows]
    except (TypeError,ValueError):raise HTTPException(409,'评论记录完整性校验失败，请联系管理员') from None


def list_comments(request,share_id,offset=0,limit=50):
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
        raise HTTPException(400,'评论分页参数无效')
    def capture(record,connection,actor):
        if record.get('allow_comments') is not True:raise HTTPException(403,'此分享未开放评论')
        comments=[_comment_public(item,record) for item in _comment_rows(connection,share_id)]
        comments.sort(key=lambda item:(item['created_at'],item['id']))
        return {'items':comments[offset:offset+limit],'offset':offset,'total':len(comments),
                'has_more':offset+limit<len(comments),'handoff_sha256':record['handoff_sha256'],
                'notice':'评论对获此分享访问权的账号可见，仅供交流，不构成财务审批或准确性确认。'}
    return _locked_snapshot(request,share_id,capture)


def add_comment(request,share_id,text,request_id,expected_handoff_sha256):
    import re
    from .store import cipher,now,seal,unseal
    if (not isinstance(text,str) or not text.strip() or len(text)>2000
            or any(ord(c)<32 and c not in '\n\r\t' for c in text)):
        raise HTTPException(400,'评论内容须为1至2000字的普通文本')
    if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}',request_id):
        raise HTTPException(400,'评论请求编号无效')
    if not isinstance(expected_handoff_sha256,str) or not re.fullmatch(r'[a-f0-9]{64}',expected_handoff_sha256):
        raise HTTPException(400,'请先核对结果版本')
    content=text.strip()
    # Initialize the shared encryption/audit key outside the SQL transaction.
    cipher()
    from . import audit
    def capture(record,connection,actor):
        if record.get('allow_comments') is not True:raise HTTPException(403,'此分享未开放评论')
        if record['handoff_sha256']!=expected_handoff_sha256:raise HTTPException(409,'分享结果版本已变化')
        ident=_comment_prefix(share_id)+_hash(json.dumps([share_id,actor,request_id],separators=(',',':')))[:44]
        existing=connection.execute(select(items.c.body).where(items.c.id==ident,items.c.kind==COMMENT_KIND)).first()
        if existing:
            try:item=json.loads(existing[0]);payload=unseal(item['sealed'])
            except Exception:raise HTTPException(409,'评论记录完整性校验失败，请联系管理员') from None
            if (payload.get('share_id')!=share_id or payload.get('actor')!=actor
                    or payload.get('handoff_sha256')!=expected_handoff_sha256 or payload.get('text')!=content):
                raise HTTPException(409,'请求编号已用于不同评论')
            return _comment_public(item,record)
        if len(_comment_rows(connection,share_id))>=MAX_COMMENTS_PER_SHARE:
            raise HTTPException(409,'评论数量达到当前服务容量，未写入')
        stamp=now();author='用户 '+_hash(share_id+'\x00'+actor)[:12]
        payload={'id':ident,'share_id':share_id,'result_id':record['result_id'],
                 'handoff_sha256':expected_handoff_sha256,'actor':actor,'author':author,'text':content,'created_at':stamp}
        item={'id':ident,'project_id':record['project_id'],'share_id':share_id,'sealed':seal(payload)}
        connection.execute(items.insert().values(id=ident,kind=COMMENT_KIND,body=json.dumps(item,ensure_ascii=False)))
        # Add the privacy-preserving audit event atomically with the comment;
        # an idempotent retry returns above and never creates a second event.
        sentinel=connection.execute(select(items.c.id).where(items.c.id=='audit-lock',items.c.kind=='system').with_for_update()).first()
        if not sentinel:raise HTTPException(503,'审计服务不可用，评论未写入')
        previous=connection.execute(select(audit.events.c.event_hash).order_by(audit.events.c.sequence.desc()).limit(1)).first()
        previous=previous[0] if previous else '0'*64
        action='share.comment.created'
        details={'share_id':share_id,'result_id':record['result_id'],'comment_id':ident,
                 'author':author,'handoff_sha256':expected_handoff_sha256}
        body=json.dumps(details,ensure_ascii=False,sort_keys=True,default=str)
        signature=audit.digest(stamp,action,ident,body,previous)
        connection.execute(audit.events.insert().values(created=stamp,action=action,subject=ident,
                           body=body,previous_hash=previous,event_hash=signature))
        return _comment_public(item,record)
    return _locked_snapshot(request,share_id,capture)
