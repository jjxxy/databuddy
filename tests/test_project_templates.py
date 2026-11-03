"""Version-bound personal PPT template management and manual generation."""
import hashlib
import io

from fastapi import Request
from fastapi.testclient import TestClient
from pptx import Presentation
import pytest
from sqlalchemy import select

from agent_service.api import app
from agent_service.config import TOKEN
from agent_service import project_templates as templates
from agent_service.store import get, patch, uid

H={'Authorization':'Bearer '+TOKEN}


@pytest.fixture(autouse=True)
def cleanup_test_jobs():
    """Template uploads enqueue probes; do not run them in later tests."""
    from agent_service.jobs import jobs,engine
    with engine.connect() as connection:before=set(connection.execute(select(jobs.c.id)).scalars())
    yield
    with engine.begin() as connection:
        query=jobs.delete()
        if before:query=query.where(jobs.c.id.not_in(before))
        connection.execute(query)


def setup_template():
    client=TestClient(app)
    p=client.post('/api/projects',headers=H,json={'name':'模板目录','request_id':uid()})
    assert p.status_code==200,p.text
    pid=p.json()['id'];root=f'/api/projects/{pid}'
    deck=Presentation();slide=deck.slides.add_slide(deck.slide_layouts[0]);slide.shapes.title.text='旧标题'
    slide.placeholders[1].text='原模板保留标识'
    data=io.BytesIO();deck.save(data)
    response=client.post(root+'/attachments',headers=H,files={'file':('template.pptx',data.getvalue(),
        'application/vnd.openxmlformats-officedocument.presentationml.presentation')},data={'request_id':uid(),'local_processing':'true'})
    assert response.status_code==200,response.text
    source=get('source',response.json()['id'])
    value={'source_id':source['id'],'revision':source.get('revision',1),'sha256':source['content_hash'],'title':'月度汇报'}
    return client,pid,source,value,slide.shapes.title.shape_id,data.getvalue()


def test_template_personal_registry_to_real_download_and_soft_remove():
    client,pid,source,value,shape,raw=setup_template();root=f'/api/projects/{pid}'
    assert any(s['id']==source['id'] for s in client.get(root+'/personal-ppt-template-sources',headers=H).json()['items'])
    saved=client.post(root+'/personal-ppt-templates',headers=H,json=value)
    assert saved.status_code==200,saved.text
    entry=saved.json();tid=entry['id'];assert entry['version']==1
    assert client.post(root+'/personal-ppt-templates',headers=H,json=value).json()==entry
    info=client.get(root+'/personal-ppt-templates/'+tid,headers=H)
    assert info.status_code==200,info.text
    assert info.json()['structure']['slides'][0]['placeholders'][0]['text']=='旧标题'
    response=client.post(root+'/personal-ppt-templates/'+tid+'/render',headers=H,json={'version':1,'name':'月度汇报',
        'replacements':[{'page':1,'shape_id':shape,'text':'收入 118.15 元'}]})
    assert response.status_code==200,response.text
    artifact=response.json();assert artifact['visual_validation'] is False and artifact['template_sha256']==value['sha256']
    assert artifact['template_freshness']=='version_bound_not_guaranteed_current'
    result=client.get(artifact['download']['url'],headers=H)
    assert result.status_code==200 and Presentation(io.BytesIO(result.content)).slides[0].shapes.title.text=='收入 118.15 元'
    renamed=client.put(root+'/personal-ppt-templates/'+tid,headers=H,json={'version':1,'title':'新名称'})
    assert renamed.status_code==200 and renamed.json()['version']==2
    assert client.put(root+'/personal-ppt-templates/'+tid,headers=H,json={'version':1,'title':'过期名称'}).status_code==400
    assert client.post(root+'/personal-ppt-templates/'+tid+'/render',headers=H,json={'version':1,'name':'报告',
        'replacements':[{'page':1,'shape_id':shape,'text':'旧的提交'}]}).status_code==400
    assert client.get(root+'/personal-ppt-templates/'+tid).status_code==401
    assert client.post(root+'/personal-ppt-templates/'+tid+'/remove',headers=H,json={'version':1}).status_code==400
    removed=client.post(root+'/personal-ppt-templates/'+tid+'/remove',headers=H,json={'version':2})
    assert removed.status_code==200 and removed.json()['source_preserved'] is True
    assert client.get(root+'/personal-ppt-templates/'+tid,headers=H).status_code==404
    assert get('source',source['id'])['content_hash']==hashlib.sha256(raw).hexdigest()
    # Re-registering is explicit and bumps the optimistic version.
    again=client.post(root+'/personal-ppt-templates',headers=H,json=value)
    assert again.status_code==200 and again.json()['version']==4


def test_template_actor_project_and_stale_version_are_rejected():
    client,pid,source,value,shape,_=setup_template()
    entry=templates.register(pid,'owner',templates.Register.model_validate(value));tid=entry['id']
    other='u_'+'a'*64
    assert templates.list_templates(pid,other)['items']==[]
    with pytest.raises(KeyError):templates.inspect(pid,tid,other)
    with pytest.raises(KeyError):templates.remove(pid,tid,other,1)
    other_pid=client.post('/api/projects',headers=H,json={'name':'其他项目','request_id':uid()}).json()['id']
    with pytest.raises(KeyError):templates.inspect(other_pid,tid,'owner')
    with pytest.raises(ValueError):templates.register(other_pid,'owner',templates.Register.model_validate(value))
    patch('source',source['id'],revision=2)
    assert templates.list_templates(pid,'owner')['items'][0]['available'] is False
    with pytest.raises(ValueError,match='版本'):templates.inspect(pid,tid,'owner')
    with pytest.raises(ValueError,match='版本'):templates.render(pid,tid,'owner',templates.Fill.model_validate({
        'version':1,'name':'报告','replacements':[{'page':1,'shape_id':shape,'text':'新内容'}]}))


def test_routes_derive_actor_from_authenticated_identity(monkeypatch):
    from agent_service.api import auth
    client,pid,source,value,shape,_=setup_template();root=f'/api/projects/{pid}'
    identity={'authenticated':True,'mode':'oidc','issuer':'https://issuer.example','subject':'alice'}
    async def signed_in(request:Request):
        request.state.identity=dict(identity)
        yield request.state.identity
    monkeypatch.setitem(app.dependency_overrides,auth,signed_in)
    entry=client.post(root+'/personal-ppt-templates',json=value)
    assert entry.status_code==200,entry.text
    tid=entry.json()['id'];assert len(client.get(root+'/personal-ppt-templates').json()['items'])==1
    identity['subject']='bob'
    assert client.get(root+'/personal-ppt-templates').json()['items']==[]
    assert client.get(root+'/personal-ppt-templates/'+tid).status_code==404
    assert client.put(root+'/personal-ppt-templates/'+tid,json={'version':1,'title':'偷改'}).status_code==404
    result=client.post(root+'/personal-ppt-templates/'+tid+'/render',json={'version':1,'name':'报告',
        'replacements':[{'page':1,'shape_id':shape,'text':'不应生成'}]})
    assert result.status_code==404
    assert client.post(root+'/personal-ppt-templates/'+tid+'/remove',json={'version':1}).status_code==404


def test_remove_between_precheck_and_render_is_revalidated(monkeypatch):
    _,pid,_,value,shape,_=setup_template()
    entry=templates.register(pid,'owner',templates.Register.model_validate(value));tid=entry['id']
    def interrupted(*args,before_render,**kwargs):
        templates.remove(pid,tid,'owner',entry['version'])
        before_render()
        pytest.fail('removed template must not be rendered')
    monkeypatch.setattr(templates.ppt_template,'render_for_project',interrupted)
    with pytest.raises(KeyError):templates.render(pid,tid,'owner',templates.Fill.model_validate({
        'version':1,'name':'报告','replacements':[{'page':1,'shape_id':shape,'text':'不应生成'}]}))


def test_byte_tampering_is_detected_before_reuse():
    from agent_service import project_data
    _,pid,source,value,shape,_=setup_template()
    entry=templates.register(pid,'owner',templates.Register.model_validate(value))
    path=project_data.source_path(source);path.write_bytes(b'corrupted template')
    with pytest.raises(ValueError):templates.inspect(pid,entry['id'],'owner')
    with pytest.raises(ValueError):templates.render(pid,entry['id'],'owner',templates.Fill.model_validate({
        'version':1,'name':'报告','replacements':[{'page':1,'shape_id':shape,'text':'不应生成'}]}))


def test_project_delete_waits_until_registration_finishes(monkeypatch):
    """The actual lifecycle operation cannot race past the registration lock."""
    from concurrent.futures import ThreadPoolExecutor,TimeoutError
    from threading import Event
    from agent_service import workspace_lifecycle
    _,pid,_,value,_,_=setup_template()
    entered=Event();release=Event();original=templates.ppt_template._template
    def paused(*args,**kwargs):
        entered.set();assert release.wait(5)
        return original(*args,**kwargs)
    monkeypatch.setattr(templates.ppt_template,'_template',paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        registration=pool.submit(templates.register,pid,'owner',templates.Register.model_validate(value))
        assert entered.wait(5)
        deletion=pool.submit(workspace_lifecycle.delete_project,pid)
        try:
            with pytest.raises(TimeoutError):deletion.result(timeout=.2)
        finally:release.set()
        assert registration.result(timeout=5)['title']=='月度汇报'
        assert deletion.result(timeout=5)['deleted_at']
    with pytest.raises(workspace_lifecycle.WorkspaceDeleted):templates.list_templates(pid,'owner')


def test_concurrent_source_replacement_cannot_change_frozen_template_bytes(monkeypatch):
    """A writer can replace source after final check: result must retain its pinned bytes.

    Source replacement does not share the template publication lock, so this
    test intentionally makes no claim that the pinned revision is still current.
    """
    from agent_service import result_delivery,office,workspace_artifacts
    from agent_service.execution import process_once
    from agent_service.jobs import list_jobs,ACTIVE
    client,pid,source,value,shape,raw=setup_template();root=f'/api/projects/{pid}'
    while any(r['status'] in ACTIVE for r in list_jobs(target=source['id'])):
        assert process_once()
    saved=client.post(root+'/personal-ppt-templates',headers=H,json=value)
    assert saved.status_code==200,saved.text
    new=Presentation();slide=new.slides.add_slide(new.slide_layouts[0]);slide.shapes.title.text='不同模板'
    slide.placeholders[1].text='新模板标识';buf=io.BytesIO();new.save(buf);new_bytes=buf.getvalue()
    original=result_delivery.atomic_replace;changed=[]
    def race(src,dst):
        if str(dst).endswith('并发汇报.pptx'):
            response=client.post('/api/sources/'+source['id']+'/replace-file',headers=H,
                files={'file':('new.pptx',new_bytes,'application/vnd.openxmlformats-officedocument.presentationml.presentation')})
            assert response.status_code==200,response.text
            changed.append(response.json())
        return original(src,dst)
    monkeypatch.setattr(result_delivery,'atomic_replace',race)
    rendered=client.post(root+'/personal-ppt-templates/'+saved.json()['id']+'/render',headers=H,json={
        'version':1,'name':'并发汇报','replacements':[{'page':1,'shape_id':shape,'text':'本次填写'}]})
    assert rendered.status_code==200,rendered.text
    result=rendered.json();assert changed and changed[0]['revision']==value['revision']+1
    assert changed[0]['content_hash']==hashlib.sha256(new_bytes).hexdigest()
    assert result['template_sha256']==hashlib.sha256(raw).hexdigest()
    assert result['template_freshness']=='version_bound_not_guaranteed_current'
    file,receipt=office.artifact(pid,result['artifact_id']);deck=Presentation(file)
    assert deck.slides[0].shapes.title.text=='本次填写'
    assert deck.slides[0].placeholders[1].text=='原模板保留标识'
    assert receipt['template_sha256']==value['sha256'] and receipt['template_revision']==value['revision']
    catalog=workspace_artifacts.catalog(pid,'office')['items']
    item=next(x for x in catalog if x['name']=='并发汇报.pptx')
    assert item['source_refs']==[{'source_id':source['id'],'revision':value['revision'],
                                  'content_sha256':value['sha256'],'role':'template_snapshot'}]
    assert '可能已更新' in item['semantic_warning'] and not item['financial_verified']
    # If access to the source record no longer belongs to this project, its
    # identifier and hash must not be exposed through an older artifact.
    patch('source',source['id'],project_id='p_foreign')
    item=next(x for x in workspace_artifacts.catalog(pid,'office')['items'] if x['name']=='并发汇报.pptx')
    assert item['source_refs']==[] and '不可用或不完整' in item['semantic_warning']


def test_legacy_office_artifact_without_template_metadata_still_lists():
    from agent_service import workspace_artifacts
    from agent_service.store import put
    _,pid,_,_,_,_=setup_template()
    put('office_artifact',{'project_id':pid,'name':'legacy.pptx','sha256':'c'*64,'state':'committed'},'of_'+uid())
    item=next(x for x in workspace_artifacts.catalog(pid,'office')['items'] if x['name']=='legacy.pptx')
    assert item['source_refs']==[] and 'semantic_warning' not in item
