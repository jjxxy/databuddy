"""Versioned recipient comments are opt-in, authenticated and audited."""
from concurrent.futures import ThreadPoolExecutor,TimeoutError
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_service import audit,data_sharing as sharing,project_data as data
from agent_service.api import app
from agent_service.jobs import get_job
from agent_service.store import delete,engine,get,items,patch
from test_business_reports import real_result

REAL_VERIFIER=sharing.verified_subject


@pytest.fixture
def comments(monkeypatch):
    for key,value in {'NEXUS_AUTH_MODE':'oidc','OIDC_ISSUER':'https://identity.example.test',
                      'OIDC_AUDIENCE':'nexus','OIDC_JWKS_URL':'https://identity.example.test/jwks',
                      'OIDC_ALLOWED_SUBJECTS':'owner'}.items():monkeypatch.setenv(key,value)
    pid,rid=real_result();job=get_job(rid);data.publish_result(pid,rid,job['commit'])
    record=sharing.create_share(pid,rid,['recipient','recipient-b'],'owner',allow_comments=True)
    monkeypatch.setattr(sharing,'verified_subject',lambda request:request.headers.get('subject','recipient'))
    yield pid,rid,job['target'],record
    with engine.begin() as connection:
        connection.execute(items.delete().where(items.c.kind==sharing.COMMENT_KIND,
                           items.c.id.like(sharing._comment_prefix(record['id']).replace('_',r'\_')+'%',escape='\\')))
    delete(sharing.KIND,record['id'])


def req(subject='recipient'):
    return SimpleNamespace(headers={'subject':subject})


def post(record,text='这是反馈',request_id='comment_request_12345678',subject='recipient',expected=None):
    return sharing.add_comment(req(subject),record['id'],text,request_id,expected or record['handoff_sha256'])


def test_comments_are_opt_in_version_pinned_idempotent_and_audited(comments):
    pid,rid,wid,record=comments
    result=post(record,text='财务说明 <script>alert(1)</script>')
    again=post(record,text='财务说明 <script>alert(1)</script>')
    assert result==again and result['handoff_sha256']==record['handoff_sha256']
    assert result['author'].startswith('用户 ') and result['text'].startswith('财务说明')
    page=sharing.list_comments(req('recipient-b'),record['id'])
    assert page['total']==1 and page['items']==[result] and '不构成财务审批' in page['notice']
    with pytest.raises(HTTPException) as error:post(record,text='不同的内容')
    assert error.value.status_code==409
    with pytest.raises(HTTPException) as error:post(record,expected='f'*64)
    assert error.value.status_code==409
    with engine.connect() as connection:
        rows=connection.execute(select(audit.events).where(audit.events.c.subject==result['id'])).mappings().all()
        item=connection.execute(select(items.c.body).where(items.c.id==result['id'])).scalar_one()
    assert len(rows)==1 and rows[0]['action']=='share.comment.created'
    assert '财务说明' not in rows[0]['body'] and '<script>' not in rows[0]['body'] and '财务说明' not in item
    assert audit.verify()['ok']


def test_share_comment_permission_identity_and_author_are_scoped(comments,monkeypatch):
    pid,rid,wid,record=comments
    first=post(record,subject='recipient')
    second=post(record,subject='recipient-b')
    assert first['id']!=second['id'] and first['author']!=second['author']
    with pytest.raises(HTTPException) as error:sharing.list_comments(req('not-allowed'),record['id'])
    assert error.value.status_code==403
    with pytest.raises(HTTPException) as error:post(record,subject='not-allowed')
    assert error.value.status_code==403
    closed=sharing.create_share(pid,rid,['recipient'],'owner')
    try:
        with pytest.raises(HTTPException) as error:sharing.list_comments(req(),closed['id'])
        assert error.value.status_code==403
        with pytest.raises(HTTPException) as error:post(closed)
        assert error.value.status_code==403
    finally:delete(sharing.KIND,closed['id'])


def test_revocation_and_version_replacement_block_comment_access(comments):
    pid,rid,wid,record=comments;post(record)
    patch('data_workflow',wid,published_run='another-result')
    with pytest.raises(HTTPException):sharing.list_comments(req(),record['id'])
    with pytest.raises(HTTPException):post(record,request_id='comment_request_98765432')
    patch('data_workflow',wid,published_run=rid)
    sharing.revoke(pid,record['id'],'owner')
    with pytest.raises(HTTPException):sharing.list_comments(req(),record['id'])
    with pytest.raises(HTTPException):post(record,request_id='comment_request_98765432')


def test_comment_and_revoke_are_serialized(comments,monkeypatch):
    pid,rid,wid,record=comments;entered=Event();release=Event();started=Event()
    original=sharing._comment_rows
    def blocked(*args,**kwargs):
        entered.set();assert release.wait(5)
        return original(*args,**kwargs)
    monkeypatch.setattr(sharing,'_comment_rows',blocked)
    def revoke():started.set();return sharing.revoke(pid,record['id'],'owner')
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer=pool.submit(post,record)
        assert entered.wait(5)
        stopper=pool.submit(revoke);assert started.wait(5)
        try:
            with pytest.raises(TimeoutError):stopper.result(timeout=.2)
        finally:release.set()
        assert writer.result(timeout=10)['text']=='这是反馈'
        assert stopper.result(timeout=10)['state']=='revoked'
    with pytest.raises(HTTPException):sharing.list_comments(req(),record['id'])


def test_comment_http_dto_is_plain_text_and_not_cached(comments):
    pid,rid,wid,record=comments
    client=TestClient(app)
    body={'text':'<b>反馈</b>','request_id':'http_comment_request_12345',
          'expected_handoff_sha256':record['handoff_sha256']}
    response=client.post('/api/shared-data/'+record['id']+'/comments',json=body)
    assert response.status_code==200,response.text
    assert response.json()['text']=='<b>反馈</b>' and 'no-store' in response.headers['cache-control']
    import hashlib,json
    with engine.connect() as connection:
        generic=connection.execute(select(audit.events).where(audit.events.c.action=='api.POST',
            audit.events.c.subject=='/api/shared-data/'+record['id']+'/comments').order_by(audit.events.c.sequence.desc())).mappings().first()
    assert generic is not None and json.loads(generic['body'])['actor']==hashlib.sha256(b'recipient').hexdigest()[:20]
    page=client.get('/api/shared-data/'+record['id']+'/comments').json()
    assert page['items'][0]['text']=='<b>反馈</b>' and page['total']==1
    assert 'actor' not in page['items'][0]


def test_concurrent_idempotent_retries_do_not_duplicate_audit(comments):
    pid,rid,wid,record=comments
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(post,record);second=pool.submit(post,record)
        a=first.result(timeout=10);b=second.result(timeout=10)
    assert a==b and sharing.list_comments(req(),record['id'])['total']==1
    with engine.connect() as connection:
        events=connection.execute(select(audit.events).where(audit.events.c.subject==a['id'])).all()
    assert len(events)==1 and audit.verify()['ok']


def test_audit_failure_rolls_back_comment(comments,monkeypatch):
    pid,rid,wid,record=comments
    monkeypatch.setattr(audit,'digest',lambda *args:(_ for _ in ()).throw(ValueError('audit unavailable')))
    with pytest.raises(ValueError):post(record)
    assert sharing.list_comments(req(),record['id'])['total']==0


def test_expiration_deletion_and_capacity_never_leave_new_comment(comments,monkeypatch):
    from agent_service import workspace_lifecycle
    pid,rid,wid,record=comments
    monkeypatch.setattr(sharing,'MAX_COMMENTS_PER_SHARE',1)
    post(record)
    with pytest.raises(HTTPException) as error:post(record,request_id='comment_request_87654321')
    assert error.value.status_code==409
    patch(sharing.KIND,record['id'],expires_at='2000-01-01T00:00:00+00:00')
    with pytest.raises(HTTPException):sharing.list_comments(req(),record['id'])
    patch(sharing.KIND,record['id'],expires_at=record['expires_at'])
    workspace_lifecycle.delete_project(pid)
    with pytest.raises(HTTPException):post(record,request_id='comment_request_87654321')


def test_comment_endpoint_blocks_cross_origin_writes(comments):
    pid,rid,wid,record=comments;client=TestClient(app)
    body={'text':'反馈','request_id':'cross_site_request_123456',
          'expected_handoff_sha256':record['handoff_sha256']}
    for headers in ({'origin':'https://other.example','sec-fetch-site':'cross-site'},
                    {'origin':'http://testserver','sec-fetch-site':'same-site'},
                    {'origin':'https://other.example'},
                    {'origin':'https://testserver'}):
        assert client.post('/api/shared-data/'+record['id']+'/comments',json=body,headers=headers).status_code==403
    assert client.post('/api/shared-data/'+record['id']+'/comments',json=body,
                       headers={'origin':'http://testserver','sec-fetch-site':'same-origin'}).status_code==200


def test_comment_with_corrupted_ciphertext_fails_closed(comments):
    import json
    pid,rid,wid,record=comments;result=post(record)
    item=get(sharing.COMMENT_KIND,result['id']);item['sealed']='not-valid-ciphertext'
    with engine.begin() as connection:
        connection.execute(items.update().where(items.c.id==result['id']).values(body=json.dumps(item)))
    with pytest.raises(HTTPException) as error:sharing.list_comments(req(),record['id'])
    assert error.value.status_code==409


def test_signed_oidc_recipient_cannot_impersonate_other_actor(comments,monkeypatch):
    import hashlib,json,jwt
    from datetime import datetime,timedelta,timezone
    from cryptography.hazmat.primitives.asymmetric import rsa
    from agent_service import identity
    pid,rid,wid,record=comments
    private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    key=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    key.update(kid='review-key',alg='RS256',use='sig',key_ops=['verify'])
    monkeypatch.setattr(identity,'keys',lambda refresh=False:[key])
    monkeypatch.setattr(sharing,'verified_subject',REAL_VERIFIER)
    def auth(subject):
        token=jwt.encode({'sub':subject,'iss':'https://identity.example.test','aud':'nexus',
                          'exp':datetime.now(timezone.utc)+timedelta(minutes=2)},private,
                         algorithm='RS256',headers={'kid':'review-key'})
        return {'Authorization':'Bearer '+token}
    body={'text':'审核建议，不构成审批','request_id':'signed_comment_request_12345',
          'expected_handoff_sha256':record['handoff_sha256']}
    client=TestClient(app);route='/api/shared-data/'+record['id']+'/comments'
    response=client.post(route,json=body,headers=auth('recipient'))
    assert response.status_code==200,response.text
    assert client.get(route,headers=auth('recipient-b')).json()['items'][0]['id']==response.json()['id']
    assert client.get(route,headers=auth('other')).status_code==403
    assert client.post(route,json=body,headers={'Authorization':'Bearer invalid'}).status_code==401
    with engine.connect() as connection:
        generic=connection.execute(select(audit.events).where(audit.events.c.action=='api.POST',
            audit.events.c.subject==route).order_by(audit.events.c.sequence.desc())).mappings().first()
    assert json.loads(generic['body'])['actor']==hashlib.sha256(b'recipient').hexdigest()[:20]


def test_comment_prefix_matches_literal_underscores(comments):
    import json
    pid,rid,wid,record=comments;post(record)
    prefix=sharing._comment_prefix(record['id'])
    # IDs that match SQL LIKE wildcards but not the literal prefix must never
    # be included in this share's encrypted discussion.
    foreign=prefix.replace('_','x')+'f'*44
    with engine.begin() as connection:
        connection.execute(items.insert().values(id=foreign,kind=sharing.COMMENT_KIND,body=json.dumps({'id':foreign,'sealed':'bad'})))
    try:assert sharing.list_comments(req(),record['id'])['total']==1
    finally:delete(sharing.COMMENT_KIND,foreign)


def test_comment_origin_uses_configured_https_origin_not_forwarded_headers(comments,monkeypatch):
    pid,rid,wid,record=comments;client=TestClient(app)
    monkeypatch.setenv('PUBLIC_APP_URL','https://testserver')
    body={'text':'反馈','request_id':'secure_origin_request_123456',
          'expected_handoff_sha256':record['handoff_sha256']}
    route='/api/shared-data/'+record['id']+'/comments'
    assert client.post(route,json=body,headers={'origin':'http://testserver',
                       'x-forwarded-proto':'https'}).status_code==403
    assert client.post(route,json=body,headers={'origin':'https://testserver'}).status_code==200
