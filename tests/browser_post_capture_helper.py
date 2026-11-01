"""Exercise the real FastAPI approval bridge against a local mock runner.

The runner uses a mocked Playwright driver, so this is not a Chromium or
enterprise-system test. The input session has already been opened by Node.
"""
import json
import os
import sys
import time
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from agent_service import agent,browser_tools,provider,tasks
from agent_service.actor_context import actor_scope
from agent_service.store import get,put,seal,uid

value=json.load(sys.stdin)
pid='p_'+uid();tid='t_'+uid();sid=value['session_id'];actor='owner';snapshot=value['snapshot']
provider.settings=lambda:{'share_data':True,'base_url':'https://fixture.test/v1','model':'fixture','tool_protocol':'json'}
browser_tools._scope=lambda turn:None
browser_tools._browser_url=lambda turn,url:url
put('project',{'name':'synthetic post bridge'},pid)
turn=put('turn',{'project_id':pid,'actor_id':actor,'text':'请查询报表并提交数据','interaction_mode':'work','state':'agent',
                 'created_at':'2026-01-01T00:00:00Z','discovered_tools':['prepare_browser_post']},tid)
tasks.ensure(turn)
put('browser_session',{'project_id':pid,'actor_id':actor,'turn_id':tid,'mode':'agent',
    'workspace_id':value['workspace_id'],'state':'open','agent_grant_origin':value['origin'],
    'agent_grant_expires':time.time()+600,'expires_at':time.time()*1000+600000,
    'snapshot_secret':seal(snapshot)},sid)
app=FastAPI()
@app.exception_handler(ValueError)
async def invalid(_request,exc):return JSONResponse({'detail':str(exc)},status_code=400)
browser_tools.install(app,lambda:None)
client=TestClient(app)
with actor_scope(actor):
    agent.opencode_step(tid,{'kind':'tool','tool':'set_task_plan','arguments':{'steps':['查看页面','核对并执行请求'],'current_step':0}},'post:plan')
    result=agent.opencode_step(tid,{'kind':'tool','tool':'prepare_browser_post','arguments':{
        'session_id':sid,'revision':snapshot['revision'],'ref':1}},'post:fixture')
    turn=get('turn',tid)
    assert turn.get('outcome')=='needs_input',result
    fid=turn['agent_trace'][-1]['observation']['fill_id']
    path='/api/projects/'+pid+'/browsers/'+sid+'/pending-actions/'+fid
    listing=client.get(path.rsplit('/',1)[0]);assert listing.status_code==200,listing.text
    item=listing.json()['items'][0]
    # A blind approval cannot click the control.
    blind=client.post(path+'/decision',json={'digest':item['digest'],'approve':True});assert blind.status_code==400
    first=client.post(path+'/reveal',json={});assert first.status_code==200,first.text
    r=client.post(path+'/decision',json={'digest':first.json()['digest'],'approve':True});assert r.status_code==200,r.text
    assert r.json()['state']=='pending'
    pending=client.get(path.rsplit('/',1)[0]).json()['items'][0]
    assert pending['kind']=='post'
    blind=client.post(path+'/decision',json={'digest':pending['digest'],'approve':True});assert blind.status_code==400
    second=client.post(path+'/reveal',json={});assert second.status_code==200,second.text
    detail=second.json();assert detail['method']=='POST' and detail['url']==value['expected_url']
    assert detail['body']==value['expected_body'] and detail['content_type']=='application/json'
    denied=client.post(path+'/decision',json={'digest':'f'*64,'approve':True});assert denied.status_code==400
    decision=client.post(path+'/decision',json={'digest':detail['digest'],'approve':True});assert decision.status_code==200,decision.text
    assert decision.json()['state']=='applied' and '不代表业务成功' in decision.json()['notice']
    repeat=client.post(path+'/decision',json={'digest':detail['digest'],'approve':True});assert repeat.status_code==200
    assert repeat.json()['state']=='applied'
    assert value['expected_body'] not in json.dumps(get('turn',tid),ensure_ascii=False)
    assert get('browser_fill',fid)['secret_payload'] is None
print(json.dumps({'two_stage_approval':True,'bridge_calls_real_runner':True,'blind_and_wrong_digest_blocked':True,'exact_request_reviewed':True,'replay_no_resend':True,'model_trace_redacted':True}))
