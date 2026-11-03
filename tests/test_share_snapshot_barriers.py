"""Captured share responses serialize with revoke, publish and removal."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agent_service import data_sharing as sharing, project_data as data, workspace_lifecycle
from agent_service.jobs import get_job
from agent_service.store import get, patch
from test_business_reports import real_result

REAL_VERIFIED_SUBJECT=sharing.verified_subject


@pytest.fixture
def shared(monkeypatch):
    for key,value in {'NEXUS_AUTH_MODE':'oidc','OIDC_ISSUER':'https://identity.example.test',
                      'OIDC_AUDIENCE':'nexus','OIDC_JWKS_URL':'https://identity.example.test/jwks',
                      'OIDC_ALLOWED_SUBJECTS':'owner'}.items():monkeypatch.setenv(key,value)
    pid,rid=real_result();run=get_job(rid);data.publish_result(pid,rid,run['commit'])
    record=sharing.create_share(pid,rid,['recipient'],'owner',allow_download=True)
    monkeypatch.setattr(sharing,'verified_subject',lambda request:'recipient')
    yield pid,rid,run['target'],record['id'],SimpleNamespace(headers={})
    from agent_service.store import delete
    delete(sharing.KIND,record['id'])


@pytest.mark.parametrize('change',['revoke','replace','delete'])
def test_page_captures_before_authority_or_version_change(shared,monkeypatch,change):
    pid,rid,wid,sid,request=shared
    entered=Event();release=Event();started=Event();real=data.read_result
    def blocked(*args,**kwargs):
        entered.set();assert release.wait(5)
        return real(*args,**kwargs)
    monkeypatch.setattr(data,'read_result',blocked)
    def change_state():
        started.set()
        if change=='revoke':return sharing.revoke(pid,sid,'owner')
        if change=='replace':return patch('data_workflow',wid,published_run='new-result')
        return workspace_lifecycle.delete_project(pid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading=pool.submit(sharing.read_shared,request,sid)
        assert entered.wait(5)
        updating=pool.submit(change_state)
        try:
            assert started.wait(5)
            with pytest.raises(TimeoutError):updating.result(timeout=.2)
        finally:release.set()
        page=reading.result(timeout=10);updating.result(timeout=10)
    assert page['total']==2
    with pytest.raises(HTTPException) as error:sharing.read_shared(request,sid)
    assert error.value.status_code in (403,410)


def test_revocation_during_csv_creation_blocks_download(shared,monkeypatch):
    pid,rid,wid,sid,request=shared
    entered=Event();release=Event();real=data.result_download
    def blocked(*args,**kwargs):
        path=real(*args,**kwargs)
        entered.set();assert release.wait(5)
        return path
    monkeypatch.setattr(data,'result_download',blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        download=pool.submit(sharing.download_shared,request,sid)
        assert entered.wait(5)
        sharing.revoke(pid,sid,'owner');release.set()
        with pytest.raises(HTTPException) as error:download.result(timeout=10)
    assert error.value.status_code==403


def test_download_snapshot_survives_later_original_change(shared):
    pid,rid,wid,sid,request=shared
    path=data.result_download(pid,rid,'csv')
    expected=path.read_bytes()
    snapshot,size=sharing.download_shared(request,sid)
    try:
        path.write_bytes(b'changed after capture')
        assert size==len(expected) and snapshot.read()==expected
    finally:
        path.write_bytes(expected)
        snapshot.close()


def test_tampered_csv_is_not_delivered(shared,monkeypatch):
    pid,rid,wid,sid,request=shared
    real=data.result_download;original=[]
    def tampered(*args,**kwargs):
        path=real(*args,**kwargs);original.append((path,path.read_bytes()))
        path.write_bytes(b'tampered')
        return path
    monkeypatch.setattr(data,'result_download',tampered)
    try:
        with pytest.raises(ValueError,match='内容与回执不一致'):sharing.download_shared(request,sid)
    finally:
        for path,content in original:path.write_bytes(content)


def test_http_share_download_streams_the_verified_snapshot(shared):
    from fastapi.testclient import TestClient
    from agent_service.api import app
    import csv,io
    pid,rid,wid,sid,request=shared
    response=TestClient(app).get('/api/shared-data/'+sid+'/download')
    assert response.status_code==200,response.text
    assert response.headers['content-type'].startswith('text/csv')
    assert response.headers['x-content-type-options']=='nosniff'
    assert 'no-store' in response.headers['cache-control']
    assert 'shared-data-result.csv' in response.headers['content-disposition']
    assert int(response.headers['content-length'])==len(response.content)
    rows=list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
    assert len(rows)==2 and rows[0]['office']=='East'
    assert 'context.json' not in response.text and 'provenance.jsonl' not in response.text


def test_identity_is_revalidated_before_snapshot_after_lock_wait(shared,monkeypatch):
    pid,rid,wid,sid,request=shared
    seen=iter(['recipient','different'])
    monkeypatch.setattr(sharing,'verified_subject',lambda request:next(seen))
    with pytest.raises(HTTPException) as error:sharing.read_shared(request,sid)
    assert error.value.status_code==401


def test_shared_page_uses_recipient_scope_and_only_exposes_result_fields(shared,monkeypatch):
    from fastapi.testclient import TestClient
    from agent_service import external_alignment
    from agent_service.actor_context import current_actor
    from agent_service.api import app
    pid,rid,wid,sid,request=shared
    actors=[]
    def receipt(pid,rid):
        actors.append(current_actor())
        return {'anchor_source_id':'owner-secret'} if current_actor()=='owner' else None
    monkeypatch.setattr(external_alignment,'receipt_details',receipt)
    response=TestClient(app).get('/api/shared-data/'+sid)
    assert response.status_code==200,response.text
    payload=response.json()
    assert 'no-store' in response.headers['cache-control']
    assert response.headers['x-content-type-options']=='nosniff'
    assert payload['run_id']==rid and len(payload['rows'])==2 and payload['schema']
    assert actors and all(actor.startswith('u_') and actor!='owner' for actor in actors)
    assert 'external_alignment_recorded_details' not in payload
    assert 'external_alignment_recorded' not in payload
    assert 'context' not in payload and 'stages' not in payload and 'workflow_id' not in payload
    assert 'owner-secret' not in response.text


def test_signed_recipient_does_not_read_owner_alignment_receipts(shared,monkeypatch):
    import hashlib,json,jwt
    from datetime import datetime,timedelta,timezone
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastapi.testclient import TestClient
    from agent_service import external_alignment,identity
    from agent_service.actor_context import current_actor
    from agent_service.api import app
    pid,rid,wid,sid,request=shared
    private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid='shared-recipient-key',alg='RS256',use='sig',key_ops=['verify'])
    monkeypatch.setattr(identity,'keys',lambda refresh=False:[jwk])
    monkeypatch.setattr(sharing,'verified_subject',REAL_VERIFIED_SUBJECT)
    owner_actor='owner';issuer='https://identity.example.test';expected='u_'+hashlib.sha256((issuer+'\x00recipient').encode()).hexdigest()
    actors=[]
    def receipt(pid,rid):
        actors.append(current_actor())
        return {'anchor_source_id':'owner-private-id'} if current_actor()==owner_actor else None
    monkeypatch.setattr(external_alignment,'receipt_details',receipt)
    token=jwt.encode({'sub':'recipient','iss':issuer,'aud':'nexus',
                      'exp':datetime.now(timezone.utc)+timedelta(minutes=2)},
                     private,algorithm='RS256',headers={'kid':'shared-recipient-key'})
    response=TestClient(app).get('/api/shared-data/'+sid,headers={'Authorization':'Bearer '+token})
    assert response.status_code==200,response.text
    assert actors==[expected] and 'owner-private-id' not in response.text
    assert set(response.json()) <= {'run_id','plan_sha256','data_as_of','rows','schema','offset','limit',
                                    'total','next_offset','complete_result_available','page_is_complete_result',
                                    'manifest_sha256','decimal_representation'}


def test_shared_csv_retains_final_rows_without_raw_provenance_and_escapes_formulas(monkeypatch):
    import csv,io
    from fastapi.testclient import TestClient
    from agent_service.api import app
    from test_project_data import project,source,document,registered,run
    for key,value in {'NEXUS_AUTH_MODE':'oidc','OIDC_ISSUER':'https://identity.example.test',
                      'OIDC_AUDIENCE':'nexus','OIDC_JWKS_URL':'https://identity.example.test/jwks',
                      'OIDC_ALLOWED_SUBJECTS':'owner'}.items():monkeypatch.setenv(key,value)
    pid=project()
    data_source=source(pid,b'id,office,amount,note\n0001,=1+2,100.000000000001,private-example\n')
    spec=document(data_source['id']);spec.pop('contract')
    wid=registered(pid,spec);job=run(pid,wid);data.publish_result(pid,job['id'],job['commit'])
    record=sharing.create_share(pid,job['id'],['recipient'],'owner',allow_download=True)
    monkeypatch.setattr(sharing,'verified_subject',lambda request:'recipient')
    try:
        response=TestClient(app).get('/api/shared-data/'+record['id']+'/download')
        assert response.status_code==200,response.text
        rows=list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
        assert len(rows)==1 and rows[0]['id']=='0001' and rows[0]['office']=="'=1+2"
        assert rows[0]['amount']=='100.000000000001'
        assert data_source['id'] not in response.text and 'context.json' not in response.text
    finally:
        from agent_service.store import delete
        delete(sharing.KIND,record['id'])
