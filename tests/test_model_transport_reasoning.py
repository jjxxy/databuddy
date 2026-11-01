"""Slow reasoning streams must not execute a partial tool call."""
import json
import math
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

from agent_service import model_transport, provider, opencode
from agent_service.store import patch
from test_opencode_agent_bridge import turn
# Load modules before tests patch store.get: modules that bind ``get`` at import
# must keep the real implementation after a test restores the monkeypatch.
from agent_service import (agent_gateway, agent_team, collaboration_budget,
                           company_search, agent_mesh, task_budget, workspace_lifecycle)


def _setup(monkeypatch):
    monkeypatch.setattr('agent_service.network_policy.check_url',lambda url:None)
    monkeypatch.setattr('agent_service.quotas.reserve',lambda amount:None)
    monkeypatch.setattr('agent_service.store.put',lambda *args,**kwargs:None)
    monkeypatch.setattr('agent_service.store.all_items',lambda kind:[])


def _event(delta, reason=None):
    return ('data: '+json.dumps({'choices':[{'index':0,'delta':delta,'finish_reason':reason}]})+'\n\n').encode()


def _tools():
    return [{'type':'function','function':{'name':'inspect','parameters':{'type':'object'}}}]


class Server:
    def __init__(self,pieces):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200);self.send_header('Content-Type','text/event-stream')
                self.send_header('Transfer-Encoding','chunked');self.end_headers()
                try:
                    for delay,piece in pieces:
                        time.sleep(delay)
                        self.wfile.write(('%x\r\n'%len(piece)).encode()+piece+b'\r\n');self.wfile.flush()
                    self.wfile.write(b'0\r\n\r\n');self.wfile.flush()
                except (BrokenPipeError,ConnectionResetError):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def __enter__(self):return f'http://127.0.0.1:{self.server.server_port}/v1'
    def __exit__(self,*args):self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)


def _call(url):
    return provider.complete([{'role':'user','content':'hi'}],128,{'base_url':url,'model':'fixture'},tools=_tools())


def test_separate_connection_and_reasoning_inactivity_timeout(monkeypatch):
    monkeypatch.delenv('ATLAS_MODEL_READ_TIMEOUT_SECONDS',raising=False)
    t=model_transport.model_http_timeout()
    assert t.connect==15 and t.read==900 and t.write==60 and t.pool==15
    monkeypatch.setenv('ATLAS_MODEL_READ_TIMEOUT_SECONDS','900')
    assert model_transport.model_http_timeout().read==900
    for value in ['0','7201','nan','inf','invalid']:
        monkeypatch.setenv('ATLAS_MODEL_READ_TIMEOUT_SECONDS',value)
        with pytest.raises(ValueError,match='配置'):model_transport.model_http_timeout()


def test_thinking_sse_heartbeat_and_fragmented_tool_call(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(model_transport,'model_http_timeout',lambda:httpx.Timeout(connect=1,read=.4,write=1,pool=1))
    first={'index':0,'id':'call_1','type':'function','function':{'name':'inspect','arguments':'{"source":'}}
    second={'index':0,'function':{'arguments':'"test"}'}}
    pieces=[(.10,b': ping\n\n'),(.10,b'data: \n\n'),(.10,_event({'reasoning_content':'thinking'})),
            (.10,_event({'tool_calls':[first]})),(.10,_event({'tool_calls':[second]})),
            (.10,_event({},'tool_calls')),(0,b'data: [DONE]\n\n')]
    with Server(pieces) as url:
        result=json.loads(_call(url))
    assert result=={'kind':'tool','tool':'inspect','arguments':{'source':'test'}}


def test_stalled_stream_timeout_is_transport_error_not_success(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(model_transport,'model_http_timeout',lambda:httpx.Timeout(connect=1,read=.05,write=1,pool=1))
    first={'index':0,'id':'call_1','type':'function','function':{'name':'inspect','arguments':'{}'}}
    with Server([(0,_event({'tool_calls':[first]})),(.20,_event({},'tool_calls'))]) as url:
        with pytest.raises(httpx.ReadTimeout):_call(url)


def test_incomplete_thinking_stream_never_yields_a_tool(monkeypatch):
    _setup(monkeypatch)
    first={'index':0,'id':'call_1','type':'function','function':{'name':'inspect','arguments':'{}'}}
    with Server([(0,_event({'reasoning_content':'thinking'})),(0,_event({'tool_calls':[first]}))]) as url:
        with pytest.raises(provider.ModelOutputError):_call(url)


def test_reasoning_events_do_not_limit_tool_calls_to_ten_thousand():
    # Heartbeats / reasoning are not model answers or executable tool calls.
    data=_event({'reasoning_content':'x'})*10001+_event({'content':'ok'})+_event({},'stop')+b'data: [DONE]\n\n'
    payload=provider._stream_response_payload(data)
    assert payload['choices'][0]['message']['content']=='ok'


def test_stateless_opencode_only_generation_uses_model_timeout(monkeypatch):
    requests=[]
    def fake_request(value,method,path,body=None,**kw):
        requests.append((path,kw))
        return {'data':{'text':'ok'}}
    monkeypatch.setattr(opencode,'request',fake_request)
    monkeypatch.setattr(opencode,'inspect',lambda value:{'opencode_fingerprint':'fixed'})
    result=opencode.complete([],{'model':'p/m','opencode_fingerprint':'fixed'},None,128)
    assert result[0]=='ok' and requests[0][0]=='/api/experimental/generate'
    assert requests[0][1]['timeout'].read==900


def test_sse_byte_boundaries_crlf_multiline_usage_and_reasoning():
    # Deliberately split Chinese UTF-8 bytes, event delimiters and JSON fields.
    data=(b': keep-alive\r\n\r\n'
          +('data: {"choices":[\r\ndata: {"index":0,"delta":{"reasoning_content":"内部推理"},"finish_reason":null}]}\r\n\r\n').encode()
          +_event({'content':'结果'})
          +_event({},'stop')
          +b'data: {"choices":[],"usage":{"completion_tokens":9}}\n\n'
          +b'data: [DONE]\n\n')
    parser=provider._SSEParser()
    for byte in data:parser.feed(bytes([byte]))
    result=parser.result()
    assert result['choices'][0]['message']['content']=='结果'
    assert result['usage']=={'completion_tokens':9}
    assert '内部推理' not in json.dumps(result,ensure_ascii=False)


def test_sse_accepts_long_reasoning_without_accumulating_raw(monkeypatch):
    _setup(monkeypatch)
    client=httpx.Client
    thinking=_event({'reasoning_content':'secret-thinking'*1000})
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b': keep-alive\n\n'
            for _ in range(400):yield thinking # > 4 MiB discarded incrementally
            yield _event({'content':'ok'})
            yield _event({},'stop')
            yield b'data: [DONE]\n\n'
    monkeypatch.setattr(provider.httpx,'Client',lambda **kwargs:client(transport=httpx.MockTransport(
        lambda req:httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Stream())),**kwargs))
    assert provider.complete([{'role':'user','content':'hi'}],128,{'base_url':'https://fixture/v1','model':'fixture'})=='ok'


def test_nonstream_whitespace_heartbeats_do_not_accumulate(monkeypatch):
    _setup(monkeypatch)
    client=httpx.Client
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(80):yield b' \r\n'*65536 # > 4 MiB blank keepalives
            yield json.dumps({'choices':[{'message':{'content':'ok'},'finish_reason':'stop'}]}).encode()
    monkeypatch.setattr(provider.httpx,'Client',lambda **kwargs:client(transport=httpx.MockTransport(
        lambda req:httpx.Response(200,headers={'Content-Type':'application/json'},stream=Stream())),**kwargs))
    assert provider.complete([{'role':'user','content':'hi'}],128,{'base_url':'https://fixture/v1','model':'fixture'})=='ok'


def test_missing_sse_content_type_with_first_comment(monkeypatch):
    _setup(monkeypatch)
    client=httpx.Client
    data=b'\n\n: keep-alive\n\n'+_event({'content':'ok'})+_event({},'stop')+b'data: [DONE]\n\n'
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for byte in data:yield bytes([byte])
    monkeypatch.setattr(provider.httpx,'Client',lambda **kwargs:client(transport=httpx.MockTransport(
        lambda req:httpx.Response(200,headers={'Content-Type':'application/json'},stream=Stream())),**kwargs))
    assert provider.complete([{'role':'user','content':'hi'}],128,{'base_url':'https://fixture/v1','model':'fixture'})=='ok'


def test_done_does_not_wait_for_connection_to_close(monkeypatch):
    _setup(monkeypatch)
    client=httpx.Client
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield _event({'content':'ok'})+_event({},'stop')+b'data: [DONE]\n\n'
            raise httpx.ReadTimeout('upstream did not close')
    monkeypatch.setattr(provider.httpx,'Client',lambda **kwargs:client(transport=httpx.MockTransport(
        lambda req:httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Stream())),**kwargs))
    assert provider.complete([{'role':'user','content':'hi'}],128,{'base_url':'https://fixture/v1','model':'fixture'})=='ok'


def test_unfinished_oversized_sse_event_is_rejected():
    parser=provider._SSEParser()
    with pytest.raises(provider.ModelOutputError) as exc:parser.feed(b'data: '+b'a'*(4*1024*1024+1))
    assert exc.value.code=='response_too_large'


def test_two_fragmented_tools_remain_two_not_one():
    a={'index':0,'id':'one','type':'function','function':{'name':'inspect','arguments':'{"x":'}}
    b={'index':1,'id':'two','type':'function','function':{'name':'other','arguments':'{}'}}
    c={'index':0,'function':{'arguments':'1}'}}
    data=_event({'tool_calls':[a,b]})+_event({'tool_calls':[c]})+_event({},'tool_calls')+b'data: [DONE]\n\n'
    message=provider._stream_response_payload(data)['choices'][0]['message']
    assert len(message['tool_calls'])==2 and message['tool_calls'][0]['function']['arguments']=='{"x":1}'


def test_midstream_disconnect_after_partial_tool_never_returns_it(monkeypatch):
    _setup(monkeypatch)
    client=httpx.Client
    first={'index':0,'id':'call_1','type':'function','function':{'name':'inspect','arguments':'{}'}}
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield _event({'tool_calls':[first]})
            raise httpx.ReadError('remote closed unexpectedly')
    monkeypatch.setattr(provider.httpx,'Client',lambda **kwargs:client(transport=httpx.MockTransport(
        lambda req:httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Stream())),**kwargs))
    with pytest.raises(httpx.ReadError):_call('https://fixture/v1')


def test_sse_error_after_fragments_has_no_tool_and_no_secret(monkeypatch):
    _setup(monkeypatch)
    client=httpx.Client
    first={'index':0,'id':'call_1','type':'function','function':{'name':'inspect','arguments':'{}'}}
    data=_event({'tool_calls':[first]})+b'data: {"error":{"code":"rate_limit_exceeded","message":"secret"}}\n\n'
    monkeypatch.setattr(provider.httpx,'Client',lambda **kwargs:client(transport=httpx.MockTransport(
        lambda req:httpx.Response(200,headers={'Content-Type':'text/event-stream'},content=data)),**kwargs))
    with pytest.raises(provider.ModelServiceError) as exc:_call('https://fixture/v1')
    assert 'secret' not in str(exc.value)


def test_stop_after_tool_fragments_is_not_tool_completion(monkeypatch):
    _setup(monkeypatch)
    client=httpx.Client
    first={'index':0,'id':'call_1','type':'function','function':{'name':'inspect','arguments':'{}'}}
    data=_event({'tool_calls':[first]})+_event({},'stop')+b'data: [DONE]\n\n'
    monkeypatch.setattr(provider.httpx,'Client',lambda **kwargs:client(transport=httpx.MockTransport(
        lambda req:httpx.Response(200,headers={'Content-Type':'text/event-stream'},content=data)),**kwargs))
    with pytest.raises(provider.ModelOutputError) as exc:_call('https://fixture/v1')
    assert exc.value.code=='native_invalid_response'


@pytest.mark.parametrize('first',[b'event: message\n',b'id: abc\n',b'retry: 1000\n'])
def test_sniffs_standard_sse_fields_with_missing_content_type(monkeypatch,first):
    _setup(monkeypatch);client=httpx.Client
    data=first+_event({'content':'ok'})+_event({},'stop')+b'data: [DONE]\n\n'
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for byte in data:yield bytes([byte])
    monkeypatch.setattr(provider.httpx,'Client',lambda **kwargs:client(transport=httpx.MockTransport(
        lambda req:httpx.Response(200,headers={'Content-Type':'application/json'},stream=Stream())),**kwargs))
    assert provider.complete([{'role':'user','content':'hi'}],128,{'base_url':'https://fixture/v1','model':'fixture'})=='ok'


def test_many_empty_content_events_do_not_accumulate():
    parser=provider._SSEParser()
    for _ in range(20000):parser.feed(_event({'content':''}))
    assert parser.content==[] and parser.content_size==0
    parser.feed(_event({'content':'ok'})+_event({},'stop')+b'data: [DONE]\n\n')
    assert parser.result()['choices'][0]['message']['content']=='ok'


def test_active_cancelled_task_aborts_heartbeat_stream(monkeypatch,turn):
    _setup(monkeypatch);client=httpx.Client
    fake_time=[0.0]
    monkeypatch.setattr(provider.time,'monotonic',lambda:fake_time[0])
    monkeypatch.setattr('agent_service.workspace_lifecycle.assert_turn_live',lambda turn:None)
    monkeypatch.setattr(provider,'assert_turn_live',lambda turn:None)
    monkeypatch.setattr('agent_service.agent_gateway.assert_turn_allowed',lambda turn:None)
    monkeypatch.setattr('agent_service.agent_team.assert_turn_allowed',lambda turn:None)
    monkeypatch.setattr('agent_service.agent_gateway.admit_model_request',lambda *a,**kw:None)
    monkeypatch.setattr('agent_service.collaboration_budget.admit',lambda *a,**kw:None)
    monkeypatch.setattr('agent_service.company_search.assert_model_allowed',lambda pid:None)
    monkeypatch.setattr('agent_service.agent_mesh.assert_project_model_allowed',lambda pid:None)
    first={'index':0,'id':'call_1','type':'function','function':{'name':'inspect','arguments':'{}'}}
    closed=[]
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b': keep-alive\n\n'
            patch('turn',turn['id'],state='done');fake_time[0]=3
            yield _event({'tool_calls':[first]})+_event({},'tool_calls')+b'data: [DONE]\n\n'
        def close(self):closed.append(True)
    monkeypatch.setattr(provider.httpx,'Client',lambda **kwargs:client(transport=httpx.MockTransport(
        lambda req:httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Stream())),**kwargs))
    token=provider._scope.set(turn['id'])
    try:
        with pytest.raises(httpx.ReadError,match='已停止'):_call('https://fixture/v1')
        assert closed==[True]
    finally:provider._scope.reset(token)


def test_revoked_gateway_stops_on_next_heartbeat(monkeypatch,turn):
    _setup(monkeypatch);client=httpx.Client
    fake_time=[0.0]
    allowed=[True]
    def guard(turn):
        if not allowed[0]:raise ValueError('任务授权已撤销')
    monkeypatch.setattr(provider.time,'monotonic',lambda:fake_time[0])
    monkeypatch.setattr(provider,'assert_turn_live',lambda turn:None)
    monkeypatch.setattr('agent_service.agent_gateway.assert_turn_allowed',guard)
    monkeypatch.setattr('agent_service.agent_team.assert_turn_allowed',lambda turn:None)
    monkeypatch.setattr('agent_service.agent_gateway.admit_model_request',lambda *a,**kw:None)
    monkeypatch.setattr('agent_service.collaboration_budget.admit',lambda *a,**kw:None)
    monkeypatch.setattr('agent_service.company_search.assert_model_allowed',lambda pid:None)
    monkeypatch.setattr('agent_service.agent_mesh.assert_project_model_allowed',lambda pid:None)
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b': keep-alive\n\n'
            allowed[0]=False;fake_time[0]=3
            yield _event({'content':'private late answer'})+_event({},'stop')+b'data: [DONE]\n\n'
    monkeypatch.setattr(provider.httpx,'Client',lambda **kwargs:client(transport=httpx.MockTransport(
        lambda req:httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Stream())),**kwargs))
    token=provider._scope.set(turn['id'])
    try:
        with pytest.raises(ValueError,match='撤销'):_call('https://fixture/v1')
    finally:provider._scope.reset(token)
