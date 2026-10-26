import hashlib,json
import httpx,pytest
from agent_service import agent,agent_context as ctx,provider,tasks,workspace
from agent_service.store import get,put,patch,uid,now

def task(text='写一份项目报告',**fields):
    pid='p_'+uid();put('project',{'name':'Harness验收'},pid)
    turn=put('turn',{'project_id':pid,'text':text,'interaction_mode':'work','state':'done','created_at':now(),**fields},'t_'+uid())
    tasks.ensure(turn);return get('turn',turn['id'])

def native_tools():return [{'type':'function','function':{'name':'finish_task','parameters':agent.FinalAnswer.model_json_schema()}}]

def mock_provider(monkeypatch,message,finish_reason='stop'):
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://model.test/v1','model':'native','api_key':'test-key'})
    requests=[];original=httpx.Client
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':message,'finish_reason':finish_reason}],'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}})
    monkeypatch.setattr(provider.httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(respond)))
    return requests

def test_native_function_call_not_text_json_and_schema_budget(monkeypatch):
    calls=mock_provider(monkeypatch,{'tool_calls':[{'function':{'name':'finish_task','arguments':json.dumps({'answer':'已核对','status':'complete'})}}]})
    t=task('解释工作模式')
    with provider.task_scope(t['id']):step=agent.Step.model_validate_json(provider.complete([{'role':'user','content':'解释'}],tools=native_tools()))
    assert step.kind=='final' and step.answer=='已核对'
    assert calls[0]['tool_choice']=='required' and calls[0]['parallel_tool_calls'] is False
    assert get('turn',t['id'])['model_usage']['input_characters']>len(json.dumps(calls[0]['messages']))

@pytest.mark.parametrize('message,reason',[
    ({'tool_calls':[{'function':{'name':'finish_task','arguments':'{}'}},{'function':{'name':'finish_task','arguments':'{}'}}]},'stop'),
    ({'tool_calls':[{'function':{'name':'hidden_shell','arguments':'{}'}}]},'stop'),
    ({'tool_calls':[{'function':{'name':'finish_task','arguments':'{broken'}}]},'stop'),
    ({'content':'{"kind":"final","answer":"fake"}'},'stop'),
    ({'content':'cut short'},'length'),
])
def test_native_invalid_or_partial_plan_never_executes(monkeypatch,message,reason):
    mock_provider(monkeypatch,message,reason)
    with pytest.raises(provider.ModelOutputError):provider.complete([{'role':'user','content':'test'}],tools=native_tools())

def test_native_connection_probe_is_required_and_credentials_stay_private(monkeypatch,tmp_path):
    # Configuration is global and correctly refuses changes during active work.
    # Other modules intentionally leave resumable synthetic turns in the shared
    # test database; this connection probe must use its own real empty store.
    from sqlalchemy import create_engine
    from agent_service import store
    isolated=create_engine('sqlite:///'+str(tmp_path/'connection-probe.sqlite'))
    store.metadata.create_all(isolated)
    monkeypatch.setattr(store,'engine',isolated)
    monkeypatch.setattr('agent_service.connectors.safe_api_url',lambda url:'203.0.113.1')
    calls=[]
    def complete(*args,**kwargs):
        calls.append(kwargs)
        return json.dumps({'kind':'tool','tool':'connection_check','arguments':{'ok':True}}) if kwargs.get('tools') else 'OK'
    monkeypatch.setattr(provider,'complete',complete)
    value=provider.ProviderInput(base_url='https://native-model.test/v1',model='test',api_key='disposable-native-key',tool_protocol='native')
    from agent_service.store import delete
    try:
        result=provider.configure(value)
        assert result['tool_protocol']=='native' and 'api_key' not in result and calls[-1]['tools']
    finally:
        delete('system','model-provider')
        isolated.dispose()

def test_dynamic_discovery_excludes_write_tools_in_chat_and_disabled_runner():
    t=task('你好',interaction_mode='chat')
    assert len(ctx.selected(t))<len(agent.TOOLS)//2
    found=ctx.discover(t,ctx.DiscoverTools(query='write_file delete_file run_python_file'))
    assert all(not agent.TOOLS[x['name']][1] for x in found['tools'])
    assert 'run_python_file' not in ctx.allowed(t)
    work=task('你好');ctx.discover(work,ctx.DiscoverTools(query='edit_file'))
    assert 'edit_file' in ctx.selected(get('turn',work['id']))

def test_result_context_can_recover_tail_and_is_task_scoped(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    full={'rows':[['value'] for _ in range(3000)]+[['TAIL-RECEIPT']]}
    t=task(agent_trace=[{'id':'result-step','tool':'query_sql','status':'done','arguments':{'sql':'select * from business.business_objects'},'observation':full}])
    excerpt=ctx.trace_context(t,agent.public_observation)[0]['observation']
    assert excerpt['not_full_result'] and 'TAIL-RECEIPT' not in json.dumps(excerpt)
    raw=json.dumps(full,ensure_ascii=False)
    recovered=ctx.read_result(t,ctx.ToolResult(step_id='result-step',offset=len(raw)-200,limit=200))
    assert 'TAIL-RECEIPT' in recovered['content'] and recovered['next_offset'] is None
    other=task()
    with pytest.raises(ValueError,match='没有该工具结果'):ctx.read_result(other,ctx.ToolResult(step_id='result-step'))
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':False})
    assert 'TAIL-RECEIPT' not in ctx.read_result(t,ctx.ToolResult(step_id='result-step'))['content']

def test_exact_edit_conflict_unique_fragment_and_recovery_after_crash(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    t=task('修改报告',agent_trace=[{'id':'edit-step','tool':'edit_file','status':'started','arguments':{}}])
    pid=t['project_id'];workspace.write_file(pid,'reports/a.md','head\nold rule\ntail\n')
    before=workspace.read_file(pid,'reports/a.md')
    edit=ctx.FileEdit(path='reports/a.md',expected_sha256=hashlib.sha256(before.encode()).hexdigest(),old_text='old rule',new_text='new rule')
    first=ctx.edit_file(pid,edit,t)
    # Simulate a worker crash after atomic file replacement, before observe().
    second=ctx.edit_file(pid,edit,t)
    assert first==second and workspace.read_file(pid,'reports/a.md')=='head\nnew rule\ntail\n'
    with pytest.raises(ValueError,match='版本已变化'):ctx.edit_file(pid,edit)
    patch('turn',t['id'],agent_trace=[{'tool':'edit_file','status':'done','arguments':edit.model_dump(),'observation':first}])
    assert tasks.verify(get('turn',t['id']))['verified']
    workspace.write_file(pid,'reports/a.md','other editor\n')
    assert not tasks.verify(get('turn',t['id']))['verified']

def test_project_file_search_has_paths_lines_and_coverage(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    t=task();workspace.write_file(t['project_id'],'notes/policy.md','header\n采购流程\n采购金额\n')
    result=ctx.search_files(t['project_id'],ctx.FileSearch(query='采购',path_glob='notes/*.md',limit=1))
    assert result['matches'][0]['line']==2 and result['next_offset']==1 and not result['complete_search']
    next_page=ctx.search_files(t['project_id'],ctx.FileSearch(query='采购',path_glob='notes/*.md',offset=1))
    assert next_page['matches'][0]['line']==3

def test_repetition_stops_unchanged_retry_but_allows_changed_arguments_and_resume():
    trace=[{'tool':'read_file','arguments':{'path':'x.md'},'status':'done'}]*2
    t=task(agent_trace=trace)
    assert ctx.repetition(t,agent.Step(kind='tool',tool='read_file',arguments={'path':'x.md'}))
    assert not ctx.repetition(t,agent.Step(kind='tool',tool='read_file',arguments={'path':'y.md'}))
    assert not ctx.repetition(dict(t,agent_budget_base=2),agent.Step(kind='tool',tool='read_file',arguments={'path':'x.md'}))

def test_native_agent_discovers_executes_and_finalizes(monkeypatch):
    t=task('请修改项目文件报告')
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://model.test','model':'native','tool_protocol':'native','share_data':True})
    seen=[]
    def respond(messages,**kwargs):
        seen.extend(x['function']['name'] for x in kwargs['tools'])
        return json.dumps({'kind':'tool','tool':'write_file','arguments':{'path':'reports/test.md','content':'verified'}})
    monkeypatch.setattr(provider,'complete',respond)
    step=agent.plan(t);assert step.tool=='write_file' and 'finish_task' in seen
    patch('turn',t['id'],state='agent')
    agent._execute_node({'turn_id':t['id'],'step':step.model_dump()})
    agent._execute_node({'turn_id':t['id'],'step':{'kind':'final','answer':'报告已保存'}})
    assert get('turn',t['id'])['outcome']=='verified_complete'

def test_capability_api_is_authenticated_and_does_not_invent_a_quality_score():
    from fastapi.testclient import TestClient
    from agent_service.api import app
    from agent_service.config import TOKEN
    client=TestClient(app)
    assert client.get('/api/agent/capabilities').status_code==401
    value=client.get('/api/agent/capabilities',headers={'Authorization':'Bearer '+TOKEN}).json()
    assert value['version']=='0.20.0' and value['intelligence_score'] is None
    assert value['chat_tools']<value['work_tools']
