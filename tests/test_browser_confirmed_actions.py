"""A user reviews the exact scoped action before any browser effect."""
import base64
import hashlib
import json
import time
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from agent_service import agent, browser_tools, provider, tasks
from agent_service.actor_context import actor_scope
from agent_service.store import get, put, patch, seal, uid, engine, items


@pytest.fixture
def setup(monkeypatch):
    from test_interactive_work import png
    pid='p_'+uid();tid='t_'+uid();sid=hashlib.sha256(uid().encode()).hexdigest()
    put('project',{'name':'受控页面'},pid)
    turn=put('turn',{'project_id':pid,'actor_id':'owner','text':'请在网页填写业务数据','interaction_mode':'work',
        'state':'agent','created_at':'2026-01-01T00:00:00Z','discovered_tools':['prepare_browser_fill','prepare_browser_submit']},tid)
    tasks.ensure(turn)
    content=png()
    snapshot={'session_id':sid,'url':'https://erp.example/report','revision':'a'*64,'title':'报表',
        'text':'营业额','controls':[{'label':'金额','tag':'input','type':'text','visible':True,'disabled':False,'sensitive':False},
                                    {'label':'提交','tag':'button','type':'submit','visible':True,'disabled':False,'sensitive':False}],
        'mode':'agent','image_base64':base64.b64encode(content).decode(),'image_sha256':hashlib.sha256(content).hexdigest(),
        'untrusted_content':True}
    put('browser_session',{'project_id':pid,'actor_id':'owner','turn_id':tid,'mode':'agent','workspace_id':'b'*64,
        'state':'open','agent_grant_origin':'https://erp.example','agent_grant_expires':time.time()+600,
        'expires_at':time.time()*1000+600000,'snapshot_secret':seal(snapshot)},sid)
    monkeypatch.setattr(browser_tools,'_scope',lambda turn:None)
    monkeypatch.setattr(browser_tools,'_browser_url',lambda turn,url:url)
    monkeypatch.setattr(browser_tools,'status',lambda:{'configured':True})
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True,'base_url':'https://fixture.test/v1','model':'fixture','tool_protocol':'json'})
    calls=[]
    def request(session,operation,body):
        calls.append((session,operation,body))
        if body.get('submit_confirmed'):
            return {**snapshot,'post_receipt':{**body['post_target'],'http_status':200,'verified_business_outcome':False}}
        return snapshot
    monkeypatch.setattr(browser_tools,'_request',request)
    app=FastAPI()
    from fastapi.responses import JSONResponse
    @app.exception_handler(ValueError)
    async def invalid(_request,exc):return JSONResponse({'detail':str(exc)},status_code=400)
    browser_tools.install(app,lambda:None)
    yield pid,tid,sid,calls,TestClient(app)
    from agent_service.store import all_items,delete
    for item in all_items('browser_fill'):
        if item.get('session_id')==sid:delete('browser_fill',item['id'])
    delete('browser_session',sid);delete('turn',tid);delete('project',pid)


def prepare(setup,tool,args):
    pid,tid,sid,calls,client=setup
    step={'kind':'tool','tool':tool,'arguments':{'session_id':sid,'revision':'a'*64,**args}}
    with actor_scope('owner'):answer=agent.opencode_step(tid,step,'session:one')
    record=get('turn',tid)
    assert record['outcome']=='needs_input',answer
    fid=record['agent_trace'][-1]['observation']['fill_id']
    path='/api/projects/'+pid+'/browsers/'+sid+'/pending-actions/'+fid
    return path,record


def test_sensitive_value_requires_exact_scoped_approval_and_is_not_stored_in_trace(setup):
    pid,tid,sid,calls,client=setup;secret='个人收入 12345.67'
    path,turn=prepare(setup,'prepare_browser_fill',{'ref':1,'text':secret})
    assert not calls and secret not in json.dumps(turn,ensure_ascii=False)
    from agent_service.conversation import public_turn
    assert secret not in json.dumps(public_turn(turn),ensure_ascii=False)
    with engine.connect() as c:
        rows=c.execute(select(items.c.body).where(items.c.id.in_([tid,path.split('/')[-1]]))).all()
    assert all(secret not in row[0] for row in rows)
    listing=client.get(path.rsplit('/',1)[0]);assert listing.status_code==200 and secret not in listing.text
    digest=listing.json()['items'][0]['digest']
    assert client.post(path+'/decision',json={'digest':digest,'approve':True}).status_code==400 and not calls
    revealed=client.post(path+'/reveal',json={}).json();assert revealed['text']==secret
    denied=client.post(path+'/decision',json={'digest':'f'*64,'approve':True});assert denied.status_code==400 and not calls
    applied=client.post(path+'/decision',json={'digest':revealed['digest'],'approve':True});assert applied.status_code==200,applied.text
    assert calls[0][2]['text']==secret and calls[0][2]['fill_confirmed'] is True
    original=get('turn',tid);assert original['resume_available'] is True and original['agent_trace'][-1]['observation']['state']=='applied'
    assert '用户已批准' in original['answer'] and secret not in json.dumps(original,ensure_ascii=False)
    assert client.post(path+'/decision',json={'digest':revealed['digest'],'approve':True}).json()['state']=='applied'
    assert len(calls)==1 and get('browser_fill',path.split('/')[-1])['secret_payload'] is None


def test_reject_discards_content_without_browser_action(setup):
    pid,tid,sid,calls,client=setup
    path,_=prepare(setup,'prepare_browser_fill',{'ref':1,'text':'拒绝的内容'})
    item=client.post(path+'/reveal',json={}).json()
    assert client.post(path+'/decision',json={'digest':item['digest'],'approve':False}).json()['state']=='rejected'
    assert get('turn',tid)['agent_trace'][-1]['observation']['state']=='rejected'
    assert get('browser_fill',path.split('/')[-1])['secret_payload'] is None and not calls



def test_lost_response_is_unknown_and_never_replayed(setup,monkeypatch):
    pid,tid,sid,calls,client=setup
    path,_=prepare(setup,'prepare_browser_fill',{'ref':1,'text':'25.00'})
    pending=client.post(path+'/reveal',json={}).json();attempts=[]
    def fail(*args):attempts.append(args);raise ValueError('network lost')
    monkeypatch.setattr(browser_tools,'_request',fail)
    response=client.post(path+'/decision',json={'digest':pending['digest'],'approve':True})
    assert response.status_code==400 and len(attempts)==1
    assert get('browser_session',sid)['unknown_pending'] and get('browser_fill',path.split('/')[-1])['state']=='unknown'
    assert get('turn',tid)['agent_trace'][-1]['observation']['state']=='unknown'
    assert client.post(path+'/decision',json={'digest':pending['digest'],'approve':True}).status_code==400 and len(attempts)==1


def test_changed_page_or_revoked_grant_cannot_execute(setup):
    pid,tid,sid,calls,client=setup
    path,_=prepare(setup,'prepare_browser_fill',{'ref':1,'text':'25.00'})
    pending=client.post(path+'/reveal',json={}).json()
    patch('browser_session',sid,agent_grant_expires=0)
    assert client.post(path+'/decision',json={'digest':pending['digest'],'approve':True}).status_code==400 and not calls
