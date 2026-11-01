"""OpenAI-compatible SSE gateways must not break the nonstream caller."""
import json
import httpx
import pytest

from agent_service import provider


def sse(*events, done=True):
    result=''.join('data: '+json.dumps(event,ensure_ascii=False)+'\n\n' for event in events)
    return (result+('data: [DONE]\n\n' if done else '')).encode()


def call(monkeypatch, content, *, tools=None, content_type='text/event-stream', status=200, encoding=None):
    requests=[]; client=httpx.Client
    monkeypatch.setattr('agent_service.network_policy.check_url',lambda url:None)
    monkeypatch.setattr('agent_service.quotas.reserve',lambda amount:None)
    monkeypatch.setattr('agent_service.store.put',lambda *args,**kwargs:None)
    monkeypatch.setattr('agent_service.store.all_items',lambda kind:[])
    def respond(request):
        requests.append(json.loads(request.content))
        headers={'Content-Type':content_type}
        if encoding:headers['Content-Encoding']=encoding
        return httpx.Response(status,content=content,headers=headers)
    monkeypatch.setattr(provider.httpx,'Client',lambda **kwargs:client(transport=httpx.MockTransport(respond),**kwargs))
    result=provider.complete([{'role':'user','content':'hi'}],128,{'base_url':'https://fixture.test/v1','model':'fixture'},tools=tools)
    assert requests[0]['stream'] is False
    return result


def chunk(delta, finish=None):return {'choices':[{'index':0,'delta':delta,'finish_reason':finish}]}


def test_nonstream_request_reconstructs_streamed_text(monkeypatch):
    data=sse(chunk({'role':'assistant','content':'你好'}),chunk({'content':'，世界'}),chunk({},'stop'))
    assert call(monkeypatch,data)=='你好，世界'


def test_tool_arguments_are_reassembled_and_validated(monkeypatch):
    tools=[{'type':'function','function':{'name':'inspect','parameters':{'type':'object'}}}]
    a={'index':0,'id':'call_1','type':'function','function':{'name':'inspect','arguments':'{"source":'}}
    b={'index':0,'function':{'arguments':'"test"}'}}
    data=sse(chunk({'tool_calls':[a]}),chunk({'tool_calls':[b]}),chunk({},'tool_calls'))
    assert json.loads(call(monkeypatch,data,tools=tools))=={'kind':'tool','tool':'inspect','arguments':{'source':'test'}}


def test_stream_error_is_classified_without_consuming_prior_content(monkeypatch):
    with pytest.raises(provider.ContextWindowExceeded):
        call(monkeypatch,sse(chunk({'content':'partial'}),{'error':{'code':'context_length_exceeded'}}))


def test_stream_error_after_tool_fragments_is_not_a_tool_result(monkeypatch):
    tools=[{'type':'function','function':{'name':'inspect','parameters':{'type':'object'}}}]
    a={'index':0,'id':'call_1','type':'function','function':{'name':'inspect','arguments':'{}'}}
    with pytest.raises(provider.ContextWindowExceeded):
        call(monkeypatch,sse(chunk({'tool_calls':[a]}),{'error':{'code':'context_length_exceeded'}}),tools=tools)


def test_stream_cannot_append_tool_calls_after_completion(monkeypatch):
    tools=[{'type':'function','function':{'name':'inspect','parameters':{'type':'object'}}}]
    fragment={'index':0,'function':{'name':'inspect','arguments':'{}'}}
    with pytest.raises(provider.ModelOutputError):
        call(monkeypatch,sse(chunk({},'stop'),chunk({'tool_calls':[fragment]})),tools=tools)


@pytest.mark.parametrize('reason',['error','cancelled','unknown'])
def test_unknown_finish_is_not_a_success(monkeypatch,reason):
    with pytest.raises(provider.ModelOutputError):call(monkeypatch,sse(chunk({'content':'partial'}),chunk({},reason)))


def test_multiple_choices_are_not_silently_discarded(monkeypatch):
    with pytest.raises(provider.ModelOutputError):
        call(monkeypatch,sse({'choices':[{'index':0,'delta':{}},{'index':1,'delta':{}}]}))


def test_compressed_response_is_decoded_only_once(monkeypatch):
    import gzip
    assert call(monkeypatch,gzip.compress(sse(chunk({'content':'你好'}),chunk({},'stop'))),encoding='gzip')=='你好'


@pytest.mark.parametrize('data',[
    b'data: [DONE]\n\n',
    b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\ndata: [DONE]\n\n',
    b'data: {"choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":"stop"}',
    b'data: not-json\n\n',
])
def test_incomplete_or_invalid_stream_is_never_a_success(monkeypatch,data):
    with pytest.raises(provider.ModelOutputError):call(monkeypatch,data)


def test_streamed_truncation_remains_an_output_error(monkeypatch):
    with pytest.raises(provider.ModelOutputError) as exc:
        call(monkeypatch,sse(chunk({'content':'partial'}),chunk({},'length')))
    assert exc.value.code=='output_truncated'


def test_response_read_is_bounded(monkeypatch):
    with pytest.raises(provider.ModelOutputError) as exc:call(monkeypatch,b'x'*(4*1024*1024+1))
    assert exc.value.code=='response_too_large'


def test_real_local_http_gateway_ignores_nonstream_request(monkeypatch):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from threading import Thread
    requests=[]
    data=sse(chunk({'content':'分块'}),chunk({'content':'响应'}),chunk({},'stop'))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('Transfer-Encoding','chunked');self.end_headers()
            for offset in range(0,len(data),7):
                piece=data[offset:offset+7]
                self.wfile.write(('%x\r\n'%len(piece)).encode()+piece+b'\r\n');self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n');self.wfile.flush()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setattr('agent_service.network_policy.check_url',lambda url:None)
    monkeypatch.setattr('agent_service.quotas.reserve',lambda amount:None)
    monkeypatch.setattr('agent_service.store.put',lambda *args,**kwargs:None)
    monkeypatch.setattr('agent_service.store.all_items',lambda kind:[])
    try:
        config={'base_url':f'http://127.0.0.1:{server.server_port}/v1','model':'fixture'}
        assert provider.complete([{'role':'user','content':'hi'}],128,config)=='分块响应'
        assert requests[0]['stream'] is False
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)


def test_oversized_error_preserves_http_status(monkeypatch):
    with pytest.raises(httpx.HTTPStatusError) as exc:
        call(monkeypatch,b'x'*(4*1024*1024+1),status=429)
    assert exc.value.response.status_code==429
