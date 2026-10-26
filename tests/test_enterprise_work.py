import json,time,threading,base64,zipfile
from pathlib import Path
import pytest,httpx,jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from agent_service import work_tools as w,office,provider,workspace,identity,runtime
from agent_service.store import get,put,patch,uid,now,engine,unseal
from agent_service.conversation import create_project,ProjectInput,default_session,submit,TurnInput
from agent_service.api import app
from agent_service.config import TOKEN
H={'Authorization':'Bearer '+TOKEN}

@pytest.fixture
def project():
    pid=create_project(ProjectInput(name='企业工作验证',request_id=uid()))['id'];yield pid
    from agent_service.store import all_items
    from agent_service.jobs import list_jobs,request_cancel,ACTIVE
    for t in all_items('turn'):
        if t['project_id']==pid and t['state'] not in ('done','failed'):patch('turn',t['id'],state='done')
    for a in w.actions(pid):
        if a['state'] in ('pending','approved','executing'):patch('work_action',a['id'],state='cancelled',finished_at=now())
        for j in list_jobs(target=a['id']):
            if j['status'] in ACTIVE:request_cancel(j['id'])

def turn(pid,text='帮我完成工作'):
    t=submit(pid,TurnInput(text=text,request_id=uid(),session_id=default_session(pid)))
    return patch('turn',t['id'],agent_trace=[{'id':uid(),'tool':'prepare_work_action','status':'started','arguments':{}}])

@pytest.fixture
def remote(monkeypatch):
    calls=[];original=httpx.Client
    tools=[{'name':'search','inputSchema':{'type':'object','properties':{'query':{'type':'string'}},'required':['query'],'additionalProperties':False}}, {'name':'create_ticket','description':'Write, despite an untrusted annotation','annotations':{'readOnlyHint':True},'inputSchema':{'type':'object','properties':{'title':{'type':'string'}},'required':['title'],'additionalProperties':False}}]
    def handler(req):
        assert req.url.host=='8.8.8.8' and req.headers['host']=='work.test'
        value=json.loads(req.content);calls.append(value);method=value['method']
        if method=='initialize':result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}}}
        elif method=='notifications/initialized':return httpx.Response(202)
        elif method=='tools/list':result={'tools':tools}
        else:result={'content':[{'type':'text','text':'实际执行回执'}]}
        return httpx.Response(200,json={'jsonrpc':'2.0','id':value['id'],'result':result},headers={'Mcp-Session-Id':'test-session'})
    def client(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        return original(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(w.httpx,'Client',client);monkeypatch.setattr('agent_service.connectors.safe_api_url',lambda url:'8.8.8.8')
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://model.test','model':'test','api_key':'hidden','share_data':True})
    return calls,tools

def connect(pid):return w.configure(pid,w.ConnectorInput(name='办公系统',url='https://work.test/mcp',credential='Bearer fixture-only-secret',read_tools=['search'],write_tools=['create_ticket'],storage_approved=True,model_approved=True))

def test_mcp_read_pins_destination_and_enforces_owner_allowlist(project,remote):
    c=connect(project);t=turn(project);result=w.read_tool(t,w.ToolCall(connector_id=c['id'],tool='search',arguments={'query':'依据'}))
    assert result['result']['content'][0]['text']=='实际执行回执'
    assert 'fixture-only-secret' not in json.dumps(c) and 'fixture-only-secret' not in json.dumps(get('work_connector',c['id']))
    with pytest.raises(ValueError):w.read_tool(t,w.ToolCall(connector_id=c['id'],tool='create_ticket',arguments={'title':'不许直接调用'}))
    with pytest.raises(ValueError):w.read_tool(t,w.ToolCall(connector_id=c['id'],tool='search',arguments={'unknown':1}))
    other=create_project(ProjectInput(name='其他项目',request_id=uid()))['id']
    with pytest.raises(ValueError):w.list_tools(turn(other),c['id'])
    # Work system observations are encrypted at rest before they can enter history.
    patch('turn',t['id'],agent_trace=[{'tool':'read_work_tool','observation':{'text':'内部敏感结果'}}])
    from agent_service.store import items
    from sqlalchemy import select
    with engine.connect() as db:raw=db.execute(select(items.c.body).where(items.c.id==t['id'])).scalar()
    assert '内部敏感结果' not in raw

def test_single_use_exact_approval_no_auto_external_replay(project,remote):
    c=connect(project);t=turn(project);v=w.ToolCall(connector_id=c['id'],tool='create_ticket',arguments={'title':'具体任务'})
    a=w.propose(t,v);assert a['state']=='pending';assert not any(x['method']=='tools/call' for x in remote[0])
    with pytest.raises(ValueError):w.decide(project,a['id'],w.ActionDecision(digest='0'*64,approve=True))
    w.decide(project,a['id'],w.ActionDecision(digest=a['digest'],approve=True));w.decide(project,a['id'],w.ActionDecision(digest=a['digest'],approve=True))
    assert w.perform(a['id'])['state']=='succeeded';assert w.perform(a['id'])['state']=='succeeded'
    writes=[x for x in remote[0] if x['method']=='tools/call'];assert len(writes)==1 and writes[0]['params']['arguments']=={'title':'具体任务'}
    assert writes[0]['params']['_meta']['atlas/idempotencyKey']==a['id']

def test_interrupted_external_write_becomes_unknown_not_resent(project,remote):
    c=connect(project);a=w.propose(turn(project),w.ToolCall(connector_id=c['id'],tool='create_ticket',arguments={'title':'原系统核对'}));w.decide(project,a['id'],w.ActionDecision(digest=a['digest'],approve=True));patch('work_action',a['id'],state='executing')
    before=len(remote[0]);assert w.perform(a['id'])['state']=='unknown';assert len(remote[0])==before
    assert w.perform(a['id'])['state']=='unknown';assert len(remote[0])==before

def test_connector_drift_or_rejected_action_never_calls_write(project,remote):
    c=connect(project);a=w.propose(turn(project),w.ToolCall(connector_id=c['id'],tool='create_ticket',arguments={'title':'草稿'}));w.decide(project,a['id'],w.ActionDecision(digest=a['digest'],approve=False))
    with pytest.raises(ValueError):w.decide(project,a['id'],w.ActionDecision(digest=a['digest'],approve=True))
    assert w.perform(a['id'])['state']=='rejected'
    b=w.propose(turn(project),w.ToolCall(connector_id=c['id'],tool='create_ticket',arguments={'title':'新任务'}));w.decide(project,b['id'],w.ActionDecision(digest=b['digest'],approve=True));patch('work_connector',c['id'],revision='changed')
    assert w.perform(b['id'])['state']=='failed';assert not any(x['method']=='tools/call' for x in remote[0])

def test_model_change_revokes_work_history_export(project,remote,monkeypatch):
    c=connect(project);monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://other.test','model':'other','share_data':True})
    from agent_service.company_search import assert_model_allowed,CompanySearchError
    with pytest.raises(CompanySearchError):assert_model_allowed(project)

@pytest.mark.parametrize('kind,ext',[('document','docx'),('spreadsheet','xlsx'),('presentation','pptx')])
def test_real_office_package_download_hash_and_idempotency(project,kind,ext):
    t=turn(project,'帮我生成办公文件');spec={'kind':kind,'title':'业务成果','sections':[{'heading':'核实结果','body':'有来源的内容','bullets':['后续行动']}],'sheets':[{'name':'记录','columns':['名称','金额'],'rows':[['=HYPERLINK("https://bad.test")',12],['客户二',23]]}]}
    workspace.write_file(project,'deliverable.json',json.dumps(spec,ensure_ascii=False));result=office.render(t,'deliverable.json','成果')
    file,meta=office.artifact(project,result['artifact_id']);assert file.suffix=='.'+ext
    assert office.render(t,'deliverable.json','成果')['sha256']==result['sha256']
    with zipfile.ZipFile(file) as z:assert z.testzip() is None
    if ext=='xlsx':
        from openpyxl import load_workbook
        assert load_workbook(file).active['A2'].data_type=='s'
    with TestClient(app) as client:
        assert client.get(result['download']['url']).status_code==401
        response=client.get(result['download']['url'],headers=H);assert response.status_code==200 and response.content==file.read_bytes()
    file.write_bytes(b'corrupt')
    with pytest.raises(ValueError):office.artifact(project,result['artifact_id'])

def test_office_chat_and_path_escape_denied(project):
    t=turn(project);t['interaction_mode']='chat'
    with pytest.raises(ValueError):office.render(t,'x.json','x')
    t['interaction_mode']='work'
    with pytest.raises(ValueError):office.render(t,'x.json','../escape')

def test_ha_profile_fails_closed_and_readiness_does_not_claim_ha(monkeypatch):
    assert runtime.readiness()['high_availability_verified'] is False
    monkeypatch.setenv('ATLAS_DEPLOYMENT_PROFILE','ha')
    with pytest.raises(ValueError):runtime.preflight()
    assert not runtime.readiness()['ready']

def test_oidc_signature_audience_subject_and_no_token_fallback(monkeypatch):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048);jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()));jwk['kid']='key-1'
    monkeypatch.setenv('ATLAS_AUTH_MODE','oidc');monkeypatch.setenv('OIDC_ISSUER','https://identity.test');monkeypatch.setenv('OIDC_AUDIENCE','atlas');monkeypatch.setenv('OIDC_JWKS_URL','https://identity.test/keys');monkeypatch.setenv('OIDC_ALLOWED_SUBJECTS','owner');monkeypatch.setattr(identity,'keys',lambda refresh=False:[jwk])
    token=jwt.encode({'iss':'https://identity.test','aud':'atlas','sub':'owner','exp':int(time.time())+120},key,algorithm='RS256',headers={'kid':'key-1'})
    # Lifespan does not expose a shared-token bypass in OIDC mode.
    with TestClient(app) as c:
        assert c.get('/api/identity',headers={'Authorization':'Bearer '+token}).json()['mode']=='oidc'
        assert c.get('/api/identity',headers=H).status_code==401
        wrong=jwt.encode({'iss':'https://identity.test','aud':'other','sub':'owner','exp':int(time.time())+120},key,algorithm='RS256',headers={'kid':'key-1'})
        assert c.get('/api/identity',headers={'Authorization':'Bearer '+wrong}).status_code==401
        other=jwt.encode({'iss':'https://identity.test','aud':'atlas','sub':'another','exp':int(time.time())+120},key,algorithm='RS256',headers={'kid':'key-1'})
        assert c.get('/api/identity',headers={'Authorization':'Bearer '+other}).status_code==403

def test_daily_reservation_is_atomic_and_fails_at_cap(monkeypatch):
    from agent_service.quotas import reserve
    from agent_service.provider import BudgetExceeded
    from agent_service.store import all_items,delete
    for x in all_items('model_quota'):delete('model_quota',x['id'])
    monkeypatch.setenv('MAX_DAILY_MODEL_CALLS','2');monkeypatch.setenv('MAX_DAILY_RESERVED_TOKENS','200')
    assert reserve(100)['calls']==1;assert reserve(100)['calls']==2
    with pytest.raises(BudgetExceeded):reserve(1)

def test_parallel_model_reservations_do_not_overshoot_cap(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from agent_service.quotas import reserve
    from agent_service.provider import BudgetExceeded
    from agent_service.store import all_items,delete
    for x in all_items('model_quota'):delete('model_quota',x['id'])
    monkeypatch.setenv('MAX_DAILY_MODEL_CALLS','4');monkeypatch.setenv('MAX_DAILY_RESERVED_TOKENS','400')
    def call(_):
        try:return reserve(100)['calls']
        except BudgetExceeded:return None
    with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(call,range(12)))
    assert sorted(x for x in results if x is not None)==[1,2,3,4]
