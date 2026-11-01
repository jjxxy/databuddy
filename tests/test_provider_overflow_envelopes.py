"""Provider-declared input overflow must reach the real recovery path."""
import json

import httpx
import pytest

from agent_service import agent, provider
from agent_service.store import get
from test_context_window_recovery import long_turn
from test_opencode_agent_bridge import turn


@pytest.mark.parametrize('window,expected', [(256, 64), (1024, 256), (3200, 800), (4096, 3200)])
def test_small_configured_context_keeps_output_reservation_within_window(window, expected):
    from agent_service.provider_compatibility import response_token_allowance
    value = {'base_url': 'https://fixture.test/v1', 'model': 'fixture', 'context_window_tokens': window}
    assert response_token_allowance(value) == expected
    assert response_token_allowance(value) < window


@pytest.mark.parametrize('limits,expected', [
    ({'context_window_tokens': 1024}, 256),
    ({'context_window_tokens': 4096, 'max_output_tokens': 500}, 500),
    ({'max_output_tokens': 300}, 300),
    ({}, 1800),
])
def test_explicit_request_output_respects_known_model_limits(monkeypatch, limits, expected):
    client = httpx.Client
    sent = []
    monkeypatch.setattr('agent_service.network_policy.check_url', lambda url: None)
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': 'ok'}}]})
    monkeypatch.setattr(provider.httpx, 'Client', lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    assert provider.complete([{'role': 'user', 'content': 'hello'}], 1800,
                             {'base_url': 'https://fixture.test/v1', 'model': 'fixture', **limits}) == 'ok'
    assert sent[0]['max_tokens'] == expected


@pytest.mark.parametrize('status,error,expected', [
    (500, {'code': 'context_length_exceeded'}, True),
    (500, {'code': 500, 'name': 'ContextOverflowError'}, True),
    (503, {'data': {'code': 'input_token_limit_exceeded'}}, True),
    (500, {'code': 'server_error', 'message': 'context length exceeded'}, False),
    (429, {'code': 'context_length_exceeded'}, False),
    (401, {'code': 'context_length_exceeded'}, False),
])
def test_only_explicit_server_error_codes_trigger_5xx_compaction(status, error, expected):
    request = httpx.Request('POST', 'https://fixture.test/v1/chat/completions')
    response = httpx.Response(status, request=request, json={'error': error})
    assert provider.context_overflow_error(httpx.HTTPStatusError('error', request=request, response=response)) is expected


@pytest.mark.parametrize('error', [
    {'code': 'context_length_exceeded', 'message': 'request too large'},
    {'code': 'input_token_limit_exceeded'},
    {'message': 'Input tokens exceed the maximum limit'},
    'maximum context length exceeded',
])
def test_http_success_error_envelope_recovers_without_replaying_tools(turn, monkeypatch, error):
    current = long_turn(turn)
    sent = []
    client = httpx.Client
    monkeypatch.setattr('agent_service.network_policy.check_url', lambda url: None)

    def respond(request):
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            return httpx.Response(200, json={'error': error})
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content':
            json.dumps({'kind': 'final', 'answer': '仍需核对完整结果', 'status': 'needs_input'})}}]})

    monkeypatch.setattr(provider.httpx, 'Client', lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    with provider.task_scope(current['id']):
        result = agent.plan(current)
    assert result.kind == 'final' and len(sent) == 2
    assert len(json.dumps(sent[1], ensure_ascii=False)) < len(json.dumps(sent[0], ensure_ascii=False))
    saved = get('turn', current['id'])
    assert saved['context_window']['recoveries'] == 1
    assert saved['agent_trace'] == current['agent_trace']


@pytest.mark.parametrize('error', [
    {'code': 'rate_limit_error', 'message': 'context length exceeded'},
    {'code': 'insufficient_quota', 'message': 'context length exceeded'},
    {'code': 'invalid_api_key', 'message': 'context length exceeded'},
    {'code': 'other_error', 'message': 'input tokens are tracked for billing'},
])
def test_non_window_http_success_error_is_not_misclassified(monkeypatch, error):
    client = httpx.Client
    monkeypatch.setattr('agent_service.network_policy.check_url', lambda url: None)
    monkeypatch.setattr(provider.httpx, 'Client', lambda **kwargs: client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'error': error})), **kwargs))
    with pytest.raises(provider.ModelServiceError) as caught:
        provider.complete([{'role': 'user', 'content': 'hello'}], 128,
                          {'base_url': 'https://fixture.test/v1', 'model': 'fixture', 'api_key': ''})
    assert not isinstance(caught.value, provider.ContextWindowExceeded)
