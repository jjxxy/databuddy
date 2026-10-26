import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from agent_service import data_sharing as sharing, identity, project_data
from agent_service.store import get, patch, put, uid,all_items,delete


@pytest.fixture
def configured(monkeypatch):
    values={'ATLAS_AUTH_MODE':'oidc','OIDC_ISSUER':'https://identity.example.test',
            'OIDC_AUDIENCE':'atlas','OIDC_JWKS_URL':'https://identity.example.test/jwks',
            'OIDC_ALLOWED_SUBJECTS':'workspace-owner'}
    for name,value in values.items():monkeypatch.setenv(name,value)
    pid=put('project',{'name':'结果共享'},'p_'+uid())['id']
    other=put('project',{'name':'其他项目'},'p_'+uid())['id']
    metadata={'plan_sha256':'a'*64,'handoff_sha256':'b'*64}
    def result_folder(project_id,result_id):
        if project_id!=pid or result_id!='verified-result':raise ValueError('结果不属于当前项目')
        return None,dict(metadata)
    monkeypatch.setattr(project_data,'result_folder',result_folder)
    yield pid,other,metadata
    # These unit tests stub a result that has no real run record. Do not leave
    # artificial active shares for later dependency-safe retention tests.
    for record in all_items(sharing.KIND):
        if record.get('project_id') in (pid,other):delete(sharing.KIND,record['id'])


def request(token=''):
    return SimpleNamespace(headers={'Authorization':'Bearer '+token} if token else {})


def create(pid,**kw):
    return sharing.create_share(pid,'verified-result',['recipient-a','recipient-b'],'workspace-owner',**kw)


def test_explicit_subject_authorization_is_one_result_only(configured,monkeypatch):
    pid,other,_=configured;record=create(pid)
    monkeypatch.setattr(sharing,'verified_subject',lambda r:'recipient-a')
    result=sharing.authorize(request(),record['id'])
    assert result['project_id']==pid and result['result_id']=='verified-result'
    assert result['allow_download'] is False
    assert sharing.list_shares(other)==[]
    assert 'allowed_subject_hashes' not in record
    assert 'recipient-a' not in json.dumps(get(sharing.KIND,record['id']))
    assert 'workspace-owner' not in json.dumps(get(sharing.KIND,record['id']))
    monkeypatch.setattr(sharing,'verified_subject',lambda r:'not-allowed')
    with pytest.raises(HTTPException) as error:sharing.authorize(request(),record['id'])
    assert error.value.status_code==403


def test_expiry_revocation_and_snapshot_changes_fail_closed(configured,monkeypatch):
    pid,_,metadata=configured;start=datetime(2026,10,21,tzinfo=timezone.utc)
    monkeypatch.setattr(sharing,'_utcnow',lambda:start)
    monkeypatch.setattr(sharing,'verified_subject',lambda r:'recipient-a')
    record=create(pid,expires_hours=1,allow_download=True)
    assert sharing.authorize(request(),record['id'])['allow_download']
    monkeypatch.setattr(sharing,'_utcnow',lambda:start+timedelta(hours=1))
    with pytest.raises(HTTPException) as error:sharing.authorize(request(),record['id'])
    assert error.value.status_code==403
    monkeypatch.setattr(sharing,'_utcnow',lambda:start)
    revoked=sharing.revoke(pid,record['id'],'workspace-owner')
    assert revoked['version']==2 and revoked['state']=='revoked'
    assert sharing.revoke(pid,record['id'],'another-owner')['version']==2
    with pytest.raises(HTTPException):sharing.authorize(request(),record['id'])
    fresh=create(pid);metadata['handoff_sha256']='c'*64
    with pytest.raises(HTTPException) as error:sharing.authorize(request(),fresh['id'])
    assert error.value.status_code==410


def test_owner_scope_and_input_boundaries(configured):
    pid,other,_=configured
    with pytest.raises(ValueError,match='当前项目'):
        sharing.create_share(other,'verified-result',['recipient-a'],'owner')
    record=create(pid)
    with pytest.raises(ValueError,match='当前项目'):sharing.revoke(other,record['id'],'owner')
    for subjects in ([],['*'],['same','same'],[' '],[' padded ']):
        with pytest.raises(ValueError):sharing.create_share(pid,'verified-result',subjects,'owner')
    for hours in (True,0,721):
        with pytest.raises(ValueError):create(pid,expires_hours=hours)
    with pytest.raises(ValueError):create(pid,allow_download=1)


def test_shared_workspace_token_does_not_enable_scoped_sharing(configured,monkeypatch):
    pid,_,_=configured;record=create(pid)
    monkeypatch.setenv('ATLAS_AUTH_MODE','token')
    monkeypatch.setattr(sharing,'verified_subject',lambda r:'recipient-a')
    with pytest.raises(ValueError,match='OIDC'):create(pid)
    with pytest.raises(HTTPException) as error:sharing.authorize(request('workspace-token'),record['id'])
    assert error.value.status_code==401


def test_changed_identity_configuration_does_not_regrant_existing_share(configured,monkeypatch):
    pid,_,_=configured;record=create(pid)
    monkeypatch.setattr(sharing,'verified_subject',lambda r:'recipient-a')
    monkeypatch.setenv('OIDC_ISSUER','https://other.example.test')
    with pytest.raises(HTTPException) as error:sharing.authorize(request(),record['id'])
    assert error.value.status_code==403


def test_naive_or_malformed_share_expiration_fails_closed(configured,monkeypatch):
    pid,_,_=configured;record=create(pid)
    monkeypatch.setattr(sharing,'verified_subject',lambda r:'recipient-a')
    for expiry in ('2030-01-01T00:00:00','never'):
        patch(sharing.KIND,record['id'],expires_at=expiry)
        with pytest.raises(HTTPException) as error:sharing.authorize(request(),record['id'])
        assert error.value.status_code==403


@pytest.fixture
def signer(configured,monkeypatch):
    private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid='fixed-key',alg='RS256',use='sig',key_ops=['verify'])
    monkeypatch.setattr(identity,'keys',lambda refresh=False:[jwk])
    def sign(**updates):
        now=datetime.now(timezone.utc)
        payload={'sub':'recipient-a','iss':'https://identity.example.test','aud':'atlas',
                 'exp':now+timedelta(minutes=5),'iat':now}
        payload.update(updates)
        return jwt.encode(payload,private,algorithm='RS256',headers={'kid':'fixed-key'})
    return sign,jwk


def test_signed_recipient_can_read_share_but_cannot_become_workspace_owner(configured,signer):
    pid,_,_=configured;sign,_=signer;record=create(pid)
    req=request(sign())
    assert sharing.verified_subject(req)=='recipient-a'
    assert sharing.authorize(req,record['id'])['result_id']=='verified-result'
    with pytest.raises(HTTPException) as error:identity.principal(req)
    assert error.value.status_code==403


@pytest.mark.parametrize('updates',[
    {'iss':'https://wrong.example.test'}, {'aud':'wrong-audience'},
    {'exp':datetime(2000,1,1,tzinfo=timezone.utc)}, {'sub':''},
])
def test_signed_jwt_still_requires_issuer_audience_expiry_and_subject(configured,signer,updates):
    sign,_=signer
    with pytest.raises(HTTPException) as error:sharing.verified_subject(request(sign(**updates)))
    assert error.value.status_code==401


def test_unsigned_proxy_headers_and_ambiguous_jwks_are_not_trusted(configured,signer,monkeypatch):
    sign,jwk=signer
    with pytest.raises(HTTPException):sharing.verified_subject(SimpleNamespace(headers={'X-User':'recipient-a'}))
    monkeypatch.setattr(identity,'keys',lambda refresh=False:[jwk,jwk])
    with pytest.raises(HTTPException):sharing.verified_subject(request(sign()))


def test_unapproved_jwt_algorithm_is_rejected(configured):
    payload={'sub':'recipient-a','iss':'https://identity.example.test','aud':'atlas',
             'exp':datetime.now(timezone.utc)+timedelta(minutes=1)}
    token=jwt.encode(payload,'a'*64,algorithm='HS256',headers={'kid':'fixed-key'})
    with pytest.raises(HTTPException) as error:sharing.verified_subject(request(token))
    assert error.value.status_code==401
