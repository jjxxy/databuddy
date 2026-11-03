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
import json
import re
import time
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import delete, select
from .store import items, transaction
from .actor_context import actor_scope, current_actor
from . import workspace_artifacts

_ttl = 90
_max_entries = 256
_ticket_kind = 'onlyoffice_ticket'
_control_id = 'onlyoffice_ticket_control_v1'


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
    # Store only an irreversible digest of the bearer token. A dedicated DB
    # row serializes issuance across API processes and enforces the global cap.
    ticket_id = _ticket_id(token)
    binding = _config_binding(server, bridge, allowed, secret)
    entry = {'pid': pid, 'aid': aid, 'actor': actor, 'format': kind,
             'sha256': meta['download_sha256'], 'expires': deadline,
             'source': allowed, 'config_binding': binding}
    with transaction() as connection:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        insert = pg_insert if connection.dialect.name == 'postgresql' else sqlite_insert
        connection.execute(insert(items).values(id=_control_id,kind='onlyoffice_control',body='{}')
                           .on_conflict_do_nothing(index_elements=['id']))
        control=connection.execute(select(items.c.id).where(items.c.id==_control_id,
            items.c.kind=='onlyoffice_control').with_for_update()).first()
        if not control:raise HTTPException(503, '文件查看服务暂不可用')
        rows=connection.execute(select(items.c.id,items.c.body).where(items.c.kind==_ticket_kind)).all()
        expired=[]
        for row in rows:
            try:
                saved=json.loads(row.body);expires=saved['expires']
                if type(expires) not in (int,float) or not __import__('math').isfinite(expires):raise ValueError()
            except (ValueError,TypeError,KeyError):
                raise HTTPException(503, '文件查看服务暂不可用') from None
            if expires<=time.time():expired.append(row.id)
        if expired:connection.execute(delete(items).where(items.c.kind==_ticket_kind,items.c.id.in_(expired)))
        if len(rows)-len(expired)>=_max_entries:raise HTTPException(503, '文件查看请求过多，请稍后重试')
        connection.execute(items.insert().values(id=ticket_id,kind=_ticket_kind,
                           body=json.dumps(entry,ensure_ascii=False)))
    doc_key = hmac.new(secret.encode(), '\0'.join((pid, actor, aid, meta['download_sha256'])).encode(), hashlib.sha256).hexdigest()
    config = {'document': {'fileType': kind, 'key': doc_key,
                           'title': meta['name'], 'url': bridge + '/api/onlyoffice/file/' + token,
                           'permissions': {'edit': False, 'review': False, 'comment': False}},
              'documentType': document_types[kind],
              'editorConfig': {'mode': 'view', 'lang': 'zh-CN',
                               'user': {'id': hashlib.sha256(str(actor).encode()).hexdigest()[:24],
                                        'name': '查看者'},
                               'coEditing': {'mode': 'strict', 'change': False}},
              'width': '100%', 'height': '100%'}
    config['token'] = jwt.encode(config, secret, algorithm='HS256')
    return {'server_origin': server, 'config': config, 'expires_in': _ttl}


def _ticket_id(token):
    if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):
        raise HTTPException(404, '文件链接无效或已过期')
    return 'oft_'+hashlib.sha256(token.encode('ascii')).hexdigest()[:60]


def _config_binding(server,bridge,allowed,secret):
    # Rotating the secret or changing trusted endpoints revokes issued tickets.
    value=json.dumps([server,bridge,allowed],ensure_ascii=True,separators=(',',':')).encode()
    return hmac.new(secret.encode(),value,hashlib.sha256).hexdigest()


def _validated(token, remote, consume):
    identity=_ticket_id(token)
    try:server,bridge,allowed,secret=_config()
    except HTTPException:raise HTTPException(404, '文件链接无效或已过期') from None
    if remote!=allowed:raise HTTPException(404, '文件链接无效或已过期')
    with transaction() as connection:
        row=connection.execute(select(items.c.body).where(items.c.id==identity,
            items.c.kind==_ticket_kind).with_for_update()).first()
        if not row:raise HTTPException(404, '文件链接无效或已过期')
        try:
            entry=json.loads(row.body)
            if (not isinstance(entry,dict) or set(entry)!={'pid','aid','actor','format','sha256','expires','source','config_binding'}
                    or type(entry['expires']) not in (int,float) or not __import__('math').isfinite(entry['expires'])
                    or entry['expires']<=time.time() or entry['source']!=remote
                    or not isinstance(entry['config_binding'],str)
                    or not hmac.compare_digest(entry['config_binding'],_config_binding(server,bridge,allowed,secret))):
                raise ValueError()
        except (ValueError,TypeError,KeyError):
            raise HTTPException(404, '文件链接无效或已过期') from None
        if consume:
            # Same transaction/row lock makes redemption one-time across replicas.
            connection.execute(delete(items).where(items.c.id==identity,items.c.kind==_ticket_kind))
    return entry


def _resolve(entry):
    with actor_scope(entry['actor']):
        path, meta = workspace_artifacts.file(entry['pid'], entry['aid'], entry['format'])
    if meta['download_sha256'] != entry['sha256']:
        raise HTTPException(409, '文件版本已变化，请重新打开')
    return path, meta


def redeem(token, remote):
    return _resolve(_validated(token, remote, True))


def peek(token, remote):
    """Validate a ticket without consuming it (used for HEAD capability probes)."""
    return _resolve(_validated(token, remote, False))


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

    @app.api_route('/api/onlyoffice/file/{token}', methods=['GET', 'HEAD'])
    def fetch_file(token: str, request: Request):
        if not request.client:
            raise HTTPException(404, '文件链接无效或已过期')
        # The document server probes PDF sources with a byte-range GET (and
        # HEAD) before downloading the whole file. Those probes must not
        # consume the one-time ticket, or the following full GET fails.
        probe = request.method == 'HEAD' or bool(request.headers.get('range'))
        resolve = peek if probe else redeem
        path, meta = checked(resolve, token, request.client.host)
        return FileResponse(path, filename=meta['name'], media_type='application/octet-stream',
                            headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
