import base64,json,time,subprocess,sys,os
from datetime import datetime,timezone,timedelta
import httpx,pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
from sqlalchemy import select,update
from agent_service import activity as a
from agent_service.api import app
from agent_service.store import get,put,patch,uid,now,engine,items,unseal
from agent_service.config import TOKEN
from agent_service.conversation import create_project,ProjectInput,default_session,submit,TurnInput
H={'Authorization':'Bearer '+TOKEN}

@pytest.fixture
def workspace():
    pid=create_project(ProjectInput(name='生命周期验证',request_id=uid()))['id'];cid=default_session(pid);did='d_'+uid()+uid()[:8]
    with engine.begin() as c:c.execute(a.outbox.delete())
    a.register(did)
    yield pid,cid,did
    a.unsubscribe(did)

def terminal(workspace,**kw):
    pid,cid,did=workspace
    turn=submit(pid,TurnInput(text='生命周期消息',request_id=uid(),session_id=cid,interaction_mode='chat'))
    return patch('turn',turn['id'],state='done',finished_at=now(),outcome='answered',answer='公司敏感内容不会出现在通知中',**kw)

def sub():
    encode=lambda b:base64.urlsafe_b64encode(b).rstrip(b'=').decode()
    key=ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
    return {'endpoint':'https://fcm.googleapis.com/fcm/send/private-capability','keys':{'p256dh':encode(key),'auth':encode(os.urandom(16))}}

def enable(workspace,monkeypatch):
    monkeypatch.setenv('PUBLIC_APP_URL','https://atlas.example')
    did=workspace[2];a.preferences(did,a.Preferences(desktop=True));a.subscribe(did,a.SubscriptionInput(**sub()))

@pytest.mark.parametrize(('value','kind'),[({'outcome':'needs_input'},'needs_input'),({'state':'failed','outcome':'paused'},'partial'),({'outcome':'partial'},'partial'),({'outcome':'cancelled'},'cancelled'),({'result':{'candidate':True}},'approval'),({'outcome':'verified_complete'},'completed')])
def test_classification(value,kind):assert a.classify({'state':'done',**value})==kind

def test_events_durable_dedup_ack_and_backfill(workspace):
    pid,cid,did=workspace
    old=terminal(workspace);patch('turn',old['id'],finished_at=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat())
    recent=terminal(workspace);a.reconcile();a.reconcile()
    first=a.feed(did);assert any(e['turn_id']==recent['id'] for e in first['unread'])
    assert not any(e['turn_id']==old['id'] for e in first['unread'])
    assert len([e for e in first['events'] if e['turn_id']==recent['id']])==1
    event=next(e for e in first['unread'] if e['turn_id']==recent['id'])
    assert '敏感' not in json.dumps(event,ensure_ascii=False) and set(event)<=set(('seq','project_id','session_id','turn_id','run_id','kind','created','label'))
    a.mark_read(did,a.ReadInput(sequences=[event['seq'],10**12]));a.register(did)
    assert event['seq'] not in [e['seq'] for e in a.feed(did)['unread']]
    a.mark_read(did,a.ReadInput(through=10**12));next_turn=terminal(workspace);a.reconcile()
    assert any(e['turn_id']==next_turn['id'] for e in a.feed(did)['unread'])

def test_waits_for_git_finalize_and_cursor_catches_every_event(workspace):
    t=terminal(workspace);patch('turn',t['id'],interaction_mode='work',before_version={'commit':'a'*40})
    a.reconcile();assert not any(e['turn_id']==t['id'] for e in a.feed(workspace[2])['events'])
    patch('turn',t['id'],work_version={'commit':'b'*40});a.reconcile();cursor=a.feed(workspace[2])['cursor']
    ids={terminal(workspace)['id'] for _ in range(5)};a.reconcile();found=set()
    while True:
        f=a.feed(workspace[2],cursor,2);found.update(e['turn_id'] for e in f['events']);cursor=f['cursor']
        if not f['has_more']:break
    assert ids<=found

def test_encrypted_cas_view_and_sensitive_input(workspace):
    pid,cid,did=workspace;v=a.ViewInput(project_id=pid,session_id=cid,draft='未完成的公司分析草稿',writer='tab1')
    r=a.save_view(did,v);assert r['revision']==1
    stored=get('browser_view',a.view_key(did,pid,cid));assert '公司分析' not in json.dumps(stored,ensure_ascii=False)
    assert a.read_view(did,pid,cid)['draft']==v.draft
    with pytest.raises(a.ViewConflict):a.save_view(did,v)
    pending={'text':'尚未提交的任务','request_id':'pending-request-1','session_id':cid,'interaction_mode':'work'}
    a.save_view(did,v.model_copy(update={'revision':1,'pending':pending}));assert a.read_view(did,pid,cid)['pending']==pending
    r=a.save_view(did,v.model_copy(update={'revision':2,'draft':'api_key=private-material','pending':dict(pending,text='Bearer private-material')}))
    assert not r['draft_saved'] and not r['pending_saved'];assert a.read_view(did,pid,cid)['draft']==''
    with pytest.raises((KeyError,ValueError)):a.save_view(did,v.model_copy(update={'session_id':'wrong-session'}))

def test_api_auth_conflict_and_ambiguous_send(workspace):
    pid,cid,did=workspace
    with TestClient(app) as c:
        assert c.get('/api/activity?device_id='+did).status_code==401
        value={'project_id':pid,'session_id':cid,'draft':'保留输入'}
        assert c.put('/api/browser/'+did+'/view',headers=H,json=value).status_code==200
        assert c.put('/api/browser/'+did+'/view',headers=H,json=value).status_code==409
        key=uid();body={'text':'项目改名为后台验证','request_id':key,'session_id':cid}
        assert c.get('/api/projects/'+pid+'/message-status?request_id='+key,headers=H).json()=={'accepted':False}
        first=c.post('/api/projects/'+pid+'/messages',headers=H,json=body).json()
        assert c.post('/api/projects/'+pid+'/messages',headers=H,json=body).json()['id']==first['id']
        assert c.get('/api/projects/'+pid+'/message-status?request_id='+key,headers=H).json()['accepted']
    patch('turn',first['id'],state='done',finished_at=now())

@pytest.mark.parametrize('endpoint',['http://fcm.googleapis.com/a','https://127.0.0.1/a','https://fcm.googleapis.com.evil.test/a','https://user@fcm.googleapis.com/a','https://fcm.googleapis.com:444/a','https://fcm.googleapis.com/a#x'])
def test_subscription_rejects_invalid_destinations(endpoint):
    with pytest.raises(ValueError):a.validate_subscription(dict(sub(),endpoint=endpoint))

def test_webpush_real_encryption_pinned_transport_and_private_dns_rejected(workspace,monkeypatch):
    enable(workspace,monkeypatch);seen=[];original=httpx.Client
    monkeypatch.setattr(a.socket,'getaddrinfo',lambda *args,**kwargs:[(2,1,6,'',('8.8.8.8',443))])
    def client(**kw):
        assert kw['trust_env'] is False and kw['follow_redirects'] is False
        return original(transport=httpx.MockTransport(lambda req:(seen.append(req) or httpx.Response(201))))
    monkeypatch.setattr(a.httpx,'Client',client)
    payload={'seq':1,'project_id':workspace[0],'session_id':workspace[1],'kind':'completed'}
    a.pinned_push(sub(),payload)
    assert seen[0].url.host=='8.8.8.8' and seen[0].headers['host']=='fcm.googleapis.com'
    assert seen[0].headers['content-encoding']=='aes128gcm' and b'project_id' not in seen[0].content
    assert seen[0].headers['authorization'].startswith('vapid ')
    monkeypatch.setattr(a.socket,'getaddrinfo',lambda *args,**kwargs:[(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(ValueError,match='非公网'):a.pinned_push(sub(),payload)

def test_outbox_retries_leases_expiry_read_and_token_rotation(workspace,monkeypatch):
    enable(workspace,monkeypatch);did=workspace[2];t=terminal(workspace);a.reconcile();a.queue_pushes()
    sent=[]
    def flaky(s,p):sent.append(p);raise RuntimeError('sensitive upstream response')
    monkeypatch.setattr(a,'pinned_push',flaky);assert a.dispatch_once()
    with engine.begin() as c:
        row=c.execute(select(a.outbox).where(a.outbox.c.device_id==did)).mappings().one();assert row['state']=='retrying' and row['attempts']==1
        c.execute(update(a.outbox).where(a.outbox.c.device_id==did).values(state='sending',lease_until=time.time()-1))
    monkeypatch.setattr(a,'pinned_push',lambda s,p:sent.append(p));assert a.dispatch_once()
    with engine.connect() as c:assert c.execute(select(a.outbox.c.state).where(a.outbox.c.device_id==did)).scalar()=='sent'
    terminal(workspace);a.reconcile();a.queue_pushes();patch('browser_device',did,token_binding='rotated');assert a.dispatch_once()
    with engine.connect() as c:assert 'retrying' not in c.execute(select(a.outbox.c.state).where(a.outbox.c.device_id==did)).scalars().all()
    with pytest.raises(ValueError):a.feed(did)
    a.register(did);assert not a.device(did)['push_enabled']

def test_read_suppresses_push_and_410_disables_subscription(workspace,monkeypatch):
    enable(workspace,monkeypatch);did=workspace[2];first=terminal(workspace);a.reconcile();a.queue_pushes()
    event=next(e for e in a.feed(did)['unread'] if e['turn_id']==first['id']);a.mark_read(did,a.ReadInput(sequences=[event['seq']]))
    def expired(s,p):
        from pywebpush import WebPushException
        import requests
        r=requests.Response();r.status_code=410;raise WebPushException('private endpoint',response=r)
    monkeypatch.setattr(a,'pinned_push',expired);a.dispatch_once();assert a.device(did)['push_enabled']
    terminal(workspace);a.reconcile();a.queue_pushes();a.dispatch_once();assert not a.device(did)['push_enabled']

def test_browser_close_then_agent_restart_preserves_work_and_view(workspace):
    pid,cid,did=workspace
    with TestClient(app) as c:
        v={'project_id':pid,'session_id':cid,'mode':'work','draft':'回来后继续输入'}
        assert c.put('/api/browser/'+did+'/view',headers=H,json=v).status_code==200
        turn=c.post('/api/projects/'+pid+'/messages',headers=H,json={'text':'项目改名为离线客户端验证','request_id':uid(),'session_id':cid}).json()
    # No open HTTP client while a separate server process executes the queued task.
    process=subprocess.Popen([sys.executable,'-m','agent_service.orchestrator'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if get('turn',turn['id']).get('work_version'):break
            time.sleep(.15)
        assert get('project',pid)['name']=='离线客户端验证'
        assert get('turn',turn['id'])['work_version']
    finally:process.terminate();process.wait(5)
    with TestClient(app) as again:
        assert again.get('/api/browser/'+did+'/view',params={'project_id':pid,'session_id':cid},headers=H).json()['draft']==v['draft']
        assert again.get('/api/projects/'+pid+'/conversation',headers=H).json()['turns'][0]['id']==turn['id']
    a.reconcile();assert any(e['turn_id']==turn['id'] for e in a.feed(did)['unread'])

def test_notification_and_encrypted_view_survive_full_backup_restore(tmp_path):
    source=tmp_path/'original';destination=tmp_path/'restored';archive=tmp_path/'workspace.zip'
    env=dict(os.environ,DATA_DIR=str(source),DATABASE_URL='sqlite:///'+str(source/'metadata.sqlite'))
    script='''import json,sys
from agent_service import activity as a,maintenance
from agent_service.conversation import create_project,ProjectInput,default_session,submit,TurnInput
from agent_service.store import patch,now
p=create_project(ProjectInput(name="备份恢复",request_id="backup-lifecycle-fixture"))["id"];c=default_session(p);d="d_"+"ab"*16
a.register(d);a.save_view(d,a.ViewInput(project_id=p,session_id=c,draft="加密草稿恢复"))
t=submit(p,TurnInput(text="测试",request_id="backup-lifecycle-turn",interaction_mode="chat"));patch("turn",t["id"],state="done",finished_at=now(),outcome="answered")
a.reconcile();event=next(e for e in a.feed(d)["unread"] if e["turn_id"]==t["id"]);a.mark_read(d,a.ReadInput(sequences=[event["seq"]]))
maintenance.backup(sys.argv[1]);print(json.dumps({"project":p,"session":c,"device":d,"seq":event["seq"]}))'''
    result=json.loads(subprocess.check_output([sys.executable,'-c',script,str(archive)],env=env,text=True).splitlines()[-1])
    restore='''import json,sys
from agent_service import activity as a,maintenance
v=json.loads(sys.argv[2]);maintenance.restore(sys.argv[1]);assert a.read_view(v["device"],v["project"],v["session"])["draft"]=="加密草稿恢复"
assert any(e["seq"]==v["seq"] for e in a.feed(v["device"])["events"]);assert not any(e["seq"]==v["seq"] for e in a.feed(v["device"])["unread"])
print("restored")'''
    env.update(DATA_DIR=str(destination),DATABASE_URL='sqlite:///'+str(destination/'metadata.sqlite'))
    assert subprocess.check_output([sys.executable,'-c',restore,str(archive),json.dumps(result)],env=env,text=True).strip()=='restored'
