"""Two human approvals bind the exact browser-captured request, not a guess."""
import hashlib, json, time
from agent_service import browser_tools
from agent_service.actor_context import actor_scope
from agent_service.store import get,patch,seal,unseal
from test_browser_confirmed_actions import setup,prepare


def test_post_requires_click_then_exact_body_approval_and_never_replays(setup,monkeypatch):
    pid,tid,sid,calls,client=setup
    before=unseal(get('browser_session',sid)['snapshot_secret'])
    body='{"period":"2026-10","amount":"18.00"}';data=body.encode()
    meta={'id':'e'*64,'digest':'f'*64,'method':'POST','url':'https://erp.example/report?region=east',
          'content_type':'application/json','body_sha256':hashlib.sha256(data).hexdigest(),
          'bytes':len(data),'expires_at':int((time.time()+120)*1000)}
    meta['digest']=hashlib.sha256(json.dumps({k:meta[k] for k in ('method','url','content_type','body_sha256','bytes')},ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    count=[]
    def request(session,op,payload):
        count.append((op,payload))
        if op=='action':return {**before,'pending_post':meta}
        if op=='post-reveal':return {'session_id':sid,'pending_post':{**meta,'body':body}}
        if op=='post-decision':return {'session_id':sid,'state':'sent','transport_code':200}
        if op=='observe':return before
        raise AssertionError(op)
    monkeypatch.setattr(browser_tools,'_request',request)
    path,turn=prepare(setup,'prepare_browser_post',{'ref':2})
    assert not count
    proposed=client.post(path+'/reveal',json={}).json();assert proposed['kind']=='capture_post'
    result=client.post(path+'/decision',json={'digest':proposed['digest'],'approve':True})
    assert result.status_code==200,result.text
    assert result.json()['state']=='pending' and [op for op,_ in count]==['action','post-reveal']
    assert get('turn',tid)['agent_trace'][-1]['observation']['browser_fill_pending'] is True
    listing=client.get(path.rsplit('/',1)[0]).json()['items'][0];assert listing['kind']=='post' and body not in json.dumps(listing)
    assert client.post(path+'/decision',json={'digest':listing['digest'],'approve':True}).status_code==400
    revealed=client.post(path+'/reveal',json={}).json()
    assert revealed['body']==body and revealed['url']==meta['url'] and revealed['method']=='POST'
    assert client.post(path+'/decision',json={'digest':proposed['digest'],'approve':True}).status_code==400
    accepted=client.post(path+'/decision',json={'digest':revealed['digest'],'approve':True})
    assert accepted.status_code==200,accepted.text
    assert accepted.json()['state']=='applied' and '不代表业务成功' in accepted.json()['notice']
    assert [op for op,_ in count].count('post-decision')==1
    assert client.post(path+'/decision',json={'digest':revealed['digest'],'approve':True}).json()['state']=='applied'
    assert [op for op,_ in count].count('post-decision')==1
    assert body not in json.dumps(get('turn',tid),ensure_ascii=False)


def test_changed_or_secret_payload_rejected_without_authorization(setup,monkeypatch):
    pid,tid,sid,calls,client=setup
    before=unseal(get('browser_session',sid)['snapshot_secret'])
    data='{"password":"not-allowed"}'
    meta={'id':'e'*64,'digest':'f'*64,'method':'POST','url':'https://erp.example/submit',
          'content_type':'application/json','body_sha256':hashlib.sha256(data.encode()).hexdigest(),
          'bytes':len(data.encode()),'expires_at':int((time.time()+120)*1000)}
    meta['digest']=hashlib.sha256(json.dumps({k:meta[k] for k in ('method','url','content_type','body_sha256','bytes')},ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    invoked=[]
    def request(session,op,payload):
        invoked.append(op)
        if op=='action':return {**before,'pending_post':meta}
        if op=='post-reveal':return {'session_id':sid,'pending_post':{**meta,'body':data}}
        raise AssertionError('request must not be sent')
    monkeypatch.setattr(browser_tools,'_request',request)
    path,_=prepare(setup,'prepare_browser_post',{'ref':2});first=client.post(path+'/reveal',json={}).json()
    result=client.post(path+'/decision',json={'digest':first['digest'],'approve':True})
    assert result.status_code==400 and 'post-decision' not in invoked
    assert get('browser_session',sid)['unknown_pending'] is True
    assert client.post(path+'/decision',json={'digest':first['digest'],'approve':True}).status_code==400


def test_duplicate_json_fields_cannot_be_approved():
    import pytest
    raw='{"amount":"100.00","amount":"1000.00"}'
    meta={'id':'e'*64,'method':'POST','url':'https://erp.example/submit','content_type':'application/json',
          'body_sha256':hashlib.sha256(raw.encode()).hexdigest(),'bytes':len(raw.encode()),
          'expires_at':int((time.time()+60)*1000)}
    meta['digest']=hashlib.sha256(json.dumps({k:meta[k] for k in ('method','url','content_type','body_sha256','bytes')},ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    record={'project_id':'p','actor_id':'a','turn_id':'t','session_id':'s','origin':'https://erp.example'}
    with pytest.raises(ValueError,match='不能完整解析'):
        browser_tools._validate_captured_post(record,{'pending_post':{**meta,'body':raw}},meta)


def test_ambiguous_or_undecodable_url_and_form_rejected():
    import pytest
    record={'project_id':'p','actor_id':'a','turn_id':'t','session_id':'s','origin':'https://erp.example'}
    for url,body in [('https://erp.example/q?amount=1&amount=2','month=10'),
                     ('https://erp.example/q','amount=1&amount=2'),
                     ('https://erp.example/q','name=%FF'),('https://erp.example/q?name=%XX','a=1')]:
        meta={'id':'e'*64,'method':'POST','url':url,'content_type':'application/x-www-form-urlencoded',
              'body_sha256':hashlib.sha256(body.encode()).hexdigest(),'bytes':len(body.encode()),
              'expires_at':int((time.time()+60)*1000)}
        meta['digest']=hashlib.sha256(json.dumps({k:meta[k] for k in ('method','url','content_type','body_sha256','bytes')},ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        with pytest.raises(ValueError):browser_tools._validate_captured_post(record,{'pending_post':{**meta,'body':body}},meta)
