"""Conversation focus, interrupts, safe retries and evidence use real storage/Git."""
import json,threading
from pathlib import Path
import httpx,pytest,duckdb
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN,DATA
from agent_service.store import get,put,patch,all_items,now
from agent_service.conversation import create_project,ProjectInput,SessionInput,create_session,submit,TurnInput,tick,handle,default_session
from agent_service import provider,workspace,agent

client=TestClient(app);H={'Authorization':'Bearer '+TOKEN}

@pytest.fixture
def project(request):
    pid=create_project(ProjectInput(name='对话可靠性',request_id='reliability-'+request.node.name))['id']
    yield pid
    for t in all_items('turn'):
        if t['project_id']==pid and t['state'] not in ('done','failed'):patch('turn',t['id'],state='done')
    from agent_service.jobs import list_jobs,request_cancel,ACTIVE
    targets={v['id'] for kind in ('source','pipeline') for v in all_items(kind) if v.get('project_id')==pid}
    for target in targets:
        for job in list_jobs(target=target):
            if job['status'] in ACTIVE:request_cancel(job['id'])

def send(pid,text,key,**kwargs):return submit(pid,TurnInput(text=text,request_id=key,**kwargs))['id']

def complete(tid):
    for _ in range(30):
        tick();turn=get('turn',tid)
        if turn['state'] in ('done','failed'):return turn
    raise AssertionError(get('turn',tid))

def fake_app(pid,key,name):
    sid='source-'+key;put('source',{'project_id':pid,'name':name+'资料','kind':'file','status':'ready','profile':{'fields':[]}},sid)
    return put('pipeline',{'project_id':pid,'name':name,'source_id':sid,'application_spec':{'title':name},'commit':'a'*40,'cron':None,'published_run':None,'candidate_run':None},'app-'+key)


def test_session_focus_is_frozen_and_ambiguity_is_conversational(project):
    a=fake_app(project,'focus-a','收款追踪');b=fake_app(project,'focus-b','库存追踪')
    other=create_session(project,SessionInput(request_id='focus-session-2'))['id']
    first=send(project,'看看收款追踪','focus-first');second=send(project,'看看库存追踪','focus-second',session_id=other)
    patch('project',project,pipeline_id=b['id'],source_id=b['source_id'])
    again=send(project,'重跑这个应用','focus-again')
    assert get('turn',first)['application_id']==a['id'] and get('turn',second)['application_id']==b['id']
    assert get('turn',again)['application_id']==a['id']
    cleared=send(project,'重跑这个应用','focus-cleared',clear_application=True)
    handle(get('turn',cleared));t=get('turn',cleared)
    assert len(t['suggestions'])==2 and '多个应用' in t['answer']
    attached=send(project,'分析这份资料','focus-attached',source_id=b['source_id'])
    assert not get('turn',attached).get('application_id')


def test_stop_only_cancels_its_conversation_and_is_idempotent(project):
    other=create_session(project,SessionInput(request_id='stop-session-2'))['id']
    a=send(project,'第一项任务','stop-first');b=send(project,'另一段对话','stop-second',session_id=other)
    sid=send(project,'停止','stop-command')
    assert get('turn',a)['outcome']=='cancelled' and get('turn',b)['state']=='queued'
    assert sid==send(project,'停止','stop-command')


def test_interrupt_during_model_call_discards_late_write(project,monkeypatch):
    started=threading.Event();release=threading.Event()
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://model.invalid/v1','model':'simulated','share_data':True})
    def slow(*args,**kwargs):
        started.set();assert release.wait(5)
        return json.dumps({'kind':'tool','tool':'write_file','arguments':{'path':'late.md','content':'must not be written'}})
    monkeypatch.setattr(provider,'complete',slow)
    tid=send(project,'帮我写报告','interrupt-original')
    worker=threading.Thread(target=tick);worker.start()
    try:
        assert started.wait(3)
        stop=send(project,'停止','interrupt-stop')
        assert get('turn',stop)['state']=='done' and get('turn',tid)['outcome']=='cancelled'
    finally:release.set();worker.join(8)
    assert not worker.is_alive() and not (workspace.folder(project)/'files/late.md').exists()


def test_model_failure_retries_then_continues_without_repeating_completed_write(project,monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://model.invalid/v1','model':'simulated','share_data':True})
    calls=[]
    def model(messages,**kwargs):
        calls.append(messages)
        if len(calls)==1:return json.dumps({'kind':'tool','tool':'write_file','arguments':{'path':'report.md','content':'第一阶段成果'}})
        raise httpx.HTTPStatusError('do not expose provider body',request=httpx.Request('POST','https://model.invalid'),response=httpx.Response(503))
    monkeypatch.setattr(provider,'complete',model)
    tid=send(project,'写报告并总结','retry-original')
    tick();path=workspace.folder(project)/'files/report.md';assert path.read_text()=='第一阶段成果'
    stamp=path.stat().st_mtime_ns
    for _ in range(3):
        patch('turn',tid,model_retry_at=0);tick()
    paused=get('turn',tid);assert paused['state']=='failed' and paused['resume_available'] and len(paused['agent_trace'])==1
    assert 'model.invalid' not in paused['answer'] and 'provider body' not in paused['answer']
    captured=[]
    def resumed(messages,**kwargs):captured.append(messages);return json.dumps({'kind':'final','answer':'报告已保留，继续处理完成。'})
    monkeypatch.setattr(provider,'complete',resumed)
    result=complete(send(project,'继续','retry-resume'))
    assert result['state']=='done' and result['resumed_from']==tid and len(result['agent_trace'])==1
    assert path.stat().st_mtime_ns==stamp and not result['work_version']['changed']
    context=json.loads(captured[0][-1]['content']);assert context['request']=='写报告并总结' and context['completed_steps'][0]['tool']=='write_file'


def test_schedule_model_must_match_explicit_time_and_reject_question(project):
    base={'project_id':project,'interaction_mode':'work','text':'每天上午9点更新'}
    agent.authorize(base,'schedule_daily',{'hour':9,'minute':0})
    with pytest.raises(ValueError):agent.authorize(base,'schedule_daily',{'hour':3,'minute':0})
    with pytest.raises(ValueError):agent.authorize(dict(base,text='不要每天上午9点更新'),'schedule_daily',{'hour':9,'minute':0})
    with pytest.raises(ValueError):agent.authorize(dict(base,text='每天上午9点更新是什么意思？'),'schedule_daily',{'hour':9,'minute':0})


def test_change_summary_uses_all_groups_not_preview_and_never_exposes_rows(project):
    from agent_service.experience import comparison,freshness,attention
    from agent_service.jobs import save
    p=fake_app(project,'difference','差异应用');ids=[]
    for suffix,amount in [('before',1),('after',2)]:
        rid='comparison-'+suffix;folder=DATA/'runs'/rid;folder.mkdir(parents=True)
        with duckdb.connect(str(folder/'warehouse.duckdb')) as con:
            con.execute('create schema business')
            con.execute('create table business.business_objects as select cast(i as varchar) object_id,cast(i as varchar) c_1,?::decimal(18,4) c_2 from range(51) t(i)',[amount])
            con.execute('create table business.summary as select c_1 category,c_2 amount_total from business.business_objects')
        spec={'primary_key':'c_1','group_by':'c_1','amount_field':'c_2','currency':'CNY'}
        manifest={'commit':'a'*40,'run_id':rid,'records':51,'fields':[{'name':'c_1'},{'name':'c_2'}],'spec':spec,'summary':[{'amount_total':str(amount)}]*50,'application':{'title':'差异应用'}}
        (folder/'output.json').write_text(json.dumps(manifest))
        save({'id':rid,'kind':'pipeline','target':p['id'],'status':'validated','validated':True,'artifact_dir':'runs/'+rid,'commit':'a'*40});ids.append(rid)
    p=patch('pipeline',p['id'],published_run=ids[0],candidate_run=ids[1],data_as_of='2000-01-01T00:00:00+00:00')
    diff=comparison(p)
    assert diff['metric_delta']=='51.0000' and diff['changed']==51 and diff['added']==diff['removed']==0
    assert 'rows' not in diff and freshness(p)['stale']
    assert '数据已超过设定时效' in attention(project)[0]['issues']


def test_structured_connection_preserves_goal_without_exposing_credentials(project):
    from agent_service.conversation import connect
    tid=send(project,'配置连接','goal-connect-turn')
    body={'name':'目标API','kind':'api','config':{'url':'https://example.com/data'},'goal':'按客户汇总'}
    assert connect(get('turn',tid),json.dumps(body))
    t=get('turn',tid)
    assert not t['connect_only'] and t['workflow_goal']=='按客户汇总' and not t['secret_text']
    public=client.get('/api/projects/'+project+'/conversation',headers=H).json()
    assert 'example.com' not in json.dumps(public)


def test_new_connection_leaves_previous_app_focus(project):
    application=fake_app(project,'connection-focus','旧应用')
    body={'name':'新的API','kind':'api','config':{'url':'https://example.com/data'},'goal':'按客户汇总'}
    tid=send(project,json.dumps(body),'connection-from-app',application_id=application['id'])
    handle(get('turn',tid));turn=get('turn',tid)
    assert turn['state']=='waiting_source' and turn['application_id'] is None
    from agent_service.conversation import session_application
    assert session_application(project,default_session(project)) is None


@pytest.mark.parametrize(('text','cron'),[
    ('每天上午9点更新','0 9 * * *'),('工作日18:30更新','30 18 * * 1-5'),
    ('每周一9点更新','0 9 * * 1'),('每周日晚上8点更新','0 20 * * 0'),
    ('每2小时更新','0 */2 * * *')])
def test_explicit_cadences_and_ambiguous_or_negative_requests(text,cron):
    from agent_service.cadence import parse
    assert parse(text)['cron']==cron
    assert parse('解释'+text+'是什么意思？') is None
    assert parse('不要'+text) is None
    assert parse('帮我按客户汇总，'+text,embedded=True)['cron']==cron
    with pytest.raises(ValueError):parse('每5小时更新')
    with pytest.raises(ValueError):parse('每天25:00更新')


def test_ordinary_conversation_ignores_legacy_step_budget_and_keeps_audit(project,monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://model.invalid/v1','model':'simulated','share_data':True})
    count=[0]
    def vary(*args,**kwargs):
        count[0]+=1
        if count[0]>20:return json.dumps({'kind':'final','answer':'已有信息已核对，当前缺少来源资料。','status':'needs_input'})
        return json.dumps({'kind':'tool','tool':'project_context' if count[0]%2 else 'list_data_applications'})
    monkeypatch.setattr(provider,'complete',vary)
    tid=send(project,'梳理项目资料并制定方案','budget-original')
    from agent_service.tasks import ensure
    from agent_service.task_budget import LEGACY_DEFAULTS
    contract=ensure(get('turn',tid))
    saved={**contract,'budget_policy':'bounded','budgets':dict(LEGACY_DEFAULTS)}
    patch('turn',tid,task_contract=saved)
    result=complete(tid)
    assert result['outcome']=='needs_input' and len(result['agent_trace'])==20 and count[0]==21
    assert result['task_contract']['budgets']==LEGACY_DEFAULTS,'historical contract remains available for audit'


def test_identical_operation_loop_stops_before_consuming_entire_budget(project,monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://model.invalid','model':'simulated','share_data':True})
    monkeypatch.setattr(provider,'complete',lambda *a,**k:json.dumps({'kind':'tool','tool':'project_context'}))
    result=complete(send(project,'了解项目资料','repeat-loop'))
    assert result['outcome']=='paused' and result['resume_available']
    actual=[step for step in result['agent_trace'] if step.get('observation',{}).get('executed') is not False]
    blocked=[step for step in result['agent_trace'] if step.get('observation',{}).get('executed') is False]
    assert len(actual)==2 and all(step['status']=='done' for step in actual)
    assert len(blocked)==2 and all(step['status']=='failed' and step['observation']['code']=='TOOL_NO_PROGRESS' for step in blocked)


def test_continue_button_binds_specific_task_and_rejects_other_conversation(project,monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://model.invalid/v1','model':'simulated','share_data':True})
    first=send(project,'第一项任务','specific-resume-first');patch('turn',first,state='failed',resume_available=True)
    second=send(project,'第二项任务','specific-resume-second');patch('turn',second,state='failed',resume_available=True)
    monkeypatch.setattr(provider,'complete',lambda *args,**kwargs:json.dumps({'kind':'final','answer':'第一项任务已继续。'}))
    resumed=complete(send(project,'继续','specific-resume-action',resume_turn_id=first))
    assert resumed['resumed_from']==first and get('turn',second)['resume_available']
    assert send(project,'继续','specific-resume-action',resume_turn_id=first)==resumed['id']
    other=create_session(project,SessionInput(request_id='specific-resume-other'))['id']
    with pytest.raises(ValueError):send(project,'继续','specific-resume-denied',resume_turn_id=second,session_id=other)
