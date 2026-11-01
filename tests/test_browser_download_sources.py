"""Browser downloads are scoped, validated and imported as actual project files."""
import base64
import hashlib
import time
import pytest
from agent_service import browser_downloads as downloads, browser_tools
from agent_service.actor_context import actor_scope
from agent_service.config import DATA
from agent_service.store import get, put, seal, uid
from agent_service.execution import process_once


@pytest.fixture
def scenario(monkeypatch):
    pid='p_'+uid();tid='t_'+uid();sid=hashlib.sha256(uid().encode()).hexdigest()
    put('project',{'name':'网页取数'},pid)
    turn=put('turn',{'project_id':pid,'actor_id':'owner','text':'从网页下载数据并分析',
        'interaction_mode':'work','state':'agent_tool','agent_trace':[{'id':'step1','status':'started'}]},tid)
    snapshot={'url':'https://reports.example.com/report','revision':'a'*64,'controls':[{'label':'导出数据','visible':True,'disabled':False,'sensitive':False}]}
    put('browser_session',{'project_id':pid,'actor_id':'owner','turn_id':tid,'mode':'agent',
        'workspace_id':'b'*64,'state':'open','agent_grant_origin':'https://reports.example.com','agent_grant_expires':time.time()+600,'expires_at':time.time()*1000+60000,'snapshot_secret':seal(snapshot)},sid)
    monkeypatch.setattr(browser_tools,'_scope',lambda turn:None)
    raw='办事处,收入\n东部,9.00\n'.encode()
    calls=[]
    def request(session, operation, body):
        calls.append((session,operation,body))
        if operation=='download':
            return {'filename':'收入.csv','download_id':body['action_id'],
                    'bytes':len(raw),'content_sha256':hashlib.sha256(raw).hexdigest()}
        if operation=='download-chunk':
            chunk=raw[body['offset']:body['offset']+body['length']]
            return {'download_id':body['download_id'],'offset':body['offset'],'bytes':len(chunk),
                    'content_base64':base64.b64encode(chunk).decode()}
        if operation=='download-release':return {'released':True}
        raise AssertionError(operation)
    monkeypatch.setattr(browser_tools,'_request',request)
    args={'session_id':sid,'revision':'a'*64,'ref':1}
    yield turn,args,calls,raw
    from agent_service.store import delete
    delete('browser_session',sid)
    delete('turn',tid)
    delete('project',pid)


def test_download_creates_project_source_and_replay_is_idempotent(scenario):
    turn,args,calls,raw=scenario
    with actor_scope('owner'):
        result=downloads.execute(turn,'download_browser_source',args)
        replay=downloads.execute(turn,'download_browser_source',args)
    assert replay==result and len([x for x in calls if x[1]=='download'])==1
    assert calls[0][1]=='download' and calls[0][2]['agent_authorized'] is True
    source=get('source',result['source_id']);assert source['project_id']==turn['project_id']
    assert source['content_hash']==hashlib.sha256(raw).hexdigest() and source['revision']==1
    from agent_service.store import unseal
    assert (DATA/'uploads'/unseal(source['secret_config'])['stored_name']).read_bytes()==raw
    # Other tests may have queued earlier probes; drain only a bounded number
    # of jobs until this exact source finishes, without assuming queue order.
    for _ in range(100):
        if get('source',source['id'])['status'] in ('ready','failed'):break
        if not process_once():break
    assert get('source',source['id'])['status']=='ready'


def test_download_rejects_wrong_actor_and_stale_control(scenario):
    turn,args,calls,_=scenario
    with actor_scope('u_'+'f'*64):
        with pytest.raises(ValueError,match='当前用户'):downloads.execute(turn,'download_browser_source',args)
    with actor_scope('owner'):
        with pytest.raises(ValueError,match='页面已变化'):downloads.execute(turn,'download_browser_source',{**args,'revision':'c'*64})
    assert calls==[]


def test_unknown_download_and_login_html_never_become_source(scenario,monkeypatch):
    turn,args,calls,_=scenario
    monkeypatch.setattr(browser_tools,'_request',lambda *a,**k:{'state':'unknown'})
    with actor_scope('owner'):
        with pytest.raises(ValueError,match='尚未确认'):downloads.execute(turn,'download_browser_source',args)
    # Only a user takeover/review may clear an ambiguous browser state.
    from agent_service.store import patch
    patch('browser_session',args['session_id'],state='open',unknown_pending=False)
    html=b'<html>sign in</html>'
    def html_request(sid,operation,body):
        if operation=='download':return {'filename':'income.csv','download_id':body['action_id'],'bytes':len(html),'content_sha256':hashlib.sha256(html).hexdigest()}
        return {'download_id':body['download_id'],'offset':body['offset'],'bytes':len(html),'content_base64':base64.b64encode(html).decode()}
    monkeypatch.setattr(browser_tools,'_request',html_request)
    with actor_scope('owner'):
        with pytest.raises(ValueError,match='网页'):downloads.execute(turn,'download_browser_source',args)


def test_same_step_cannot_change_download_target(scenario,monkeypatch):
    turn,args,_,_=scenario
    monkeypatch.setattr(browser_tools,'_request',lambda *a,**k:{'state':'unknown'})
    with actor_scope('owner'):
        with pytest.raises(ValueError):downloads.execute(turn,'download_browser_source',args)
        with pytest.raises(ValueError,match='更换目标'):downloads.execute(turn,'download_browser_source',{**args,'ref':2})


def test_current_origin_grant_is_required_and_expires(scenario):
    from agent_service.store import patch
    turn,args,calls,_=scenario
    with actor_scope('owner'):
        patch('browser_session',args['session_id'],agent_grant_origin='https://other.example.com')
        with pytest.raises(ValueError,match='授权'):downloads.execute(turn,'download_browser_source',args)
        patch('browser_session',args['session_id'],agent_grant_origin='https://reports.example.com',agent_grant_expires=time.time()-1)
        with pytest.raises(ValueError,match='授权'):downloads.execute(turn,'download_browser_source',args)
    assert calls==[]


def test_download_verifies_hash_and_blocks_retry_until_user_review(scenario,monkeypatch):
    turn,args,calls,raw=scenario
    def bad_hash(sid,operation,body):
        if operation=='download':return {'filename':'income.csv','download_id':body['action_id'],'bytes':len(raw),'content_sha256':'0'*64}
        return {'download_id':body['download_id'],'offset':body['offset'],'bytes':len(raw),'content_base64':base64.b64encode(raw).decode()}
    monkeypatch.setattr(browser_tools,'_request',bad_hash)
    with actor_scope('owner'):
        with pytest.raises(ValueError,match='摘要'):downloads.execute(turn,'download_browser_source',args)
        assert get('browser_session',args['session_id'])['unknown_pending'] is True
        with pytest.raises(ValueError,match='未确认'):downloads.execute(turn,'download_browser_source',args)


def test_large_download_is_chunked_and_recovers_without_recapture(scenario,monkeypatch):
    turn,args,calls,_=scenario
    from agent_service.store import patch
    from agent_service import agent
    step={'id':'step1','status':'started','tool':'download_browser_source','arguments':args}
    patch('turn',turn['id'],agent_trace=[step])
    assert agent._started_needs_confirmation(step,get('turn',turn['id'])) is True
    raw=b'area,amount\n'+b'east,9.00\n'*250000
    assert len(raw)>2*1024*1024
    interrupted={'value':False}
    def request(sid,operation,body):
        calls.append((sid,operation,body))
        if operation=='download':
            return {'filename':'large.csv','download_id':body['action_id'],'bytes':len(raw),'content_sha256':hashlib.sha256(raw).hexdigest()}
        if operation=='download-release':return {'released':True}
        if body['offset'] and not interrupted['value']:
            interrupted['value']=True;raise ValueError('temporary connection lost')
        chunk=raw[body['offset']:body['offset']+body['length']]
        return {'download_id':body['download_id'],'offset':body['offset'],'bytes':len(chunk),'content_base64':base64.b64encode(chunk).decode()}
    monkeypatch.setattr(browser_tools,'_request',request)
    with actor_scope('owner'):
        with pytest.raises(ValueError,match='connection lost'):downloads.execute(turn,'download_browser_source',args)
        assert get('browser_session',args['session_id'])['unknown_pending'] is True
        assert agent._started_needs_confirmation(step,get('turn',turn['id'])) is False
        assert agent._started_needs_confirmation({**step,'arguments':{**args,'ref':2}},get('turn',turn['id'])) is True
        owner=get('browser_session',args['session_id'])['unknown_pending_owner']
        patch('browser_session',args['session_id'],unknown_pending_owner='action:other')
        assert agent._started_needs_confirmation(step,get('turn',turn['id'])) is True
        with pytest.raises(ValueError,match='另一项'):downloads.execute(turn,'download_browser_source',args)
        patch('browser_session',args['session_id'],unknown_pending_owner=owner)
        result=downloads.execute(turn,'download_browser_source',args)
    assert len([x for x in calls if x[1]=='download'])==1
    assert len([x for x in calls if x[1]=='download-chunk'])>4
    source=get('source',result['source_id']);assert source['bytes']==len(raw)
    assert source['content_hash']==hashlib.sha256(raw).hexdigest()
    assert not list((DATA/'uploads').glob('.browser-download-*.part'))


def test_over_limit_metadata_rejected_before_any_chunk(scenario,monkeypatch):
    turn,args,calls,_=scenario
    def request(sid,operation,body):
        calls.append((sid,operation,body))
        return {'filename':'huge.csv','download_id':body['action_id'],'bytes':downloads._LIMIT+1,'content_sha256':'0'*64}
    monkeypatch.setattr(browser_tools,'_request',request)
    with actor_scope('owner'):
        with pytest.raises(ValueError,match='体积'):downloads.execute(turn,'download_browser_source',args)
    assert [x[1] for x in calls]==['download']


def test_cancelled_task_stops_between_chunks(scenario,monkeypatch):
    from agent_service.store import patch
    turn,args,calls,_=scenario
    raw=b'field,amount\n'+b'east,9.00\n'*80000
    def request(sid,operation,body):
        calls.append((sid,operation,body))
        if operation=='download':
            return {'filename':'rows.csv','download_id':body['action_id'],'bytes':len(raw),'content_sha256':hashlib.sha256(raw).hexdigest()}
        assert operation=='download-chunk'
        chunk=raw[body['offset']:body['offset']+body['length']]
        patch('turn',turn['id'],outcome='cancelled')
        return {'download_id':body['download_id'],'offset':body['offset'],'bytes':len(chunk),'content_base64':base64.b64encode(chunk).decode()}
    monkeypatch.setattr(browser_tools,'_request',request)
    try:
        with actor_scope('owner'):
            with pytest.raises(ValueError,match='已停止'):
                downloads.execute(turn,'download_browser_source',args)
        assert [x[1] for x in calls]==['download','download-chunk']
    finally:
        for path in (DATA/'uploads').glob('.browser-download-*.part'):path.unlink()
