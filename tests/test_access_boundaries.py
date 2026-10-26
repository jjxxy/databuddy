"""Credential confidentiality and the explicit single-owner workspace boundary."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from agent_service import api, data_sharing, identity, project_data
from agent_service.config import DATA, TOKEN
from agent_service.store import all_items, delete, engine, get, now, put, seal, uid, unseal


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('ATLAS_AUTH_MODE', 'token')
    monkeypatch.setattr(api, '_requests', {})
    return TestClient(api.app)


H = {'Authorization': 'Bearer ' + TOKEN}


@pytest.mark.parametrize('method,path,payload', [
    ('post', '/api/sources', {'kind': 'api', 'config': {
        'url': 'https://source.test?token=FIXTURE-URL-SECRET',
        'headers': {'Authorization': 'Bearer FIXTURE-HEADER-SECRET'}}}),
    ('put', '/api/model', {'base_url': 'https://model.test/v1', 'model': 'fixture',
                           'api_key': {'nested': 'FIXTURE-MODEL-SECRET'}}),
    ('put', '/api/projects/nonexistent/company-search', {
        'search_url': 'https://company.test/search',
        'headers': {'Authorization': 'Bearer FIXTURE-COMPANY-SECRET'},
        'principal_label': 'owner', 'user_scoped_ack': False, 'storage_approved': False}),
    ('post', '/api/projects/nonexistent/report-connectors', {
        'name': 'fixture', 'url': {'nested': 'FIXTURE-REPORT-SECRET'},
        'headers': {'Authorization': 'Bearer FIXTURE-REPORT-HEADER-SECRET'}}),
    # An unrecognized JSON key can itself contain a credential, so validation
    # error locations must not be reflected either.
    ('post', '/api/sources', {'name': 'fixture', 'kind': 'api', 'config': {},
                             'FIXTURE-KEY-SECRET': 'ignored'}),
])
def test_invalid_requests_never_return_raw_credentials(client, method, path, payload, monkeypatch):
    monkeypatch.setenv('ATLAS_OPERATOR_TOKEN', 'fixture-operator-token-123456789')
    headers = dict(H, **{'X-Atlas-Operator-Token': 'fixture-operator-token-123456789'})
    project = None
    if '/api/projects/nonexistent/' in path:
        project = put('project', {'name': 'Validation boundary', 'created_at': now()}, 'p_' + uid())
        path = path.replace('/api/projects/nonexistent/', '/api/projects/' + project['id'] + '/')
    try:
        response = getattr(client, method)(path, headers=headers, json=payload)
        assert response.status_code == 422
        assert 'FIXTURE-' not in response.text
        assert isinstance(response.json()['detail'], str)
        assert response.headers['Cache-Control'] == 'no-store'
    finally:
        if project:delete('project', project['id'])


def test_invalid_connection_update_does_not_echo_or_store_secret(client):
    original = {'url': 'https://source.test', 'headers': {'Authorization': 'Bearer old'}}
    source = put('source', {'name': 'connection', 'kind': 'api',
                            'secret_config': seal(original), 'revision': 1})
    try:
        response = client.patch('/api/sources/' + source['id'], headers=H, json={
            'config': {'url': 'https://source.test', 'max_pages': 'FIXTURE-PATCH-SECRET',
                       'headers': {'Authorization': 'Bearer FIXTURE-HEADER-SECRET'}}})
        assert response.status_code == 400
        assert 'FIXTURE-' not in response.text
        assert get('source', source['id']) == source
    finally:
        delete('source', source['id'])


def test_initial_model_validation_failure_does_not_log_secret(tmp_path):
    data = tmp_path / 'cloud-data'
    # The invalid shape fails before any model or network request. A clean
    # environment also prevents importing any real deployment configuration.
    environment = {
        'PATH': os.environ.get('PATH', ''), 'DATA_DIR': str(data),
        'DATABASE_URL': 'sqlite:///' + str(data / 'metadata.sqlite'),
        'APP_TOKEN': 'fixture-only-access-token-123456',
        'ATLAS_INITIAL_MODEL': json.dumps({'api_key': 'FIXTURE-CLOUD-SECRET'}),
    }
    # Winsock initialization needs Windows' own installation path. Keep only
    # these OS variables; no deployed model/connector configuration is inherited.
    for key in ('SystemRoot','SYSTEMROOT','WINDIR'):
        if key in os.environ:environment[key]=os.environ[key]
    completed = subprocess.run([sys.executable, '-m', 'agent_service.cloud'],
                               env=environment, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 2
    assert 'ValidationError' in completed.stderr
    assert 'FIXTURE-CLOUD-SECRET' not in completed.stdout + completed.stderr
    assert 'input_value' not in completed.stderr


def test_attachments_keep_project_ownership_and_retry_content(client, monkeypatch):
    monkeypatch.setattr(api, 'enqueue', lambda *args, **kwargs: {'id': 'fixture-probe'})
    projects = [put('project', {'name': 'attachment project', 'created_at': now()}, 'p_' + uid())
                for _ in range(2)]
    content = b'id,amount\n1,5\n'
    sources = []
    original_uploads = set((DATA / 'uploads').glob('*'))
    try:
        for project in projects:
            response = client.post('/api/projects/' + project['id'] + '/attachments', headers=H,
                                   files={'file': ('orders.csv', content, 'text/csv')},
                                   data={'request_id': 'same-request-id'})
            assert response.status_code == 200
            sources.append(get('source', response.json()['id']))
            assert sources[-1]['project_id'] == project['id']
        assert sources[0]['id'] != sources[1]['id']
        expected_uploads = set((DATA / 'uploads').glob('*'))
        duplicate = client.post('/api/projects/' + projects[0]['id'] + '/attachments', headers=H,
                                files={'file': ('orders.csv', content, 'text/csv')},
                                data={'request_id': 'same-request-id'})
        assert duplicate.json()['id'] == sources[0]['id']
        conflict = client.post('/api/projects/' + projects[0]['id'] + '/attachments', headers=H,
                               files={'file': ('orders.csv', b'id,amount\n1,999\n', 'text/csv')},
                               data={'request_id': 'same-request-id'})
        assert conflict.status_code == 409
        assert set((DATA / 'uploads').glob('*')) == expected_uploads
        assert get('source', sources[0]['id']) == sources[0]
        rejected = client.post('/api/projects/' + projects[1]['id'] + '/messages', headers=H,
                               json={'text': '分析', 'source_id': sources[0]['id'],
                                     'request_id': 'cross-project-request'})
        assert rejected.status_code == 400
        route = '/api/sources/' + sources[0]['id'] + '/original'
        assert client.get(route).status_code == 401
        assert client.get(route, headers=H).content == content
    finally:
        for source in sources:
            (DATA / 'uploads' / unseal(source['secret_config'])['stored_name']).unlink(missing_ok=True)
            for version in all_items('source_version'):
                if version.get('source_id') == source['id']:
                    delete('source_version', version['id'])
            delete('source', source['id'])
        for project in projects:
            delete('project', project['id'])
        assert set((DATA / 'uploads').glob('*')) == original_uploads


def test_run_responses_omit_private_execution_snapshots(client):
    from agent_service import jobs
    project = put('project', {'name': 'Private snapshots', 'created_at': now()}, 'p_' + uid())
    source = put('source', {'project_id': project['id'], 'name': 'Private source', 'kind': 'api',
                            'secret_config': seal({'token': 'FIXTURE-SOURCE-SECRET'})}, 's_' + uid())
    source_snapshot = {source['id']: {'secret_config': seal({'token': 'FIXTURE-SOURCE-SECRET'})}}
    report_snapshot = seal({'headers': {'Authorization': 'Bearer FIXTURE-REPORT-SECRET'}})
    job = jobs.save({'kind': 'probe', 'target': source['id'], 'status': 'running',
                     'lease_token': 'FIXTURE-LEASE-SECRET', 'source_snapshot': source_snapshot,
                     'secret_report_snapshot': report_snapshot})
    try:
        for response in (client.get('/api/runs?target=' + source['id'], headers=H),
                         client.get('/api/runs/' + job['id'], headers=H),
                         client.post('/api/runs/' + job['id'] + '/cancel', headers=H)):
            assert response.status_code == 200
            for private_value in ('source_snapshot', 'secret_report_snapshot', 'lease_token',
                                  'secret_config', report_snapshot, 'FIXTURE-LEASE-SECRET'):
                assert private_value not in response.text
        assert client.get('/api/runs/' + job['id']).status_code == 401
    finally:
        with engine.begin() as connection:
            connection.execute(jobs.jobs.delete().where(jobs.jobs.c.id == job['id']))
        delete('source', source['id'])
        delete('project', project['id'])


def test_scoped_share_identity_never_grants_owner_routes(client, monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid='boundary-key', alg='RS256', use='sig', key_ops=['verify'])
    configuration = {'ATLAS_AUTH_MODE': 'oidc', 'OIDC_ISSUER': 'https://identity.test',
                     'OIDC_AUDIENCE': 'atlas', 'OIDC_JWKS_URL': 'https://identity.test/jwks',
                     'OIDC_ALLOWED_SUBJECTS': 'owner-a,owner-b'}
    for name, value in configuration.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(identity, 'keys', lambda refresh=False: [jwk])

    def authorization(subject):
        token = jwt.encode({'sub': subject, 'iss': configuration['OIDC_ISSUER'],
                            'aud': 'atlas', 'exp': datetime.now(timezone.utc) + timedelta(minutes=2)},
                           private, algorithm='RS256', headers={'kid': 'boundary-key'})
        return {'Authorization': 'Bearer ' + token}

    project = put('project', {'name': 'private owner project', 'created_at': now()}, 'p_' + uid())
    metadata = {'plan_sha256': 'a' * 64, 'handoff_sha256': 'b' * 64}
    monkeypatch.setattr(project_data, 'result_folder', lambda pid, rid: (None, metadata))
    monkeypatch.setattr(project_data, 'read_result', lambda pid, rid, offset, limit:
                        {'result_id': rid, 'rows': [{'amount': '1.23'}]})
    share = data_sharing.create_share(project['id'], 'one-result', ['recipient'], 'owner-a')
    try:
        assert client.get('/api/shared-data/' + share['id'], headers=authorization('recipient')).json() == {
            'result_id': 'one-result', 'rows': [{'amount': '1.23'}]}
        assert client.get('/api/shared-data/' + share['id'] + '/download',
                          headers=authorization('recipient')).status_code == 403
        for route in ('/api/projects', '/api/sources', '/api/runs', '/api/model', '/api/platform',
                      '/api/projects/' + project['id'] + '/conversation'):
            assert client.get(route, headers=authorization('recipient')).status_code == 403
            assert client.get(route, headers=H).status_code == 401
        # Both allowed owner subjects can access this same project. Use its
        # sessions route so unrelated minimal project fixtures do not affect
        # the assertion through workspace-wide project-list rendering.
        for subject in ('owner-a', 'owner-b'):
            response = client.get('/api/projects/' + project['id'] + '/sessions', headers=authorization(subject))
            assert response.status_code == 200
            assert {session['project_id'] for session in response.json()} == {project['id']}
    finally:
        delete(data_sharing.KIND, share['id'])
        delete('project', project['id'])
