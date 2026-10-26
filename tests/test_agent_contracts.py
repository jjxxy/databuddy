import json
from pathlib import Path
import httpx,pytest
from agent_service import agent,analysis,provider,tasks,workspace
from agent_service.store import put,get,patch,all_items,now,uid

def task(text,**fields):
    pid='p_'+uid();put('project',{'name':'验收','source_id':None,'pipeline_id':None},pid)
    tid='t_'+uid();put('turn',{'project_id':pid,'text':text,'interaction_mode':'work','state':'done','created_at':now(),**fields},tid)
    return get('turn',tid)

@pytest.mark.parametrize('text,tool,args',[
 ('不要重跑','run_data_application',{'application_id':'irrelevant'}),
 ('不要暂停自动更新','pause_updates',{}),
 ('解释一下“重跑这个应用”是什么意思','run_data_application',{}),
 ('如果我说发布结果会怎样','publish_result',{}),
 ('别修改文件','write_file',{}),
 ('不要删除文件','delete_file',{}),
 ('解释如何改名为销售','rename_project',{'name':'销售'}),
 ('不要创建应用','create_data_application',{}),
 ('不用切换来源','switch_source',{}),
])
def test_discussion_and_veto_cannot_authorize_mutations(text,tool,args):
    turn=task(text)
    with pytest.raises(ValueError):agent.authorize(turn,tool,args)

@pytest.mark.parametrize('text',['根据公司发文创建报销明细应用，先列明细并说明依据','创建报销应用并解释口径'])
def test_explicit_build_with_explanation_is_still_a_build(text):
    turn=task(text)
    assert tasks.ensure(turn)['kind']=='build'
    agent.authorize(get('turn',turn['id']),'create_data_application',{})

@pytest.mark.parametrize('question',['已付款的有多少条？','金额超过100的数量','今年的订单有几条','客户甲的金额','退款最多的是谁'])
def test_conditional_questions_never_receive_unfiltered_count(monkeypatch,tmp_path,question):
    monkeypatch.setattr(provider,'settings',lambda:{})
    manifest={'records':999,'summary':[],'commit':'a'*40,'run_id':'run','spec':{}}
    value=analysis.ask(tmp_path,manifest,question)
    assert value['mode']=='needs_input' and '999' not in value['answer']

def test_failed_model_sql_does_not_answer_a_different_question(monkeypatch,tmp_path):
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://model.invalid','model':'test'})
    monkeypatch.setattr(provider,'complete',lambda *a,**k:'{"sql":"delete from business.business_objects"}')
    value=analysis.ask(tmp_path,{'records':77,'summary':[],'spec':{},'fields':[]},'已付款的有多少条？')
    assert value['mode']=='needs_input' and '77' not in value['answer']

def test_frozen_target_survives_project_focus_changes():
    t=task('重跑这个应用',application_id='bound_'+uid());pid=t['project_id'];bound=t['application_id'];other='other_'+uid()
    for name in (bound,other):put('pipeline',{'project_id':pid,'name':name},name)
    tasks.ensure(t);patch('project',pid,pipeline_id=other)
    assert agent.target_pipeline(get('turn',t['id']),{})['id']==bound
    with pytest.raises(ValueError,match='本轮任务范围'):agent.target_pipeline(get('turn',t['id']),{'pipeline_id':other})

def test_completion_requires_real_artifact_and_cannot_repeat_false_success():
    t=task('写一份报告');tasks.ensure(t)
    patch('turn',t['id'],state='agent')
    step={'kind':'final','answer':'已写好报告并且测试通过'}
    agent._execute_node({'turn_id':t['id'],'step':step})
    assert get('turn',t['id'])['state']=='agent'
    agent._execute_node({'turn_id':t['id'],'step':step})
    done=get('turn',t['id'])
    assert done['outcome']=='blocked' and '已写好报告' not in done['answer']
    workspace.write_file(t['project_id'],'report.md','verified')
    patch('turn',t['id'],agent_trace=[{'tool':'write_file','status':'done','arguments':{'path':'report.md','content':'verified'}}])
    assert tasks.verify(get('turn',t['id']))['verified']
    workspace.write_file(t['project_id'],'report.md','different')
    assert not tasks.verify(get('turn',t['id']))['verified']

def test_model_budget_usage_and_secret_free_failure_receipts(monkeypatch):
    t=task('讨论');tasks.ensure(t)
    configuration={'base_url':'https://model.invalid/v1','model':'test','api_key':'top-secret'}
    monkeypatch.setattr(provider,'settings',lambda:configuration)
    def reply(request):return httpx.Response(200,json={'choices':[{'message':{'content':'OK'}}],'usage':{'prompt_tokens':11,'completion_tokens':3,'total_tokens':14}})
    real_client=httpx.Client
    monkeypatch.setattr(provider.httpx,'Client',lambda **kwargs:real_client(transport=httpx.MockTransport(reply)))
    with provider.task_scope(t['id']):assert provider.complete([{'role':'user','content':'private prompt'}])=='OK'
    usage=get('turn',t['id'])['model_usage'];assert usage['calls']==1 and usage['total_tokens']==14
    event=next(x for x in all_items('model_event') if x.get('turn_id')==t['id'])
    assert event['status']=='succeeded' and 'private prompt' not in json.dumps(event) and 'top-secret' not in json.dumps(event)
    contract=get('turn',t['id'])['task_contract'];contract['budget_policy']='bounded';contract['budgets']['model_calls']=1;patch('turn',t['id'],task_contract=contract)
    with provider.task_scope(t['id']),pytest.raises(provider.BudgetExceeded):provider.complete([{'role':'user','content':'second'}])
    assert get('turn',t['id'])['model_usage']['calls']==1

def test_business_memory_requires_user_quote_and_is_versioned():
    from agent_service.knowledge import remember,rules
    t=task('记住口径：只统计已付款订单')
    with pytest.raises(ValueError):remember(t,'paid','只统计已发货订单')
    remember(t,'paid','只统计已付款订单')
    first=workspace.checkpoint(t['project_id'])
    assert rules(t['project_id'])[0]['source_turn_id']==t['id']
    workspace.write_file(t['project_id'],'notes/business-rules.json','[]')
    workspace.checkpoint(t['project_id']);workspace.restore(t['project_id'],first['commit'],t['id'])
    assert rules(t['project_id'])[0]['quote']=='只统计已付款订单'

def test_unconfigured_runner_cannot_execute_generated_code(monkeypatch):
    from agent_service import isolation
    monkeypatch.delenv('ATLAS_RUNNER_URL',raising=False)
    t=task('运行 scripts/check.py 脚本')
    assert not isolation.status()['configured']
    with pytest.raises(ValueError,match='尚未配置'):isolation.run(t,'scripts/check.py',[],20)
    assert tasks.ensure(t)['kind']=='execution'
    assert not tasks.verify(get('turn',t['id']))['verified']

def test_remote_runner_protocol_binds_inputs_and_enforces_limits(monkeypatch):
    from agent_service import isolation,connectors
    monkeypatch.setenv('ATLAS_RUNNER_URL','https://runner.invalid')
    monkeypatch.setenv('ATLAS_RUNNER_TOKEN','runner-secret')
    monkeypatch.setenv('ATLAS_RUNNER_ISOLATION','gvisor')
    monkeypatch.setenv('RAILWAY_TOKEN','never-send-host-env')
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    monkeypatch.setattr(connectors,'safe_api_url',lambda url:'127.0.0.1')
    t=task('运行 scripts/check.py 脚本',agent_trace=[{'id':'persisted-step','tool':'run_python_file','status':'started','arguments':{'path':'scripts/check.py'}}])
    workspace.write_file(t['project_id'],'scripts/check.py','print(42)\n')
    received=[]
    def reply(request):
        body=json.loads(request.content);received.append(body)
        assert body['network']=='none' and body['memory_mb']==256 and body['timeout_seconds']==20
        assert 'never-send-host-env' not in request.content.decode() and 'runner-secret' not in request.content.decode()
        return httpx.Response(200,json={**{k:body[k] for k in ('idempotency_key','input_sha256','isolation','network')},'execution_id':'simulated-execution','exit_code':0,'stdout':'42\n'})
    original=httpx.Client
    monkeypatch.setattr(isolation.httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(reply)))
    first=isolation.run(t,'scripts/check.py',[],20);isolation.run(t,'scripts/check.py',[],20)
    assert received[0]['idempotency_key']==received[1]['idempotency_key']
    trace=t['agent_trace'];trace[0].update(status='done',observation=first)
    patch('turn',t['id'],agent_trace=trace)
    assert tasks.verify(get('turn',t['id']))['verified']
    workspace.write_file(t['project_id'],'scripts/check.py','print(43)\n')
    assert not tasks.verify(get('turn',t['id']))['verified']


def test_literal_creation_cannot_drop_user_punctuation():
    from agent_service import tasks,agent,workspace
    from agent_service.store import put,uid,get,now,patch
    pid='p_'+uid();put('project',{'name':'Exact literal'},pid)
    text='创建 reports/exact.md 文件，内容逐字为：OPENCODE-ATLAS-VERIFIED-2032。'
    turn=put('turn',{'project_id':pid,'text':text,'state':'agent','interaction_mode':'work','created_at':now()},'t_'+uid())
    contract=tasks.ensure(turn)
    assert contract['kind']=='file' and 'file_exact' in contract['requirements']
    import pytest
    with pytest.raises(ValueError,match='逐字'):agent.authorize(get('turn',turn['id']),'write_file',{'path':'reports/exact.md','content':'OPENCODE-ATLAS-VERIFIED-2032'})
    content='OPENCODE-ATLAS-VERIFIED-2032。'
    agent.authorize(get('turn',turn['id']),'write_file',{'path':'reports/exact.md','content':content})
    workspace.write_file(pid,'reports/exact.md',content)
    patch('turn',turn['id'],agent_trace=[{'tool':'write_file','status':'done','arguments':{'path':'reports/exact.md','content':content},'observation':{}}])
    assert tasks.verify(get('turn',turn['id']))['verified']
    workspace.write_file(pid,'reports/exact.md',content+'\n')
    assert 'file_exact' in tasks.verify(get('turn',turn['id']))['missing']
    patch('turn',turn['id'],state='done')


def test_literal_constraints_do_not_guess_ordinary_prose():
    from agent_service.tasks import literal_file
    assert literal_file('创建 reports/x.md，内容大致介绍工作计划。') is None
    assert literal_file('创建 reports/x.md，内容逐字为：```text\n带标点。\n```')['bytes']==len('带标点。'.encode())
    assert literal_file('创建 reports/x.md 和 reports/y.md，内容逐字为：不猜文件。') is None
