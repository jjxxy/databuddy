"""Gateway protocol/security tests use generated fixture keys, not real credentials."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from agent_service.workspace_gateway import ConfigurationError, GatewayConfig, create_app


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.parts = chunks
        self.closed = False

    async def __aiter__(self):
        for part in self.parts:
            await asyncio.sleep(0)
            yield part

    async def aclose(self):
        self.closed = True


def response(content=b'ok', status=200, headers=None):
    return httpx.Response(status, headers=headers or {}, stream=Chunks(content))


@pytest.fixture
def settings(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid='fixture-rsa', alg='RS256', use='sig')
    jwks = tmp_path / 'public-jwks.json'
    jwks.write_text(json.dumps({'keys': [jwk]}))
    config = {
        'schema_version': 1, 'public_origin': 'https://atlas.example',
        'oidc': {'issuer': 'https://identity.example/realm', 'audience': 'atlas', 'jwks_file': str(jwks)},
        'workspaces': [
            {'id': 'alice', 'backend_url': 'http://atlas-alice:8000', 'subjects': ['alice-sub']},
            {'id': 'bob', 'backend_url': 'http://atlas-bob:8000', 'subjects': ['bob-sub']},
        ],
    }
    return config, key, jwks, jwk


def access_token(settings, subject='alice-sub', **claims):
    now = int(time.time())
    payload = {'iss': settings[0]['oidc']['issuer'], 'aud': 'atlas', 'sub': subject, 'iat': now, 'exp': now + 300}
    payload.update(claims)
    return jwt.encode(payload, settings[1], algorithm='RS256', headers={'kid': 'fixture-rsa'})


def auth(settings, subject='alice-sub', **claims):
    return {'Authorization': 'Bearer ' + access_token(settings, subject, **claims)}


def client_for(settings, handler=None, **limits):
    config = deepcopy(settings[0])
    if limits:
        config['limits'] = limits
    app = create_app(GatewayConfig.parse(config), transport=httpx.MockTransport(handler or (lambda request: response())))
    return TestClient(app, base_url='https://atlas.example')


def test_independent_workspaces_preserve_full_bytes_and_verified_bearer(settings):
    values = {'atlas-alice': {}, 'atlas-bob': {}}
    tokens = {name: access_token(settings, name + '-sub') for name in ('alice', 'bob')}
    seen = []

    async def backend(request):
        host = request.url.host
        claims = jwt.decode(request.headers['authorization'][7:], settings[1].public_key(), algorithms=['RS256'], audience='atlas', issuer=settings[0]['oidc']['issuer'])
        assert host == 'atlas-' + claims['sub'].removesuffix('-sub')
        data = await request.aread()
        seen.append((host, request.method, data))
        if request.method == 'POST':
            values[host][request.url.path] = data
            return response(data, 201, {'content-type': 'application/octet-stream'})
        return response(values[host].get(request.url.path, b'missing'), headers={'content-disposition': 'attachment; filename="full.csv"'})

    with client_for(settings, backend) as client:
        def work(name):
            exact = (name + ',300.000000000002\n').encode() * 40
            headers = {'Authorization': 'Bearer ' + tokens[name]}
            assert client.post('/api/projects/same-id/file', content=exact, headers=headers).content == exact
            result = client.get('/api/projects/same-id/file', headers=headers)
            assert result.content == exact
            assert result.headers['cache-control'] == 'no-store'
            assert result.headers['content-disposition'].endswith('"full.csv"')
            return exact
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(work, ['alice', 'bob']))
        assert results[0] != results[1]
    assert len(seen) == 4


def test_public_shell_explicit_allowlist_and_no_business_auth_bypass(settings):
    called = []
    with client_for(settings, lambda r: called.append(r) or response()) as client:
        assert client.get('/').status_code == 200
        assert 'atlas_token' in client.get('/app.js').text
        assert client.get('/health').json()['scope'] == 'isolated_workspace_gateway'
        for path in ('/api/projects', '/api/browser/register'):
            assert client.get(path).status_code == 401
        for path in ('/pyproject.toml', '/static/../config.py', '/docs', '/openapi.json'):
            assert client.get(path).status_code in (400, 404)
        assert client.post('/').status_code == 405
        assert client.get('/health', headers={'Host': 'attacker.example'}).status_code == 400
    assert not called


def test_workbench_and_data_application_assets_are_served_by_isolated_gateway(settings):
    """The actual HTML entry points must not reference assets blocked by the gateway."""
    from html.parser import HTMLParser
    from urllib.parse import urlsplit

    class Assets(HTMLParser):
        def __init__(self):super().__init__();self.scripts=[];self.files=[]
        def handle_starttag(self,tag,attrs):
            values=dict(attrs)
            if tag=='script' and values.get('src'):
                self.scripts.append(urlsplit(values['src']).path);self.files.append(urlsplit(values['src']).path)
            if tag=='link' and values.get('href'):
                self.files.append(urlsplit(values['href']).path)

    with client_for(settings) as client:
        for page in ('/','/data-app.html'):
            markup=client.get(page);assert markup.status_code==200
            assets=Assets();assets.feed(markup.text)
            assert assets.scripts[0]=='/secure-id.js'
            for asset in assets.files:
                response=client.get(asset)
                assert response.status_code==200,(page,asset,response.status_code)
                assert response.headers['cache-control']=='no-store'


@pytest.mark.parametrize('changes', [
    {'iss': 'https://evil.example'}, {'aud': 'other'}, {'aud': ['other', 'not-atlas']},
    {'exp': 1}, {'sub': 123}, {'iat': 'future-fixture'},
    {'exp': 'overlong-fixture'}, {'exp': '9999999999'}, {'iat': True},
])
def test_invalid_signed_claims_never_reach_backend(settings, changes):
    # Compute relative claims when the test runs. A long full suite can exceed
    # the offset from module collection and accidentally make a future iat valid.
    changes = dict(changes)
    if changes.get('iat') == 'future-fixture':changes['iat'] = int(time.time()) + 600
    if changes.get('exp') == 'overlong-fixture':changes['exp'] = int(time.time()) + 7200
    called = []
    with client_for(settings, lambda r: called.append(r) or response()) as client:
        assert client.get('/api/projects', headers=auth(settings, **changes)).status_code == 401
    assert not called


def test_unknown_subject_and_algorithm_confusion(settings):
    config = GatewayConfig.parse(settings[0])
    with pytest.raises(HTTPException) as denied:
        config.authenticate(access_token(settings, 'unassigned'))
    assert denied.value.status_code == 403
    payload = {'iss': settings[0]['oidc']['issuer'], 'aud': 'atlas', 'sub': 'alice-sub', 'iat': int(time.time()), 'exp': int(time.time()) + 120}
    forged = jwt.encode(payload, 'test-only-HMAC-not-a-service-secret', algorithm='HS256', headers={'kid': 'fixture-rsa'})
    with pytest.raises(HTTPException) as denied:
        config.authenticate(forged)
    assert denied.value.status_code == 401
    with client_for(settings) as client:
        assert client.get('/api/projects', headers=[('Authorization', 'Bearer ' + access_token(settings)), ('Authorization', 'Bearer another')]).status_code == 401
        assert client.get('/api/projects', headers={'X-Forwarded-Access-Token': access_token(settings)}).status_code == 401


def test_only_explicit_headers_forwarded_and_response_credentials_removed(settings):
    captured = []
    def backend(request):
        captured.append(request)
        return response(headers={'Set-Cookie': 'foreign=secret', 'Location': 'https://evil.example', 'X-Internal-Secret': 'secret', 'Content-Type': 'text/event-stream', 'Cache-Control': 'public'})
    with client_for(settings, backend) as client:
        headers = auth(settings) | {'Cookie': 'private=secret', 'X-Atlas-Operator-Token': 'operator-secret',
            'X-Forwarded-Access-Token': 'different-token', 'X-Forwarded-Host': 'evil.example',
            'X-Workspace': 'bob', 'Connection': 'X-Hop-Secret', 'X-Hop-Secret': 'must-not-forward', 'Accept': 'text/event-stream', 'If-Match': 'revision-2'}
        result = client.get('/api/projects?workspace=bob', headers=headers)
        assert result.status_code == 200
        assert result.headers['content-type'] == 'text/event-stream'
        assert not any(k in result.headers for k in ('set-cookie', 'location', 'x-internal-secret'))
        assert result.headers['cache-control'] == 'no-store'
    request = captured[0]
    assert request.url.host == 'atlas-alice'
    assert request.headers['authorization'] == headers['Authorization']
    assert request.headers['if-match'] == 'revision-2'
    for name in ('cookie', 'x-atlas-operator-token', 'x-forwarded-access-token', 'x-forwarded-host', 'x-workspace', 'x-hop-secret'):
        assert name not in request.headers
    assert request.headers.get('connection') != 'X-Hop-Secret'


@pytest.mark.parametrize('path', ['/a2a', '/mcp', '/api/operator/agent-clients', '/api/agent/v1/tasks', '/api/mcp', '/api/a2a'])
def test_service_and_operator_paths_not_routed(settings, path):
    called = []
    with client_for(settings, lambda r: called.append(r) or response()) as client:
        assert client.post(path, headers=auth(settings)).status_code == 404
    assert not called


@pytest.mark.parametrize('path', ['/api/%6fperator/clients', '/api/projects%2fsecret', '/api/projects%5csecret', '/api//projects', '/api/projects?query=%0d%0aevil', '/api/projects?query=%00'])
def test_noncanonical_paths_and_control_queries_rejected(settings, path):
    called = []
    with client_for(settings, lambda r: called.append(r) or response()) as client:
        assert client.get(path, headers=auth(settings)).status_code == 400
    assert not called


def test_cross_origin_no_cookie_auth_and_safe_query_passes(settings):
    captured = []
    with client_for(settings, lambda r: captured.append(r) or response()) as client:
        assert client.post('/api/projects', headers=auth(settings) | {'Origin': 'https://other.example'}).status_code == 403
        assert client.post('/api/projects', headers=auth(settings) | {'Sec-Fetch-Site': 'cross-site'}).status_code == 403
        assert client.options('/api/projects', headers=auth(settings)).status_code == 405
        assert client.get('/api/projects?search=%E8%B4%A2%E5%8A%A1', headers=auth(settings)).status_code == 200
    assert captured[0].url.query == b'search=%E8%B4%A2%E5%8A%A1'


def test_bounded_upload_rejected_before_backend_with_and_without_length(settings):
    called = []
    with client_for(settings, lambda r: called.append(r) or response(), max_request_bytes=4) as client:
        assert client.post('/api/upload', content=b'12345', headers=auth(settings)).status_code == 413
        assert client.post('/api/upload', content=iter([b'123', b'45']), headers=auth(settings)).status_code == 413
        assert client.post('/api/upload', content=b'123', headers=auth(settings) | {'Content-Encoding': 'gzip'}).status_code == 415
        assert client.post('/api/upload', content=b'1234', headers=auth(settings)).status_code == 200
    assert len(called) == 1


def test_response_length_limit_closes_upstream_and_releases_slot(settings):
    streams = []
    def backend(request):
        stream = Chunks(b'12345')
        streams.append(stream)
        return httpx.Response(200, headers={'Content-Length': '5'}, stream=stream)
    with client_for(settings, backend, max_response_bytes=4, max_concurrent_requests=1) as client:
        for _ in range(2):
            assert client.get('/api/projects', headers=auth(settings)).status_code == 502
    assert all(stream.closed for stream in streams)


def test_unknown_length_overflow_aborts_not_truncated_success(settings):
    streams = []
    def backend(request):
        stream = Chunks(b'123', b'45')
        streams.append(stream)
        return httpx.Response(200, stream=stream)
    with client_for(settings, backend, max_response_bytes=4, max_concurrent_requests=1) as client:
        for _ in range(2):
            with pytest.raises(Exception, match='limit|TaskGroup'):
                client.get('/api/projects', headers=auth(settings))
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize('failure,status', [('redirect', 502), ('connection', 502), ('timeout', 504), ('encoded', 502)])
def test_failures_never_retry_and_release_capacity(settings, failure, status):
    calls = []
    streams = []
    def backend(request):
        calls.append(request)
        if failure == 'connection':
            raise httpx.ConnectError('fixture', request=request)
        if failure == 'timeout':
            raise httpx.ReadTimeout('fixture', request=request)
        result = response(status=302 if failure == 'redirect' else 200,
                          headers={'Location': 'http://atlas-bob:8000'} if failure == 'redirect' else {'Content-Encoding': 'gzip'})
        streams.append(result.stream)
        return result
    with client_for(settings, backend, max_concurrent_requests=1) as client:
        for _ in range(2):
            result = client.post('/api/projects', content=b'write', headers=auth(settings))
            assert result.status_code == status
    assert len(calls) == 2
    assert all(s.closed for s in streams)


@pytest.mark.parametrize('mutation', [
    lambda c: c.update(schema_version=True), lambda c: c.update(unknown=True),
    lambda c: c.update(public_origin='http://atlas.example'),
    lambda c: c['workspaces'][1].update(id='alice'),
    lambda c: c['workspaces'][1].update(subjects=['alice-sub']),
    lambda c: c['workspaces'][1].update(backend_url='http://ATLAS-ALICE:8000/'),
    lambda c: c['workspaces'][0].update(backend_url='http://user:secret@atlas-alice:8000'),
    lambda c: c['workspaces'][0].update(backend_url='http://atlas-alice:8000/path'),
    lambda c: c['workspaces'][0].update(backend_url='http://atlas-alice:8000?x=1'),
    lambda c: c['workspaces'][0].update(backend_url='http://8.8.8.8'),
    lambda c: c['workspaces'][0].update(backend_url='http://evil.example'),
    lambda c: c.update(limits={'max_request_bytes': True}),
    lambda c: c.update(limits={'max_response_bytes': 0}),
    lambda c: c['oidc'].update(issuer='https://user:secret@identity.example'),
])
def test_invalid_configuration_fails_closed(settings, mutation):
    config = deepcopy(settings[0])
    mutation(config)
    with pytest.raises(ConfigurationError):
        GatewayConfig.parse(config)


@pytest.mark.parametrize('change', ['duplicate', 'private', 'wrong-alg', 'weak', 'symmetric'])
def test_jwks_public_key_validation(settings, change):
    config, private, path, public = settings
    keys = [deepcopy(public)]
    if change == 'duplicate':
        keys.append(public)
    elif change == 'private':
        keys[0]['d'] = 'private-material'
    elif change == 'wrong-alg':
        keys[0]['alg'] = 'HS256'
    elif change == 'weak':
        keys[0] = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa.generate_private_key(public_exponent=65537, key_size=1024).public_key())) | {'kid': 'weak', 'alg': 'RS256'}
    else:
        keys = [{'kty': 'oct', 'kid': 'symmetric', 'k': 'secret', 'alg': 'HS256'}]
    path.write_text(json.dumps({'keys': keys}))
    with pytest.raises(ConfigurationError):
        GatewayConfig.parse(config)


def test_loading_rejects_duplicate_json(settings, tmp_path):
    path = tmp_path / 'gateway.json'
    path.write_text('{"schema_version":1,"schema_version":1}')
    with pytest.raises(ConfigurationError):
        GatewayConfig.load(path)
    path.write_text(json.dumps(settings[0]))
    assert GatewayConfig.load(path).routes['alice-sub'][0] == 'alice'


def test_loading_rejects_symlinks(settings, tmp_path):
    path = tmp_path / 'gateway.json'
    path.write_text(json.dumps(settings[0]))
    link = tmp_path / 'link.json'
    try:
        link.symlink_to(path)
    except OSError as exc:
        if getattr(exc, 'winerror', None) == 1314:
            pytest.skip('Native Windows symlink privilege is unavailable')
        raise
    with pytest.raises(ConfigurationError):
        GatewayConfig.load(link)
    assert GatewayConfig.load(path).routes['alice-sub'][0] == 'alice'


def test_client_disconnect_during_download_closes_backend_and_releases_slot():
    from agent_service.workspace_gateway import Limits, _ProxyResponse

    async def scenario():
        stream = Chunks(b'first', b'second')
        upstream = httpx.Response(200, stream=stream)
        semaphore = asyncio.Semaphore(1)
        await semaphore.acquire()
        proxy = _ProxyResponse(upstream, semaphore, Limits())
        sent = []

        async def send(message):
            sent.append(message)
            if message['type'] == 'http.response.body':
                raise OSError('fixture client connection closed')

        async def receive():
            return {'type': 'http.disconnect'}

        try:
            await proxy({'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'}}, receive, send)
        except Exception:
            pass  # The transport must fail, but the isolated backend must close.
        assert stream.closed
        assert not semaphore.locked()
        assert any(message['type'] == 'http.response.body' for message in sent)
    asyncio.run(scenario())


def test_cancelled_stream_closes_backend_and_releases_slot():
    from agent_service.workspace_gateway import Limits, _ProxyResponse

    class WaitingStream(httpx.AsyncByteStream):
        def __init__(self):
            self.started = asyncio.Event()
            self.closed = False

        async def __aiter__(self):
            self.started.set()
            await asyncio.Event().wait()
            yield b'never'

        async def aclose(self):
            self.closed = True

    async def scenario():
        stream = WaitingStream()
        semaphore = asyncio.Semaphore(1)
        await semaphore.acquire()
        proxy = _ProxyResponse(httpx.Response(200, stream=stream), semaphore, Limits())
        async def send(message):
            pass
        async def receive():
            await asyncio.Event().wait()
        task = asyncio.create_task(proxy({'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'}}, receive, send))
        await asyncio.wait_for(stream.started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
        assert not semaphore.locked()
    asyncio.run(scenario())


def test_disconnected_upload_never_forwarded_and_tempfile_closed(settings, monkeypatch):
    import agent_service.workspace_gateway as gateway
    original = gateway.tempfile.SpooledTemporaryFile
    temporary = []
    called = []
    def track(*args, **kwargs):
        file = original(*args, **kwargs)
        temporary.append(file)
        return file
    monkeypatch.setattr(gateway.tempfile, 'SpooledTemporaryFile', track)
    app = create_app(GatewayConfig.parse(settings[0]), transport=httpx.MockTransport(lambda r: called.append(r) or response()))

    async def scenario():
        async with app.router.lifespan_context(app):
            incoming = iter([{'type': 'http.request', 'body': b'incomplete', 'more_body': True}, {'type': 'http.disconnect'}])
            sent = []
            async def receive():
                return next(incoming)
            async def send(message):
                sent.append(message)
            scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'}, 'http_version': '1.1',
                'method': 'POST', 'scheme': 'https', 'path': '/api/upload', 'raw_path': b'/api/upload',
                'query_string': b'', 'root_path': '', 'server': ('atlas.example', 443), 'client': ('127.0.0.1', 1),
                'headers': [(b'host', b'atlas.example'), (b'authorization', ('Bearer ' + access_token(settings)).encode())]}
            await app(scope, receive, send)
            assert sent[0]['status'] == 499
            assert not app.state.semaphore.locked()
    asyncio.run(scenario())
    assert temporary and all(file.closed for file in temporary)
    assert not called


def test_oidc_audience_membership_and_configured_lifetime(settings):
    config = deepcopy(settings[0])
    config['oidc']['max_token_lifetime_seconds'] = 7200
    parsed = GatewayConfig.parse(config)
    token = access_token(settings, aud=['other', 'atlas'], exp=int(time.time()) + 7000)
    assert parsed.authenticate(token)[0] == 'alice'
    with pytest.raises(HTTPException):
        GatewayConfig.parse(settings[0]).authenticate(token)


def test_active_download_capacity_is_bounded_and_released(settings):
    class HeldStream(httpx.AsyncByteStream):
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = False
        async def __aiter__(self):
            self.started.set()
            await self.release.wait()
            yield b'complete'
        async def aclose(self):
            self.closed = True

    async def scenario():
        config = deepcopy(settings[0])
        config['limits'] = {'max_concurrent_requests': 1}
        stream = HeldStream()
        calls = []
        def backend(request):
            calls.append(request)
            return httpx.Response(200, stream=stream) if len(calls) == 1 else response(b'next')
        app = create_app(GatewayConfig.parse(config), transport=httpx.MockTransport(backend))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://atlas.example') as client:
                first = asyncio.create_task(client.get('/api/projects', headers=auth(settings)))
                await asyncio.wait_for(stream.started.wait(), 1)
                saturated = await client.get('/api/projects', headers=auth(settings, 'bob-sub'))
                assert saturated.status_code == 503
                assert saturated.headers['retry-after'] == '1'
                assert len(calls) == 1
                stream.release.set()
                assert (await first).content == b'complete'
                assert stream.closed
                assert (await client.get('/api/projects', headers=auth(settings, 'bob-sub'))).content == b'next'
        assert len(calls) == 2
    asyncio.run(scenario())


def test_stream_total_deadline_closes_backend():
    from agent_service.workspace_gateway import Limits, _ProxyResponse
    class SlowStream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            await asyncio.sleep(2)
            yield b'too late'
        async def aclose(self):
            self.closed = True
    async def scenario():
        stream = SlowStream()
        semaphore = asyncio.Semaphore(1)
        await semaphore.acquire()
        proxy = _ProxyResponse(httpx.Response(200, stream=stream), semaphore, Limits(upstream_timeout_seconds=1))
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            pass
        with pytest.raises((TimeoutError, ClientDisconnect)):
            await proxy({'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'}}, receive, send)
        assert stream.closed
        assert not semaphore.locked()
    asyncio.run(scenario())


def test_upload_total_deadline_closes_spool_without_backend(settings, monkeypatch):
    import agent_service.workspace_gateway as gateway
    original = gateway.tempfile.SpooledTemporaryFile
    files, calls = [], []
    def track(*args, **kwargs):
        file = original(*args, **kwargs)
        files.append(file)
        return file
    monkeypatch.setattr(gateway.tempfile, 'SpooledTemporaryFile', track)
    config = deepcopy(settings[0])
    config['limits'] = {'request_timeout_seconds': 1, 'max_concurrent_requests': 1}
    app = create_app(GatewayConfig.parse(config), transport=httpx.MockTransport(lambda r: calls.append(r) or response()))
    async def scenario():
        async with app.router.lifespan_context(app):
            sent = []
            async def receive():
                await asyncio.sleep(2)
                return {'type': 'http.request', 'body': b'too late', 'more_body': False}
            async def send(message):
                sent.append(message)
            scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'}, 'http_version': '1.1',
                'method': 'POST', 'scheme': 'https', 'path': '/api/upload', 'raw_path': b'/api/upload',
                'query_string': b'', 'root_path': '', 'server': ('atlas.example', 443), 'client': ('127.0.0.1', 1),
                'headers': [(b'host', b'atlas.example'), (b'authorization', ('Bearer ' + access_token(settings)).encode())]}
            await app(scope, receive, send)
            assert sent[0]['status'] == 504
            assert not app.state.semaphore.locked()
    asyncio.run(scenario())
    assert files and all(file.closed for file in files)
    assert not calls


def test_backend_cookies_never_persist_or_cross_workspace_ports(settings):
    """A shared HTTP transport must not turn backend cookies into ambient identity."""
    config = deepcopy(settings[0])
    config['workspaces'][0]['backend_url'] = 'http://127.0.0.1:8001'
    config['workspaces'][1]['backend_url'] = 'http://127.0.0.1:8002'
    seen = []

    def backend(request):
        seen.append((request.url.port, request.headers.get('cookie')))
        # Both host-only and broad Domain cookies previously entered HTTPX's jar.
        return httpx.Response(200, stream=Chunks(b'ok'), headers=[
            ('Set-Cookie', 'owner=alice-secret; Path=/'),
            ('Set-Cookie', 'broad=other-secret; Domain=127.0.0.1; Path=/'),
        ])

    app = create_app(GatewayConfig.parse(config), transport=httpx.MockTransport(backend))
    with TestClient(app, base_url='https://atlas.example') as client:
        for subject in ('alice-sub', 'bob-sub', 'alice-sub', 'bob-sub'):
            result = client.get('/api/projects', headers=auth(settings, subject))
            assert result.status_code == 200
            assert 'set-cookie' not in result.headers
            assert not list(app.state.client.cookies.jar)
    assert seen == [(8001, None), (8002, None), (8001, None), (8002, None)]


def test_gateway_preserves_backend_runtime_revision_for_ui_diagnostics(settings):
    revision='2026.10.30.1'
    with client_for(settings,lambda request:response(b'{}',headers={'content-type':'application/json',
            'x-atlas-runtime-revision':revision})) as client:
        value=client.get('/api/projects',headers=auth(settings))
        assert value.status_code==200
        assert value.headers['x-atlas-runtime-revision']==revision
        assert value.headers['cache-control']=='no-store'
