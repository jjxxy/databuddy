"""Native prompt admission, plain replies and one no-effect protocol repair."""
import pytest
import httpx
import json
from types import SimpleNamespace
from urllib.request import ProxyHandler, Request, build_opener

from agent_service import agent, opencode_runtime as runtime, provider, tasks
from agent_service.store import get, patch
from test_opencode_agent_bridge import turn


def session(monkeypatch, contexts, *, reused=False, on_context=None, prompt_has_id=True):
    events=[];requests=[];metadata={}
    monkeypatch.setattr(runtime,'require_ready',lambda:{'ready':True})
    monkeypatch.setattr(runtime,'connection',lambda *_:{'base_url':'http://127.0.0.1:1',
        'api_key':'fixture','model_ref':{'providerID':'fixture','id':'fixture'}})
    def request(_value,method,path,body=None,**_kwargs):
        requests.append((method,path,body))
        if method=='POST' and path=='/api/session':
            metadata.update(body['metadata']);return {'data':{'id':'ses_idle'}}
        if method=='PATCH' and path.endswith('ses_idle'):
            metadata.update(body['metadata']);return {'data':{}}
        if method=='POST' and path.endswith('/model'):return {'data':{}}
        if method=='POST' and path.endswith('/interrupt'):return {'data':{}}
        if method=='POST' and path.endswith('/prompt'):
            metadata['prompt_text']=body['text']
            number=sum(1 for method,path,_ in requests if method=='POST' and path.endswith('/prompt'))
            return {'data':{'id':'msg_prompt_'+str(number)} if prompt_has_id else {'accepted':True}}
        if method=='GET' and path.endswith('/context'):
            if not contexts:pytest.fail('excessive context reads')
            events.append('context')
            if on_context:on_context(metadata,len(events))
            data=contexts.pop(0)
            return {'data':[dict(item,text=metadata['prompt_text']) if isinstance(item,dict) and item.get('text')=='<current prompt>' else item for item in data]}
        raise AssertionError((method,path))
    monkeypatch.setattr(runtime,'_request',request)
    return requests,events


def test_native_plain_chat_reply_uses_existing_validated_final_path(turn,monkeypatch):
    patch('turn',turn['id'],interaction_mode='chat',text='你好',task_contract=None)
    tasks.ensure(get('turn',turn['id']))
    requests,_=session(monkeypatch,[[{'id':'msg_prompt_1','type':'user'},
        {'id':'msg_answer','type':'assistant','finish':'stop','content':[{'type':'text','text':'你好，有什么可以帮你？'}]},
        {'id':'msg_idle','type':'idle','outcome':'succeeded'}]])
    result=runtime.run_session(get('turn',turn['id']),'chat',{},lambda step,call:agent.opencode_step(turn['id'],step,call))
    saved=get('turn',turn['id'])
    assert result['state']=='done' and saved['outcome']=='answered'
    assert saved['answer']=='你好，有什么可以帮你？'
    assert len([r for r in requests if r[1].endswith('/prompt')])==1


def test_work_free_text_is_corrected_once_then_waits_for_verified_result(turn,monkeypatch):
    from agent_service.store import patch as patch_turn
    contexts=[
      [{'id':'msg_prompt_1','type':'user'},{'id':'msg_free','type':'assistant','finish':'stop',
        'content':[{'type':'text','text':'报告已经完成'}]},{'id':'msg_idle_1','type':'idle','outcome':'succeeded'}],
      [{'id':'msg_prompt_1','type':'user'},{'id':'msg_idle_1','type':'idle','outcome':'succeeded'},
       {'id':'msg_prompt_2','type':'user'},{'id':'msg_idle_2','type':'idle','outcome':'succeeded'}],
    ]
    requests,_=session(monkeypatch,contexts)
    with pytest.raises(provider.ModelOutputError,match='未能按当前工具协议'):
        runtime.run_session(turn,'work',{},lambda *_:pytest.fail('work prose is not a validated finish'))
    assert get('turn',turn['id'])['state']=='agent'
    assert len([r for r in requests if r[1].endswith('/prompt')])==2


def test_existing_idle_is_not_confused_with_queued_resume(turn,monkeypatch):
    patch('turn',turn['id'],opencode_session_id='ses_idle',opencode_memory_generation=0)
    prior=[{'id':'msg_old_user','type':'user'},
           {'type':'assistant','error':{'code':'context_length_exceeded'}},
           {'id':'msg_old_idle','type':'idle','outcome':'succeeded'},
           {'type':'idle','outcome':'succeeded'}]  # even a malformed old marker is not this prompt
    # First read is the existing session baseline, second still has only the
    # old idle, and the third contains the delivered new prompt.
    contexts=[prior,prior,prior+[{'id':'msg_prompt_1','type':'user'},
        {'id':'msg_idle_new','type':'idle','outcome':'failed'}]]
    requests,events=session(monkeypatch,contexts,reused=True)
    monkeypatch.setattr('agent_service.memory_service.generation',lambda *_:0)
    with pytest.raises(provider.ModelServiceError,match='模型服务未完成'):
        runtime.run_session(get('turn',turn['id']),'work',{},lambda *_:None)
    assert len(events)==3
    assert len([r for r in requests if r[1].endswith('/prompt')])==1


def test_idless_prompt_ignores_old_idless_idle_and_accepts_new_valid_reply(turn,monkeypatch):
    patch('turn',turn['id'],interaction_mode='chat',text='你好',task_contract=None,
          opencode_session_id='ses_idle',opencode_memory_generation=0)
    tasks.ensure(get('turn',turn['id']))
    prior=[{'type':'assistant','error':{'code':'context_length_exceeded'}},
           {'type':'idle','outcome':'failed'}]
    delayed=[{'type':'assistant','error':{'code':'context_length_exceeded'}},
             {'type':'idle','outcome':'failed'}]
    contexts=[prior,prior+delayed,prior+delayed+[{'type':'user','text':'<current prompt>'},
        {'id':'msg_new','type':'assistant','finish':'stop','content':[{'type':'text','text':'你好！'}]},
        {'type':'idle','outcome':'succeeded'}]]
    _,events=session(monkeypatch,contexts,reused=True,prompt_has_id=False)
    monkeypatch.setattr('agent_service.memory_service.generation',lambda *_:0)
    result=runtime.run_session(get('turn',turn['id']),'chat',{},lambda step,call:agent.opencode_step(turn['id'],step,call))
    assert len(events)==3 and result['state']=='done'
    assert get('turn',turn['id'])['answer']=='你好！'


def test_idless_prompt_with_rewritten_old_context_fails_closed(turn,monkeypatch):
    patch('turn',turn['id'],opencode_session_id='ses_idle',opencode_memory_generation=0)
    prior=[{'type':'idle','outcome':'failed'}]
    session(monkeypatch,[prior,[{'type':'idle','outcome':'succeeded'}]],reused=True,prompt_has_id=False)
    monkeypatch.setattr('agent_service.memory_service.generation',lambda *_:0)
    with pytest.raises(provider.ModelServiceError,match='无法确认 Agent 会话中新增消息的归属'):
        runtime.run_session(get('turn',turn['id']),'work',{},lambda *_:pytest.fail('ambiguous message'))


def test_idless_prompt_does_not_attribute_delayed_old_idle_to_new_request(turn,monkeypatch):
    patch('turn',turn['id'],opencode_session_id='ses_idle',opencode_memory_generation=0)
    clock=[0]
    monkeypatch.setattr(runtime,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    monkeypatch.setattr('agent_service.memory_service.generation',lambda *_:0)
    prior=[{'id':'old_user','type':'user'}]
    delayed=prior+[{'type':'assistant','error':{'code':'context_length_exceeded'}},
                   {'type':'idle','outcome':'failed'}]
    def advance_clock(_,number):
        if number==2:clock[0]=60
    session(monkeypatch,[prior,delayed],reused=True,prompt_has_id=False,on_context=advance_clock)
    with pytest.raises(provider.ModelServiceError,match='无法确认 Agent 已开始处理'):
        runtime.run_session(get('turn',turn['id']),'work',{},lambda *_:pytest.fail('no new result'))


def test_native_rate_limit_uses_normal_retry_classification_without_leaking_error(turn,monkeypatch):
    session(monkeypatch,[[{'id':'msg_prompt_1','type':'user'},
        {'id':'msg_answer','type':'assistant','error':{'data':{'statusCode':429},'message':'secret upstream detail'}},
        {'id':'msg_idle','type':'idle','outcome':'failed'}]])
    with pytest.raises(httpx.HTTPStatusError) as caught:
        runtime.run_session(turn,'work',{},lambda *_:pytest.fail('no valid tool call'))
    assert caught.value.response.status_code==429
    assert 'secret upstream detail' not in str(caught.value)


def test_active_model_request_does_not_hit_admission_timeout(turn,monkeypatch):
    clock=[0]
    monkeypatch.setattr(runtime,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    def request_model(metadata,number):
        if number!=1:return
        bridge=metadata['atlas_bridge']
        body={'session_id':'ses_idle','op':'model_request','kind':'generation',
              'input_characters':100,'target_url':'https://fixture.test/v1/chat/completions','model_id':'fixture'}
        request=Request(bridge['url'],data=json.dumps(body).encode(),headers={'Content-Type':'application/json',
            'Authorization':'Bearer '+bridge['token']},method='POST')
        with build_opener(ProxyHandler({})).open(request,timeout=10) as response:
            assert json.loads(response.read())['ok']
        clock[0]=60  # more than the admission timeout while a model request is in flight
    _,events=session(monkeypatch,[[{'type':'idle'}],[{'id':'msg_prompt_1','type':'user'},
        {'id':'msg_idle','type':'idle','outcome':'failed'}]],on_context=request_model)
    with pytest.raises(provider.ModelServiceError,match='模型服务未完成'):
        runtime.run_session(turn,'work',{},lambda *_:pytest.fail('no valid tool call'))
    assert len(events)==2
    assert get('turn',turn['id'])['model_usage']['calls']==1


def test_unstarted_admitted_prompt_reports_stall_after_wait(turn,monkeypatch):
    clock=[0]
    monkeypatch.setattr(runtime,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    _,events=session(monkeypatch,[[{'type':'idle'}]],on_context=lambda *_:clock.__setitem__(0,60))
    with pytest.raises(provider.ModelServiceError,match='会话未开始处理'):
        runtime.run_session(turn,'work',{},lambda *_:pytest.fail('no valid tool call'))
    assert len(events)==1


def test_finished_mutating_receipt_is_not_automatically_replayed(turn,monkeypatch):
    patch('turn',turn['id'],agent_trace=[{'id':'prior','tool':'write_file','status':'done',
        'arguments':{'path':'reports/native.md','content':'once'},'observation':{'path':'reports/native.md'}}])
    requests,_=session(monkeypatch,[[{'id':'msg_prompt_1','type':'user'},
       {'id':'msg_answer','type':'assistant','finish':'stop','content':[{'type':'text','text':'done'}]},
       {'id':'msg_idle','type':'idle','outcome':'succeeded'}]])
    with pytest.raises(provider.ModelOutputError,match='未能按当前工具协议'):
        runtime.run_session(get('turn',turn['id']),'work',{},lambda *_:pytest.fail('no new effects'))
    assert len([r for r in requests if r[1].endswith('/prompt')])==1
