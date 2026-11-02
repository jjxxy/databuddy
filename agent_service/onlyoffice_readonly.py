"""Opt-in, short-lived read-only ONLYOFFICE file handoff.

All addresses and the JWT secret are operator supplied; users never provide a
fetch URL. A handoff is bound to the actor, project and exact file bytes. This
module intentionally does not implement editing, callbacks or publication.
"""
from __future__ import annotations

import ipaddress
import hashlib
import hmac
import os
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse
from .actor_context import actor_scope, current_actor
from . import workspace_artifacts

_entries = {}
_lock = threading.Lock()
_ttl = 90
_max_entries = 256


def _origin(raw):
    parsed = urlsplit(raw)
    try:
        address = ipaddress.ip_address(parsed.hostname or '')
        port = parsed.port
    except ValueError:
        raise ValueError('ONLYOFFICE 地址必须是管理员配置的内网 IP') from None
    if (parsed.scheme not in ('http', 'https') or not (address.is_private or address.is_loopback)
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/') or not port):
        raise ValueError('ONLYOFFICE 地址配置无效')
    return f'{parsed.scheme}://{parsed.netloc}'


def _config():
    secret = os.getenv('ATLAS_ONLYOFFICE_JWT_SECRET', '')
    try:
        server = _origin(os.getenv('ATLAS_ONLYOFFICE_BROWSER_ORIGIN', ''))
        bridge = _origin(os.getenv('ATLAS_ONLYOFFICE_FILE_ORIGIN', ''))
        allowed = ipaddress.ip_address(os.getenv('ATLAS_ONLYOFFICE_SOURCE_IP', ''))
    except ValueError:
        raise HTTPException(503, 'ONLYOFFICE 只读服务尚未完成安全配置') from None
    if len(secret) < 32 or not (allowed.is_private or allowed.is_loopback):
        raise HTTPException(503, 'ONLYOFFICE 只读服务尚未完成安全配置')
    return server, bridge, str(allowed), secret


def issue(pid, aid):
    import jwt
    server, bridge, allowed, secret = _config()
    path, meta = workspace_artifacts.file(pid, aid)
    kind = meta.get('download_format')
    document_types = {'docx': 'word', 'xlsx': 'cell', 'pptx': 'slide', 'pdf': 'pdf'}
    if kind not in document_types:
        raise HTTPException(400, '此文件格式暂不支持 ONLYOFFICE 显示')
    actor = current_actor()
    token = secrets.token_urlsafe(32)
    deadline = time.time() + _ttl
    with _lock:
        for key, value in list(_entries.items()):
            if value['expires'] < time.time():
                del _entries[key]
        if len(_entries) >= _max_entries:
            raise HTTPException(503, '文件查看请求过多，请稍后重试')
        _entries[token] = {'pid': pid, 'aid': aid, 'actor': actor, 'format': kind,
                           'sha256': meta['download_sha256'], 'expires': deadline, 'source': allowed}
    doc_key = hmac.new(secret.encode(), '\0'.join((pid, actor, aid, meta['download_sha256'])).encode(), hashlib.sha256).hexdigest()
    config = {'document': {'fileType': kind, 'key': doc_key,
                           'title': meta['name'], 'url': bridge + '/api/onlyoffice/file/' + token,
                           'permissions': {'edit': False, 'review': False, 'comment': False}},
              'documentType': document_types[kind], 'editorConfig': {'mode': 'view', 'lang': 'zh-CN'},
              'width': '100%', 'height': '100%'}
    config['token'] = jwt.encode(config, secret, algorithm='HS256')
    return {'server_origin': server, 'config': config, 'expires_in': _ttl}


def redeem(token, remote):
    with _lock:
        entry = _entries.get(token)
        if not entry or entry['expires'] < time.time() or remote != entry['source']:
            raise HTTPException(404, '文件链接无效或已过期')
        del _entries[token]
    with actor_scope(entry['actor']):
        path, meta = workspace_artifacts.file(entry['pid'], entry['aid'], entry['format'])
    if meta['download_sha256'] != entry['sha256']:
        raise HTTPException(409, '文件版本已变化，请重新打开')
    return path, meta


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

    @app.post('/api/projects/{pid}/artifacts/{aid}/onlyoffice-view', dependencies=[Depends(auth)])
    def open_view(pid: str, aid: str):
        return checked(issue, pid, aid)

    @app.get('/api/onlyoffice/file/{token}')
    def fetch_file(token: str, request: Request):
        if not request.client:
            raise HTTPException(404, '文件链接无效或已过期')
        path, meta = checked(redeem, token, request.client.host)
        return FileResponse(path, filename=meta['name'], media_type='application/octet-stream',
                            headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
