import hashlib
import io
from pptx import Presentation
import pytest
from agent_service import ppt_template


def test_template_preserves_original_and_requires_version_and_placeholder(tmp_path,monkeypatch):
    path=tmp_path/'template.pptx';deck=Presentation();slide=deck.slides.add_slide(deck.slide_layouts[0]);slide.shapes.title.text='原模板标题';deck.save(path)
    raw=path.read_bytes();sha=hashlib.sha256(raw).hexdigest();source={'id':'s1','project_id':'p1','name':'template.pptx','kind':'file','revision':3,'content_hash':sha}
    def owned(pid,kind,sid):
        if pid!='p1' or sid!='s1':raise ValueError('来源不属于项目')
        return source
    monkeypatch.setattr(ppt_template.project_data,'owned',owned);monkeypatch.setattr(ppt_template.project_data,'source_path',lambda _:path)
    items=ppt_template.inspect('p1','s1',3,sha)['slides'][0]['placeholders'];target=next(item for item in items if item['text']=='原模板标题')
    result=ppt_template.render_bytes('p1','s1',3,sha,[{'page':1,'shape_id':target['shape_id'],'text':'净收入 118.15 元'}]);changed=Presentation(io.BytesIO(result))
    assert changed.slides[0].shapes.title.text=='净收入 118.15 元'
    assert len(changed.slide_masters)==len(deck.slide_masters) and len(changed.slide_layouts)==len(deck.slide_layouts)
    assert path.read_bytes()==raw
    with pytest.raises(ValueError):ppt_template.inspect('other','s1',3,sha)
    with pytest.raises(ValueError,match='版本'):ppt_template.inspect('p1','s1',2,sha)
    with pytest.raises(ValueError,match='摘要'):ppt_template.inspect('p1','s1',3,'0'*64)
    with pytest.raises(ValueError,match='占位符'):ppt_template.render_bytes('p1','s1',3,sha,[{'page':1,'shape_id':999,'text':'x'}])


def test_registered_template_artifact_can_be_downloaded(tmp_path,monkeypatch):
    from sqlalchemy import create_engine
    from agent_service import store,office,workspace
    monkeypatch.delenv('NEXUS_LOCAL_WORKSPACE',raising=False)
    engine=create_engine('sqlite:///'+str(tmp_path/'db.sqlite'));store.metadata.create_all(engine)
    for module in (store,office,workspace):monkeypatch.setattr(module,'DATA',tmp_path)
    monkeypatch.setattr(store,'engine',engine)
    pid='p_'+store.uid();store.put('project',{'name':'模板项目'},pid)
    turn=store.put('turn',{'project_id':pid,'text':'请按模板生成PPT汇报','interaction_mode':'work','state':'agent','created_at':store.now()},'t_'+store.uid())
    template=tmp_path/'source.pptx';deck=Presentation();slide=deck.slides.add_slide(deck.slide_layouts[0]);slide.shapes.title.text='标题';deck.save(template)
    raw=template.read_bytes();sha=hashlib.sha256(raw).hexdigest();source={'id':'s1','project_id':pid,'name':'source.pptx','kind':'file','revision':1,'content_hash':sha}
    monkeypatch.setattr(ppt_template.project_data,'owned',lambda p,k,s:source if p==pid and s=='s1' else (_ for _ in ()).throw(ValueError('来源不属于项目')))
    monkeypatch.setattr(ppt_template.project_data,'source_path',lambda _:template)
    result=ppt_template.render(turn,'s1',1,sha,'经营汇报',[{'page':1,'shape_id':slide.shapes.title.shape_id,'text':'经营收入 118.15 元'}])
    path,record=office.artifact(pid,result['artifact_id'])
    assert Presentation(path).slides[0].shapes.title.text=='经营收入 118.15 元'
    assert record['template_sha256']==sha and result['visual_validation'] is False
    assert template.read_bytes()==raw
    with pytest.raises(ValueError):office.artifact('other',result['artifact_id'])
    engine.dispose()


def test_template_fills_explicit_text_boxes_and_preserves_other_shapes(tmp_path,monkeypatch):
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches,Pt
    path=tmp_path/'text-template.pptx';deck=Presentation();slide=deck.slides.add_slide(deck.slide_layouts[6])
    box=slide.shapes.add_textbox(Inches(1),Inches(1),Inches(6),Inches(1))
    run=box.text_frame.paragraphs[0].add_run();run.text='示例标题';run.font.size=Pt(24);run.font.bold=True
    label=slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,Inches(1),Inches(3),Inches(4),Inches(1));label.text='原有部门'
    untouched=slide.shapes.add_textbox(Inches(1),Inches(5),Inches(5),Inches(1));untouched.text='不修改的说明'
    deck.save(path);raw=path.read_bytes();sha=hashlib.sha256(raw).hexdigest()
    source={'id':'s1','project_id':'p1','name':'template.pptx','kind':'file','revision':1,'content_hash':sha}
    monkeypatch.setattr(ppt_template.project_data,'owned',lambda p,k,s:source if p=='p1' and s=='s1' else (_ for _ in ()).throw(ValueError('来源不属于项目')))
    monkeypatch.setattr(ppt_template.project_data,'source_path',lambda _:path)
    items=ppt_template.inspect('p1','s1',1,sha)['slides'][0]['text_shapes'];assert {x['shape_id'] for x in items}=={box.shape_id,label.shape_id,untouched.shape_id}
    data=ppt_template.render_bytes('p1','s1',1,sha,[{'page':1,'shape_id':box.shape_id,'text':'收入 118.15 元'},{'page':1,'shape_id':label.shape_id,'text':'华东'}])
    changed=Presentation(io.BytesIO(data));shapes={s.shape_id:s for s in changed.slides[0].shapes}
    assert shapes[box.shape_id].text=='收入 118.15 元' and shapes[label.shape_id].text=='华东'
    assert shapes[untouched.shape_id].text=='不修改的说明' and path.read_bytes()==raw
    style=shapes[box.shape_id].text_frame.paragraphs[0].runs[0].font;assert style.bold and style.size==Pt(24)


def test_template_tools_are_unavailable_to_scoped_delegation():
    from agent_service.agent_context import allowed
    tools={'inspect_pptx_template','render_pptx_template'}
    assert tools.isdisjoint(allowed({'interaction_mode':'work','gateway_client_id':'external'}))
    assert tools.isdisjoint(allowed({'interaction_mode':'chat','collaboration_parent_turn_id':'parent'}))


def test_uploaded_template_to_downloaded_artifact_end_to_end(tmp_path):
    from fastapi.testclient import TestClient
    from agent_service.api import app
    from agent_service.config import TOKEN
    from agent_service.store import get,uid
    from agent_service.execution import process_once
    client=TestClient(app);h={'Authorization':'Bearer '+TOKEN}
    def req(method,path,**kwargs):
        result=client.request(method,path,headers=h,**kwargs);assert result.status_code==200,result.text;return result.json()
    pid=req('POST','/api/projects',json={'name':'模板验收','request_id':uid()})['id'];root='/api/projects/'+pid
    deck=Presentation();slide=deck.slides.add_slide(deck.slide_layouts[0]);slide.shapes.title.text='模板标题';buf=io.BytesIO();deck.save(buf);raw=buf.getvalue()
    sid=req('POST',root+'/attachments',files={'file':('template.pptx',raw,'application/vnd.openxmlformats-officedocument.presentationml.presentation')},data={'request_id':uid(),'local_processing':'true'})['id']
    for _ in range(100):
        source=get('source',sid)
        if source.get('status') in ('ready','failed'):break
        assert process_once()
    ref={'source_id':sid,'revision':source['revision'],'expected_sha256':source['content_hash']}
    found=ppt_template.inspect(pid,**ref)['slides'][0]['placeholders'];target=next(x for x in found if x['text']=='模板标题')
    turn=req('POST',root+'/messages',json={'text':'按模板生成PPT汇报','request_id':uid(),'source_id':sid})
    result=ppt_template.render(get('turn',turn['id']),**ref,name='月结汇报',replacements=[{'page':1,'shape_id':target['shape_id'],'text':'收入 118.15 元'}])
    response=client.get(result['download']['url'],headers=h);assert response.status_code==200
    assert Presentation(io.BytesIO(response.content)).slides[0].shapes.title.text=='收入 118.15 元'
    assert ppt_template._template(pid,**ref)==raw
