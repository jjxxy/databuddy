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


def create_share(project_id, result_id, allowed_subjects, actor,
                 expires_hours=168, allow_download=False):
    """Owner API only; no invitations or messages are sent by this function."""
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


def authorize(request, share_id):
    _require_oidc(request_context=True)
    subject = verified_subject(request)
    try:
        record = get(KIND, share_id)
    except KeyError:
        raise HTTPException(404, '分享不存在') from None
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
    try:
        _, metadata = result_folder(record['project_id'], record['result_id'])
        if metadata['plan_sha256'] != record['plan_sha256'] or metadata['handoff_sha256'] != record['handoff_sha256']:
            raise ValueError('Shared snapshot changed')
    except (KeyError, ValueError, OSError):
        raise HTTPException(410, '所分享的固定结果版本已不可用') from None
    return record
