"""Version-bound personal PPT template management and manual generation."""
import hashlib
import io

from fastapi import Request
from fastapi.testclient import TestClient
from pptx import Presentation
import pytest

from agent_service.api import app
from agent_service.config import TOKEN
from agent_service import project_templates as templates
from agent_service.store import get, patch, uid

H={'Authorization':'Bearer '+TOKEN}


def setup_template():
    client=TestClient(app)
    p=client.post('/api/projects',headers=H,json={'name':'模板目录','request_id':uid()})
    assert p.status_code==200,p.text
    pid=p.json()['id'];root=f'/api/projects/{pid}'
    deck=Presentation();slide=deck.slides.add_slide(deck.slide_layouts[0]);slide.shapes.title.text='旧标题'
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
