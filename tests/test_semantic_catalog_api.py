"""Owner-reviewed catalog APIs and Agent proposal-only integration."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from agent_service import agent, agent_context, api, data_tools, identity, provider
from agent_service import semantic_catalog as catalogs
from agent_service.config import TOKEN
from test_semantic_catalog import definition, field, project, source

H = {'Authorization': 'Bearer ' + TOKEN}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('ATLAS_AUTH_MODE', 'token')
    monkeypatch.setattr(api, '_requests', {})
    return TestClient(api.app)


@pytest.fixture
def owner_identity(monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid='catalog-key', alg='RS256', use='sig', key_ops=['verify'])
    values = {'ATLAS_AUTH_MODE': 'oidc', 'OIDC_ISSUER': 'https://catalog-identity.test',
              'OIDC_AUDIENCE': 'atlas', 'OIDC_JWKS_URL': 'https://catalog-identity.test/jwks',
              'OIDC_ALLOWED_SUBJECTS': 'catalog-owner'}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(identity, 'keys', lambda refresh=False: [jwk])
    token = jwt.encode({'sub': 'catalog-owner', 'iss': values['OIDC_ISSUER'], 'aud': 'atlas',
                        'exp': datetime.now(timezone.utc) + timedelta(minutes=2)},
                       private, algorithm='RS256', headers={'kid': 'catalog-key'})
    return {'Authorization': 'Bearer ' + token}


def setup_catalog(client, headers=H):
    pid = project(); registered, _ = source(pid)
    response = client.post('/api/projects/' + pid + '/semantic-catalogs',
                           headers=headers, json=definition(registered))
    assert response.status_code == 200, response.text
    return pid, registered, response.json()


def test_catalog_owner_api_auth_versions_and_hash_bound_review(client):
    pid, registered, draft = setup_catalog(client)
    root = '/api/projects/' + pid + '/semantic-catalogs'
    review = {'expected_sha256': draft['content_sha256']}
    for method, route, body in (
            ('get', root, None), ('post', root, definition(registered)),
            ('get', root + '/' + draft['id'], None),
            ('post', root + '/' + draft['id'] + '/approve', review),
            ('post', root + '/' + draft['id'] + '/retire', review)):
        response = getattr(client, method)(route, **({'json': body} if body else {}))
        assert response.status_code == 401
    assert draft['status'] == 'draft' and draft['approval'] is None
    assert draft['created_actor_sha256'] == hashlib.sha256(b'owner_workspace').hexdigest()
    wrong = {'expected_sha256': '0' * 64}
    route = root + '/' + draft['id']
    assert client.post(route + '/approve', headers=H, json=wrong).status_code == 400
    assert catalogs.get_catalog(pid, draft['id'])['status'] == 'draft'
    approved = client.post(route + '/approve', headers=H, json=review)
    assert approved.status_code == 200, approved.text
    assert approved.json()['approval']['content_sha256'] == draft['content_sha256']
    changed = definition(registered); changed['fields'][0]['period'] = '2026-11'
    second = client.post(root, headers=H, json=changed).json()
    assert second['version'] == 2 and second['id'] != draft['id'] and second['status'] == 'draft'
    page = client.get(root, headers=H, params={'status': 'approved', 'limit': 1}).json()
    assert page['total'] == 1 and page['items'][0]['id'] == draft['id']
    assert client.post(route + '/retire', headers=H, json=wrong).status_code == 400
    retired = client.post(route + '/retire', headers=H, json=review).json()
    assert retired['status'] == 'retired'
    assert client.post(route + '/approve', headers=H, json=review).status_code == 400
    assert client.get(root, headers=H, params={'status': 'retired'}).json()['total'] == 1
    assert client.get(root, headers=H, params={'limit': 41}).status_code == 400


def test_catalog_review_actor_is_the_verified_owner_identity(client, owner_identity):
    pid, _, draft = setup_catalog(client, owner_identity)
    route = '/api/projects/' + pid + '/semantic-catalogs/' + draft['id']
    digest = hashlib.sha256(b'catalog-owner').hexdigest()
    assert draft['created_actor_sha256'] == digest
    # A client cannot replace the authenticated reviewer through body fields.
    assert client.post(route + '/approve', headers=owner_identity,
                       json={'expected_sha256': draft['content_sha256'], 'actor': 'model'}).status_code == 422
    approved = client.post(route + '/approve', headers=owner_identity,
                           json={'expected_sha256': draft['content_sha256']}).json()
    assert approved['approval']['actor_sha256'] == digest
    retired = client.post(route + '/retire', headers=owner_identity,
                          json={'expected_sha256': draft['content_sha256']}).json()
    assert retired['retirement']['actor_sha256'] == digest


def test_catalog_api_rejects_cross_project_records_and_source(client):
    pid, registered, draft = setup_catalog(client); other = project()
    root = '/api/projects/' + other + '/semantic-catalogs'
    assert client.get(root, headers=H).json()['items'] == []
    assert client.get(root + '/' + draft['id'], headers=H).status_code == 400
    assert client.post(root, headers=H, json=definition(registered)).status_code == 400
    for action in ('approve', 'retire'):
        assert client.post(root + '/' + draft['id'] + '/' + action, headers=H,
                           json={'expected_sha256': draft['content_sha256']}).status_code == 400
    assert catalogs.get_catalog(pid, draft['id'])['status'] == 'draft'


def test_catalog_alignment_api_uses_fixed_approved_catalog_and_explicit_date(client):
    pid, _, draft = setup_catalog(client)
    root = '/api/projects/' + pid
    request = {'source': field('incoming'), 'catalog_id': draft['id'],
               'expected_catalog_sha256': draft['content_sha256'], 'as_of_date': '2026-10-21'}
    endpoint = root + '/semantic-catalog-alignments'
    assert client.post(endpoint, json=request).status_code == 401
    assert client.post(endpoint, headers=H, json=request).status_code == 400
    assert client.post(root + '/semantic-catalogs/' + draft['id'] + '/approve', headers=H,
                       json={'expected_sha256': draft['content_sha256']}).status_code == 200
    response = client.post(endpoint, headers=H, json=request)
    assert response.status_code == 200, response.text
    proposal = response.json()
    assert proposal['status'] == 'proposed'
    assert proposal['catalog_binding']['catalog_id'] == draft['id']
    assert proposal['catalog_binding']['content_sha256'] == draft['content_sha256']
    assert proposal['catalog_binding']['as_of_date'] == '2026-10-21'
    for change in ({'targets': [field('forged')]}, {'catalog': [field('forged')]}, {'actor': 'model'}):
        assert client.post(endpoint, headers=H, json={**request, **change}).status_code == 422
    missing_date = dict(request); missing_date.pop('as_of_date')
    assert client.post(endpoint, headers=H, json=missing_date).status_code == 422
    for change in ({'expected_catalog_sha256': '0' * 64}, {'as_of_date': '2027-01-01'}):
        assert client.post(endpoint, headers=H, json={**request, **change}).status_code == 400
    other = project()
    assert client.post('/api/projects/' + other + '/semantic-catalog-alignments',
                       headers=H, json=request).status_code == 400
    assert client.post(root + '/semantic-catalogs/' + draft['id'] + '/retire', headers=H,
                       json={'expected_sha256': draft['content_sha256']}).status_code == 200
    assert client.post(endpoint, headers=H, json=request).status_code == 400


def test_agent_catalog_tools_are_discoverable_but_cannot_review(client, monkeypatch):
    pid, registered, draft = setup_catalog(client)
    turn = {'project_id': pid, 'text': '请分析语义包并匹配字段口径', 'interaction_mode': 'work'}
    names = {'semantic_catalog_guide', 'list_semantic_catalogs', 'inspect_semantic_catalog',
             'propose_semantic_catalog', 'propose_catalog_alignment'}
    assert names <= set(agent.TOOLS)
    assert names <= set(agent_context.selected(turn))
    assert not any('catalog' in name and ('approve' in name or 'retire' in name) for name in agent.TOOLS)
    chat_tools = set(agent_context.selected(dict(turn, interaction_mode='chat')))
    assert {'semantic_catalog_guide', 'list_semantic_catalogs', 'inspect_semantic_catalog'} <= chat_tools
    assert not {'propose_semantic_catalog', 'propose_catalog_alignment'} & chat_tools
    guide = data_tools.execute(turn, 'semantic_catalog_guide', {})
    assert 'source' in guide['schema']['required'] and 'as_of_date' in guide['instructions']
    monkeypatch.setattr(provider, 'settings', lambda: {'share_data': False})
    for name, args in (('list_semantic_catalogs', {}),
                       ('inspect_semantic_catalog', {'catalog_id': draft['id']}),
                       ('propose_catalog_alignment', {})):
        with pytest.raises(ValueError, match='模型读取项目内容'):
            data_tools.execute(turn, name, args)
    monkeypatch.setattr(provider, 'settings', lambda: {'share_data': True})
    proposed_definition = definition(registered); proposed_definition['key'] = 'agent-finance'
    created = data_tools.execute(dict(turn, text='请创建语义包草稿'), 'propose_semantic_catalog',
                                 {'definition': proposed_definition})
    assert created['status'] == 'draft' and created['approval'] is None
    assert created['created_actor_sha256'] == hashlib.sha256(b'agent_proposal').hexdigest()
    for text, mode in (('不要创建语义包', 'work'), ('请创建语义包', 'chat')):
        with pytest.raises(ValueError):
            data_tools.execute(dict(turn, text=text, interaction_mode=mode), 'propose_semantic_catalog',
                               {'definition': proposed_definition})
    read = data_tools.execute(turn, 'inspect_semantic_catalog', {'catalog_id': draft['id']})
    assert read['id'] == draft['id']
    with pytest.raises(ValueError, match='当前项目'):
        data_tools.execute(dict(turn, project_id=project()), 'inspect_semantic_catalog', {'catalog_id': draft['id']})
    catalogs.approve(pid, draft['id'], draft['content_sha256'], 'owner')
    page = data_tools.execute(turn, 'list_semantic_catalogs', {})
    assert page['total'] == 1 and page['items'][0]['id'] == draft['id']
    alignment = {'source': field('incoming'), 'catalog_id': draft['id'],
                 'expected_catalog_sha256': draft['content_sha256'],
                 'as_of_date': '2026-10-21', 'model_candidates': []}
    result = data_tools.execute(turn, 'propose_catalog_alignment', alignment)
    assert result['catalog_binding']['catalog_id'] == draft['id'] and result['status'] == 'proposed'
    with pytest.raises(ValueError):
        data_tools.execute(dict(turn, text='不要分析或匹配字段'), 'propose_catalog_alignment', alignment)
    with pytest.raises(ValueError):
        agent.authorize(dict(turn, interaction_mode='chat'), 'propose_catalog_alignment', alignment)
