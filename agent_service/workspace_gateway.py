"""OIDC router for separately deployed owner workspaces; never a tenant database.

Only verified issuer/subject selects a configured backend. This module deliberately
has no dependency on Nexus WorkStudio config/store: the gateway owns no business data/key.
Run with NEXUS_WORKSPACE_GATEWAY_CONFIG and uvicorn ...workspace_gateway:app.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import ipaddress
from http.cookiejar import CookieJar, DefaultCookiePolicy
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote, urlsplit

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.requests import ClientDisconnect


class ConfigurationError(ValueError):
    pass


def _object(value, fields, required=None):
    if not isinstance(value, dict) or set(value) - set(fields) or not set(fields if required is None else required) <= set(value):
        raise ConfigurationError('Invalid gateway configuration fields')
    return value


def _text(value, maximum=2048):
    if not isinstance(value, str) or not value or len(value) > maximum or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ConfigurationError('Invalid gateway configuration text')
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError('Duplicate JSON field')
        result[key] = value
    return result


def _read_json(path, maximum):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ConfigurationError('Configuration must be an absolute regular file')
    with path.open('rb') as source:
        raw = source.read(maximum + 1)
    if len(raw) > maximum:
        raise ConfigurationError('Configuration exceeds size limit')
    try:
        return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=lambda _: (_ for _ in ()).throw(ConfigurationError('Invalid JSON number')))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError('Invalid configuration JSON') from exc


def _origin(value, *, backend=False):
    value = _text(value)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError('Invalid origin') from exc
    if (parsed.scheme not in (('http', 'https') if backend else ('https',)) or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or parsed.path not in ('', '/')
            or '?' in value or '#' in value or '\\' in value or '%' in value or port == 0):
        raise ConfigurationError('An origin without credentials, path or query is required')
    host = parsed.hostname.lower()
    if not re.fullmatch(r'[a-z0-9.-]+|[a-f0-9:]+', host) or '..' in host or host.endswith('.'):
        raise ConfigurationError('Invalid origin hostname')
    if backend and parsed.scheme == 'http':
        try:
            ip = ipaddress.ip_address(host)
            if not (ip.is_private or ip.is_loopback) or ip.is_unspecified or ip.is_multicast:
                raise ConfigurationError('Plain HTTP backend must use a private address')
        except ValueError:
            if not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', host):
                raise ConfigurationError('Plain HTTP backend must use a private service name')
    authority = '[' + host + ']' if ':' in host else host
    if port is not None and port != (443 if parsed.scheme == 'https' else 80):
        authority += ':' + str(port)
    return parsed.scheme + '://' + authority


@dataclass(frozen=True)
class Limits:
    max_request_bytes: int = 26 * 1024 * 1024
    max_response_bytes: int = 128 * 1024 * 1024
    request_timeout_seconds: int = 120
    upstream_timeout_seconds: int = 120
    max_concurrent_requests: int = 64

    @classmethod
    def parse(cls, value):
        _object(value, cls.__dataclass_fields__, required=()) if value else None
        if not isinstance(value, dict):
            raise ConfigurationError('Invalid limits')
        bounds = {'max_request_bytes': (1, 128 * 1024 * 1024), 'max_response_bytes': (1, 1024 * 1024 * 1024),
                  'request_timeout_seconds': (1, 600), 'upstream_timeout_seconds': (1, 600), 'max_concurrent_requests': (1, 512)}
        for name, number in value.items():
            if name not in bounds or type(number) is not int or not bounds[name][0] <= number <= bounds[name][1]:
                raise ConfigurationError('Invalid gateway limit')
        return cls(**value)


@dataclass(frozen=True)
class GatewayConfig:
    public_origin: str
    issuer: str
    audience: str
    keys: dict
    routes: dict
    limits: Limits
    max_token_lifetime_seconds: int = 3600

    @classmethod
    def load(cls, path):
        return cls.parse(_read_json(path, 1024 * 1024))

    @classmethod
    def parse(cls, value):
        _object(value, ('schema_version', 'public_origin', 'oidc', 'workspaces', 'limits'),
                ('schema_version', 'public_origin', 'oidc', 'workspaces'))
        if type(value['schema_version']) is not int or value['schema_version'] != 1:
            raise ConfigurationError('Unsupported gateway schema')
        oidc = _object(value['oidc'], ('issuer', 'audience', 'jwks_file', 'max_token_lifetime_seconds'), ('issuer', 'audience', 'jwks_file'))
        lifetime = oidc.get('max_token_lifetime_seconds', 3600)
        if type(lifetime) is not int or not 60 <= lifetime <= 86400:
            raise ConfigurationError('Invalid access token lifetime policy')
        issuer = _text(oidc['issuer'])
        parsed = urlsplit(issuer)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or '\\' in issuer:
            raise ConfigurationError('OIDC issuer must be a trusted HTTPS issuer')
        audience = _text(oidc['audience'], 256)
        jwks = _object(_read_json(oidc['jwks_file'], 256000), ('keys',))
        if not isinstance(jwks['keys'], list) or not 1 <= len(jwks['keys']) <= 100:
            raise ConfigurationError('Invalid public JWKS')
        keys = {}
        for key in jwks['keys']:
            if not isinstance(key, dict) or any(k in key for k in ('d', 'p', 'q', 'dp', 'dq', 'qi', 'oth', 'k')):
                raise ConfigurationError('Only public verification keys are accepted')
            kid = _text(key.get('kid'), 256)
            alg = key.get('alg') or {'RSA': 'RS256', 'EC': 'ES256'}.get(key.get('kty'))
            if kid in keys or alg not in ('RS256', 'ES256') or key.get('use', 'sig') != 'sig' or key.get('key_ops', ['verify']) != ['verify']:
                raise ConfigurationError('Duplicate or unsupported verification key')
            try:
                public = jwt.PyJWK.from_dict(key, algorithm=alg)
                if alg == 'RS256' and (key.get('kty') != 'RSA' or public.key.key_size < 2048):
                    raise ValueError('Weak key')
                if alg == 'ES256' and (key.get('kty') != 'EC' or key.get('crv') != 'P-256'):
                    raise ValueError('Wrong curve')
            except Exception as exc:
                raise ConfigurationError('Invalid public verification key') from exc
            keys[kid] = (alg, public.key)
        workspaces = value['workspaces']
        if not isinstance(workspaces, list) or not 1 <= len(workspaces) <= 1000:
            raise ConfigurationError('At least one workspace is required')
        ids, backends, routes = set(), set(), {}
        for workspace in workspaces:
            _object(workspace, ('id', 'backend_url', 'subjects'))
            identity = workspace['id']
            if not isinstance(identity, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', identity) or identity in ids:
                raise ConfigurationError('Invalid or duplicate workspace id')
            backend = _origin(workspace['backend_url'], backend=True)
            if backend in backends:
                raise ConfigurationError('Workspaces must have distinct backends')
            subjects = workspace['subjects']
            if not isinstance(subjects, list) or not 1 <= len(subjects) <= 1000:
                raise ConfigurationError('Workspace requires explicit subjects')
            for subject in subjects:
                subject = _text(subject, 512)
                if subject in routes:
                    raise ConfigurationError('Subject belongs to more than one route')
                routes[subject] = (identity, backend)
            ids.add(identity)
            backends.add(backend)
        origin = _origin(value['public_origin'])
        if origin in backends:
            raise ConfigurationError('Gateway cannot route to itself')
        return cls(origin, issuer, audience, keys, routes, Limits.parse(value.get('limits', {})), lifetime)

    def authenticate(self, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 16000:
            raise HTTPException(401, 'Invalid access token')
        try:
            header = jwt.get_unverified_header(token)
            alg, key = self.keys[header['kid']]
            if header.get('alg') != alg or header.get('crit') or header.get('jku') or header.get('jwk') or header.get('x5u'):
                raise ValueError('Unsupported JWT header')
            claims = jwt.decode(token, key, algorithms=[alg], audience=self.audience, issuer=self.issuer,
                                leeway=5, options={'require': ['exp', 'iat', 'iss', 'aud', 'sub']})
            if not isinstance(claims['sub'], str) or type(claims['exp']) not in (int, float) or type(claims['iat']) not in (int, float):
                raise ValueError('Invalid claims')
            if claims['exp'] <= claims['iat'] or claims['exp'] - claims['iat'] > self.max_token_lifetime_seconds:
                raise ValueError('Access token lifetime exceeds deployment policy')
        except Exception:
            raise HTTPException(401, 'Invalid access token') from None
        route = self.routes.get(claims['sub'])
        if route is None:
            raise HTTPException(403, 'No workspace is assigned to this identity')
        return route


_SECURITY_HEADERS = {
    'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
    'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",
}


class _SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def secure_send(message):
            if message['type'] == 'http.response.start':
                owned = {k.lower().encode() for k in _SECURITY_HEADERS}
                message['headers'] = [(k, v) for k, v in message.get('headers', []) if k.lower() not in owned]
                message['headers'] += [(k.lower().encode(), v.encode()) for k, v in _SECURITY_HEADERS.items()]
            await send(message)
        await self.app(scope, receive, secure_send)


_STATIC = frozenset(('index.html', 'app.js', 'style.css', 'icon.svg', 'manifest.webmanifest', 'sw.js',
    'workspace-login.js', 'workspace-session.js',
    'secure-id.js', 'lifecycle.js', 'safe-markdown.js', 'workspace-features.js', 'workspace-shell.js', 'agent-collaboration.js',
    'local-data.html', 'local-data.js', 'local-data.css', 'work-runtime.js', 'interactive-work.js',
    'data-app.html', 'data-app.js', 'data-app.css', 'data-workbench.html', 'data-workbench.js', 'data-workbench.css',
    'business-flows.html', 'business-flows.js', 'business-flows.css', 'business-flow-view.js', 'business-flow-view.css',
    'flow-edit-handoff.js', 'workbench-dag.js', 'react-dag/web-dag.css', 'react-dag/web-dag.js',
    'chat-workflows.css', 'chat-workflows.js', 'chat-workflow-results.js', 'chat-workflow-schedule.js',
    'chat-business-run.js', 'chat-data-workflow-run.js'))
_DENIED_API = frozenset(('operator', 'agent', 'mcp', 'a2a'))
_REQUEST_HEADERS = frozenset(('accept', 'accept-language', 'content-type', 'if-match', 'if-none-match', 'range', 'if-range'))
_RESPONSE_HEADERS = frozenset(('content-type', 'content-disposition', 'content-range', 'accept-ranges', 'etag', 'last-modified', 'retry-after', 'x-nexus-runtime-revision'))


def _validate_target(request):
    raw = request.scope.get('raw_path', b'')
    if len(raw) > 8192 or b'%' in raw or b'\\' in raw or b'//' in raw or any(c < 32 or c > 126 for c in raw):
        raise HTTPException(400, 'Noncanonical path')
    path = raw.decode('ascii')
    if any(part in ('.', '..') for part in path.split('/')) or path != request.url.path:
        raise HTTPException(400, 'Noncanonical path')
    query = request.scope.get('query_string', b'')
    if len(query) > 16384 or any(c < 32 or c == 127 for c in query):
        raise HTTPException(400, 'Invalid query')
    try:
        encoded_query = query.decode('ascii')
        if re.search(r'%(?![0-9A-Fa-f]{2})', encoded_query):
            raise ValueError()
        decoded = unquote(encoded_query, errors='strict')
        if any(ord(c) < 32 or ord(c) == 127 for c in decoded):
            raise ValueError()
    except (UnicodeError, ValueError):
        raise HTTPException(400, 'Invalid query') from None
    return path, query


def _matches_login_generation(request,login):
    import hmac
    values=request.headers.getlist('x-nexus-session')
    expected=login.generation(request)
    return bool(len(values)==1 and re.fullmatch(r'[A-Za-z0-9_-]{43}',values[0])
                and expected is not None and hmac.compare_digest(values[0],expected))


class _ProxyResponse(StreamingResponse):
    def __init__(self, upstream, semaphore, limits):
        self.upstream, self.semaphore, self.limits = upstream, semaphore, limits
        headers = {k: v for k, v in upstream.headers.items() if k in _RESPONSE_HEADERS}
        headers.update(_SECURITY_HEADERS)
        length = upstream.headers.get('content-length')
        if length is not None:
            headers['Content-Length'] = length
        super().__init__(self.chunks(), status_code=upstream.status_code, headers=headers)

    async def chunks(self):
        count = 0
        async with asyncio.timeout(self.limits.upstream_timeout_seconds):
            async for chunk in self.upstream.aiter_raw():
                count += len(chunk)
                if count > self.limits.max_response_bytes:
                    # Do not turn a partial CSV/Office file into a successful EOF.
                    raise RuntimeError('Upstream response exceeded the configured limit')
                yield chunk

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await asyncio.shield(self.upstream.aclose())
            finally:
                self.semaphore.release()


class _NoCookies(DefaultCookiePolicy):
    # The shared transport must never persist another workspace's session state.
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


def create_app(config=None, *, transport=None):
    @asynccontextmanager
    async def lifespan(application):
        active = config or GatewayConfig.load(os.environ.get('NEXUS_WORKSPACE_GATEWAY_CONFIG', ''))
        application.state.config = active
        application.state.semaphore = asyncio.Semaphore(active.limits.max_concurrent_requests)
        async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                cookies=CookieJar(policy=_NoCookies()),
                timeout=httpx.Timeout(active.limits.upstream_timeout_seconds, connect=10),
                limits=httpx.Limits(max_connections=active.limits.max_concurrent_requests, max_keepalive_connections=20)) as client:
            application.state.client = client
            login_config=os.getenv('NEXUS_WORKSPACE_OIDC_LOGIN_CONFIG','')
            if login_config:
                from .gateway_oidc_login import Login
                application.state.login=Login(active,_read_json(login_config,16384),client)
            else:application.state.login=None
            yield

    application = FastAPI(title='Nexus WorkStudio isolated-workspace gateway', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    application.add_middleware(_SecurityHeaders)

    @application.api_route('/{path:path}', methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
    async def route(request: Request, path: str):
        active = request.app.state.config
        target, query = _validate_target(request)
        if request.headers.get('host', '').lower() != urlsplit(active.public_origin).netloc.lower():
            raise HTTPException(400, 'Unrecognized gateway host')
        if target == '/health' and request.method in ('GET', 'HEAD'):
            return JSONResponse({'ok': True, 'scope': 'isolated_workspace_gateway'}, headers={'Cache-Control': 'no-store'})
        login=request.app.state.login
        if target == '/auth/status' and request.method=='GET':
            generation=login.generation(request) if login else None
            return JSONResponse({'enabled':bool(login),'authenticated':bool(generation),'generation':generation},headers={'Cache-Control':'no-store'})
        if target in ('/auth/login','/auth/callback','/auth/logout'):
            if not login:raise HTTPException(404,'Enterprise login is not configured')
            if target=='/auth/login' and request.method=='GET':return login.start()
            if target=='/auth/callback' and request.method=='GET':return await login.callback_response(request)
            if target=='/auth/logout' and request.method=='POST':
                if request.headers.get('origin')!=active.public_origin or request.headers.get('sec-fetch-site')=='cross-site':
                    raise HTTPException(403,'Cross-origin access is not supported')
                if login.has_session(request):
                    if not _matches_login_generation(request,login):
                        raise HTTPException(401,'登录账号或会话已变化，请重新进入')
                return login.logout(request)
            raise HTTPException(405,'Method not allowed')
        if target == '/' or target.removeprefix('/') in _STATIC:
            if request.method not in ('GET', 'HEAD'):
                raise HTTPException(405, 'Method not allowed')
            filename = 'index.html' if target == '/' else target[1:]
            file = Path(__file__).parent / 'static' / filename
            if not file.is_file():
                raise HTTPException(404, 'Static asset not found')
            return FileResponse(file, headers=_SECURITY_HEADERS)
        if not target.startswith('/api/') or target.split('/')[2] in _DENIED_API:
            raise HTTPException(404, 'Not exposed by this gateway')
        if request.method == 'OPTIONS':
            raise HTTPException(405, 'Cross-origin access is not supported')
        if request.headers.get('origin') not in (None, active.public_origin) or request.headers.get('sec-fetch-site') == 'cross-site':
            raise HTTPException(403, 'Cross-origin access is not supported')
        if login:
            # In SSO mode all browser/API requests are bound to a live gateway
            # session. A stale bearer token from another tab cannot survive
            # account switch or logout and silently retain old authority.
            if not login.has_session(request):
                raise HTTPException(401,'企业登录已过期，请重新登录')
            if not _matches_login_generation(request,login):
                raise HTTPException(401,'登录账号或会话已变化，请重新进入')
        authorizations = request.headers.getlist('authorization')
        if authorizations:
            if len(authorizations)!=1 or not authorizations[0].startswith('Bearer '):
                raise HTTPException(401,'A bearer access token is required')
            token=authorizations[0][7:]
            if login and login.has_session(request):
                # A stale manual credential in an older tab must not silently
                # override a different identity signed in through the gateway.
                cookie_token=await login.token(request)
                try:
                    bearer_claims=jwt.decode(token,options={'verify_signature':False})
                    cookie_claims=jwt.decode(cookie_token,options={'verify_signature':False})
                    active.authenticate(token)
                    if bearer_claims.get('sub')!=cookie_claims.get('sub'):raise ValueError()
                except Exception:raise HTTPException(401,'登录账号已变化，请刷新页面') from None
        elif login:
            if request.method not in ('GET','HEAD') and request.headers.get('origin')!=active.public_origin:
                raise HTTPException(403,'A same-origin request is required')
            token=await login.token(request)
        else:
            raise HTTPException(401, 'A bearer access token is required')
        _, backend = active.authenticate(token)
        if request.headers.get('content-encoding', 'identity') != 'identity':
            raise HTTPException(415, 'Encoded request bodies are not supported')
        lengths = request.headers.getlist('content-length')
        if len(lengths) > 1 or lengths and (not lengths[0].isdigit() or int(lengths[0]) > active.limits.max_request_bytes):
            raise HTTPException(413, 'Request body exceeds the configured limit')
        semaphore = request.app.state.semaphore
        if semaphore.locked():
            raise HTTPException(503, 'Gateway concurrency limit reached', headers={'Retry-After': '1'})
        await semaphore.acquire()
        handed_off = False
        upstream = None
        try:
            # Validate the complete bounded upload before the backend sees any bytes.
            with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode='w+b') as spool:
                size = 0
                async with asyncio.timeout(active.limits.request_timeout_seconds):
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > active.limits.max_request_bytes:
                            raise HTTPException(413, 'Request body exceeds the configured limit')
                        spool.write(chunk)
                if lengths and int(lengths[0]) != size:
                    raise HTTPException(400, 'Request body length mismatch')
                if login and not login.still_current(request,request.headers.get('x-nexus-session'),token):
                    raise HTTPException(401,'登录账号或会话已变化，请重新进入')
                spool.seek(0)
                async def body():
                    while chunk := spool.read(65536):
                        yield chunk
                headers = {k: v for k, v in request.headers.items() if k in _REQUEST_HEADERS}
                headers.update({'Authorization': 'Bearer ' + token, 'Accept-Encoding': 'identity', 'Content-Length': str(size)})
                url = httpx.URL(backend + target).copy_with(query=query)
                outbound = request.app.state.client.build_request(request.method, url, headers=headers, content=body())
                outbound.headers.pop('cookie', None)
                async with asyncio.timeout(active.limits.upstream_timeout_seconds):
                    upstream = await request.app.state.client.send(outbound, stream=True)
            if 300 <= upstream.status_code < 400 and upstream.status_code != 304:
                raise HTTPException(502, 'Backend redirects are not supported')
            if upstream.headers.get('content-encoding', 'identity') != 'identity':
                raise HTTPException(502, 'Encoded backend response is not supported')
            length = upstream.headers.get('content-length')
            if length is not None and (not length.isdigit() or int(length) > active.limits.max_response_bytes):
                raise HTTPException(502, 'Backend response exceeds the configured limit')
            response = _ProxyResponse(upstream, semaphore, active.limits)
            handed_off = True
            return response
        except (TimeoutError, httpx.TimeoutException):
            raise HTTPException(504, 'Backend or upload timed out; operation outcome may be unknown') from None
        except httpx.HTTPError:
            raise HTTPException(502, 'Backend connection failed; operation outcome may be unknown') from None
        except ClientDisconnect:
            raise HTTPException(499, 'Client disconnected') from None
        finally:
            if not handed_off:
                try:
                    if upstream is not None:
                        await asyncio.shield(upstream.aclose())
                finally:
                    semaphore.release()

    return application


app = create_app()
