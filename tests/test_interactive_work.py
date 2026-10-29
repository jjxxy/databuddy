"""HTTP/provider fixtures test contracts, not live model output or gVisor."""
import base64
import hashlib
import io
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from agent_service import agent_context, browser_tools as browser, media_tools as media, provider, workspace_artifacts
from agent_service.actor_context import actor_scope
from agent_service.store import all_items, delete, get, now, patch, put, uid, unseal
from browser_runner.proxy import destination


def png():
    output=io.BytesIO();Image.new('RGB',(80,60),'blue').save(output,format='PNG');return output.getvalue()


@pytest.fixture
def interactive(monkeypatch):
    kinds=('project','turn','conversation','browser_session','browser_action','web_evidence','media_artifact','workspace_artifact','media_job','voice_call')
    before={k:{v['id'] for v in all_items(k)} for k in kinds}
    project=put('project',{'name':'Interactive fixture','created_at':now()},'p_'+uid())
    turn=put('turn',{'project_id':project['id'],'actor_id':'owner','text':'生成图片并浏览网站',
        'state':'agent','interaction_mode':'work','created_at':now()})
    monkeypatch.setenv('ATLAS_WEB_ENABLED','1');monkeypatch.setenv('ATLAS_WEB_ALLOWED_HOSTS','public.test')
    monkeypatch.setenv('ATLAS_BROWSER_URL','https://browser.test');monkeypatch.setenv('ATLAS_BROWSER_TOKEN','b'*40)
    monkeypatch.setenv('ATLAS_MEDIA_BASE_URL','https://media.test/v1');monkeypatch.setenv('ATLAS_MEDIA_API_KEY','private-provider-key')
    for name in ('IMAGE','TRANSCRIBE','SPEECH','REALTIME'):monkeypatch.setenv('ATLAS_'+name+'_MODEL','explicit-'+name.lower()+'-fixture')
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True,'engine':'atlas'})
    monkeypatch.setattr('agent_service.connectors.safe_api_url',lambda url:'93.184.216.34')
    yield turn
    for kind,previous in before.items():
        for value in all_items(kind):
            if value['id'] not in previous:delete(kind,value['id'])


def test_generated_image_is_real_versioned_artifact_reused_without_second_charge(interactive,monkeypatch):
    calls=[];content=png()
    def request(path,**kwargs):
        calls.append((path,kwargs));return json.dumps({'data':[{'b64_json':base64.b64encode(content).decode()}]}).encode()
    monkeypatch.setattr(media,'_request',request)
    result=media.execute(interactive,'generate_image',{'prompt':'画一张蓝色卡片'})
    path,record=workspace_artifacts.file(interactive['project_id'],result['artifact_id'])
    assert path.read_bytes()==content and record['download_sha256']==hashlib.sha256(content).hexdigest()
    assert workspace_artifacts.preview(interactive['project_id'],result['artifact_id'])['render']=='image'
    again=media.execute(interactive,'generate_image',{'prompt':'画一张蓝色卡片'})
    assert again['artifact_id']==result['artifact_id'] and len(calls)==1
    assert 'private-provider-key' not in json.dumps(all_items('media_job'))
    assert all_items('media_job')[-1].get('result_secret')
    assert any(v['id']==result['artifact_id'] for v in workspace_artifacts.catalog(interactive['project_id'])['items'])


def test_media_task_cannot_complete_with_only_a_prose_claim(interactive,monkeypatch):
    from agent_service import tasks
    turn=patch('turn',interactive['id'],text='生成一张图片')
    assert tasks.ensure(turn)['kind']=='image'
    assert tasks.verify(get('turn',turn['id']))['missing']==['image']
    monkeypatch.setattr(media,'_request',lambda *a,**k:json.dumps({'data':[{'b64_json':base64.b64encode(png()).decode()}]}).encode())
    result=media.execute(turn,'generate_image',{'prompt':'蓝色卡片'})
    turn=patch('turn',turn['id'],agent_trace=[{'tool':'generate_image','status':'done','arguments':{'prompt':'蓝色卡片'},'observation':result}])
    assert tasks.verify(turn)['verified']
    path,_=workspace_artifacts.file(turn['project_id'],result['artifact_id']);original=path.read_bytes()
    try:
        path.write_bytes(b'corrupted image')
        assert not tasks.verify(get('turn',turn['id']))['verified']
    finally:path.write_bytes(original)


def test_edit_sends_actual_selected_image_bytes_and_cross_actor_cannot_read(interactive,monkeypatch):
    content=png();original=media.save(interactive['project_id'],content,'reference.png',turn_id=interactive['id'])
    calls=[]
    def request(path,**kwargs):
        calls.append((path,kwargs));return json.dumps({'data':[{'b64_json':base64.b64encode(content).decode()}]}).encode()
    monkeypatch.setattr(media,'_request',request)
    media.execute(interactive,'generate_image',{'prompt':'把蓝色改成深蓝','reference_artifact_id':original['artifact_id']})
    assert calls[0][0]=='images/edits' and calls[0][1]['files']['image'][1]==content
    other=put('project',{'name':'Other'},'p_'+uid())
    with pytest.raises(KeyError):workspace_artifacts.file(other['id'],original['artifact_id'])
    with actor_scope('u_'+'b'*64):
        with pytest.raises(KeyError):workspace_artifacts.file(interactive['project_id'],original['artifact_id'])


def test_media_timeout_does_not_replay_billable_request(interactive,monkeypatch):
    calls=[]
    def uncertain(*args,**kwargs):calls.append(1);raise ValueError('fixture lost receipt')
    monkeypatch.setattr(media,'_request',uncertain)
    with pytest.raises(ValueError):media.execute(interactive,'generate_image',{'prompt':'蓝色卡片'})
    with pytest.raises(ValueError,match='不自动重复'):media.execute(interactive,'generate_image',{'prompt':'蓝色卡片'})
    assert len(calls)==1


def test_image_urls_and_truncated_png_are_not_successful_files(interactive,monkeypatch):
    monkeypatch.setattr(media,'_request',lambda *a,**k:b'{"data":[{"url":"http://127.0.0.1/secret"}]}')
    with pytest.raises(ValueError,match='有效PNG'):media.execute(interactive,'generate_image',{'prompt':'蓝色卡片'})
    with pytest.raises(ValueError):media.verify_image(png()[:25])
    assert not all_items('media_artifact')


def test_media_transport_pins_dns_keeps_secret_on_fixed_endpoint_and_rejects_redirect(interactive,monkeypatch):
    calls=[];redirect=[False];original=httpx.Client
    def respond(request):
        calls.append(request)
        if redirect[0]:return httpx.Response(302,headers={'location':'https://evil.test/'})
        return httpx.Response(200,content=b'fixture response')
    monkeypatch.setattr(media.httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(respond),**kwargs))
    assert media._request('images/generations',payload={'prompt':'test'})==b'fixture response'
    req=calls[-1]
    assert req.url.host=='93.184.216.34' and req.headers['Host']=='media.test'
    assert req.extensions['sni_hostname']=='media.test' and req.headers['Authorization']=='Bearer private-provider-key'
    redirect[0]=True
    with pytest.raises(ValueError,match='未返回可确认'):media._request('images/generations',payload={})
    assert len(calls)==2


def test_media_request_quota_and_model_permission(interactive,monkeypatch):
    monkeypatch.setenv('ATLAS_MEDIA_DAILY_REQUESTS','1')
    media.perform(interactive['project_id'],'one','a',lambda:{'ok':True})
    with pytest.raises(ValueError,match='上限'):media.perform(interactive['project_id'],'two','b',lambda:{'ok':True})
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':False})
    with pytest.raises(ValueError,match='允许'):media.project_scope(interactive['project_id'])


@pytest.fixture
def browser_fixture(interactive,monkeypatch):
    content=png();state={'mode':'agent','calls':[]}
    def request(sid,operation,body):
        state['calls'].append((sid,operation,body))
        if operation=='health':return {'production_policy':True,'restricted_proxy':True,'workspace_id':body['workspace_id']}
        if operation=='mode':state['mode']=body['mode']
        if operation=='close':return {'closed':True}
        return {'session_id':sid,'revision':'a'*64,'url':'https://public.test/article','title':'实际页面',
            'text':'合计为118.1500。','controls':[{'ref':1,'tag':'a','href':'https://public.test/next','visible':True}],
            'mode':state['mode'],'image_base64':base64.b64encode(content).decode(),
            'image_sha256':hashlib.sha256(content).hexdigest(),'untrusted_content':True}
    monkeypatch.setattr(browser,'_request',request)
    return interactive,state


def test_browser_handoff_blocks_agent_observation_and_records_citable_page(browser_fixture):
    turn,state=browser_fixture
    opened=browser.execute(turn,'open_browser',{'url':'https://public.test/article'})
    sid=opened['session_id'];eid=opened['evidence_id']
    from agent_service import web_research
    citation={'evidence_id':eid,'quote':'合计为118.1500。','claim':'合计118.1500'}
    assert web_research.validate_citations(get('turn',turn['id']),[citation],'合计118.1500')
    assert 'image_base64' not in opened
    browser.execute(turn,'request_browser_takeover',{'session_id':sid,'reason':'请登录'})
    count=len(state['calls'])
    assert browser.execute(turn,'observe_browser',{'session_id':sid})['awaiting_user']
    assert len(state['calls'])==count
    next_turn=put('turn',dict(turn,id=uid(),text='已登录，请继续'),uid())
    listing=browser.execute(next_turn,'browser_capabilities',{})
    assert listing['sessions'][0]['id']==sid and listing['sessions'][0]['mode']=='human'
    assert 'text' not in listing['sessions'][0] and len(state['calls'])==count
    browser.mode(browser.owned(turn['project_id'],sid),'agent')
    assert browser.execute(turn,'observe_browser',{'session_id':sid})['text']=='合计为118.1500。'
    assert '实际页面' not in json.dumps(get('browser_session',sid),ensure_ascii=False)


def test_browser_waiting_for_login_is_not_task_completion(browser_fixture):
    from agent_service import tasks
    turn,_=browser_fixture;turn=patch('turn',turn['id'],text='浏览这个网站')
    opened=browser.execute(turn,'open_browser',{'url':'https://public.test/'})
    sid=opened['session_id'];browser.execute(turn,'request_browser_takeover',{'session_id':sid,'reason':'登录后继续'})
    turn=patch('turn',turn['id'],agent_trace=[{'tool':'request_browser_takeover','status':'done',
        'arguments':{'session_id':sid},'observation':{'awaiting_user':True}}])
    assert 'browser_awaiting_user' in tasks.verify(turn)['missing']
    browser.mode(browser.owned(turn['project_id'],sid),'agent')
    assert tasks.verify(get('turn',turn['id']))['verified']


def test_browser_is_owner_project_and_public_data_scoped(browser_fixture):
    turn,_=browser_fixture;result=browser.execute(turn,'open_browser',{'url':'https://public.test/'})
    sid=result['session_id'];other=put('project',{'name':'Other'},'p_'+uid())
    with pytest.raises(KeyError):browser.owned(other['id'],sid)
    with actor_scope('u_'+'a'*64):
        with pytest.raises(KeyError):browser.owned(turn['project_id'],sid)
    patch('project',turn['project_id'],company_private=True)
    with pytest.raises(ValueError,match='内部'):browser.execute(turn,'observe_browser',{'session_id':sid})


def test_human_password_is_transient_and_repeat_request_does_not_submit_twice(browser_fixture):
    turn,state=browser_fixture;result=browser.execute(turn,'open_browser',{'url':'https://public.test/'})
    sid=result['session_id'];browser.mode(browser.owned(turn['project_id'],sid),'human')
    app=FastAPI();browser.install(app,lambda:None);client=TestClient(app)
    path='/api/projects/'+turn['project_id']+'/browsers/'+sid+'/action'
    body={'request_id':'a'*32,'revision':'a'*64,'action':'fill','ref':1,'text':'DO-NOT-PERSIST-PASSWORD'}
    assert client.post(path,json=body).status_code==200
    assert client.post(path,json=body).status_code==200
    actions=[v for v in state['calls'] if v[1]=='action'];assert len(actions)==1
    assert actions[0][2]['text']=='DO-NOT-PERSIST-PASSWORD'
    for kind in ('browser_session','browser_action','turn'):
        assert 'DO-NOT-PERSIST-PASSWORD' not in json.dumps(all_items(kind))


def test_recording_transcription_and_realtime_use_real_protocol_without_exposing_key(interactive,monkeypatch):
    calls=[]
    def request(path,**kwargs):
        calls.append((path,kwargs))
        if path=='audio/transcriptions':return '{"text":"请生成报告"}'.encode()
        if path.endswith('/hangup'):return b''
        return b'v=0\r\nfixture-answer',{'location':'/v1/realtime/calls/rtc_fixture'}
    monkeypatch.setattr(media,'_request',request)
    app=FastAPI();media.install(app,lambda:None);client=TestClient(app);base='/api/projects/'+interactive['project_id']
    response=client.post(base+'/voice/transcribe',data={'request_id':'a'*32},files={'audio':('recording.webm',b'actual-audio-fixture','audio/webm')})
    assert response.status_code==200 and response.json()['text']=='请生成报告'
    assert calls[0][1]['files']['file'][1]==b'actual-audio-fixture'
    response=client.post(base+'/voice/connect',json={'request_id':'b'*32,'sdp':'v=0\r\nfixture-offer'})
    assert response.status_code==200 and response.json()['sdp'].startswith('v=0')
    session=json.loads(calls[-1][1]['files']['session'][1]);assert session['tools']==[] and session['max_output_tokens']==1024
    assert 'private-provider-key' not in response.text
    assert '请生成报告' not in json.dumps(all_items('media_job'),ensure_ascii=False)
    cid=response.json()['call_id']
    with actor_scope('u_'+'c'*64):
        with pytest.raises(KeyError):media.end_call(get('voice_call',cid))
        assert client.post(base+'/voice/calls/'+cid+'/end').status_code==404
    assert client.post(base+'/voice/calls/'+cid+'/end').json()['closed']
    assert calls[-1][0]=='realtime/calls/rtc_fixture/hangup'
    count=len(calls);assert client.post(base+'/voice/calls/'+cid+'/end').json()['closed'];assert len(calls)==count


def test_voice_deadline_is_server_side_and_failed_hangup_is_retried(interactive,monkeypatch):
    from agent_service.store import seal
    value=put('voice_call',{'project_id':interactive['project_id'],'actor_id':'owner','state':'open','deadline':0,
        'call_secret':seal('rtc_deadline'),'provider_sha256':hashlib.sha256(b'https://media.test/v1').hexdigest()},'vc_'+uid())
    calls=[]
    def failed(*a,**k):calls.append(a[0]);raise ValueError('fixture transport unavailable')
    monkeypatch.setattr(media,'_request',failed);media.reconcile_calls()
    assert get('voice_call',value['id'])['state']=='open' and get('voice_call',value['id'])['close_requested']
    monkeypatch.setattr(media,'_request',lambda *a,**k:b'');media.reconcile_calls()
    assert get('voice_call',value['id'])['state']=='closed'


def test_recording_transport_is_bounded_before_multipart_parsing(interactive,monkeypatch):
    monkeypatch.setattr(media,'_request',lambda *a,**k:pytest.fail('oversized audio must not reach provider'))
    app=FastAPI();media.install(app,lambda:None);client=TestClient(app)
    response=client.post('/api/projects/'+interactive['project_id']+'/voice/transcribe',
                         content=b'x'*(13*1024*1024+1),headers={'Content-Type':'multipart/form-data; boundary=fixture'})
    assert response.status_code==413


def test_browser_registry_selects_project_secret_and_never_falls_back_for_partial_entry(interactive,monkeypatch):
    wid=hashlib.sha256(('owner'+interactive['project_id']).encode()).hexdigest()
    monkeypatch.setenv('ATLAS_BROWSER_ENDPOINTS_JSON',json.dumps({wid:{'url':'https://tenant-browser.test','token':'tenant-secret-'*4}}))
    original=httpx.Client;calls=[]
    def respond(request):
        calls.append(request);return httpx.Response(200,json={'production_policy':True,'workspace_id':wid})
    monkeypatch.setattr(browser.httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(respond),**kwargs))
    browser._request('','health',{'workspace_id':wid})
    assert calls[-1].url.host=='tenant-browser.test' and calls[-1].headers['Authorization']=='Bearer '+'tenant-secret-'*4
    assert 'tenant-secret' not in json.dumps(browser.status())
    monkeypatch.setenv('ATLAS_BROWSER_ENDPOINTS_JSON',json.dumps({wid:{'url':'https://partial.test'}}))
    with pytest.raises(ValueError,match='尚未分配'):browser._request('','health',{'workspace_id':wid})
    assert len(calls)==1


def test_browser_refuses_service_bound_to_a_different_workspace(interactive,monkeypatch):
    calls=[]
    def request(sid,operation,body):
        calls.append(operation);return {'production_policy':True,'restricted_proxy':True,'workspace_id':'0'*64}
    monkeypatch.setattr(browser,'_request',request)
    with pytest.raises(ValueError,match='绑定'):browser.execute(interactive,'open_browser',{'url':'https://public.test/'})
    assert calls==['health'] and not all_items('browser_session')


def test_literal_file_mentioning_image_generation_keeps_exact_file_contract(interactive):
    from agent_service import tasks
    turn=patch('turn',interactive['id'],text='创建文件 notes.txt，内容逐字为：生成一张图片')
    contract=tasks.ensure(turn)
    assert contract['kind']=='file' and contract['requirements']==['file','file_exact']


def test_separate_media_destination_cannot_inherit_private_text_model_approval(interactive,monkeypatch):
    monkeypatch.setattr('agent_service.company_search.assert_model_allowed',lambda pid:None)
    patch('project',interactive['project_id'],work_private=True)
    with pytest.raises(ValueError,match='独立目的地'):media.project_scope(interactive['project_id'])


@pytest.mark.parametrize('address',['127.0.0.1','10.1.2.3','169.254.169.254','224.0.0.1','::1','::ffff:127.0.0.1'])
def test_browser_proxy_rejects_private_metadata_multicast_and_mapped_ip(monkeypatch,address):
    import socket
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(None,None,None,None,(address,443))])
    with pytest.raises(ValueError):destination('allowed.test',443,{'allowed.test'})


def test_browser_proxy_pins_public_ip_and_enforces_host_port(monkeypatch):
    import socket
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(None,None,None,None,('93.184.216.34',443))])
    assert destination('allowed.test',443,{'allowed.test'})=='93.184.216.34'
    with pytest.raises(ValueError):destination('other.test',443,{'allowed.test'})
    with pytest.raises(ValueError):destination('allowed.test',22,{'allowed.test'})


def test_unconfigured_tools_are_hidden_and_chat_cannot_generate(interactive,monkeypatch):
    assert 'open_browser' in agent_context.allowed(interactive)
    assert 'generate_image' not in agent_context.allowed(dict(interactive,interaction_mode='chat'))
    monkeypatch.delenv('ATLAS_IMAGE_MODEL');monkeypatch.delenv('ATLAS_BROWSER_TOKEN')
    allowed=agent_context.allowed(interactive)
    assert 'generate_image' not in allowed and 'open_browser' not in allowed
