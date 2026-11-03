import json
import pytest
from fastapi.testclient import TestClient
from agent_service import artifact_sharing as sharing
from agent_service.store import get, patch
from test_artifact_sharing import oidc, spec, payload
from test_project_data import project


def test_opt_in_identity_version_retry_and_revocation(oidc,monkeypatch):
    pid=project();item=spec(pid);client=TestClient(__import__('agent_service.api',fromlist=['app']).app)
    share=sharing.create(pid,payload(item,allow_comments=True),'owner');url='/api/shared-artifacts/'+share['id']
    preview=client.get(url);assert preview.status_code==200
    data=preview.json();assert data['allow_comments'] is True and len(data['viewer_hash'])==64
    value={'text':'请核对\n收入','request_id':'comment_request_0001','expected_content_sha256':data['sha256'],'expected_viewer_hash':data['viewer_hash']}
    assert client.post(url+'/comments',json=value,headers={'Origin':'https://evil.test'}).status_code==403
    wrong=client.post(url+'/comments',json={**value,'expected_content_sha256':'0'*64});assert wrong.status_code==409
    response=client.post(url+'/comments',json=value);assert response.status_code==200,response.text
    replay=client.post(url+'/comments',json=value);assert replay.json()==response.json()
    assert client.post(url+'/comments',json={**value,'text':'different'}).status_code==409
    listing=client.get(url+'/comments');assert listing.status_code==200 and listing.json()['total']==1
    assert listing.json()['items'][0]['text']=='请核对\n收入'
    assert 'no-store' in listing.headers['cache-control']
    monkeypatch.setattr(sharing.identity,'verified_subject',lambda request:'intruder')
    assert client.post(url+'/comments',json=value).status_code==403
    assert client.get(url+'/comments').status_code==403
    monkeypatch.setattr(sharing.identity,'verified_subject',lambda request:'recipient')
    sharing.revoke(pid,share['id'],'owner');assert client.get(url+'/comments').status_code==403
    assert client.post(url+'/comments',json=value).status_code==403


def test_default_disabled_tampering_expiry_and_account_switch(oidc,monkeypatch):
    pid=project();item=spec(pid);client=TestClient(__import__('agent_service.api',fromlist=['app']).app)
    disabled=sharing.create(pid,payload(item),'owner');url='/api/shared-artifacts/'+disabled['id']
    data=client.get(url).json();assert data['allow_comments'] is False
    assert client.get(url+'/comments').status_code==403
    share=sharing.create(pid,payload(item,allowed_subjects=['recipient','other'],allow_comments=True,request_id='other_share_12345678'),'owner');url='/api/shared-artifacts/'+share['id']
    data=client.get(url).json();value={'text':'检查','request_id':'another_request_1234','expected_content_sha256':data['sha256'],'expected_viewer_hash':data['viewer_hash']}
    monkeypatch.setattr(sharing.identity,'verified_subject',lambda request:'other')
    assert client.post(url+'/comments',json=value).status_code==409
    data=client.get(url).json();value['expected_viewer_hash']=data['viewer_hash'];response=client.post(url+'/comments',json=value);assert response.status_code==200,response.text
    comment_id=response.json()['id'];patch(sharing.COMMENT_KIND,comment_id,sealed='bad')
    assert client.get(url+'/comments').status_code==409
    record=get(sharing.KIND,share['id']);path=sharing.DATA/record['relative_path'];original=path.read_bytes()
    try:
        path.write_bytes(b'invalid');assert client.post(url+'/comments',json={**value,'request_id':'third_request_123456'}).status_code==410
    finally:path.write_bytes(original)
    patch(sharing.KIND,share['id'],expires_at='2000-01-01T00:00:00+00:00')
    assert client.get(url+'/comments').status_code==403


def test_concurrent_retry_and_audit_failure_rollback(oidc,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from agent_service import audit
    pid=project();item=spec(pid);share=sharing.create(pid,payload(item,allow_comments=True),'owner');request=SimpleNamespace()
    data=sharing.preview(request,share['id']);value={'text':'并发核对','request_id':'same_comment_request1','expected_content_sha256':data['sha256'],'expected_viewer_hash':data['viewer_hash']}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(sharing.add_comment,request,share['id'],value) for _ in range(2)]
        first,second=[f.result(timeout=10) for f in futures]
    assert first==second and sharing.list_comments(request,share['id'])['total']==1
    def fail(*args,**kwargs):raise RuntimeError('audit unavailable')
    with monkeypatch.context() as scoped:
        scoped.setattr(audit,'digest',fail)
        with pytest.raises(RuntimeError,match='audit unavailable'):
            sharing.add_comment(request,share['id'],{**value,'request_id':'another_comment_1234'})
    assert sharing.list_comments(request,share['id'])['total']==1


def test_publisher_can_review_retained_comments_but_other_actor_cannot(oidc):
    from fastapi import HTTPException
    from types import SimpleNamespace
    pid=project();other=project();item=spec(pid);share=sharing.create(pid,payload(item,allow_comments=True),'owner')
    request=SimpleNamespace();data=sharing.preview(request,share['id'])
    sharing.add_comment(request,share['id'],{'text':'需要修改','request_id':'owner_review_request1','expected_content_sha256':data['sha256'],'expected_viewer_hash':data['viewer_hash']})
    sharing.revoke(pid,share['id'],'owner')
    result=sharing.list_owner_comments(pid,share['id'],'owner');assert result['total']==1 and result['items'][0]['text']=='需要修改'
    with pytest.raises(HTTPException) as error:sharing.list_owner_comments(pid,share['id'],'different')
    assert error.value.status_code==404
    with pytest.raises(HTTPException) as error:sharing.list_owner_comments(other,share['id'],'owner')
    assert error.value.status_code==404


def test_comment_routes_verify_real_signed_tokens(oidc,monkeypatch):
    import jwt
    from datetime import datetime,timedelta,timezone
    from cryptography.hazmat.primitives.asymmetric import rsa
    from agent_service import identity
    pid=project();item=spec(pid);share=sharing.create(pid,payload(item,allow_comments=True),'owner')
    monkeypatch.undo()
    for k,v in {'NEXUS_AUTH_MODE':'oidc','OIDC_ISSUER':'https://id.example.test','OIDC_AUDIENCE':'nexus','OIDC_JWKS_URL':'https://id.example.test/keys','OIDC_ALLOWED_SUBJECTS':'owner'}.items():monkeypatch.setenv(k,v)
    private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()));jwk.update(kid='k',alg='RS256',use='sig',key_ops=['verify'])
    monkeypatch.setattr(identity,'keys',lambda refresh=False:[jwk])
    def token(subject):return jwt.encode({'sub':subject,'iss':'https://id.example.test','aud':'nexus','exp':datetime.now(timezone.utc)+timedelta(minutes=2)},private,algorithm='RS256',headers={'kid':'k'})
    client=TestClient(__import__('agent_service.api',fromlist=['app']).app);url='/api/shared-artifacts/'+share['id'];hdr={'Authorization':'Bearer '+token('recipient')}
    assert client.get(url+'/comments').status_code==401
    assert client.get(url+'/comments',headers={'Authorization':'Bearer '+token('intruder')}).status_code==403
    data=client.get(url,headers=hdr).json();body={'text':'报告校验','request_id':'real_jwt_comment_123','expected_content_sha256':data['sha256'],'expected_viewer_hash':data['viewer_hash']}
    assert client.post(url+'/comments',json=body,headers=hdr).status_code==200
    assert client.get(url+'/comments',headers=hdr).json()['total']==1
