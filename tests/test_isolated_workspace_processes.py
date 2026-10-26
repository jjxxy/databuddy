"""Real process + HTTP + storage isolation, without Docker, IdP or LLM calls.

Only JWKS retrieval is replaced by a local public fixture. Every backend still
performs its real independent signature, issuer, audience and subject checks.
Workers are not started: upload probe jobs exercise durable queue isolation.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


ROOT = Path(__file__).parents[1]
ISSUER = 'https://isolated-idp.example.test'
AUDIENCE = 'atlas-process-test'
HOST = 'atlas-process.example.test'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def start(stack, tmp_path, name, port, env, bootstrap):
    output = stack.enter_context((tmp_path / (name + '.log')).open('w+'))
    process = subprocess.Popen([sys.executable, '-c', bootstrap], cwd=ROOT, env=env,
                               stdout=output, stderr=subprocess.STDOUT)
    stack.callback(stop, process)
    base = 'http://127.0.0.1:' + str(port)
    deadline = time.monotonic() + 45
    with httpx.Client(trust_env=False, headers={'Host': HOST}, timeout=2) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output.seek(0)
                raise AssertionError(name + ' exited: ' + output.read())
            try:
                if client.get(base + '/health').status_code == 200:
                    return base
            except httpx.HTTPError:
                pass
            time.sleep(.1)
    output.seek(0)
    raise AssertionError(name + ' not healthy: ' + output.read())


def test_two_real_backends_keep_projects_sources_artifacts_and_jobs_separate(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid='process-test-key', use='sig', alg='RS256')
    jwks = tmp_path / 'public-jwks.json'
    jwks.write_text(json.dumps({'keys': [public]}))
    tokens = {}
    for subject in ('alice', 'bob', 'unassigned'):
        tokens[subject] = jwt.encode({'iss': ISSUER, 'aud': AUDIENCE, 'sub': subject,
            'iat': int(time.time()), 'exp': int(time.time()) + 600}, key, algorithm='RS256', headers={'kid': public['kid']})
    # Do not inherit production credentials, proxy variables or deployment flags.
    base_env = {'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(ROOT),
                'PYTHONUNBUFFERED': '1', 'DO_NOT_TRACK': '1', 'DLT_TELEMETRY': 'false'}
    ports = {name: free_port() for name in ('alice', 'bob', 'gateway')}
    assert len(set(ports.values())) == 3
    with ExitStack() as stack:
        backends = {}
        for subject in ('alice', 'bob'):
            data = tmp_path / subject
            data.mkdir()
            env = dict(base_env, DATA_DIR=str(data), DATABASE_URL='sqlite:///' + str(data / 'metadata.sqlite'),
                       HOME=str(data), APP_TOKEN='fixture-only-distinct-' + subject + '-token',
                       ATLAS_AUTH_MODE='oidc', OIDC_ISSUER=ISSUER, OIDC_AUDIENCE=AUDIENCE,
                       OIDC_ALLOWED_SUBJECTS=subject, OIDC_JWKS_URL=ISSUER + '/jwks',
                       OIDC_ACCEPT_PROXY_HEADER='0', TEST_JWKS=str(jwks), TEST_PORT=str(ports[subject]))
            bootstrap = """
import json, os
from agent_service import identity
identity.keys = lambda refresh=False: json.load(open(os.environ['TEST_JWKS']))['keys']
import uvicorn
uvicorn.run('agent_service.api:app', host='127.0.0.1', port=int(os.environ['TEST_PORT']), log_level='warning')
"""
            backends[subject] = start(stack, tmp_path, subject, ports[subject], env, bootstrap)
        config = {'schema_version': 1, 'public_origin': 'https://' + HOST,
                  'oidc': {'issuer': ISSUER, 'audience': AUDIENCE, 'jwks_file': str(jwks)},
                  'workspaces': [{'id': s, 'backend_url': backends[s], 'subjects': [s]} for s in ('alice', 'bob')]}
        config_path = tmp_path / 'gateway.json'
        config_path.write_text(json.dumps(config))
        gateway = start(stack, tmp_path, 'gateway', ports['gateway'],
                        dict(base_env, ATLAS_WORKSPACE_GATEWAY_CONFIG=str(config_path), TEST_PORT=str(ports['gateway'])),
                        "import os,uvicorn; uvicorn.run('agent_service.workspace_gateway:app',host='127.0.0.1',port=int(os.environ['TEST_PORT']),log_level='warning')")
        clients = {subject: stack.enter_context(httpx.Client(base_url=gateway, trust_env=False, timeout=30,
                   headers={'Host': HOST, 'Authorization': 'Bearer ' + tokens[subject], 'Origin': 'https://' + HOST})) for subject in ('alice', 'bob')}

        def create(subject):
            client = clients[subject]
            response = client.post('/api/projects', json={'name': subject + '-private', 'request_id': subject + '-private-project'})
            assert response.status_code == 200, response.text
            pid = response.json()['id']
            content = ('owner,amount\n' + subject + ',123\n').encode()
            response = client.post('/api/projects/' + pid + '/attachments',
                                   files={'file': ('private.csv', content, 'text/csv')}, data={'request_id': 'same-upload-request'})
            assert response.status_code == 200, response.text
            sid = response.json()['id']
            catalog = client.get('/api/projects/' + pid + '/artifacts?kind=source')
            assert catalog.status_code == 200, catalog.text
            aid = catalog.json()['items'][0]['id']
            download = client.get('/api/projects/' + pid + '/artifacts/' + aid + '/download')
            assert download.status_code == 200 and download.content == content
            runs = client.get('/api/runs').json()
            assert len(runs) == 1 and runs[0]['target'] == sid and runs[0]['status'] == 'queued'
            return {'pid': pid, 'sid': sid, 'aid': aid, 'job': runs[0]['id'], 'content': content}

        with ThreadPoolExecutor(max_workers=2) as pool:
            values = dict(zip(('alice', 'bob'), pool.map(create, ('alice', 'bob'))))
        for subject, other in (('alice', 'bob'), ('bob', 'alice')):
            client = clients[subject]
            own, foreign = values[subject], values[other]
            projects = client.get('/api/projects').json()
            assert [p['id'] for p in projects] == [own['pid']]
            for route in ('/api/projects/' + foreign['pid'] + '/conversation',
                          '/api/projects/' + foreign['pid'] + '/artifacts/' + foreign['aid'] + '/download',
                          '/api/sources/' + foreign['sid'] + '/original',
                          '/api/runs/' + foreign['job']):
                response = client.get(route)
                assert response.status_code == 404, (route, response.status_code, response.text)
                assert foreign['content'].decode() not in response.text
            # Independently reject the other identity even if the gateway is bypassed.
            assert httpx.get(backends[subject] + '/api/projects', headers={'Authorization': 'Bearer ' + tokens[other]}, trust_env=False).status_code == 403
            assert client.get('/api/projects', headers={'X-Atlas-Workspace': other, 'X-Forwarded-User': other}).json() == projects
        # IDs may intentionally collide across separate databases; content must not.
        colliding = {}
        for subject in ('alice', 'bob'):
            response = clients[subject].post('/api/projects', json={
                'name': subject + '-same-id-private', 'request_id': 'shared-idempotency-key'})
            assert response.status_code == 200, response.text
            colliding[subject] = response.json()
        assert colliding['alice']['id'] == colliding['bob']['id']
        for subject in ('alice', 'bob'):
            projects = clients[subject].get('/api/projects').json()
            selected = [p for p in projects if p['id'] == colliding[subject]['id']]
            assert len(selected) == 1 and selected[0]['name'] == subject + '-same-id-private'
        with httpx.Client(base_url=gateway, headers={'Host': HOST}, trust_env=False) as client:
            assert client.get('/api/projects').status_code == 401
            assert client.get('/api/projects', headers={'Authorization': 'Bearer ' + tokens['unassigned']}).status_code == 403
        # Durable physical storage and encryption keys are separate, not row filters.
        assert (tmp_path / 'alice/connector.key').read_bytes() != (tmp_path / 'bob/connector.key').read_bytes()
        for subject, other in (('alice', 'bob'), ('bob', 'alice')):
            uploads = list((tmp_path / subject / 'uploads').iterdir())
            assert uploads and any(p.read_bytes() == values[subject]['content'] for p in uploads if p.is_file())
            assert all(p.read_bytes() != values[other]['content'] for p in uploads if p.is_file())
            assert (tmp_path / subject / 'metadata.sqlite').is_file()
