"""Actual gateway HTTP routes, durable turns and machine authorization."""
import json,time
from datetime import datetime,timedelta,timezone
import pytest
from fastapi import FastAPI,Depends,Header,HTTPException
from fastapi.testclient import TestClient
from agent_service import agent_gateway as gateway,conversation,model_catalog,provider
from agent_service.actor_context import current_actor
from agent_service.store import get,put,patch,all_items,delete,uid,now

@pytest.fixture
def setup(monkeypatch):
    kinds=('agent_client','gateway_task','turn','project','conversation','agent_handoff_artifact')
    before={kind:{v['id'] for v in all_items(kind)} for kind in kinds}
    monkeypatch.setattr(model_catalog,'public_catalog',lambda:{'models':[{'id':'finance','name':'Finance'},{'id':'other','name':'Other'}]})
    monkeypatch.setattr(model_catalog,'select',lambda model_id=None,**kwargs:{'model_id':model_id or 'finance','model_name':'Finance','model_catalog_revision':'test-frozen-model'})
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    app=FastAPI()
    async def auth(authorization:str=Header(default='')):
        if authorization!='Bearer business-fixture-token':raise HTTPException(401)
    async def operator(authorization:str=Header(default=''),x_atlas_operator_token:str=Header(default='')):
        if authorization!='Bearer business-fixture-token' or x_atlas_operator_token!='operator-fixture-token':raise HTTPException(403)
    gateway.install(app,auth,operator)
    @app.get('/api/business',dependencies=[Depends(auth)])
    def business():return {'owner':True}
    pid=put('project',{'name':'Gateway project','created_at':now()},'p_'+uid())['id']
    other=put('project',{'name':'Other project','created_at':now()},'p_'+uid())['id']
    with TestClient(app) as client:yield client,pid,other
    for kind in kinds:
        for item in all_items(kind):
            if item['id'] not in before[kind]:delete(kind,item['id'])

OP={'Authorization':'Bearer business-fixture-token','X-Atlas-Operator-Token':'operator-fixture-token'}

def issue(setup,**extra):
    client,pid,_=setup
    response=client.post('/api/operator/agent-clients',headers=OP,json={'name':'Finance peer','project_ids':[pid],'approved_model_ids':['finance'],**extra})
    assert response.status_code==200,response.text
    value=response.json();return value,{'Authorization':'Bearer '+value['token']}

def submit(setup,headers,request_id=None,**extra):
    client,pid,_=setup
    return client.post('/api/agent/v1/tasks',headers=headers,json={'project_id':pid,'request_id':request_id or 'request_'+uid(),'goal':'核对已有资料并说明缺口',**extra})

def test_machine_token_separate_hashed_and_returned_once(setup):
    client,pid,_=setup;created,headers=issue(setup)
    stored=get('agent_client',created['client']['id'])
    assert created['token'] not in json.dumps(stored) and stored['token_hash']
    assert created['client']['actor_id'].startswith('u_') and created['client']['actor_id']!='owner'
    assert client.get('/api/business',headers=headers).status_code==401
    assert client.post('/api/agent/v1/tasks',headers={'Authorization':'Bearer business-fixture-token'},json={}).status_code==401
    assert client.get('/.well-known/agent-card.json').status_code==401
    listed=client.get('/api/operator/agent-clients',headers=OP).json()
    assert created['token'] not in json.dumps(listed) and 'token_hash' not in json.dumps(listed)
    assert client.post('/api/operator/agent-clients',headers=headers,json={}).status_code==403

def test_submission_atomically_freezes_scope_actor_model_and_readonly(setup):
    client,pid,other=setup;created,headers=issue(setup)
    response=submit(setup,headers);assert response.status_code==202,response.text
    turn=get('turn',response.json()['turn_id'])
    assert turn['gateway_client_id']==created['client']['id'] and turn['actor_id']==created['client']['actor_id']
    assert turn['interaction_mode']=='chat' and turn['gateway_limits']['model_calls']==12
    assert turn['model_id']=='finance' and turn['model_catalog_revision']=='test-frozen-model'
    assert current_actor()=='owner'
    for extra in ({'project_id':other},{'model_id':'other'},{'interaction_mode':'work'}):
        denied=submit(setup,headers,**extra);assert denied.status_code==403,denied.text
    assert submit(setup,headers,gateway_client_id='other',actor_id='owner').status_code==400
    names={'read_file','write_file','publish_result','confirm_metric','delegate_to_agents','run_python_file','read_source_table'}
    assert gateway.allowed_tools(turn,names)=={'read_file','read_source_table'}
    assert gateway.allowed_tools(dict(turn,interaction_mode='work'),names)=={'read_file','write_file','read_source_table'}

def test_durable_idempotence_and_other_client_task_denied(setup):
    client,pid,_=setup;_,headers=issue(setup)
    one=submit(setup,headers,'same-request-123').json();two=submit(setup,headers,'same-request-123').json()
    assert one['id']==two['id'] and one['turn_id']==two['turn_id']
    assert submit(setup,headers,'same-request-123',goal='改变任务').status_code==409
    _,other_headers=issue(setup);other=submit(setup,other_headers,'same-request-123').json()
    assert other['id']!=one['id']
    assert client.get('/api/agent/v1/tasks/'+one['id'],headers=other_headers).status_code==404

def test_rotation_revocation_expiry_stop_stale_tool_and_model(setup):
    client,pid,_=setup;created,headers=issue(setup)
    status=submit(setup,headers).json();turn=get('turn',status['turn_id'])
    gateway.assert_turn_allowed(turn)
    rotated=client.post('/api/operator/agent-clients/'+created['client']['id']+'/rotate',headers=OP,json={'expected_revision':created['client']['revision']})
    assert rotated.status_code==200
    assert client.get('/api/agent/v1/tasks/'+status['id'],headers=headers).status_code==401
    with pytest.raises(gateway.GatewayError):gateway.assert_turn_allowed(turn)
    with pytest.raises(gateway.GatewayError):gateway.admit_model_request(turn,100,128)
    fresh_headers={'Authorization':'Bearer '+rotated.json()['token']}
    assert client.get('/api/agent/v1/tasks/'+status['id'],headers=fresh_headers).status_code==403
    new=submit(setup,fresh_headers).json();newturn=get('turn',new['turn_id'])
    patch('agent_client',created['client']['id'],expires_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
    assert submit(setup,fresh_headers).status_code==403
    with pytest.raises(gateway.GatewayError):gateway.assert_turn_allowed(newturn)

def test_failed_preparation_does_not_leave_a_permanent_active_slot(setup):
    client,pid,_=setup;created,headers=issue(setup,max_active_tasks=1)
    bad=submit(setup,headers,'invalid-source-123',source_ids=['missing-source'])
    assert bad.status_code==400
    failed=[t for t in all_items('gateway_task') if t['client_id']==created['client']['id']]
    assert len(failed)==1 and failed[0]['state']=='failed' and not failed[0].get('turn_id')
    valid=submit(setup,headers,'valid-after-failure-123')
    assert valid.status_code==202,valid.text
    assert get('turn',valid.json()['turn_id'])['gateway_client_id']==created['client']['id']
    assert submit(setup,headers,'invalid-source-123',source_ids=['missing-source']).status_code==429

def test_cancelled_preparation_still_rejects_changed_idempotent_body(setup):
    client,pid,_=setup;_,headers=issue(setup)
    request_id='cancel-invalid-source-123'
    assert submit(setup,headers,request_id,source_ids=['missing-source']).status_code==400
    failed=next(t for t in all_items('gateway_task') if t['request_id']==request_id)
    assert client.post('/api/agent/v1/tasks/'+failed['id']+'/cancel',headers=headers).json()['status']=='cancelled'
    assert submit(setup,headers,request_id,source_ids=['missing-source']).json()['status']=='cancelled'
    assert submit(setup,headers,request_id,goal='更换目标').status_code==409

def test_rotation_sweep_cannot_cancel_new_revision_task(setup,monkeypatch):
    client,pid,_=setup;created,headers=issue(setup)
    old=submit(setup,headers).json();new_tasks=[]
    sweep=gateway._cancel_client_tasks
    def interleaved(identity,*,revision=None):
        current=get('agent_client',identity)
        new_tasks.append(gateway.submit_task(current,{'project_id':pid,'request_id':'new-revision-request','goal':'新授权任务'}))
        sweep(identity,revision=revision)
    monkeypatch.setattr(gateway,'_cancel_client_tasks',interleaved)
    rotated=client.post('/api/operator/agent-clients/'+created['client']['id']+'/rotate',headers=OP,json={'expected_revision':created['client']['revision']})
    assert rotated.status_code==200
    assert get('turn',old['turn_id'])['gateway_cancelled']
    assert get('turn',new_tasks[0]['turn_id'])['state']=='queued'

def test_done_without_business_outcome_is_not_completed(setup):
    client,pid,_=setup;_,headers=issue(setup)
    task=submit(setup,headers).json()
    patch('turn',task['turn_id'],state='done',outcome=None)
    assert client.get('/api/agent/v1/tasks/'+task['id'],headers=headers).json()['status']=='failed'
    patch('turn',task['turn_id'],outcome='verified_complete',verification={'verified':True})
    assert client.get('/api/agent/v1/tasks/'+task['id'],headers=headers).json()['status']=='completed'

def test_paused_task_is_cancellable_but_completed_a2a_task_is_not(setup):
    client,pid,_=setup;_,headers=issue(setup)
    status=submit(setup,headers).json()
    patch('turn',status['turn_id'],state='done',outcome='paused',resume_available=True)
    assert client.post('/api/agent/v1/tasks/'+status['id']+'/cancel',headers=headers).json()['status']=='cancelled'
    done=submit(setup,headers).json()
    patch('turn',done['turn_id'],state='done',outcome='answered',answer='只读解释完成')
    result=client.post('/a2a',headers={**headers,'A2A-Version':'1.0'},json={'jsonrpc':'2.0','id':7,'method':'CancelTask','params':{'id':done['id']}}).json()
    assert 'error' in result
    assert get('turn',done['turn_id'])['outcome']=='answered'

def test_corrupt_artifacts_cannot_be_reported_as_completed(setup,monkeypatch):
    from agent_service import agent_handoff
    client,pid,_=setup;_,headers=issue(setup)
    status=submit(setup,headers).json()
    patch('turn',status['turn_id'],state='done',outcome='answered',answer='结果已保存')
    def broken(turn):raise ValueError('untrusted bytes mismatch')
    monkeypatch.setattr(agent_handoff,'build_output',broken)
    checked=client.get('/api/agent/v1/tasks/'+status['id'],headers=headers)
    assert checked.status_code==200
    value=checked.json()
    assert value['status']=='failed' and not value['integrity_verified'] and value['output_bundle'] is None
    assert 'untrusted bytes mismatch' not in checked.text

def test_deleted_project_and_self_target_chain_reject_before_submission(setup):
    from agent_service import agent_mesh
    client,pid,_=setup;_,headers=issue(setup)
    rejected=submit(setup,headers,provenance={'hops':1,'visited_instance_ids':[agent_mesh.instance_id()]})
    assert rejected.status_code==400 and rejected.json()['code']=='agent_loop'
    task=submit(setup,headers).json()
    patch('project',pid,deleted_at=now())
    assert client.get('/api/agent/v1/tasks/'+task['id'],headers=headers).status_code==404
    assert submit(setup,headers).status_code==404

def test_cancel_rechecks_persisted_turn_for_inflight_callback(setup):
    client,pid,_=setup;_,headers=issue(setup)
    status=submit(setup,headers).json();old=get('turn',status['turn_id'])
    cancelled=client.post('/api/agent/v1/tasks/'+status['id']+'/cancel',headers=headers)
    assert cancelled.json()['status']=='cancelled'
    with pytest.raises(gateway.GatewayError,match='取消'):gateway.assert_turn_allowed(old)
    with pytest.raises(gateway.GatewayError):gateway.admit_model_request(old,10,128)

def test_model_and_active_task_budgets_are_durable(setup):
    client,pid,_=setup;created,headers=issue(setup,max_active_tasks=1,max_total_model_calls=2)
    status=submit(setup,headers).json();turn=get('turn',status['turn_id'])
    assert submit(setup,headers).status_code==429
    gateway.admit_model_request(turn,100,128);gateway.admit_model_request(turn,200,256)
    with pytest.raises(gateway.GatewayError,match='总预算'):gateway.admit_model_request(turn,1,128)
    record=get('agent_client',created['client']['id'])
    assert record['model_requests']==2 and record['input_characters']==300 and record['output_tokens_budgeted']==384

def a2a1(pid,request_id):
    return {'jsonrpc':'2.0','id':request_id,'method':'SendMessage','params':{'message':{'role':'ROLE_USER','messageId':request_id,
        'parts':[{'text':'请核对来源'},{'data':{'atlas':{'project_id':pid,'request_id':request_id,'goal':'请核对来源'}}}]},'configuration':{'returnImmediately':True}}}

def test_a2a1_async_envelope_partial_failure_and_03_compatibility(setup):
    client,pid,_=setup;_,headers=issue(setup);h={**headers,'A2A-Version':'1.0'}
    card=client.get('/.well-known/agent-card.json',headers=h).json()
    assert card['supportedInterfaces'][0]['protocolVersion']=='1.0' and not card['capabilities']['streaming']
    assert card['securitySchemes']['atlasBearer']['httpAuthSecurityScheme']['scheme']=='Bearer'
    response=client.post('/a2a',headers=h,json=a2a1(pid,'a2a-request-123'))
    task=response.json()['result']['task'];assert task['status']['state']=='TASK_STATE_SUBMITTED' and 'kind' not in task
    record=get('gateway_task',task['id']);patch('turn',record['turn_id'],state='done',outcome='partial',answer='尚缺预算',verification={'verified':False})
    finished=client.post('/a2a',headers=h,json={'jsonrpc':'2.0','id':2,'method':'GetTask','params':{'id':task['id']}}).json()['result']
    assert finished['status']['state']=='TASK_STATE_FAILED' and finished['status']['message']['role']=='ROLE_AGENT'
    legacy=client.post('/a2a',headers=headers,json={'jsonrpc':'2.0','id':3,'method':'tasks/get','params':{'id':task['id']}}).json()['result']
    assert legacy['kind']=='task' and legacy['status']['state']=='failed'
    assert client.post('/a2a',headers={**headers,'A2A-Version':'9.9'},json={}).status_code==400

def test_a2a1_requires_async_and_rejects_wrong_shapes_and_urls(setup):
    client,pid,_=setup;_,headers=issue(setup);h={**headers,'A2A-Version':'1.0'}
    body=a2a1(pid,'reject-request-123');body['params'].pop('configuration')
    rejected=client.post('/a2a',headers=h,json=body).json();assert 'error' in rejected and 'returnImmediately' in rejected['error']['message']
    body=a2a1(pid,'reject-request-124');body['params']['message']['parts'].append({'url':'https://untrusted.example/input.csv'})
    assert 'error' in client.post('/a2a',headers=h,json=body).json()
    body=a2a1(pid,'reject-request-125');body['params']['message']['role']='user'
    assert 'error' in client.post('/a2a',headers=h,json=body).json()

def test_mcp_real_initialize_directory_submit_and_read(setup):
    client,pid,_=setup;created,headers=issue(setup)
    def rpc(method,params=None):return client.post('/mcp/atlas',headers=headers,json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})
    init=rpc('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'test','version':'1'}}).json()['result']
    assert init['protocolVersion']=='2025-06-18' and 'tools' in init['capabilities']
    assert client.post('/mcp/atlas',headers=headers,json={'jsonrpc':'2.0','method':'notifications/initialized'}).status_code==202
    names={t['name'] for t in rpc('tools/list').json()['result']['tools']};assert names=={'atlas_submit_task','atlas_get_task','atlas_cancel_task','atlas_read_artifact'}
    accepted=rpc('tools/call',{'name':'atlas_submit_task','arguments':{'project_id':pid,'request_id':'mcp-request-123','goal':'只解释已有结果'}}).json()['result']
    status=accepted['structuredContent'];assert status['status']=='submitted' and not accepted['isError']
    read=rpc('tools/call',{'name':'atlas_get_task','arguments':{'task_id':status['id']}}).json()['result']
    assert read['structuredContent']['id']==status['id'] and created['token'] not in json.dumps(read)
    assert 'error' in rpc('tools/call',{'name':'shell','arguments':{}}).json()

@pytest.mark.parametrize('path',['/api/agent/v1/tasks','/a2a','/mcp/atlas'])
def test_origins_body_limits_and_duplicate_json_do_not_reach_executor(setup,path):
    client,pid,_=setup;_,headers=issue(setup)
    assert client.post(path,headers={**headers,'Origin':'https://attacker.invalid'},json={}).status_code==403
    assert client.post(path,headers={**headers,'Content-Length':str(gateway.MAX_BODY+1)},content='{}').status_code==413
    assert client.post(path,headers=headers,content='{"a":1,"a":2}').status_code==400

def test_hop_limit_and_changed_grants_never_resume_old_work(setup):
    client,pid,_=setup;created,headers=issue(setup)
    assert submit(setup,headers,provenance={'chain_id':'x','hops':2}).status_code==400
    task=submit(setup,headers).json();turn=get('turn',task['turn_id'])
    changed={k:v for k,v in created['client'].items() if k in gateway.ClientInput.model_fields}
    changed.update(ttl_seconds=3600,expected_revision=created['client']['revision'])
    result=client.put('/api/operator/agent-clients/'+created['client']['id'],headers=OP,json=changed)
    assert result.status_code==200,result.text
    with pytest.raises(gateway.GatewayError):gateway.assert_turn_allowed(turn)

