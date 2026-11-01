"""A legacy routing 429 waits in the durable scheduler, not in the worker."""
import json

import pytest

from agent_service import conversation, provider
from agent_service.store import get, patch
from test_model_plan_recovery import choice, model
from test_opencode_agent_bridge import turn


@pytest.fixture
def legacy_turn(turn, model, monkeypatch):
    config, requests, responses = model
    monkeypatch.setattr(conversation, 'LLM_BASE', config['base_url'])
    monkeypatch.setattr(conversation, 'LLM_MODEL', config['model'])
    monkeypatch.setattr(conversation, 'LLM_KEY', config['api_key'])
    # Exercise the actual legacy routing code while preserving tick's durable
    # retry gate and the real provider's HTTP status handling.
    monkeypatch.setattr(conversation, 'handle', conversation.legacy_handle)
    current=patch('turn',turn['id'],state='queued',text='请解释这项数据处理请求')
    return current, requests, responses


def test_429_is_retried_without_blocking_and_original_request_finishes(legacy_turn):
    import time
    turn, requests, responses=legacy_turn
    responses.extend([429,429,choice(json.dumps({'action':'reply','reply':'可以继续处理。'},ensure_ascii=False))])
    first_start=time.time()
    conversation.tick()
    first_end=time.time()
    saved=get('turn',turn['id'])
    assert saved['state']=='queued' and saved['legacy_model_retry_count']==1
    assert first_start+2<=saved['model_retry_at']<=first_end+2
    assert '限流' not in saved.get('answer','') and not saved.get('agent_trace')
    conversation.tick()
    assert len(requests)==1, 'scheduler must not retry before the backoff expires'
    patch('turn',turn['id'],model_retry_at=0)
    second_start=time.time()
    conversation.tick()
    second_end=time.time()
    assert get('turn',turn['id'])['legacy_model_retry_count']==2
    assert second_start+4<=get('turn',turn['id'])['model_retry_at']<=second_end+4
    patch('turn',turn['id'],model_retry_at=0)
    conversation.tick()
    saved=get('turn',turn['id'])
    assert saved['state']=='done' and saved['answer']=='可以继续处理。'
    assert saved['legacy_model_retry_count']==0 and saved['model_retry_at']==0
    assert len(requests)==3
    assert all(json.loads(request['messages'][1]['content'])['request']==turn['text'] for request in requests)


def test_repeated_429_pauses_with_clear_error_and_retains_work(legacy_turn):
    turn, requests, responses=legacy_turn
    responses.extend([429,429,429])
    for _ in range(3):
        conversation.tick()
        if get('turn',turn['id'])['state']!='done':patch('turn',turn['id'],model_retry_at=0)
    saved=get('turn',turn['id'])
    assert len(requests)==3 and saved['state']=='done'
    assert saved['outcome']=='paused' and saved['resume_available']
    assert '429' in saved['answer'] and 'controlled error' not in saved['answer']
    assert not saved.get('agent_trace')


def test_non_429_response_does_not_enter_rate_limit_retry(legacy_turn):
    turn, requests, responses=legacy_turn
    responses.append(401)
    conversation.tick()
    saved=get('turn',turn['id'])
    assert len(requests)==1 and saved['state']=='done'
    assert not saved.get('legacy_model_retry_count') and not saved.get('model_retry_at')
    assert '429' not in saved['answer'] and 'controlled error' not in saved['answer']


def test_cancelled_task_is_not_restarted_after_inflight_429(legacy_turn,monkeypatch):
    turn, requests, responses=legacy_turn
    def cancelled(*_args,**_kwargs):
        import httpx
        patch('turn',turn['id'],state='done',outcome='cancelled',answer='已停止')
        raise httpx.HTTPStatusError('private upstream error',request=httpx.Request('POST','https://fixture.test'),response=httpx.Response(429))
    monkeypatch.setattr(provider,'complete',cancelled)
    conversation.legacy_handle(get('turn',turn['id']))
    saved=get('turn',turn['id'])
    assert saved['state']=='done' and saved['outcome']=='cancelled' and saved['answer']=='已停止'
    assert not saved.get('legacy_model_retry_count')
