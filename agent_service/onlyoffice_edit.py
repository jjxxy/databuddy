"""Opt-in ONLYOFFICE editing that never overwrites an existing artifact.

Editing is enabled only when the operator sets ATLAS_ONLYOFFICE_EDIT=1 and all
document-server addresses. A save callback (JWT verified, source-IP bound,
ticket bound) downloads the document server's saved bytes and registers them as
a NEW workspace artifact. The original bytes stay immutable; the new version is
marked as a human edit that still requires business and financial review.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import delete, select

from .actor_context import actor_scope, current_actor
from .config import DATA
from .store import get, items, now, put, transaction
from . import office, workspace, workspace_artifacts

_ticket_ttl = 8 * 3600
_max_file_tickets = 256
_edit_ticket_kind = 'onlyoffice_edit_ticket'
_file_ticket_kind = 'onlyoffice_edit_file'
_max_bytes = 100 * 1024 * 1024
_editable = {'docx': 'word', 'xlsx': 'cell', 'pptx': 'slide'}


def _origin(raw, label):
    parsed = urlsplit(raw)
    try:
        address = ipaddress.ip_address(parsed.hostname or '')
        port = parsed.port
    except ValueError:
        raise HTTPException(503, 'ONLYOFFICE 编辑服务尚未完成安全配置') from None
    if (parsed.scheme not in ('http', 'https') or not (address.is_private or address.is_loopback)
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/') or not port):
        raise HTTPException(503, 'ONLYOFFICE 编辑服务尚未完成安全配置')
    return f'{parsed.scheme}://{parsed.netloc}'


def _config():
    secret = os.getenv('ATLAS_ONLYOFFICE_JWT_SECRET', '')
    server = _origin(os.getenv('ATLAS_ONLYOFFICE_BROWSER_ORIGIN', ''), 'browser')
    bridge = _origin(os.getenv('ATLAS_ONLYOFFICE_FILE_ORIGIN', ''), 'bridge')
    fetch = _origin(os.getenv('ATLAS_ONLYOFFICE_FETCH_ORIGIN', ''), 'fetch')
    try:
        allowed = ipaddress.ip_address(os.getenv('ATLAS_ONLYOFFICE_SOURCE_IP', ''))
    except ValueError:
        raise HTTPException(503, 'ONLYOFFICE 编辑服务尚未完成安全配置') from None
    if len(secret) < 32 or not (allowed.is_private or allowed.is_loopback):
        raise HTTPException(503, 'ONLYOFFICE 编辑服务尚未完成安全配置')
    return {'server': server, 'bridge': bridge, 'fetch': fetch,
            'allowed': str(allowed), 'secret': secret}


def enabled():
    if os.getenv('ATLAS_ONLYOFFICE_EDIT', '') != '1':
        return False
    try:
        _config()
    except HTTPException:
        return False
    return True


def _digest(prefix, token):
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        raise HTTPException(404, '编辑链接无效或已过期')
    return prefix + hashlib.sha256(token.encode('ascii')).hexdigest()[:59]


def _binding(server, bridge, fetch, allowed, secret):
    value = json.dumps([server, bridge, fetch, allowed], ensure_ascii=True, separators=(',', ':')).encode()
    return hmac.new(secret.encode(), value, hashlib.sha256).hexdigest()


def _store(kind, ident, entry):
    with transaction() as connection:
        rows = connection.execute(select(items.c.id, items.c.body).where(items.c.kind == kind)).all()
        expired = []
        for row in rows:
            try:
                expires = json.loads(row.body)['expires']
                if type(expires) not in (int, float):
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                raise HTTPException(503, '编辑服务暂不可用') from None
            if expires <= time.time():
                expired.append(row.id)
        if expired:
            connection.execute(delete(items).where(items.c.kind == kind, items.c.id.in_(expired)))
        if len(rows) - len(expired) >= _max_file_tickets:
            raise HTTPException(503, '编辑请求过多，请稍后重试')
        existing = connection.execute(select(items.c.id).where(items.c.id == ident)).first()
        if existing:
            connection.execute(items.delete().where(items.c.id == ident, items.c.kind == kind))
        connection.execute(items.insert().values(id=ident, kind=kind,
                                                 body=json.dumps(entry, ensure_ascii=False)))


def issue(pid, aid):
    """Issue an edit session for one exact, verified artifact version."""
    cfg = _config()
    path, meta = workspace_artifacts.file(pid, aid)
    kind = meta.get('download_format')
    if kind not in _editable:
        raise HTTPException(400, '此文件格式不支持在线编辑')
    actor = current_actor()
    file_token = secrets.token_urlsafe(32)
    edit_token = secrets.token_urlsafe(32)
    deadline = time.time() + _ticket_ttl
    binding = _binding(cfg['server'], cfg['bridge'], cfg['fetch'], cfg['allowed'], cfg['secret'])
    common = {'pid': pid, 'aid': aid, 'actor': actor, 'format': kind,
              'sha256': meta['download_sha256'], 'expires': deadline, 'source': cfg['allowed'],
              'config_binding': binding, 'name': meta['name']}
    _store(_file_ticket_kind, _digest('oef_', file_token), common)
    doc_key = hmac.new(cfg['secret'].encode(), '\0'.join(
        ('edit', pid, actor, aid, meta['download_sha256'])).encode(), hashlib.sha256).hexdigest()
    _store(_edit_ticket_kind, _digest('oec_', edit_token),
           dict(common, key=doc_key, file_token_digest=_digest('oef_', file_token)))
    config = {
        'document': {
            'fileType': kind, 'key': doc_key, 'title': meta['name'],
            'url': cfg['bridge'] + '/api/onlyoffice/edit-file/' + file_token,
            'permissions': {'edit': True, 'download': True, 'print': True,
                            'review': False, 'comment': False},
        },
        'documentType': _editable[kind],
        'editorConfig': {
            'mode': 'edit', 'lang': 'zh-CN',
            'callbackUrl': cfg['bridge'] + '/api/onlyoffice/callback/' + edit_token,
            'user': {'id': hashlib.sha256(str(actor).encode()).hexdigest()[:24], 'name': '编辑者'},
        },
        'width': '100%', 'height': '100%',
    }
    config['token'] = _jwt_encode(config, cfg['secret'])
    return {'server_origin': cfg['server'], 'config': config, 'expires_in': _ticket_ttl,
            'save_behavior': '保存会生成新的成果版本；原成果不变，编辑内容仍需复核'}


def _jwt_encode(payload, secret):
    import jwt
    return jwt.encode(payload, secret, algorithm='HS256')


def _jwt_decode(token, secret):
    import jwt
    return jwt.decode(token, secret, algorithms=['HS256'])


def _read_entry(kind, ident):
    with transaction() as connection:
        row = connection.execute(select(items.c.body).where(
            items.c.id == ident, items.c.kind == kind)).first()
    if not row:
        return None
    try:
        entry = json.loads(row.body)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def fetch_file(token, remote):
    """Multi-use, source-bound, expiry-bound fetch for one edit session."""
    ident = _digest('oef_', token)
    try:
        cfg = _config()
    except HTTPException:
        raise HTTPException(404, '编辑链接无效或已过期') from None
    if remote != cfg['allowed']:
        raise HTTPException(404, '编辑链接无效或已过期')
    entry = _read_entry(_file_ticket_kind, ident)
    if (not entry or type(entry.get('expires')) not in (int, float)
            or entry['expires'] <= time.time()
            or not hmac.compare_digest(str(entry.get('config_binding')), _binding(
                cfg['server'], cfg['bridge'], cfg['fetch'], cfg['allowed'], cfg['secret']))):
        raise HTTPException(404, '编辑链接无效或已过期')
    with actor_scope(entry['actor']):
        path, meta = workspace_artifacts.file(entry['pid'], entry['aid'], entry['format'])
    if meta['download_sha256'] != entry['sha256']:
        raise HTTPException(409, '文件版本已变化，请重新打开')
    return path, meta


def _download(url, cfg):
    """Fetch the document server's saved bytes through the configured bridge.

    Never trust the host reported by the callback; only the path and query are
    reused so the document server cannot steer this process to another host.
    """
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https'):
        raise ValueError('保存地址无效')
    target = cfg['fetch'] + urlunsplit(('', '', parts.path, parts.query, ''))
    with httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client:
        with client.stream('GET', target) as response:
            if response.status_code >= 300:
                raise ValueError(f'读取保存文件失败：HTTP {response.status_code}')
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > _max_bytes:
                    raise ValueError('保存文件超过大小限制')
    return bytes(content)


def save(pid, source_aid, name, fmt, raw):
    """Register edited bytes as a new, clearly-marked artifact version."""
    digest = hashlib.sha256(raw).hexdigest()
    office_id = 'of_' + hashlib.sha256(json.dumps(
        [pid, 'onlyoffice-edit', source_aid, digest], ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()[:40]
    folder = (DATA / 'deliverables' / office_id).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    stem = (Path(name).stem or '编辑成果')[:70]
    target = folder / f'{stem}（编辑）.{fmt}'
    # Keep the real suffix: openpyxl/python-docx select a parser by extension.
    temporary = folder / ('tmp-' + target.name)
    temporary.write_bytes(raw)
    try:
        office._verify_structure(temporary, fmt)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    record = {'id': office_id, 'project_id': pid, 'relative_path': str(target.relative_to(DATA)),
              'sha256': digest, 'bytes': len(raw), 'format': fmt, 'name': target.name,
              'state': 'committed', 'created_at': now(), 'source_spec': None, 'source_sha256': None,
              'source_artifact_id': source_aid, 'edit_source': 'onlyoffice',
              'precision_policy': 'text_exact', 'visual_validation': False,
              'formula_recalculation': False,
              'notice': '人工在线编辑版本；原成果未改动，内容和财务口径仍需复核'}
    put('office_artifact', record, office_id)
    public = workspace_artifacts._register(
        pid, 'office', office_id, target.name, digest, format=fmt,
        private={'office_id': office_id}, status='saved',
        semantic_status='not_confirmed', metric_status='not_confirmed')
    return {'artifact_id': office_id, 'workspace_artifact_id': (public or {}).get('id'),
            'sha256': digest, 'name': target.name}


def callback(token, payload, remote):
    """Handle a document-server save callback. Never trusts unsigned fields."""
    ident = _digest('oec_', token)
    try:
        cfg = _config()
    except HTTPException:
        return JSONResponse({'error': 1})
    if remote != cfg['allowed']:
        return JSONResponse({'error': 1})
    entry = _read_entry(_edit_ticket_kind, ident)
    if (not entry or type(entry.get('expires')) not in (int, float)
            or entry['expires'] <= time.time()
            or not hmac.compare_digest(str(entry.get('config_binding')), _binding(
                cfg['server'], cfg['bridge'], cfg['fetch'], cfg['allowed'], cfg['secret']))):
        return JSONResponse({'error': 1})
    signed = payload.get('token') if isinstance(payload, dict) else None
    if not isinstance(signed, str):
        return JSONResponse({'error': 1})
    try:
        data = _jwt_decode(signed, cfg['secret'])
    except Exception:
        return JSONResponse({'error': 1})
    if not isinstance(data, dict) or data.get('key') != entry.get('key'):
        return JSONResponse({'error': 1})
    status = data.get('status')
    if status not in (2, 6):
        # 1 opened, 3 save error, 4 closed without changes, 7 force-save error.
        return JSONResponse({'error': 0 if status in (1, 3, 4, 7) else 1})
    url = data.get('url')
    if not isinstance(url, str):
        return JSONResponse({'error': 1})
    try:
        raw = _download(url, cfg)
        with actor_scope(entry['actor']):
            with workspace.lock(entry['pid']):
                result = save(entry['pid'], entry['aid'], entry['name'], entry['format'], raw)
    except (ValueError, OSError, KeyError):
        return JSONResponse({'error': 1})
    return JSONResponse({'error': 0, 'saved_artifact': result})


def install(app, auth):
    def checked(function, *args):
        try:
            return function(*args)
        except workspace_artifacts.ArtifactGone:
            raise HTTPException(410, '成果已归档') from None
        except KeyError:
            raise HTTPException(404, '成果不存在或不属于当前项目') from None
        except (workspace_artifacts.ArtifactError, ValueError, OSError):
            raise HTTPException(409, '成果文件不可用或无法核验') from None

    @app.post('/api/projects/{pid}/artifacts/{aid}/onlyoffice-edit', dependencies=[Depends(auth)])
    def open_edit(pid: str, aid: str):
        if not enabled():
            raise HTTPException(503, '在线编辑尚未启用')
        return checked(issue, pid, aid)

    @app.get('/api/onlyoffice/edit-file/{token}')
    def edit_file(token: str, request: Request):
        if not request.client:
            raise HTTPException(404, '编辑链接无效或已过期')
        path, meta = checked(fetch_file, token, request.client.host)
        return FileResponse(path, filename=meta['name'], media_type='application/octet-stream',
                            headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

    @app.post('/api/onlyoffice/callback/{token}')
    async def edit_callback(token: str, request: Request):
        if not request.client:
            return JSONResponse({'error': 1})
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({'error': 1})
        return callback(token, payload, request.client.host)

    app.state.onlyoffice_edit = True
