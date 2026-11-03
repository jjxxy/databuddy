"""Instance OIDC catalog: operator publication and project-local copies."""
import hashlib,io,threading
from concurrent.futures import ThreadPoolExecutor
from fastapi import Request
from fastapi.testclient import TestClient
from pptx import Presentation
import pytest
from sqlalchemy import select
from agent_service import organization_templates as org, identity, project_templates
from agent_service.api import app,auth
from agent_service.config import TOKEN
from agent_service.store import get,uid
from test_project_templates import setup_template


@pytest.fixture(autouse=True)
def isolated_jobs_and_catalog(monkeypatch):
    from agent_service.jobs import jobs,engine
    from agent_service.store import items
    with engine.connect() as c: before=set(c.execute(select(jobs.c.id)).scalars())
    monkeypatch.setattr(identity,'mode',lambda:'oidc');monkeypatch.setattr(identity,'validate_config',lambda:None)
    monkeypatch.setattr(identity,'principal',lambda request:{'authenticated':True,'mode':'oidc','subject':'fixture','scope':'owner_workspace'})
    monkeypatch.setenv('NEXUS_REQUIRE_SINGLE_OIDC_SUBJECT','0');monkeypatch.delenv('ATLAS_REQUIRE_SINGLE_OIDC_SUBJECT',raising=False)
    monkeypatch.setenv('OIDC_ISSUER','https://issuer.example.test');monkeypatch.setenv('OIDC_AUDIENCE','finance-service')
    yield
    with engine.begin() as c:
        q=jobs.delete()
        if before:q=q.where(jobs.c.id.not_in(before))
        c.execute(q);c.execute(items.delete().where(items.c.kind==org.KIND))


def fixture():
    client,pid,source,value,shape,raw=setup_template()
    request={'source_project_id':pid,'source_id':source['id'],'revision':value['revision'],
             'sha256':value['sha256'],'title':'组织月报','confirm_full_document':True}
    return client,pid,source,request,shape,raw


def test_catalog_copy_is_pinned_reusable_and_withdrawn_not_recalled():
    client,pid,source,request,shape,raw=fixture();actor='u_'+'1'*64
    pub=org.publish(request,actor);assert pub['sha256']==hashlib.sha256(raw).hexdigest()
    target=client.post('/api/projects',headers={'Authorization':'Bearer '+TOKEN},json={'name':'部门项目','request_id':uid()}).json()['id']
    value={'version':1,'sha256':pub['sha256'],'request_id':'repeatable_copy_123456'}
    copied=org.copy_to_project(target,pub['id'],value,actor)
    assert org.copy_to_project(target,pub['id'],value,actor)==copied
    tid=copied['template']['id'];assert project_templates.inspect(target,tid,actor)['structure']['slides'][0]['placeholders'][0]['text']=='旧标题'
    output=project_templates.render(target,tid,actor,project_templates.Fill.model_validate({'version':1,'name':'结果','replacements':[{'page':1,'shape_id':shape,'text':'正式报告'}]}))
    from agent_service.office import artifact
    file,_=artifact(target,output['artifact_id'])
    assert Presentation(str(file)).slides[0].shapes.title.text=='正式报告'
    assert project_templates.list_templates(target,'u_'+'2'*64)['items']==[]
    assert org.withdraw(pub['id'],1,actor)['existing_copies_preserved']
    with pytest.raises(KeyError):org.copy_to_project(target,pub['id'],value,actor)
    assert project_templates.inspect(target,tid,actor)['template']['id']==tid


def test_requires_real_operator_and_explicit_full_document_authorization(monkeypatch):
    client,pid,source,request,shape,raw=fixture()
    actor={'authenticated':True,'mode':'oidc','issuer':'https://example.test','subject':'business-user'}
    async def fake_auth(req:Request):req.state.identity=actor;yield actor
    monkeypatch.setitem(app.dependency_overrides,auth,fake_auth)
    monkeypatch.setenv('NEXUS_OPERATOR_TOKEN','test-operator-secret-distinct-123')
    path='/api/organization/ppt-templates'
    assert client.post(path,json=request).status_code==403
    headers={'X-Nexus-Operator-Token':'test-operator-secret-distinct-123'}
    assert client.post(path,json={**request,'confirm_full_document':False},headers=headers).status_code==400
    response=client.post(path,json=request,headers=headers);assert response.status_code==200,response.text
    assert len(client.get(path).json()['items'])==1
    assert client.post(path+'/'+response.json()['id']+'/withdraw',json={'version':1}).status_code==403


def test_unsupported_deployments_and_tampered_snapshot_fail_closed(monkeypatch):
    client,pid,source,request,shape,raw=fixture();actor='u_'+'3'*64
    monkeypatch.setattr(identity,'mode',lambda:'token')
    with pytest.raises(ValueError,match='OIDC'):org.publish(request,actor)
    monkeypatch.setattr(identity,'mode',lambda:'oidc');monkeypatch.setenv('NEXUS_REQUIRE_SINGLE_OIDC_SUBJECT','1')
    with pytest.raises(ValueError,match='独立个人'):org.publish(request,actor)
    monkeypatch.setenv('NEXUS_REQUIRE_SINGLE_OIDC_SUBJECT','0');entry=org.publish(request,actor)
    path=org._path(entry['sha256']);path.write_bytes(b'changed')
    value={'version':1,'sha256':entry['sha256'],'request_id':'copy_tamper_test_1234'}
    with pytest.raises(ValueError,match='完整性'):org.copy_to_project(pid,entry['id'],value,actor)
    path.write_bytes(raw)


def test_concurrent_withdraw_waits_for_copy_capture(monkeypatch):
    client,pid,source,request,shape,raw=fixture();actor='u_'+'4'*64;entry=org.publish(request,actor)
    entered=threading.Event();release=threading.Event();withdrawn=threading.Event();original=org._read
    def slow(digest):
        result=original(digest);entered.set();assert release.wait(5);return result
    monkeypatch.setattr(org,'_read',slow)
    value={'version':1,'sha256':entry['sha256'],'request_id':'concurrent_copy_12345'}
    def withdraw():org.withdraw(entry['id'],1,actor);withdrawn.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        copy=pool.submit(org.copy_to_project,pid,entry['id'],value,actor);assert entered.wait(5)
        removed=pool.submit(withdraw);assert not withdrawn.wait(.1);release.set()
        assert copy.result(timeout=10)['template']['source_sha256']==entry['sha256'];removed.result(timeout=10)
    with pytest.raises(KeyError):org.copy_to_project(pid,entry['id'],value,actor)


def test_two_verified_accounts_copy_separately_and_no_remote_source_leaks():
    client,pid,source,request,shape,raw=fixture();a='u_'+'5'*64;b='u_'+'6'*64
    entry=org.publish(request,a);public=org.list_catalog()['items'][0]
    assert 'source_project_id' not in public and 'source_id' not in public
    value={'version':entry['version'],'sha256':entry['sha256'],'request_id':'same_request_id_987654'}
    first=org.copy_to_project(pid,entry['id'],value,a);second=org.copy_to_project(pid,entry['id'],value,b)
    assert first['source_id']!=second['source_id'] and first['template']['id']!=second['template']['id']
    assert project_templates.inspect(pid,first['template']['id'],a)['template']['id']==first['template']['id']
    with pytest.raises(KeyError):project_templates.inspect(pid,first['template']['id'],b)


def test_publication_and_audit_are_atomic(monkeypatch):
    client,pid,source,request,shape,raw=fixture();actor='u_'+'7'*64
    original=org._append_audit
    def failed(*args,**kwargs):raise ValueError('审计服务故障')
    monkeypatch.setattr(org,'_append_audit',failed)
    with pytest.raises(ValueError,match='审计服务'):org.publish(request,actor)
    assert org.list_catalog()['items']==[]
    monkeypatch.setattr(org,'_append_audit',original)
    assert org.publish(request,actor)['state']=='active'


def test_published_snapshot_survives_source_project_trash_until_withdrawn():
    from agent_service.workspace_lifecycle import delete_project
    client,pid,source,request,shape,raw=fixture();actor='u_'+'8'*64
    entry=org.publish(request,actor)
    target=client.post('/api/projects',headers={'Authorization':'Bearer '+TOKEN},json={'name':'接收项目','request_id':uid()}).json()['id']
    delete_project(pid)
    assert org.list_catalog()['items'][0]['id']==entry['id']
    value={'version':entry['version'],'sha256':entry['sha256'],'request_id':'after_source_delete_123'}
    copied=org.copy_to_project(target,entry['id'],value,actor)
    assert copied['template']['source_sha256']==entry['sha256']
    org.withdraw(entry['id'],entry['version'],actor)
    assert org.list_catalog()['items']==[]
    with pytest.raises(KeyError):org.copy_to_project(target,entry['id'],value,actor)


def test_catalog_is_pinned_to_identity_issuer_and_audience(monkeypatch):
    from agent_service.store import put
    client,pid,source,request,shape,raw=fixture();actor='u_'+'9'*64
    original=org.publish(request,actor)
    value={'version':1,'sha256':original['sha256'],'request_id':'scope_copy_request_123456'}
    for env,new in [('OIDC_ISSUER','https://other.example.test'),('OIDC_AUDIENCE','other-service')]:
        with monkeypatch.context() as changed:
            changed.setenv(env,new)
            assert org.list_catalog()['items']==[]
            with pytest.raises(KeyError):org.copy_to_project(pid,original['id'],value,actor)
            with pytest.raises(KeyError):org.withdraw(original['id'],1,actor)
            # Same snapshot can be deliberately published as a distinct record.
            other=org.publish(request,actor)
            assert other['id']!=original['id']
            assert [e['id'] for e in org.list_catalog()['items']]==[other['id']]
    assert [e['id'] for e in org.list_catalog()['items']]==[original['id']]
    # Legacy records without a scope cannot accidentally become public.
    legacy={**get(org.KIND,original['id']),'id':'ot_'+'a'*40}
    legacy.pop('identity_scope')
    put(org.KIND,legacy,legacy['id'])
    assert [e['id'] for e in org.list_catalog()['items']]==[original['id']]
    with pytest.raises(KeyError):org.copy_to_project(pid,legacy['id'],value,actor)
    with pytest.raises(KeyError):org.withdraw(legacy['id'],1,actor)
