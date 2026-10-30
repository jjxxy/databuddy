"""Exercise the real legacy planner against controlled compatible HTTP replies."""
import json

import httpx
import pytest

from agent_service import agent,opencode_runtime,provider,workspace
from agent_service.store import get,patch,all_items
from test_opencode_agent_bridge import turn


@pytest.fixture
def model(monkeypatch):
    config={'base_url':'https://fixture-model.test/v1','model':'fixture','api_key':'fixture-only',
            'tool_protocol':'native','share_data':True,'engine':'atlas'}
    monkeypatch.setattr(provider,'settings',lambda:config)
    monkeypatch.setattr('agent_service.network_policy.check_url',lambda url:None)
    monkeypatch.setattr(opencode_runtime,'is_enabled',lambda:False)
    monkeypatch.delenv('ATLAS_OPENCODE_MODEL_INPUT_CHAR_LIMITS',raising=False)
    client=httpx.Client;requests=[];responses=[]
    def respond(request):
        requests.append(json.loads(request.content))
        value=responses.pop(0)
        if isinstance(value,Exception):raise value
        if isinstance(value,int):return httpx.Response(value,json={'error':{'message':'controlled error'}})
        if isinstance(value,tuple):return httpx.Response(value[0],json=value[1])
        if isinstance(value,dict) and 'error' in value:return httpx.Response(200,json=value)
        if isinstance(value,bytes):return httpx.Response(200,content=value)
        return httpx.Response(200,json={'choices':[value],
            'usage':{'prompt_tokens':10,'completion_tokens':4,'total_tokens':14}})
    monkeypatch.setattr(provider.httpx,'Client',lambda **kw:client(transport=httpx.MockTransport(respond),**kw))
    return config,requests,responses


def choice(content=None,*,calls=None,finish='stop'):
    message={}
    if content is not None:message['content']=content
    if calls is not None:message['tool_calls']=calls
    return {'message':message,'finish_reason':finish}


def call(name,args):return {'type':'function','function':{'name':name,'arguments':args}}


def ordinary_chat(turn):
    return patch('turn',turn['id'],interaction_mode='chat',text='你好，请简单介绍自己',task_contract=None)


@pytest.mark.parametrize('content',['你好，我可以帮助你分析和整理信息。',
    [{'type':'text','text':'你好，'},{'type':'text','text':'我可以回答问题。'}]])
def test_real_http_native_provider_plain_chat_reply_is_returned(turn,model,content):
    _,requests,responses=model;responses.append(choice(content))
    current=ordinary_chat(turn)
    agent.advance(current)
    saved=get('turn',turn['id'])
    assert saved['state']=='done' and saved['outcome']=='answered'
    assert saved['answer']==(''.join(part['text'] for part in content) if isinstance(content,list) else content)
    assert len(requests)==1 and requests[0]['tool_choice']=='required'
    assert '只返回符合response_schema的JSON' not in requests[0]['messages'][0]['content']
    usage=saved['model_usage']
    assert usage['calls']==1 and usage['prompt_tokens']==10 and usage['completion_tokens']==4
    event=next(value for value in all_items('model_event') if value.get('turn_id')==turn['id'])
    assert event['status']=='succeeded' and event['protocol_warning']=='native_tool_call_missing'
    assert '你好' not in json.dumps(event,ensure_ascii=False)


def test_real_http_work_mode_falls_back_to_json_and_still_writes_once(turn,model):
    _,requests,responses=model
    responses.extend([choice('我来创建报告。'),
        choice('  ```json\n'+json.dumps({'kind':'tool','tool':'write_file',
            'arguments':{'path':'reports/native.md','content':'实际文件内容'}},ensure_ascii=False)+'\n```  '),
        choice(calls=[call('finish_task',{'answer':'报告已保存','status':'complete'})])])
    agent.advance(get('turn',turn['id']))
    assert workspace.read_file(turn['project_id'],'reports/native.md')=='实际文件内容'
    assert len(requests)==2 and 'tools' in requests[0] and 'tools' not in requests[1]
    assert 'response_schema' in json.loads(requests[1]['messages'][1]['content'])
    assert all(message.get('content')!='' for message in requests[1]['messages'])
    agent.advance(get('turn',turn['id']))
    assert get('turn',turn['id'])['outcome']=='verified_complete'
    assert len(get('turn',turn['id'])['agent_trace'])==1
    assert get('turn',turn['id'])['planning_errors'][-1]['code']=='native_tool_call_missing'


def test_real_http_native_object_tool_arguments_remain_validated(turn,model):
    _,requests,responses=model
    responses.append(choice(calls=[call('write_file',{'path':'reports/native.md','content':'object arguments'})]))
    agent.advance(get('turn',turn['id']))
    assert len(requests)==1
    assert workspace.read_file(turn['project_id'],'reports/native.md')=='object arguments'


def test_real_http_json_mode_plain_chat_reply_is_returned(turn,model):
    config,requests,responses=model;config['tool_protocol']='json'
    responses.append(choice('你好，可以一起分析问题。'))
    agent.advance(ordinary_chat(turn))
    current=get('turn',turn['id'])
    assert current['outcome']=='answered' and current['answer']=='你好，可以一起分析问题。'
    assert len(requests)==1 and 'tools' not in requests[0]


def test_unsupported_tool_calling_http_400_falls_back_without_expanding_grants(turn,model):
    _,requests,responses=model
    responses.extend([(400,{'error':{'code':'unsupported_parameter','param':'tools',
                  'message':'This model does not support tools'}}),
        choice(json.dumps({'kind':'tool','tool':'write_file',
                           'arguments':{'path':'reports/native.md','content':'fallback result'}}))])
    agent.advance(get('turn',turn['id']))
    assert len(requests)==2 and 'tools' in requests[0] and 'tools' not in requests[1]
    assert workspace.read_file(turn['project_id'],'reports/native.md')=='fallback result'
    assert get('turn',turn['id'])['planning_errors'][0]['code']=='native_unsupported_tool_calling'


def test_confirmed_unsupported_tools_are_reused_only_for_pinned_task_model(turn,model):
    _,requests,responses=model
    current=patch('turn',turn['id'],model_id='fixture',model_catalog_revision='pinned-v1')
    responses.extend([(400,{'error':{'code':'unsupported_parameter','param':'tools',
                  'message':'This model does not support tools'}}),
        choice(json.dumps({'kind':'tool','tool':'write_file',
                           'arguments':{'path':'reports/native.md','content':'one write'}})),
        choice(json.dumps({'kind':'final','answer':'已保存','status':'complete'}))])
    agent.advance(current)
    agent.advance(get('turn',turn['id']))
    saved=get('turn',turn['id'])
    assert len(requests)==3 and 'tools' in requests[0] and all('tools' not in r for r in requests[1:])
    assert saved['outcome']=='verified_complete' and len(saved['agent_trace'])==1
    assert saved['model_tool_compatibility']['model_catalog_revision']=='pinned-v1'


def test_unsupported_tool_marker_is_not_reused_after_model_revision_changes(turn,model):
    _,requests,responses=model
    current=patch('turn',turn['id'],model_id='fixture',model_catalog_revision='pinned-v2',
        model_tool_compatibility={'reason':'native_unsupported_tool_calling','model_id':'fixture','model_catalog_revision':'pinned-v1'})
    responses.append(choice(calls=[call('write_file',{'path':'reports/native.md','content':'fresh model'})]))
    agent.advance(current)
    assert len(requests)==1 and 'tools' in requests[0]
    assert workspace.read_file(turn['project_id'],'reports/native.md')=='fresh model'


@pytest.mark.parametrize('message',['invalid API key for tools','工具调用参数不正确',
    'tools malformed JSON, rate limit error','failed to authorize tool_choice'])
def test_tool_error_text_alone_is_not_treated_as_unsupported(message):
    request=httpx.Request('POST','https://fixture.test/v1/chat/completions')
    response=httpx.Response(400,request=request,json={'error':{'code':'invalid_request_error','message':message}})
    assert not provider._native_tool_unsupported(httpx.HTTPStatusError('bad request',request=request,response=response))


def test_invalid_json_response_is_retried_without_echoing_provider_body(turn,model):
    _,requests,responses=model
    responses.extend([b'<html>FIXTURE-PRIVATE-GATEWAY-ERROR</html>',
        choice(calls=[call('finish_task',{'answer':'已恢复正常','status':'complete'})])])
    agent.advance(ordinary_chat(turn))
    assert len(requests)==2 and get('turn',turn['id'])['outcome']=='answered'
    assert 'FIXTURE-PRIVATE-GATEWAY-ERROR' not in json.dumps(requests[1],ensure_ascii=False)
    assert get('turn',turn['id'])['planning_errors'][0]['code']=='invalid_response_json'


def test_200_error_envelope_preserves_quota_failure_without_retry_or_leak(turn,model):
    _,requests,responses=model
    responses.append({'error':{'code':'insufficient_quota','message':'FIXTURE-SECRET-KEY'}})
    agent.advance(ordinary_chat(turn))
    current=get('turn',turn['id'])
    assert len(requests)==1 and current['outcome']=='paused' and current['resume_available']
    assert '模型服务额度不足' in current['answer'] and 'FIXTURE-SECRET-KEY' not in current['answer']
    assert not current.get('planning_errors')


def test_plain_chat_does_not_save_or_reflect_private_thinking_tags(turn,model):
    _,requests,responses=model
    responses.extend([choice('<think>FIXTURE-PRIVATE-THOUGHT</think>你好'),
        choice(json.dumps({'kind':'final','answer':'你好','status':'complete'}))])
    agent.advance(ordinary_chat(turn))
    current=get('turn',turn['id'])
    assert len(requests)==2 and current['outcome']=='answered' and current['answer']=='你好'
    assert 'FIXTURE-PRIVATE-THOUGHT' not in json.dumps(current,ensure_ascii=False)
    assert 'FIXTURE-PRIVATE-THOUGHT' not in json.dumps(requests[1],ensure_ascii=False)


def test_json_mode_does_not_echo_unfinished_reasoning_tag(turn,model):
    config,requests,responses=model;config['tool_protocol']='json'
    responses.extend([choice('<think>FIXTURE-PRIVATE-THOUGHT'),
        choice(json.dumps({'kind':'final','answer':'你好','status':'complete'}))])
    agent.advance(ordinary_chat(turn))
    assert len(requests)==2 and get('turn',turn['id'])['answer']=='你好'
    assert 'FIXTURE-PRIVATE-THOUGHT' not in json.dumps(requests[1],ensure_ascii=False)


@pytest.mark.parametrize('protocol',['native','json'])
def test_structured_final_answer_does_not_expose_reasoning(turn,model,protocol):
    config,requests,responses=model;config['tool_protocol']=protocol
    bad={'answer':'<think>FIXTURE-PRIVATE-THOUGHT</think>你好','status':'complete'}
    good={'answer':'你好','status':'complete'}
    if protocol=='native':responses.extend([choice(calls=[call('finish_task',bad)]),choice(calls=[call('finish_task',good)])])
    else:responses.extend([choice(json.dumps({'kind':'final',**bad})),choice(json.dumps({'kind':'final',**good}))])
    agent.advance(ordinary_chat(turn))
    current=get('turn',turn['id'])
    assert len(requests)==2 and current['outcome']=='answered' and current['answer']=='你好'
    assert current['planning_errors'][-1]['code']=='model_output_reasoning'
    assert 'FIXTURE-PRIVATE-THOUGHT' not in json.dumps(current,ensure_ascii=False)
    assert 'FIXTURE-PRIVATE-THOUGHT' not in json.dumps(requests[1],ensure_ascii=False)


def test_native_bridge_rejects_structured_reasoning_without_persisting_it(turn):
    current=ordinary_chat(turn)
    from agent_service.tasks import ensure
    ensure(current);current=get('turn',turn['id'])
    result=agent.opencode_step(current['id'],{'kind':'final','answer':'<think>FIXTURE-PRIVATE-THOUGHT</think>你好',
                            'status':'complete'},'native:unsafe-answer')
    assert result['observation']['code']=='MODEL_OUTPUT_REASONING' and not result['observation']['executed']
    assert 'FIXTURE-PRIVATE-THOUGHT' not in json.dumps(result,ensure_ascii=False)
    assert 'FIXTURE-PRIVATE-THOUGHT' not in json.dumps(get('turn',turn['id']),ensure_ascii=False)
    agent.opencode_step(current['id'],{'kind':'final','answer':'你好','status':'complete'},'native:safe-answer')
    assert get('turn',turn['id'])['answer']=='你好'


def test_native_schema_error_feedback_includes_allowed_tool_schema(turn,model):
    _,requests,responses=model
    responses.extend([choice(calls=[call('write_file',{'path':'reports/native.md'})]),
        choice(calls=[call('write_file',{'path':'reports/native.md','content':'corrected'})])])
    agent.advance(get('turn',turn['id']))
    assert workspace.read_file(turn['project_id'],'reports/native.md')=='corrected'
    assert len(requests)==2
    feedback=requests[1]['messages'][-1]['content']
    assert 'expected_arguments' in feedback and 'content' in feedback and 'missing' in feedback
    assert all(m.get('content')!='' for m in requests[1]['messages'])


@pytest.mark.parametrize('status',[401,429,503])
def test_desktop_real_http_error_is_not_retried_as_invalid_plan(turn,model,monkeypatch,status):
    _,requests,responses=model;responses.append(status)
    monkeypatch.setenv('ATLAS_DESKTOP_MODE','1')
    with provider.task_scope(turn['id']),pytest.raises(httpx.HTTPStatusError):
        agent.plan(get('turn',turn['id']))
    assert len(requests)==1 and not get('turn',turn['id']).get('planning_errors')


def test_desktop_rate_limit_retries_and_finishes_original_chat(turn,model,monkeypatch):
    _,requests,responses=model
    monkeypatch.setenv('ATLAS_DESKTOP_MODE','1')
    responses.extend([429,choice(calls=[call('finish_task',{'answer':'继续正常回答','status':'complete'})])])
    agent.advance(ordinary_chat(turn))
    waiting=get('turn',turn['id'])
    assert waiting['state']=='agent' and waiting['model_retry_count']==1 and waiting['model_retry_at']
    patch('turn',turn['id'],model_retry_at=0)
    agent.advance(get('turn',turn['id']))
    result=get('turn',turn['id'])
    assert len(requests)==2 and result['outcome']=='answered' and result['answer']=='继续正常回答'


def test_desktop_auth_failure_is_reported_without_sensitive_gateway_body(turn,model,monkeypatch):
    _,requests,responses=model;monkeypatch.setenv('ATLAS_DESKTOP_MODE','1')
    responses.append((401,{'error':{'message':'FIXTURE-SECRET-KEY'}}))
    agent.advance(ordinary_chat(turn))
    current=get('turn',turn['id'])
    assert len(requests)==1 and current['state']=='failed' and current['resume_available']
    assert '检查模型连接或密钥' in current['answer'] and 'FIXTURE-SECRET-KEY' not in current['answer']


def test_real_transport_error_is_preserved_without_format_retry(turn,model):
    _,requests,responses=model;responses.append(httpx.ConnectError('controlled transport failure'))
    with provider.task_scope(turn['id']),pytest.raises(httpx.ConnectError):agent.plan(get('turn',turn['id']))
    assert len(requests)==1 and not get('turn',turn['id']).get('planning_errors')


def test_chat_fallback_cannot_execute_injected_write_or_hallucinate_it_succeeded(turn,model):
    _,requests,responses=model
    payload={'kind':'tool','tool':'write_file','arguments':{'path':'reports/native.md','content':'not allowed'}}
    responses.extend([choice(json.dumps(payload)),choice(json.dumps(payload)),choice(json.dumps(payload))])
    agent.advance(ordinary_chat(turn))
    current=get('turn',turn['id'])
    assert current['state']=='done' and current['outcome']=='paused' and current['resume_available']
    assert not workspace.path_for(turn['project_id'],'reports/native.md').exists()
    assert len(requests)==3 and '模型连续返回了无效计划' not in current['answer']


def test_plain_chat_claim_of_reading_project_requires_actual_tool(turn,model):
    _,requests,responses=model
    responses.extend([choice('我已核对项目，项目有三项成果。'),
        choice(json.dumps({'kind':'tool','tool':'project_context','arguments':{}}))])
    current=patch('turn',turn['id'],interaction_mode='chat',text='这个项目里有什么？',task_contract=None)
    agent.advance(current)
    saved=get('turn',turn['id'])
    assert len(requests)==2 and saved['state']=='agent' and not saved.get('answer')
    assert len(saved['agent_trace'])==1 and saved['agent_trace'][0]['tool']=='project_context'
    assert saved['agent_trace'][0]['status']=='done'


def test_content_filter_is_preserved_and_never_blindly_retried(turn,model):
    _,requests,responses=model;responses.append(choice('blocked by provider',finish='content_filter'))
    agent.advance(ordinary_chat(turn))
    current=get('turn',turn['id'])
    assert len(requests)==1 and current['outcome']=='paused' and current['resume_available']
    assert '模型服务未返回可用内容' in current['answer']


def test_invalid_reason_never_persists_raw_model_response(turn,model):
    _,requests,responses=model
    secret='FIXTURE-PRIVATE-MODEL-OUTPUT'
    responses.extend([choice(calls=[call('write_file',{'path':{'private':secret}})]),
        choice(calls=[call('write_file',{'path':'reports/native.md','content':'done'})])])
    agent.advance(get('turn',turn['id']))
    assert len(requests)==2 and secret not in json.dumps(get('turn',turn['id']).get('planning_errors',[]))
    assert secret not in requests[1]['messages'][-1]['content']


def test_native_compaction_selects_fresh_checkpoint_tool(turn,model,monkeypatch):
    from test_context_window_recovery import long_turn
    from agent_service.context_window import request_size
    current=long_turn(turn)
    messages,tools=agent.planning_context(current,native=True)
    size=request_size(messages[0]['content'],json.loads(messages[1]['content']),tools)
    monkeypatch.setenv('ATLAS_OPENCODE_MODEL_INPUT_CHAR_LIMITS',json.dumps({'fixture':int(size*.7)}))
    seen=[]
    def respond(messages,*,tools):
        seen.extend(tool['function']['name'] for tool in tools)
        checkpoint=get('turn',turn['id'])['context_window']['checkpoint_id']
        return json.dumps({'kind':'tool','tool':'read_context_checkpoint',
                           'arguments':{'checkpoint_id':checkpoint}})
    monkeypatch.setattr(provider,'complete',respond)
    with provider.task_scope(turn['id']):step=agent.plan(current)
    assert 'read_context_checkpoint' in seen and step.tool=='read_context_checkpoint'
