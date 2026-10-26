"""Verify the HTTP request and managed runtime use the same compatibility rule."""
import json

import httpx
import pytest

from agent_service import opencode_runtime, provider


@pytest.mark.parametrize('base_url,model,disabled', [
    ('https://api.deepseek.com', 'deepseek-v4-pro', True),
    ('https://API.DEEPSEEK.COM:443/v1', 'deepseek-flash', True),
    ('https://api.deepseek.com/v1', 'deepseek-chat', True),
    ('https://other.example/v1', 'deepseek-v4-pro', False),
    ('https://api.deepseek.com.attacker.example/v1', 'deepseek-v4-pro', False),
    ('https://api.deepseek.com:8443/v1', 'deepseek-v4-pro', False),
    ('https://api.deepseek.com/v1', 'different-model', False),
])
@pytest.mark.parametrize('native_tools', [False, True])
def test_bounded_http_requests_preserve_tools_and_scope_thinking_override(
        monkeypatch, base_url, model, disabled, native_tools):
    requests = []
    tools = [{'type': 'function', 'function': {'name': 'connection_check',
              'parameters': {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                             'required': ['ok']}}}]

    def respond(request):
        requests.append(json.loads(request.content))
        message = {'content': 'OK'}
        if native_tools:
            message = {'content': None, 'tool_calls': [{'id': 'call-test', 'type': 'function',
                       'function': {'name': 'connection_check', 'arguments': '{"ok":true}'}}]}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': message}]})

    client = httpx.Client
    monkeypatch.setattr(provider.httpx, 'Client',
                        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr('agent_service.network_policy.check_url', lambda value: None)
    monkeypatch.setattr('agent_service.quotas.reserve', lambda amount: None)
    monkeypatch.setattr('agent_service.store.put', lambda *args, **kwargs: None)
    monkeypatch.setattr('agent_service.store.all_items', lambda kind: [])
    monkeypatch.delenv('ATLAS_MODEL_SOURCE_ID', raising=False)
    messages = [{'role': 'user', 'content': 'Check the connection.'}]
    result = provider.complete(messages, max_tokens=128, configuration={
        'base_url': base_url, 'model': model, 'api_key': 'test-only-key'},
        tools=tools if native_tools else None)

    expected = {'model': model, 'messages': messages, 'max_tokens': 128, 'temperature': 0}
    if native_tools:
        expected.update(tools=tools, tool_choice='required', parallel_tool_calls=False)
        assert json.loads(result) == {'kind': 'tool', 'tool': 'connection_check', 'arguments': {'ok': True}}
    else:
        assert result == 'OK'
    if disabled:
        expected['thinking'] = {'type': 'disabled'}
    assert requests == [expected]


def test_managed_opencode_uses_deepseek_override_without_changing_other_provider(monkeypatch, tmp_path):
    root = tmp_path / 'runtime'
    binary = tmp_path / 'opencode'
    binary.write_bytes(b'not-executed')
    monkeypatch.setenv('ATLAS_OPENCODE_BINARY', str(binary))
    monkeypatch.setattr(opencode_runtime, '_root', lambda: root)
    monkeypatch.setattr('agent_service.store.all_items', lambda kind: [])
    monkeypatch.setattr('agent_service.network_policy.check_url', lambda value: None)
    entries = [
        {'id': 'deepseek', 'name': 'DeepSeek', 'base_url': 'https://api.deepseek.com/v1',
         'model': 'deepseek-v4-pro', 'api_key': 'test-only-key'},
        {'id': 'other', 'name': 'Other', 'base_url': 'https://other.example/v1',
         'model': 'deepseek-v4-pro', 'api_key': 'test-only-key'},
    ]
    opencode_runtime.prepare({'models': entries, 'default_model_id': 'deepseek'}, {'type': 'object'})
    config = json.loads((root / 'config/opencode/opencode.json').read_text())
    bodies = {value['settings']['baseURL']: value['body'] for value in config['providers'].values()}
    assert bodies == {
        'https://api.deepseek.com/v1': {'max_tokens': 393216, 'thinking': {'type': 'disabled'}},
        'https://other.example/v1': {'max_tokens': 3200},
    }
    managed=next(value for value in config['providers'].values() if value['settings']['baseURL']=='https://api.deepseek.com/v1')
    assert managed['models']['deepseek-v4-pro']['limit']=={'context':1048576,'output':393216}
    assert config['compaction']['auto'] is True
