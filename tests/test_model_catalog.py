"""The business model picker cannot become a provider configuration surface."""
import json
import sys
import pytest
from fastapi.testclient import TestClient
from agent_service import api, model_catalog as catalog, provider
from agent_service.conversation import TurnInput, submit, default_session, thread
from agent_service.config import TOKEN
from agent_service.store import all_items, delete, get, now, put, uid, seal, patch

H = {'Authorization': 'Bearer ' + TOKEN}


@pytest.fixture
def managed(monkeypatch):
    saved = {}
    for key in ('model-provider', 'model-catalog'):
        try:saved[key] = get('system', key)
        except KeyError:pass
        delete('system', key)
    revisions = {item['id'] for item in all_items('model_catalog')}
    pid = 'p_' + uid()
    put('project', {'name': '模型选择测试', 'created_at': now()}, pid)
    monkeypatch.setenv('ATLAS_AUTH_MODE', 'token')
    monkeypatch.delenv('ATLAS_OPERATOR_TOKEN', raising=False)
    monkeypatch.setattr(api, '_requests', {})
    yield pid, TestClient(api.app)
    for kind in ('turn', 'conversation'):
        for item in all_items(kind):
            if item.get('project_id') == pid:delete(kind, item['id'])
    delete('project', pid)
    for item in all_items('model_catalog'):
        if item['id'] not in revisions:delete('model_catalog', item['id'])
    for key in ('model-provider', 'model-catalog'):
        delete('system', key)
        if key in saved:put('system', saved[key], key)


def configuration(suffix=''):
    return {'default_model_id': 'finance', 'models': [
        {'id': 'finance', 'name': '财经分析', 'base_url': 'https://finance' + suffix + '.test/v1',
         'model': 'upstream-finance-private', 'api_key': 'FIXTURE-FINANCE-SECRET' + suffix, 'share_data': True},
        {'id': 'fast', 'name': '快速整理', 'base_url': 'https://fast.test/v1',
         'model': 'upstream-fast-private', 'api_key': 'FIXTURE-FAST-SECRET', 'share_data': True}]}


def message(pid, **kwargs):
    return submit(pid, TurnInput(text='按办事处汇总收入', request_id='req-' + uid(), **kwargs))


def test_catalog_exposes_only_business_names_and_encrypted_storage(managed):
    _, client = managed
    catalog.provision(configuration(), verify=False)
    response = client.get('/api/models', headers=H)
    assert response.status_code == 200
    assert response.json() == {'configured': True, 'default_model_id': 'finance',
                              'models': [{'id': 'finance', 'name': '财经分析'}, {'id': 'fast', 'name': '快速整理'}]}
    assert not any(value in response.text for value in ('SECRET', 'upstream', 'base_url', 'engine', 'api_key'))
    stored = get('model_catalog', get('system', 'model-catalog')['revision'])
    assert 'SECRET' not in json.dumps(stored)


@pytest.mark.parametrize('method,path,payload', [
    ('get', '/api/model', None),
    ('put', '/api/model', {'base_url': 'https://forbidden.test/v1', 'model': 'x'}),
    ('get', '/api/projects/{pid}/company-search', None),
    ('delete', '/api/projects/{pid}/company-search', None),
    ('get', '/api/projects/{pid}/work-connectors', None),
    ('post', '/api/projects/{pid}/work-connectors', {}),
])
def test_business_token_cannot_access_operator_configuration(managed, method, path, payload):
    pid, client = managed
    response = client.request(method, path.format(pid=pid), headers=H, **({'json': payload} if payload is not None else {}))
    assert response.status_code == 403
    assert '后台管理员' in response.json()['detail']


def test_operator_needs_both_credentials_and_unconfigured_operator_is_closed(managed, monkeypatch):
    _, client = managed
    operator = 'fixture-operator-token-123456789'
    headers = dict(H, **{'X-Atlas-Operator-Token': operator})
    assert client.get('/api/model', headers=headers).status_code == 403
    monkeypatch.setenv('ATLAS_OPERATOR_TOKEN', operator)
    assert client.get('/api/model', headers={'X-Atlas-Operator-Token': operator}).status_code == 401
    assert client.get('/api/model', headers=headers).status_code == 200
    monkeypatch.setenv('ATLAS_OPERATOR_TOKEN', TOKEN)
    assert client.get('/api/model', headers=dict(H, **{'X-Atlas-Operator-Token': TOKEN})).status_code == 403


def test_model_is_pinned_per_turn_and_session_choice_is_persistent(managed):
    pid, _ = managed
    catalog.provision(configuration(), verify=False)
    first = message(pid, model_id='finance')
    catalog.provision(configuration('changed'), verify=False)
    with provider.task_scope(first['id']):
        assert provider.settings()['base_url'] == 'https://finance.test/v1'
        assert provider.settings()['api_key'] == 'FIXTURE-FINANCE-SECRET'
    next_turn = message(pid)
    with provider.task_scope(next_turn['id']):
        assert provider.settings()['base_url'] == 'https://financechanged.test/v1'
    assert first['model_name'] == '财经分析'
    assert get('conversation', default_session(pid))['model_id'] == 'finance'
    assert thread(pid)['selected_model_id'] == 'finance'
    fast = message(pid, model_id='fast')
    assert fast['model_id'] == 'fast' and message(pid)['model_id'] == 'fast'


def test_legacy_connection_selection_has_immutable_snapshot_without_catalog_migration(managed):
    pid, _ = managed
    original = dict(configuration()['models'][0])
    original.pop('id');original.pop('name')
    put('system', {'secret_config': seal(original)}, 'model-provider')
    first = get('turn', message(pid)['id'])
    snapshot = first['model_catalog_revision']
    assert snapshot and first['model_id'] == 'default'
    with pytest.raises(KeyError):get('system', 'model-catalog')
    assert catalog.owner_configuration()['revision'] is None
    assert 'SECRET' not in json.dumps(get('model_catalog', snapshot))
    # Repeated equivalent selections reuse the immutable encrypted snapshot.
    assert get('turn', message(pid)['id'])['model_catalog_revision'] == snapshot
    changed = dict(original, base_url='https://changed-legacy.test/v1', model='changed-model', api_key='FIXTURE-CHANGED-SECRET')
    put('system', {'secret_config': seal(changed)}, 'model-provider')
    with provider.task_scope(first['id']):assert provider.settings() == original
    assert provider.settings() == changed
    assert get('turn', message(pid)['id'])['model_catalog_revision'] != snapshot
    assert catalog.select(resume_turn=first)['model_catalog_revision'] == snapshot


def test_runtime_startup_preserves_frozen_legacy_resume_model(managed, monkeypatch, tmp_path):
    from agent_service import opencode_runtime
    pid, _ = managed
    original = dict(configuration()['models'][0])
    original.pop('id');original.pop('name')
    put('system', {'secret_config': seal(original)}, 'model-provider')
    turn = message(pid)
    patch('turn', turn['id'], state='done', resume_available=True)
    changed = dict(original, base_url='https://new-legacy.test/v1', model='new-model', api_key='FIXTURE-NEW-SECRET')
    put('system', {'secret_config': seal(changed)}, 'model-provider')
    monkeypatch.setenv('ATLAS_OPENCODE_BINARY', sys.executable)
    monkeypatch.setattr(opencode_runtime, '_root', lambda: tmp_path / 'runtime')
    opencode_runtime.prepare(catalog.owner_configuration(), {'type': 'object'})
    runtime_config = json.loads((tmp_path / 'runtime/config/opencode/opencode.json').read_text())
    providers = runtime_config['providers']
    assert providers[opencode_runtime._identity(original)]['settings']['baseURL'] == original['base_url']
    assert providers[opencode_runtime._identity(changed)]['settings']['baseURL'] == changed['base_url']
    assert runtime_config['model'] == opencode_runtime._identity(changed) + '/' + changed['model']


def test_unversioned_legacy_resume_cannot_select_current_model_silently(managed):
    with pytest.raises(ValueError, match='缺少模型配置版本'):
        catalog.select(resume_turn={'model_id': 'default', 'model_name': '默认模型', 'model_catalog_revision': None})


def test_no_legacy_connection_retains_empty_selection(managed):
    assert catalog.select() == {}


def test_retry_cannot_change_accepted_model_and_unknown_id_is_not_accepted(managed):
    pid, client = managed
    catalog.provision(configuration(), verify=False)
    body = {'text': '测试', 'model_id': 'finance', 'request_id': 'retry-' + uid()}
    path = '/api/projects/' + pid + '/messages'
    first = client.post(path, headers=H, json=body).json()
    second = client.post(path, headers=H, json=dict(body, model_id='fast')).json()
    assert second['id'] == first['id'] and second['model_id'] == 'finance'
    invalid = dict(body, request_id='invalid-' + uid(), model_id='not-offered')
    assert client.post(path, headers=H, json=invalid).status_code == 400
    assert client.get('/api/projects/' + pid + '/message-status', headers=H,
                      params={'request_id': invalid['request_id']}).json() == {'accepted': False}


def test_removed_model_stops_pinned_calls_without_silent_fallback_and_cancel_still_works(managed):
    pid, _ = managed
    catalog.provision(configuration(), verify=False)
    first = message(pid, model_id='finance')
    changed = configuration()
    changed['default_model_id'] = 'fast'
    changed['models'] = changed['models'][1:]
    catalog.provision(changed, verify=False)
    with provider.task_scope(first['id']), pytest.raises(ValueError, match='停用'):
        provider.settings()
    stopped = submit(pid, TurnInput(text='停止', model_id='finance', request_id='stop-' + uid()))
    assert stopped['state'] == 'done'
    assert get('turn', first['id'])['outcome'] == 'cancelled'


def test_resume_reuses_original_revision_and_rejects_model_swap(managed):
    pid, _ = managed
    catalog.provision(configuration(), verify=False)
    first = get('turn', message(pid, model_id='finance')['id'])
    selection = catalog.select(resume_turn=first)
    assert selection['model_catalog_revision'] == first['model_catalog_revision']
    with pytest.raises(ValueError, match='原任务'):
        catalog.select('fast', resume_turn=first)


def test_missing_catalog_revision_never_falls_back_to_another_connection(managed):
    pid, _ = managed
    catalog.provision(configuration(), verify=False)
    first = message(pid, model_id='finance')
    delete('model_catalog', get('system', 'model-catalog')['revision'])
    with pytest.raises(ValueError, match='目录版本不可用'):catalog.public_catalog()
    with provider.task_scope(first['id']), pytest.raises(ValueError, match='目录版本不可用'):
        provider.settings()


@pytest.mark.parametrize('injected', [{'api_key': 'FIXTURE-INJECTED-SECRET'},
                                    {'engine': 'opencode'},
                                    {'base_url': 'https://attacker.test/v1'}])
def test_business_message_cannot_inject_provider_configuration(managed, injected):
    pid, client = managed
    catalog.provision(configuration(), verify=False)
    body = dict(text='分析', request_id='injection-' + uid(), model_id='finance', **injected)
    response = client.post('/api/projects/' + pid + '/messages', headers=H, json=body)
    assert response.status_code == 422 and 'INJECTED' not in response.text


def test_catalog_publishes_atomically_and_identical_restart_does_not_probe_again(managed, monkeypatch):
    _, _client = managed
    probes = []
    def validate(value, old=None):
        probes.append(value.model)
        assert value.tool_protocol == 'native'
        raw = value.model_dump()
        raw['api_key'] = value.api_key.get_secret_value()
        return raw
    monkeypatch.setattr(provider, 'validate_configuration', validate)
    catalog.provision(configuration())
    revision = get('system', 'model-catalog')['revision']
    assert len(probes) == 2
    catalog.provision(configuration())
    assert len(probes) == 2 and get('system', 'model-catalog')['revision'] == revision
    def failing(value, old=None):
        if value.model == 'upstream-fast-private':raise ValueError('failed fixture')
        return validate(value, old)
    monkeypatch.setattr(provider, 'validate_configuration', failing)
    with pytest.raises(ValueError):catalog.provision(configuration('new'))
    assert get('system', 'model-catalog')['revision'] == revision
    assert provider.settings()['base_url'] == 'https://finance.test/v1'


@pytest.mark.parametrize('change', [
    lambda data: data.update(default_model_id='missing'),
    lambda data: data['models'].append(data['models'][0]),
    lambda data: data['models'][0].update(id='../../escape'),
    lambda data: data['models'][0].update(unknown='FIXTURE-SECRET'),
    lambda data: data['models'][0].update(engine='opencode'),
    lambda data: data['models'][0].update(name='不可用\n名称'),
])
def test_invalid_catalog_does_not_expose_secret_or_publish(managed, change):
    data = configuration()
    change(data)
    with pytest.raises(ValueError) as error:catalog.provision(data)
    assert 'FIXTURE' not in str(error.value)
    with pytest.raises(KeyError):get('system', 'model-catalog')
