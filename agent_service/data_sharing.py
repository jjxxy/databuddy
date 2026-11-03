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
                 expires_hours=168, allow_download=False):
    """Owner API only; no invitations or messages are sent by this function."""
    from . import workspace
    from .workspace_lifecycle import _project_lock, assert_project_live
    # Match restore's workspace -> lifecycle order and prevent a share from
    # appearing after project removal has completed its revocation scan.
    with workspace.lock(project_id), _project_lock(project_id):
        assert_project_live(project_id)
        return _create_share(project_id,result_id,allowed_subjects,actor,expires_hours,allow_download)


def _create_share(project_id, result_id, allowed_subjects, actor, expires_hours, allow_download):
    _require_oidc()
    actor_hash = _actor(actor)
    get('project', project_id)
    if type(expires_hours) is not int or not 1 <= expires_hours <= 720:
        raise ValueError('分享有效期必须为1至720小时')
    if type(allow_download) is not bool:
        raise ValueError('下载权限必须是明确的布尔值')
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
              'plan_sha256': metadata['plan_sha256'], 'handoff_sha256': metadata['handoff_sha256'],
              'scope': 'one_immutable_result; read_only; signed_oidc_identity_required'}
    return _public(put(KIND, record, 'share_' + uid()))


def list_shares(project_id):
    get('project', project_id)
    records = [r for r in all_items(KIND) if r.get('project_id') == project_id]
    records.sort(key=lambda r: (r['created_at'], r['id']), reverse=True)
    return [_public(r) for r in records]


def revoke(project_id, share_id, actor):
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
            with actor_scope(actor):value=materialize(record)
            # Check again before releasing either lock; no path is opened by
            # the HTTP response after the authorization boundary.
            _authorize_record(record,subject)
            return value


def read_shared(request,share_id,offset=0,limit=100):
    from .project_data import read_result
    def capture(record):
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
        def capture(record):
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
