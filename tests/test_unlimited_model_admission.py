"""Large real-bridge/HTTP admissions do not hit the retired interactive defaults."""
import json
import httpx
import pytest
from agent_service import agent,provider,opencode_runtime as runtime,workspace
from agent_service.store import get,patch
from agent_service.task_budget import LEGACY_DEFAULTS
from test_opencode_agent_bridge import turn
from test_opencode_inflight_stop import native_session


def historical_usage(turn):
    patch('turn',turn['id'],task_contract={**turn['task_contract'],'budgets':dict(LEGACY_DEFAULTS)},
          model_usage={'calls':90,'input_characters':4000000,'output_tokens_budgeted':200000,
                       'duration_ms':86400000})
    return get('turn',turn['id'])


def test_native_bridge_accepts_large_request_after_old_totals_without_resetting_counters(turn,monkeypatch):
    monkeypatch.delenv('ATLAS_OPENCODE_MODEL_INPUT_CHAR_LIMITS',raising=False)
    monkeypatch.delenv('MAX_DAILY_MODEL_CALLS',raising=False)
    monkeypatch.delenv('MAX_DAILY_RESERVED_TOKENS',raising=False)
    current=historical_usage(turn)
    def during_context(bridge,state):
        reply=bridge({'op':'model_request','kind':'generation','input_characters':350000,
            'max_output_tokens':8192,'target_url':'https://fixture.test/v1/chat/completions','model_id':'fixture'})
        assert reply=={'ok':True,'max_output_tokens':8192}
        bridge({'op':'step','message_id':'write','call_id':'one',
            'step':{'kind':'tool','tool':'write_file','arguments':{'path':'reports/native.md','content':'actual receipt'}}})
        bridge({'op':'step','message_id':'finish','call_id':'two',
            'step':{'kind':'final','answer':'已保存','status':'complete'}})
    state=native_session(monkeypatch,during_context)
    result=runtime.run_session(current,'Synthetic large request',{},lambda step,call:agent.opencode_step(turn['id'],step,call))
    usage=get('turn',turn['id'])['model_usage']
    assert result['state']=='done' and usage['calls']==91
    assert usage['input_characters']==4350000 and usage['duration_ms']>=86400000
    assert usage['output_tokens_budgeted']==208192
    assert workspace.read_file(turn['project_id'],'reports/native.md')=='actual receipt'
    assert not state['model_calls']


def test_legacy_http_sends_over_80000_characters_without_a_default_task_cap(turn,monkeypatch):
    monkeypatch.delenv('ATLAS_OPENCODE_MODEL_INPUT_CHAR_LIMITS',raising=False)
    current=historical_usage(turn);sent=[]
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'OK'}}],
            'usage':{'completion_tokens':2}})
    client=httpx.Client
    monkeypatch.setattr(provider.httpx,'Client',lambda **kw:client(transport=httpx.MockTransport(respond),**kw))
    monkeypatch.setattr('agent_service.network_policy.check_url',lambda value:None)
    with provider.task_scope(turn['id']):
        assert provider.complete([{'role':'user','content':'x'*90000}])=='OK'
    assert len(sent)==1 and len(sent[0]['messages'][0]['content'])==90000
    assert get('turn',turn['id'])['model_usage']['calls']==91


def test_explicit_bounded_native_allowance_is_clipped_to_remaining_output(turn,monkeypatch):
    monkeypatch.delenv('ATLAS_OPENCODE_MODEL_INPUT_CHAR_LIMITS',raising=False)
    contract={**turn['task_contract'],'budget_policy':'bounded','budgets':dict(LEGACY_DEFAULTS,output_tokens=5000)}
    patch('turn',turn['id'],task_contract=contract)
    def during_context(bridge,state):
        reply=bridge({'op':'model_request','kind':'generation','input_characters':10000,
            'max_output_tokens':393216,'target_url':'https://fixture.test/v1/chat/completions','model_id':'fixture'})
        assert reply=={'ok':True,'max_output_tokens':5000}
        patch('turn',turn['id'],state='done')
        bridge({'op':'step','message_id':'end','call_id':'end','step':{'kind':'final','answer':'end'}})
    native_session(monkeypatch,during_context)
    assert runtime.run_session(get('turn',turn['id']),'Scoped fixture',{},lambda *args:None)['state']=='done'
    assert get('turn',turn['id'])['model_usage']['output_tokens_budgeted']==5000


def test_native_history_transport_covers_large_context_but_explicit_byte_guards_still_apply(monkeypatch):
    body={'data':[{'type':'user','content':'x'*2100000}]}
    client=httpx.Client
    def response(request):return httpx.Response(200,json=body)
    monkeypatch.setattr(runtime.httpx,'Client',lambda **kw:client(transport=httpx.MockTransport(response),**kw))
    value={'base_url':'http://127.0.0.1:1234','api_key':'synthetic-local-auth'}
    assert runtime._request(value,'GET','/api/session/synthetic/context')==body
    with pytest.raises(ValueError,match='会话上下文超过读取限制'):
        runtime._request(value,'GET','/api/session/synthetic/context',limit=1000)
