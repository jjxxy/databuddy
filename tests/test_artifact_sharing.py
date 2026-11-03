import json
import pytest
from fastapi.testclient import TestClient
from agent_service import artifact_sharing as sharing, data_sharing, workspace, workspace_artifacts, office
from agent_service.config import TOKEN
from agent_service.store import put, uid, now
from test_project_data import project


@pytest.fixture
def oidc(monkeypatch):
    for k,v in {'NEXUS_AUTH_MODE':'oidc','OIDC_ISSUER':'https://id.example.test','OIDC_AUDIENCE':'nexus','OIDC_JWKS_URL':'https://id.example.test/keys','OIDC_ALLOWED_SUBJECTS':'owner'}.items():monkeypatch.setenv(k,v)
    monkeypatch.setattr(sharing.identity,'_require_oidc',lambda **kwargs:None)
    monkeypatch.setattr(sharing.identity,'verified_subject',lambda request:'recipient')


def spec(pid):
    turn=put('turn',{'project_id':pid,'text':'生成报告','interaction_mode':'work','state':'agent','created_at':now()},'t_'+uid())
    workspace.write_file(pid,'report-spec.json',json.dumps({'kind':'document','title':'经营报告','sections':[{'body':'收入 123.45 元'}]},ensure_ascii=False))
    output=office.render(turn,'report-spec.json','经营报告')
    found=workspace_artifacts.catalog(pid,kind='office')['items']
    return next(x for x in found if x['format']=='docx' and x['name']=='经营报告.docx')


def payload(item,**kw):return {'artifact_id':item['id'],'expected_artifact_sha256':item['sha256'],'format':'docx',
    'allowed_subjects':['recipient'],'expires_hours':24,'allow_download':True,'confirm_content':True,'request_id':'request_example_1234',**kw}


def test_explicit_share_only_recipient_and_exact_version(oidc,monkeypatch):
    pid=project();item=spec(pid);client=TestClient(__import__('agent_service.api',fromlist=['app']).app)
    headers={'Authorization':'Bearer '+TOKEN}
    # Owner endpoint is still protected. Creating is explicitly confirmed and idempotent.
    assert client.post(f'/api/projects/{pid}/artifact-shares',json=payload(item)).status_code==401
    created=sharing.create(pid,payload(item),'owner');replay=sharing.create(pid,payload(item),'owner');assert replay['id']==created['id']
    with pytest.raises(ValueError,match='不同的分享'):sharing.create(pid,payload(item,expires_hours=48),'owner')
    response=client.get('/api/shared-artifacts/'+created['id']);assert response.status_code==200,response.text
    value=response.json();assert value['render']=='document' and any('123.45' in x.get('text','') for x in value['blocks'])
    assert 'project_id' not in value and 'source_refs' not in value and 'private' not in value
    downloaded=client.get('/api/shared-artifacts/'+created['id']+'/download');assert downloaded.status_code==200
    assert downloaded.headers['cache-control'].endswith('no-store') and downloaded.content[:2]==b'PK'
    monkeypatch.setattr(sharing.identity,'verified_subject',lambda request:'intruder')
    assert client.get('/api/shared-artifacts/'+created['id']).status_code==403
    monkeypatch.setattr(sharing.identity,'verified_subject',lambda request:'recipient')
    sharing.revoke(pid,created['id']);assert client.get('/api/shared-artifacts/'+created['id']).status_code==403


def test_html_safe_preview_and_version_rejection(oidc):
    pid=project();workspace.write_file(pid,'reports/view.html','<h1>报告</h1><script>alert(1)</script>')
    item=next(x for x in workspace_artifacts.catalog(pid,kind='code')['items'] if x['name']=='reports/view.html')
    value=payload(item,format='html',allow_download=False)
    share=sharing.create(pid,value,'owner');client=TestClient(__import__('agent_service.api',fromlist=['app']).app)
    response=client.get('/api/shared-artifacts/'+share['id']);assert response.status_code==200,response.text
    assert response.json()['render']=='html' and '<script>' in response.json()['text']
    assert client.get('/api/shared-artifacts/'+share['id']+'/download').status_code==403
    with pytest.raises(ValueError,match='安全预览'):sharing.create(pid,payload(item,format='html',allow_download=True,request_id='request_example_5678'),'owner')
    with pytest.raises(ValueError,match='版本'):sharing.create(pid,payload(item,format='html',allow_download=False,expected_artifact_sha256='0'*64,request_id='request_example_9999'),'owner')


def test_project_delete_then_restore_does_not_reactivate_old_share(oidc):
    from agent_service import workspace_lifecycle
    from agent_service.store import get
    pid=project();item=spec(pid);created=sharing.create(pid,payload(item),'owner')
    client=TestClient(__import__('agent_service.api',fromlist=['app']).app)
    assert client.get('/api/shared-artifacts/'+created['id']).status_code==200
    result=workspace_lifecycle.delete_project(pid)
    assert created['id'] in result['stopped_work']['revoked_share_ids']
    workspace_lifecycle.restore_project(pid)
    assert get(sharing.KIND,created['id'])['state']=='revoked'
    assert client.get('/api/shared-artifacts/'+created['id']).status_code==403


def test_revocation_serializes_with_read_capture(oidc,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    from types import SimpleNamespace
    from fastapi import HTTPException
    pid=project();item=spec(pid);created=sharing.create(pid,payload(item),'owner')
    entered=Event();release=Event();original=sharing.open_regular_read
    def blocked(path):
        entered.set();assert release.wait(5)
        return original(path)
    monkeypatch.setattr(sharing,'open_regular_read',blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        read=pool.submit(sharing.preview,SimpleNamespace(),created['id'])
        assert entered.wait(5)
        revoke=pool.submit(sharing.revoke,pid,created['id'])
        try:
            with pytest.raises(TimeoutError):revoke.result(timeout=.2)
        finally:release.set()
        assert read.result(timeout=10)['render']=='document'
        assert revoke.result(timeout=10)['state']=='revoked'
    with pytest.raises(HTTPException) as error:sharing.preview(SimpleNamespace(),created['id'])
    assert error.value.status_code==403


def test_snapshot_hash_change_and_identity_switch_are_denied(oidc,monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from agent_service.store import get
    pid=project();item=spec(pid);created=sharing.create(pid,payload(item),'owner')
    record=get(sharing.KIND,created['id']);path=sharing.DATA/record['relative_path'];original=path.read_bytes()
    try:
        path.write_bytes(b'changed')
        with pytest.raises(HTTPException) as error:sharing.preview(SimpleNamespace(),created['id'])
        assert error.value.status_code==410
    finally:path.write_bytes(original)
    subjects=iter(['recipient','other'])
    monkeypatch.setattr(sharing.identity,'verified_subject',lambda request:next(subjects))
    with pytest.raises(HTTPException) as error:sharing.preview(SimpleNamespace(),created['id'])
    assert error.value.status_code==401


def test_signed_recipient_and_project_boundary(oidc,monkeypatch):
    import jwt
    from datetime import datetime,timedelta,timezone
    from cryptography.hazmat.primitives.asymmetric import rsa
    from agent_service import identity
    from agent_service.store import get
    pid=project();other=project();item=spec(pid);created=sharing.create(pid,payload(item),'owner')
    with pytest.raises(KeyError):sharing.revoke(other,created['id'],'owner')
    assert get(sharing.KIND,created['id'])['state']=='active'
    # Exercise the real JWT verifier: a share identifier is not a credential.
    monkeypatch.undo()
    for k,v in {'NEXUS_AUTH_MODE':'oidc','OIDC_ISSUER':'https://id.example.test','OIDC_AUDIENCE':'nexus','OIDC_JWKS_URL':'https://id.example.test/keys','OIDC_ALLOWED_SUBJECTS':'owner'}.items():monkeypatch.setenv(k,v)
    private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()));jwk.update(kid='recipient-key',alg='RS256',use='sig',key_ops=['verify'])
    monkeypatch.setattr(identity,'keys',lambda refresh=False:[jwk])
    def token(subject):return jwt.encode({'sub':subject,'iss':'https://id.example.test','aud':'nexus','exp':datetime.now(timezone.utc)+timedelta(minutes=2)},private,algorithm='RS256',headers={'kid':'recipient-key'})
    client=TestClient(__import__('agent_service.api',fromlist=['app']).app);url='/api/shared-artifacts/'+created['id']
    assert client.get(url).status_code==401
    assert client.get(url,headers={'Authorization':'Bearer '+token('intruder')}).status_code==403
    response=client.get(url,headers={'Authorization':'Bearer '+token('recipient')})
    assert response.status_code==200 and 'no-store' in response.headers['cache-control']
    assert client.get('/api/projects/'+pid+'/artifact-shares',headers={'Authorization':'Bearer '+token('recipient')}).status_code==403


def test_expiry_and_revoke_are_irreversible_and_auditable(oidc):
    from agent_service.store import get,patch
    pid=project();item=spec(pid);created=sharing.create(pid,payload(item),'owner');client=TestClient(__import__('agent_service.api',fromlist=['app']).app)
    patch(sharing.KIND,created['id'],expires_at='2000-01-01T00:00:00+00:00')
    assert client.get('/api/shared-artifacts/'+created['id']).status_code==403
    sharing.revoke(pid,created['id'],'owner');first=get(sharing.KIND,created['id'])
    sharing.revoke(pid,created['id'],'owner');second=get(sharing.KIND,created['id'])
    assert first['state']=='revoked' and first['version']==2 and first['revoked_at'] and first['revoked_by_hash']
    assert second['version']==2 and second['revoked_at']==first['revoked_at']
    assert sharing.create(pid,payload(item),'owner')['state']=='revoked'
    assert client.get('/api/shared-artifacts/'+created['id']).status_code==403
