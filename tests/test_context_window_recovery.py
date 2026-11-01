"""Real provider HTTP error handling, planner retries and durable projections."""
import copy
import json

import httpx
import pytest

from agent_service import agent, provider, context_window, context_compaction, agent_progress
from agent_service.store import get, patch, put, now, uid
from test_opencode_agent_bridge import turn
from test_context_working_set import table


def test_native_oversized_history_is_a_recoverable_window_error(monkeypatch):
    from agent_service import opencode_runtime
    class Response:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def raise_for_status(self):pass
        def iter_bytes(self):yield b'x'*129
    class Client:
        def __init__(self,**_kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def stream(self,*_args,**_kwargs):return Response()
    monkeypatch.setattr(opencode_runtime.httpx,'Client',Client)
    connection={'base_url':'http://127.0.0.1:12345','api_key':'test'}
    with pytest.raises(provider.ContextWindowExceeded) as caught:
        opencode_runtime._request(connection,'GET','/api/session/s/context',limit=128)
    assert caught.value.source=='native_history_transport'
    # Other oversized transport responses must not be misclassified as model
    # context failures or cause session-recovery retries.
    with pytest.raises(ValueError,match='响应超过读取限制') as unrelated:
        opencode_runtime._request(connection,'GET','/api/info',limit=128)
    assert not isinstance(unrelated.value,provider.ContextWindowExceeded)


def long_turn(turn):
    trace=[{'id':'input-'+str(i),'tool':'read_source_table','status':'done',
        'arguments':{'source_id':'source-'+str(i)},'observation':table(i)} for i in range(7)]
    return patch('turn',turn['id'],agent_trace=trace,
        text='请核对全部资料，保留9007199254740993.000000000001。不要发布，不要发送邮件。')


def test_no_compression_below_actual_window_and_no_fixed_history_turn_cut(turn,monkeypatch):
    monkeypatch.delenv('ATLAS_OPENCODE_MODEL_INPUT_CHAR_LIMITS',raising=False)
    for i in range(12):
        put('turn',{'project_id':turn['project_id'],'state':'done','created_at':'2025-01-%02d'%(i+1),
            'text':'第%d轮原文：'%i+'约定'*2000,'answer':'保留完整回答'+str(i)},'hist_'+uid())
    messages,_=agent.planning_context(turn,native=False)
    info=json.loads(messages[1]['content'])
    assert len(info['conversation'])==12
    assert all(len(item['user'])>3500 for item in info['conversation'])
    assert 'window_checkpoint' not in info
    size=context_window.request_size(messages[0]['content'],info,None)
    monkeypatch.setenv('ATLAS_OPENCODE_MODEL_INPUT_CHAR_LIMITS',json.dumps({'fixture':int(size/0.9)}))
    again,_=agent.planning_context(get('turn',turn['id']),native=False)
    assert json.loads(again[1]['content'])['conversation']==info['conversation'],'90% of a cap must not trigger an arbitrary 85% early cut'


def test_planning_keeps_old_nondeduplicated_tool_facts_until_a_window_requires_projection(turn,monkeypatch):
    monkeypatch.delenv('ATLAS_OPENCODE_MODEL_INPUT_CHAR_LIMITS',raising=False)
    precise='9007199254740993.000000000001'
    source={'source_id':'old-source','rows':[{'金额':precise,'备注':'x'*12000}]}
    current=patch('turn',turn['id'],agent_trace=[
        {'id':'older','tool':'read_source_table','status':'done','arguments':{'source_id':'old-source'},'observation':source},
        {'id':'next','tool':'list_files','status':'done','arguments':{},'observation':{}},
        {'id':'latest','tool':'list_files','status':'done','arguments':{},'observation':{}},
    ])
    messages,_=agent.planning_context(current,native=False)
    info=json.loads(messages[1]['content'])
    assert info['completed_steps'][0]['observation']==source
    assert 'window_checkpoint' not in info


def test_model_window_estimate_accounts_for_prompt_tools_and_output_without_fixed_threshold(turn,monkeypatch):
    current=long_turn(turn)
    settings=provider.settings()
    monkeypatch.setattr(provider,'settings',lambda:settings)
    messages,tools=agent.planning_context(current,native=False)
    info=json.loads(messages[1]['content']);prompt=messages[0]['content']
    estimated=context_window.estimated_request_tokens(prompt,info,tools)
    output=2000
    settings.update(context_window_tokens=estimated+output,max_output_tokens=output)
    fits,_=agent.planning_context(get('turn',turn['id']),native=False)
    assert 'window_checkpoint' not in json.loads(fits[1]['content'])
    # The input fits on its own but its configured output reserve does not.
    settings['context_window_tokens']=int(estimated*.72)+output
    compressed,_=agent.planning_context(get('turn',turn['id']),native=False)
    projected=json.loads(compressed[1]['content'])
    assert projected['window_checkpoint']['originals_retained']
    assert projected['request']==current['text']
    assert 'target_characters' not in get('turn',turn['id'])['context_window']
    # A later, larger configured window must restore the full valid context.
    settings['context_window_tokens']=estimated+output+1000
    restored,_=agent.planning_context(get('turn',turn['id']),native=False)
    assert 'window_checkpoint' not in json.loads(restored[1]['content'])


def test_unreliable_soft_token_estimate_never_blocks_full_request(turn,monkeypatch):
    settings=provider.settings();settings.update(context_window_tokens=256,max_output_tokens=1)
    monkeypatch.setattr(provider,'settings',lambda:settings)
    attempted=[]
    def impossible(*args,**kwargs):
        attempted.append(True)
        raise provider.ContextWindowExceeded('estimate does not fit',source='irreducible')
    monkeypatch.setattr(context_window,'_project',impossible)
    messages,_=agent.planning_context(turn,native=False)
    assert attempted and json.loads(messages[1]['content'])['request']==turn['text']


@pytest.mark.parametrize('allow_recovery',[False,True])
def test_soft_estimate_never_blocks_or_writes_checkpoint_if_projection_is_unsafe(turn,monkeypatch,allow_recovery):
    settings=provider.settings();settings.update(context_window_tokens=256,max_output_tokens=1)
    monkeypatch.setattr(provider,'settings',lambda:settings)
    if not allow_recovery:monkeypatch.setattr(agent.agent_context,'allowed',lambda _turn:set())
    # The protected current request is deliberately irreducible even if the
    # read-only checkpoint tool is available.
    info={'request':'必要用户要求'*1000,'tools':[]}
    before=get('turn',turn['id'])
    view,tools=context_window.fit(before,'系统提示',info,None)
    after=get('turn',turn['id'])
    assert view==info and tools is None
    assert after.get('discovered_tools')==before.get('discovered_tools')
    assert after.get('context_window')==before.get('context_window')


def test_oversized_context_becomes_bound_checkpoint_and_exact_pages_survive(turn,monkeypatch):
    current=long_turn(turn);original=copy.deepcopy(current['agent_trace'])
    messages,_=agent.planning_context(current,native=False)
    full=json.loads(messages[1]['content']);size=context_window.request_size(messages[0]['content'],full,None)
    cap=int(size*.7);monkeypatch.setenv('ATLAS_OPENCODE_MODEL_INPUT_CHAR_LIMITS',json.dumps({'fixture':cap}))
    compacted,_=agent.planning_context(get('turn',turn['id']),native=False)
    info=json.loads(compacted[1]['content']);saved=get('turn',turn['id'])
    assert saved['context_window']['after_characters']<cap<size
    assert info['request']==current['text'] and info['task_contract']==full['task_contract']
    assert get('turn',turn['id'])['agent_trace']==original
    checkpoint=saved['context_window']['checkpoint_id'];offset=0;parts={}
    while True:
        page=context_compaction.read_checkpoint(saved,checkpoint,offset,2)
        for item in page['items']:parts.setdefault(item['section'],[]).append(item['content'])
        if page['next_offset'] is None:break
        offset=page['next_offset']
    assert parts
    for section,values in parts.items():
        recovered=json.loads(''.join(values))
        if section.startswith('completed_steps.'):
            assert recovered==full['completed_steps'][int(section.rsplit('.',1)[1])]
    assert saved['context_window_epoch']==1
    # The same compact projection cannot mint new recovery epochs on every poll.
    agent.planning_context(saved,native=False)
    assert get('turn',turn['id'])['context_window_epoch']==1
    from agent_service.memory_service import generation
    patch('context_checkpoint',checkpoint,memory_generation='retired-memory-generation')
    with pytest.raises(ValueError,match='记忆'):context_compaction.read_checkpoint(saved,checkpoint)


@pytest.mark.parametrize('desktop',[False,True])
def test_http_context_rejection_compresses_and_retries_same_task_automatically(turn,monkeypatch,desktop):
    current=long_turn(turn);sent=[];original=httpx.Client
    monkeypatch.setenv('ATLAS_DESKTOP_MODE','1' if desktop else '0')
    monkeypatch.setattr('agent_service.network_policy.check_url',lambda url:None)
    def respond(request):
        body=json.loads(request.content);sent.append(body)
        if len(sent)==1:return httpx.Response(400,json={'error':{'code':'context_length_exceeded','message':'maximum context length exceeded'}})
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'kind':'final','answer':'仍需核对完整结果','status':'needs_input'})}}],'usage':{'completion_tokens':10}})
    monkeypatch.setattr(provider.httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(respond),**kwargs))
    with provider.task_scope(current['id']):result=agent.plan(current)
    assert result.kind=='final' and len(sent)==2
    assert len(json.dumps(sent[1],ensure_ascii=False))<len(json.dumps(sent[0],ensure_ascii=False))
    assert json.loads(sent[1]['messages'][1]['content'])['request']==current['text']
    saved=get('turn',turn['id']);assert saved['context_window']['recoveries']==1 and saved['model_usage']['calls']==2
    assert saved['agent_trace']==current['agent_trace'],'no tool replay during model retry'


@pytest.mark.parametrize('status,code',[(401,'invalid_api_key'),(429,'insufficient_quota'),(500,'server_error'),(400,'invalid_request_error')])
def test_unrelated_provider_errors_are_never_treated_as_context_compression(status,code):
    request=httpx.Request('POST','https://fixture.test/v1/chat/completions')
    response=httpx.Response(status,request=request,json={'error':{'code':code,'message':'A different provider failure'}})
    error=httpx.HTTPStatusError('not context',request=request,response=response)
    assert not provider.context_overflow_error(error)


def test_native_retry_preserves_successful_writes_and_original_model(turn,monkeypatch):
    from agent_service import opencode_runtime,workspace
    current=long_turn(turn);attempts=[]
    monkeypatch.setattr(opencode_runtime,'is_enabled',lambda:True)
    def session(value,prompt,info,callback):
        attempts.append((value,info))
        if len(attempts)==1:
            callback({'kind':'tool','tool':'write_file','arguments':{'path':'reports/native.md','content':'only once'}},'old-session:write')
            patch('turn',turn['id'],opencode_session_id='old-session')
            raise provider.ContextWindowExceeded(characters=get('turn',turn['id'])['context_metrics']['request_characters'])
        assert value.get('opencode_session_id') is None
        assert any(step.get('tool')=='write_file' for step in info['completed_steps'])
        callback({'kind':'final','answer':'已保存报告','status':'complete'},'new-session:finish')
        return {'state':'done','integration':'fixture'}
    monkeypatch.setattr(opencode_runtime,'run_session',session)
    agent.advance(current);saved=get('turn',turn['id'])
    assert len(attempts)==2 and saved['state']=='done' and saved['outcome']=='verified_complete'
    assert workspace.read_file(turn['project_id'],'reports/native.md')=='only once'
    assert len([s for s in saved['agent_trace'] if s['tool']=='write_file'])==1
    assert 'old-session' in saved['opencode_retired_sessions']


def test_repeated_context_rejection_is_bounded_without_resetting_usage_or_grants(turn):
    error=provider.ContextWindowExceeded(characters=100000)
    for _ in range(3):assert context_window.recover(get('turn',turn['id']),error)
    assert not context_window.recover(get('turn',turn['id']),error)
    assert get('turn',turn['id'])['context_window']['recoveries']==3
    patch('turn',turn['id'],state='done',outcome='cancelled')
    assert not context_window.recover(get('turn',turn['id']),error)


def test_budget_failure_is_not_compressed_as_if_it_were_a_model_window(turn,monkeypatch):
    monkeypatch.setattr(provider,'complete',lambda *args,**kwargs:(_ for _ in ()).throw(provider.BudgetExceeded('explicit external grant')))
    with pytest.raises(provider.BudgetExceeded,match='external grant'):agent.plan(turn)
    assert not get('turn',turn['id']).get('context_window')
