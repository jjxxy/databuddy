from decimal import Decimal
"""Conversation acceptance: real ingestion/Git/dbt, isolation and durable retries."""
import json
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.store import get, all_items, patch, unseal
from agent_service.conversation import tick
from agent_service.execution import process_once

client=TestClient(app)
H={'Authorization':'Bearer '+TOKEN}

def project(key):
    r=client.post('/api/projects',headers=H,json={'name':'对话验收','request_id':key})
    assert r.status_code==200,r.text
    return r.json()['id']

def say(pid,text,key,source_id=None):
    r=client.post(f'/api/projects/{pid}/messages',headers=H,json={'text':text,'request_id':key,'source_id':source_id})
    assert r.status_code==200,r.text
    return r.json()['id']

def turns(pid):
    return client.get(f'/api/projects/{pid}/conversation',headers=H).json()['turns']

def test_conversation_real_processing_confirmation_schedule_and_revision():
    pid=project('conversation-main')
    assert project('conversation-main')==pid
    text='订单号,客户,金额,退款,状态\nX1,甲,100.25,10.10,已付款\nX2,乙,20,0,待付款\nX3,甲,30,2,已付款\n'
    r=client.post(f'/api/projects/{pid}/attachments',headers=H,files={'file':('chat.csv',text.encode(),'text/csv')},data={'request_id':'attach-request-1'})
    assert r.status_code==200,r.text
    source_id=r.json()['id']
    duplicate=client.post(f'/api/projects/{pid}/attachments',headers=H,files={'file':('chat.csv',text.encode(),'text/csv')},data={'request_id':'attach-request-1'})
    assert duplicate.json()['id']==source_id
    tid=say(pid,'按客户汇总，仅统计已付款订单，金额扣除退款','first-message-1',source_id)
    assert say(pid,'按客户汇总，仅统计已付款订单，金额扣除退款','first-message-1',source_id)==tid
    assert len(turns(pid))==1
    tick();assert get('turn',tid)['state']=='waiting_source'
    assert process_once()  # Actual source probe.
    tick();t=get('turn',tid);assert t['state']=='waiting_run',t
    pipeline_id=get('project',pid)['pipeline_id']
    assert len([p for p in all_items('pipeline') if p.get('origin_turn')==tid])==1
    # Simulate restart in the post-generation window; origin reuses the same Git commit/job.
    commit=get('pipeline',pipeline_id)['commit']
    patch('turn',tid,state='generating');tick()
    assert get('pipeline',pipeline_id)['commit']==commit
    assert get('turn',tid)['run_id']==t['run_id']
    assert process_once()  # Actual dlt + dbt + quality checks.
    tick();t=get('turn',tid);assert t['state']=='done',t
    assert t['result']['records']==2
    assert Decimal(t['result']['summary'][0]['amount_total'])==Decimal('118.15')
    assert t['result']['candidate']
    assert not get('pipeline',pipeline_id).get('published_run')
    say(pid,'每天上午9点更新','before-confirm-1');tick()
    assert not get('pipeline',pipeline_id).get('cron')
    say(pid,'确认发布','confirm-message-1');tick()
    assert get('pipeline',pipeline_id)['published_run']==t['run_id']
    say(pid,'每天上午9点更新','schedule-message-1');tick()
    assert get('pipeline',pipeline_id)['cron']=='0 9 * * *'
    say(pid,'有多少条记录','question-message-1');tick()
    assert '2 条' in turns(pid)[-1]['answer']
    say(pid,'导出结果','export-message-1');tick()
    url=turns(pid)[-1]['download']['url']
    assert client.get(url,headers=H).content.startswith(b'\xef\xbb\xbf')
    say(pid,'暂停自动更新','pause-message-1');tick()
    assert get('pipeline',pipeline_id)['cron'] is None
    say(pid,'只看明细','revision-message-1');tick()
    assert process_once();tick()
    revised=get('pipeline',pipeline_id)
    assert revised['commit']!=commit and revised['published_run']==t['run_id']
    assert turns(pid)[-1]['result']['candidate']
    assert revised['spec']['group_by'] is None
    assert revised['spec']['filters'][0]['value']=='已付款'


def test_conversation_project_boundary_secrets_and_unsupported_goal():
    a=project('boundary-project-a');b=project('boundary-project-b')
    r=client.post(f'/api/projects/{a}/attachments',headers=H,files={'file':('x.csv',b'id,amount\n1,5\n','text/csv')},data={'request_id':'boundary-attach-1'})
    source_id=r.json()['id']
    r=client.post(f'/api/projects/{b}/messages',headers=H,json={'text':'分析','request_id':'cross-project-1','source_id':source_id})
    assert r.status_code==400
    assert not client.get(f'/api/projects/{a}/conversation').is_success
    credential='postgresql://readonly:super-secret-value@db.example/orders'
    tid=say(b,credential,'secret-message-1')
    assert credential not in json.dumps(turns(b))
    assert unseal(get('turn',tid)['secret_text'])==credential
    tick()
    assert credential not in json.dumps(turns(b))
    # Unknown requests are clarified, never silently approximated to a default analysis.
    tid=say(a,'预测下个月的利润','unsupported-goal-1',source_id)
    assert process_once();tick()
    assert get('turn',tid)['state']=='done'
    assert not get('project',a).get('pipeline_id')
    say(a,'项目改名为研究空间','rename-message-1');tick()
    assert get('project',a)['name']=='研究空间'


def test_connection_details_collected_in_chat_and_hidden(monkeypatch):
    # Stub only the remote probe enqueue; verify real validation/storage and turn flow.
    import agent_service.execution as execution
    queued=[]
    monkeypatch.setattr(execution,'enqueue',lambda *args,**kwargs: queued.append(args) or {'id':'stub'})
    pid=project('natural-connection')
    raw='postgresql://readonly:private-value@db.example/customerdb'
    say(pid,raw,'natural-connect-1');tick()
    p=get('project',pid)
    assert p['pending_connection'] and 'private-value' not in json.dumps(turns(pid))
    assert 'pending_connection' not in client.get(f'/api/projects/{pid}/conversation',headers=H).json()['project']
    say(pid,'表名 orders，确认使用只读账号','natural-connect-2');tick()
    p=get('project',pid)
    source=get('source',p['source_id'])
    cfg=unseal(source['secret_config'])
    assert cfg['connection_string']==raw and cfg['table']=='orders' and cfg['read_only_ack'] is True
    assert source['project_id']==pid and queued==[('probe',source['id'])]
    assert not p.get('pending_connection')
    assert raw not in json.dumps(turns(pid))
    # Stop pending stub probe, so it cannot interfere with unrelated test queues.
    from agent_service.store import delete
    for t in all_items('turn'):
        if t['project_id']==pid and t['state'] not in ('done','failed'):patch('turn',t['id'],state='done')
    delete('source',source['id'])


def test_model_plans_are_validated_and_do_not_authorize_operations(monkeypatch):
    from types import SimpleNamespace
    import agent_service.conversation as chat
    payload={'action':'pause'}
    captured=[]
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'choices':[{'message':{'content':json.dumps(payload)}}]}
    class ModelClient:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,*args,**kwargs):captured.append(kwargs['json']);return Response()
    monkeypatch.setattr(chat,'LLM_BASE','https://model.invalid')
    monkeypatch.setattr(chat,'LLM_MODEL','test-model')
    from agent_service import provider
    monkeypatch.setattr(provider,'httpx',SimpleNamespace(Client=ModelClient))
    p={'id':'isolated-model-project'}
    source={'profile':{'fields':[{'name':'c_1','label':'金额','numeric':True}]},'secret_config':'must-not-be-sent'}
    d=chat.decision(p,{},'你好',source,None)
    assert d.action=='reply'
    assert 'must-not-be-sent' not in json.dumps(captured)
    payload.clear();payload.update(action='analyze',spec={'amount_field':'invented','group_by':'c_1'})
    assert chat.decision(p,{},'按金额分析',source,None).action=='reply'
