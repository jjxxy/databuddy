"""Operator configuration is durable, guarded and does not mutate tasks."""
import pytest
from fastapi.testclient import TestClient
from concurrent.futures import ThreadPoolExecutor

from agent_service import api, agent_runtime, runtime_settings
from agent_service.config import TOKEN
from agent_service.store import delete, get, put


@pytest.fixture
def client(monkeypatch):
    try:
        saved = get('system', 'agent-runtime-default')
    except KeyError:
        saved = None
    delete('system', 'agent-runtime-default')
    monkeypatch.setenv('ATLAS_AUTH_MODE', 'token')
    monkeypatch.setenv('ATLAS_OPERATOR_TOKEN', 'separate-operator-test-token')
    monkeypatch.setattr(api, '_requests', {})
    yield TestClient(api.app)
    delete('system', 'agent-runtime-default')
    if saved is not None:
        put('system', saved, 'agent-runtime-default')


def test_operator_auth_and_compare_and_swap(client):
    path = '/api/operator/agent-runtimes'
    owner = {'Authorization': 'Bearer ' + TOKEN}
    operator = {**owner, 'X-Atlas-Operator-Token': 'separate-operator-test-token'}
    payload = {'name':'atlas_legacy', 'expected_revision':'0'}
    assert client.get(path,headers=owner).status_code == 403
    assert client.put(path,headers=owner,json=payload).status_code == 403
    assert client.get(path,headers={'X-Atlas-Operator-Token':operator['X-Atlas-Operator-Token']}).status_code == 401
    info = client.get(path,headers=operator)
    assert info.status_code == 200
    assert info.json()['selection'] == {'name': None, 'revision':'0'}
    assert {item['name'] for item in info.json()['adapters']} == {'opencode','atlas_legacy'}
    first = client.put(path,headers=operator,json=payload)
    assert first.status_code == 200
    revision = first.json()['selection']['revision']
    assert runtime_settings.get_selected() == 'atlas_legacy'
    assert client.put(path,headers=operator,json={'name':'opencode','expected_revision':'0'}).status_code == 409
    assert runtime_settings.get_selected() == 'atlas_legacy'
    assert client.put(path,headers=operator,json={'name':'unknown','expected_revision':revision}).status_code == 400
    reset = client.put(path,headers=operator,json={'name':None,'expected_revision':revision})
    assert reset.status_code == 200 and runtime_settings.get_selected() is None


def test_config_change_does_not_rewrite_existing_runtime_binding(client):
    first = agent_runtime.snapshot('opencode')
    runtime_settings.set_selected('atlas_legacy','0')
    second = agent_runtime.snapshot()
    assert first['name'] == 'opencode'
    assert second['name'] == 'atlas_legacy'
    assert agent_runtime.snapshot_for_turn({'agent_runtime': first}) == first


def test_concurrent_operators_cannot_silently_overwrite(client):
    def configure(name):
        try:
            return runtime_settings.set_selected(name,'0')['name']
        except runtime_settings.SettingsConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(configure, ['opencode','atlas_legacy']))
    assert results.count('conflict') == 1
    assert runtime_settings.get_selected() in results
