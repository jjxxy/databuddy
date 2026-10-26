import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN,DATA
from agent_service.store import get,put,all_items,patch,seal,delete
from agent_service.conversation import tick
from agent_service import provider,workspace
from agent_service.execution import process_once

client=TestClient(app);H={'Authorization':'Bearer '+TOKEN}

def create(key):return client.post('/api/projects',headers=H,json={'name':'研究项目','request_id':key}).json()['id']
def send(pid,text,key,**kwargs):
    r=client.post(f'/api/projects/{pid}/messages',headers=H,json={'text':text,'request_id':key,**kwargs});assert r.status_code==200,r.text
    return r.json()['id']
def run_until_done(tid,steps=20):
    for _ in range(steps):
        tick();t=get('turn',tid)
        if t['state'] in ('done','failed'):return t
    raise AssertionError(get('turn',tid))
def fake_model(monkeypatch,steps):
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://model.invalid/v1','model':'test','api_key':'never-reveal-this-key','share_data':True})
    requests=[]
    def call(messages,**kwargs):
        requests.append(messages)
        assert steps,'Unexpected model call'
        return json.dumps(steps.pop(0),ensure_ascii=False)
    monkeypatch.setattr(provider,'complete',call)
    return requests

def tool(name,**args):return {'kind':'tool','tool':name,'arguments':args}
def final(answer):return {'kind':'final','answer':answer}


def test_real_agent_multi_step_context_modes_versions_and_cross_session_restore(monkeypatch):
    pid=create('workspace-agent-1')
    session=client.post(f'/api/projects/{pid}/sessions',headers=H,json={'name':'报告讨论','request_id':'second-session-1'}).json()['id']
    original='  # 项目口径\n只统计已付款订单。\n\n'
    steps=[tool('project_context'),tool('write_file',path='reports/brief.md',content=original),tool('read_file',path='reports/brief.md'),final('已保存报告，约定只统计已付款订单。')]
    captured=fake_model(monkeypatch,steps)
    t1=run_until_done(send(pid,'写一份报告，约定只统计已付款订单','workspace-turn-1'))
    assert t1['state']=='done' and t1['work_version']['changed']
    version1=t1['work_version']['commit']
    assert workspace.read_file(pid,'reports/brief.md')==original
    assert all(s['status']=='done' for s in t1['agent_trace'])
    assert 'never-reveal-this-key' not in json.dumps(captured)
    assert 'never-reveal-this-key' not in workspace.git(workspace.folder(pid),'show',version1+':project-state.json')
    before_count=len(workspace.history(pid))
    steps.extend([tool('search_project_history',query='订单口径'),final('之前约定只统计已付款订单。')])
    chat=run_until_done(send(pid,'之前订单口径是什么','workspace-chat-1',interaction_mode='chat',session_id=session))
    assert '已付款' in chat['answer'] and not chat.get('work_version')
    assert len(workspace.history(pid))==before_count
    assert chat['agent_trace'][0]['observation']['matches'][0]['turn_id']==t1['id']
    # The model cannot write even when it emits a write tool in chat mode.
    steps.extend([tool('write_file',path='bad.md',content='forbidden'),final('聊天模式只讨论，不改文件。')])
    readonly=run_until_done(send(pid,'帮我改报告','workspace-chat-2',interaction_mode='chat',session_id=session))
    assert readonly['state']=='done' and not (workspace.folder(pid)/'files/bad.md').exists()
    steps.extend([tool('write_file',path='reports/brief.md',content='第二版\n'),tool('write_file',path='extra.md',content='新增\n'),final('已修改报告并新增文件。')])
    t2=run_until_done(send(pid,'修改报告并新增文件','workspace-turn-2',session_id=session))
    assert t2['work_version']['commit']!=version1
    total_turns=len([t for t in all_items('turn') if t['project_id']==pid])
    # Rollback is deterministic and works without a provider; later history survives.
    restored=run_until_done(send(pid,'回退到 '+version1,'workspace-restore-1',session_id=session))
    assert restored['state']=='done',restored
    assert workspace.read_file(pid,'reports/brief.md')==original
    assert not (workspace.folder(pid)/'files/extra.md').exists()
    assert restored['work_version']['commit']!=version1
    assert workspace.resolve(pid,t2['work_version']['commit'])
    assert len([t for t in all_items('turn') if t['project_id']==pid])==total_turns+1
    # A no-change work turn links to the same head and creates no empty Git commit.
    current=restored['work_version']['commit'];steps.append(final('没有修改文件。'))
    unchanged=run_until_done(send(pid,'只解释一下结果，不要改文件','workspace-turn-3'))
    assert unchanged['work_version']['commit']==current and not unchanged['work_version']['changed']
    a=client.get(f'/api/projects/{pid}/conversation?session_id={session}',headers=H).json()
    assert a['session_id']==session and all(t.get('session_id')==session for t in a['turns'])
    assert client.get(f'/api/projects/{pid}/repository.zip',headers=H).content.startswith(b'PK')
    assert client.get(f'/api/projects/{pid}/file?path=../../metadata.sqlite',headers=H).status_code==400


def test_agent_builds_real_pipeline_waits_inspects_and_publishes(monkeypatch):
    pid=create('agent-real-build-1')
    r=client.post(f'/api/projects/{pid}/attachments',headers=H,files={'file':('orders.csv','订单号,客户,金额\nQ1,甲,10\nQ2,乙,20\n'.encode(),'text/csv')},data={'request_id':'agent-build-upload'})
    sid=r.json()['id'];assert process_once()
    s=get('source',sid);assert s['status']=='ready'
    steps=[tool('source_schema',source_id=sid),tool('build_transform',source_id=sid,spec=s['proposed_spec']),tool('view_result'),final('已生成并检查候选结果，请确认发布。')]
    fake_model(monkeypatch,steps)
    tid=send(pid,'按客户汇总并检查结果','agent-real-message',source_id=sid)
    tick();tick();assert get('turn',tid)['state']=='waiting_run'
    assert process_once();t=run_until_done(tid)
    assert t['state']=='done' and t['result']['records']==2 and t['result']['candidate']
    assert t['work_version']['changed']
    p=get('pipeline',get('project',pid)['pipeline_id'])
    assert not p.get('published_run')
    root=workspace.folder(pid)
    assert (root/'pipelines'/p['id']/'dbt/models/business_objects.sql').exists()
    # Publishing cannot be authorized by the model alone.
    steps.extend([tool('publish_result'),final('需你明确确认发布。')])
    denied=run_until_done(send(pid,'结果怎么样','agent-denied-publish'))
    assert denied['agent_trace'][0]['status']=='failed'
    assert not get('pipeline',p['id']).get('published_run')
    steps.extend([tool('publish_result'),final('已发布真实结果。')])
    published=run_until_done(send(pid,'确认发布','agent-confirm-publish'))
    assert get('pipeline',p['id'])['published_run'] and not published['result']['candidate']
    # Restore the pre-publication snapshot, including candidate and published pointers.
    restored=run_until_done(send(pid,'回退到 '+t['work_version']['commit'],'agent-restore-candidate'))
    assert restored['state']=='done',restored
    p=get('pipeline',p['id']);assert not p['published_run'] and p['candidate_run']


def test_restore_journal_recovers_and_missing_original_fails_before_mutation(monkeypatch):
    pid=create('restore-journal-1')
    workspace.write_file(pid,'memo.md','before\n');first=workspace.checkpoint(pid)
    workspace.write_file(pid,'memo.md','after\n');workspace.checkpoint(pid)
    original_checkpoint=workspace._checkpoint
    monkeypatch.setattr(workspace,'_checkpoint',lambda *args:(_ for _ in ()).throw(RuntimeError('crash at commit')))
    with pytest.raises(RuntimeError):workspace.restore(pid,first['commit'],'restore-interrupted')
    assert get('restore','restore-'+pid)['state']=='applying'
    assert client.post('/api/projects',headers=H,json={'name':'blocked','request_id':'blocked-during-restore'}).status_code==503
    monkeypatch.setattr(workspace,'_checkpoint',original_checkpoint);workspace.recover()
    assert get('restore','restore-'+pid)['state']=='done'
    assert workspace.read_file(pid,'memo.md')=='before\n'
    # Missing source bytes cannot partially restore files or metadata.
    sid='source-for-pruned-original'
    put('source',{'project_id':pid,'kind':'file','name':'missing.csv','status':'ready','secret_config':seal({'stored_name':'never-existed.csv'})},sid)
    missing=workspace.checkpoint(pid)
    workspace.write_file(pid,'memo.md','current\n');workspace.checkpoint(pid)
    with pytest.raises(ValueError,match='原件'):workspace.restore(pid,missing['commit'],'restore-missing')
    assert workspace.read_file(pid,'memo.md')=='current\n'
    delete('source',sid)


def test_model_connection_credentials_are_sealed_and_not_returned(monkeypatch):
    from agent_service import connectors
    monkeypatch.setattr(connectors,'safe_api_url',lambda url:'203.0.113.1')
    monkeypatch.setattr(provider,'complete',lambda *args,**kwargs:'OK')
    monkeypatch.setenv('ATLAS_OPERATOR_TOKEN','fixture-operator-token-123456789')
    operator_headers=dict(H,**{'X-Atlas-Operator-Token':'fixture-operator-token-123456789'})
    try:
        r=client.put('/api/model',headers=operator_headers,json={'base_url':'https://model.example/v1','model':'strong-model','api_key':'sensitive-model-key','share_data':False})
        assert r.status_code==200,r.text
        assert r.json()['configured'] and 'sensitive-model-key' not in r.text
        saved=get('system','model-provider')
        assert 'sensitive-model-key' not in json.dumps(saved)
        assert provider.settings()['api_key']=='sensitive-model-key'
        assert 'api_key' not in client.get('/api/model',headers=operator_headers).json()
    finally:delete('system','model-provider')
