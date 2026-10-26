"""Real bridge callbacks win races with an already in-flight idle poll."""
import json
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from agent_service import agent, opencode_runtime as runtime, provider, tasks, workspace
from agent_service.store import get
from test_opencode_agent_bridge import turn


def native_session(monkeypatch, during_context):
    """Fake only OpenCode; retain the real HTTP bridge, state and admission."""
    state = {'metadata': None, 'in_context': False, 'events': [], 'replies': [],
             'model_calls': [], 'interrupts': 0, 'context_reads': 0}
    monkeypatch.setattr(runtime, 'require_ready', lambda: {'ready': True})
    monkeypatch.setattr(runtime, 'connection', lambda *args: {'base_url': 'http://127.0.0.1:1',
        'api_key': 'synthetic-local-service-auth', 'model_ref': {'providerID': 'fixture', 'id': 'fixture'}})
    def no_model(*args, **kwargs):
        state['model_calls'].append(True)
        raise AssertionError('This regression must not call a model')
    monkeypatch.setattr(provider, 'complete', no_model)

    def bridge(payload):
        assert state['in_context'], 'The bridge callback must occur inside the context request'
        metadata = state['metadata']['atlas_bridge']
        request = Request(metadata['url'], data=json.dumps({'session_id': 'ses_race', **payload}).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + metadata['token']}, method='POST')
        with build_opener(ProxyHandler({})).open(request, timeout=10) as response:
            reply = json.loads(response.read())
        state['replies'].append(reply)
        state['events'].append('bridge-response')
        return reply

    def request(value, method, path, body=None, **kwargs):
        if method == 'POST' and path == '/api/session':
            state['metadata'] = body['metadata']
            return {'data': {'id': 'ses_race'}}
        if method == 'POST' and path.endswith('/prompt'):return {'data': {'accepted': True}}
        if method == 'POST' and path.endswith('/interrupt'):
            state['interrupts'] += 1
            return {'data': {}}
        if method == 'GET' and path.endswith('/context'):
            state['context_reads'] += 1
            assert state['context_reads'] == 1, 'The first idle poll must settle this regression'
            state['in_context'] = True;state['events'].append('context-start')
            try:
                during_context(bridge, state)
                return {'data': [{'type': 'idle'}]}
            finally:
                state['events'].append('context-end');state['in_context'] = False
        raise AssertionError('Unexpected native request: ' + method + ' ' + path)
    monkeypatch.setattr(runtime, '_request', request)
    return state


def test_model_budget_rejection_during_context_poll_preserves_original_budget_error(turn, monkeypatch):
    monkeypatch.setenv('ATLAS_OPENCODE_MODEL_INPUT_CHAR_LIMITS','{"fixture":80000}')
    callbacks = []
    def during_context(bridge, state):
        reply = bridge({'op': 'model_request', 'kind': 'generation',
            'input_characters': 80001,
            'target_url': 'https://fixture.test/v1/chat/completions', 'model_id': 'fixture'})
        assert '管理员配置的上下文字符上限' in reply['error']
    state = native_session(monkeypatch, during_context)
    with pytest.raises(provider.BudgetExceeded, match='管理员配置的上下文字符上限'):
        runtime.run_session(turn, 'Synthetic bounded task', {}, lambda *args: callbacks.append(args))
    assert state['events'] == ['context-start', 'bridge-response', 'context-end']
    assert state['interrupts'] == 1 and not state['model_calls'] and not callbacks
    assert get('turn', turn['id']).get('model_usage', {}).get('calls', 0) == 0
    assert not workspace.path_for(turn['project_id'], 'reports/native.md').exists()


def test_validated_completion_during_context_poll_is_not_replaced_with_idle_failure(turn, monkeypatch):
    receipt = 'durable synthetic report'
    def during_context(bridge, state):
        write = bridge({'op': 'step', 'message_id': 'msg_write', 'call_id': 'call_write',
            'step': {'kind': 'tool', 'tool': 'write_file',
                     'arguments': {'path': 'reports/native.md', 'content': receipt}}})
        assert write['state'] == 'agent' and not write['terminal']
        final = bridge({'op': 'step', 'message_id': 'msg_final', 'call_id': 'call_final',
            'step': {'kind': 'final', 'status': 'complete', 'answer': '报告已保存'}})
        assert final['terminal'] and final['state'] == 'done'
    state = native_session(monkeypatch, during_context)
    result = runtime.run_session(turn, 'Create a synthetic report', {},
                                lambda step, call: agent.opencode_step(turn['id'], step, call))
    current = get('turn', turn['id'])
    assert result['state'] == 'done' and current['outcome'] == 'verified_complete'
    assert tasks.verify(current)['verified']
    assert workspace.read_file(turn['project_id'], 'reports/native.md') == receipt
    assert len(current['agent_trace']) == 1 and current['agent_trace'][0]['status'] == 'done'
    assert state['events'] == ['context-start', 'bridge-response', 'bridge-response', 'context-end']
    assert state['interrupts'] == 1 and not state['model_calls']


def test_unverified_idle_without_a_concurrent_stop_still_fails_closed(turn, monkeypatch):
    state = native_session(monkeypatch, lambda bridge, state: None)
    with pytest.raises(provider.ModelOutputError, match='尚未提交通过验收的结果'):
        runtime.run_session(turn, 'Do not accept unverified free text', {},
                            lambda *args: pytest.fail('Idle polling must not invent a tool callback'))
    current = get('turn', turn['id'])
    assert current['state'] == 'agent' and not current.get('verification', {}).get('verified')
    assert not workspace.path_for(turn['project_id'], 'reports/native.md').exists()
    assert state['events'] == ['context-start', 'context-end']
    assert state['interrupts'] == 1 and not state['model_calls']
