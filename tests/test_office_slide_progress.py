"""Progress is derived from constructed slides, never from a model claim."""
import json
import pytest
from agent_service import office, office_slide_progress, store, workspace
from agent_service.actor_context import actor_scope
from test_office_integrity_regressions import isolated_office


def start(turn, step):
    return store.patch('turn', turn['id'], state='agent_tool', actor_id='owner', agent_trace=[{
        'id': step, 'tool': 'render_office_artifact', 'status': 'started'}])


def spec():
    return {'kind': 'presentation', 'title': '季度经营', 'sections': [
        {'heading':'收入', 'body':'核对期间', 'table':{'columns':['地区','金额'], 'rows':[['华东','123.45']]},
         'chart':{'title':'收入','unit':'元','points':[{'label':'华东','value':'123.45'}]}}]}


def test_real_page_events_are_visible_before_publication_and_cache_does_not_replay(isolated_office, monkeypatch):
    turn,_ = isolated_office; turn = start(turn, 'step-one')
    workspace.write_file(turn['project_id'], 'report.json', json.dumps(spec(),ensure_ascii=False))
    events=[]; original=office_slide_progress.SlideProgress.page
    def page(self, number, total, slide):
        original(self,number,total,slide)
        current=store.get('turn',turn['id'])['office_slide_progress']
        events.append(current)
        assert current['phase']=='building'
        assert not store.all_items('office_artifact')
    monkeypatch.setattr(office_slide_progress.SlideProgress,'page',page)
    result=office.render(turn,'report.json','报告')
    assert [len(e['pages']) for e in events]==list(range(1,len(events)+1))
    assert len(events)==4 and all(e['total']==4 for e in events)
    final=store.get('turn',turn['id'])['office_slide_progress']
    assert final['phase']=='verified' and result['structural_validation'] is True
    content=' '.join(line for page in final['pages'] for line in page['lines'])
    assert '华东 | 123.45' in content and '123.45' in content
    assert final['pages'][0]['title']=='季度经营'
    from agent_service.conversation import public_turn
    with actor_scope('owner'):assert public_turn(store.get('turn',turn['id']))['office_slide_progress']==final
    with actor_scope('u_'+'f'*64):assert 'office_slide_progress' not in public_turn(store.get('turn',turn['id']))
    events.clear(); turn=start(turn,'step-two')
    again=office.render(turn,'report.json','报告')
    assert again['sha256']==result['sha256'] and not events
    assert store.get('turn',turn['id'])['office_slide_progress'] is None


def test_validation_failure_never_marks_preview_verified(isolated_office,monkeypatch):
    turn,_=isolated_office;turn=start(turn,'step-failure')
    workspace.write_file(turn['project_id'],'report.json',json.dumps(spec(),ensure_ascii=False))
    monkeypatch.setattr(office,'_verify_structure',lambda *args,**kwargs: (_ for _ in ()).throw(ValueError('bad structure')))
    with pytest.raises(ValueError,match='bad structure'):office.render(turn,'report.json','错误报告')
    progress=store.get('turn',turn['id'])['office_slide_progress']
    assert progress['phase']=='interrupted' and len(progress['pages'])==progress['total']
    assert not store.all_items('office_artifact')


def test_cancelled_turn_is_not_resurrected_or_given_more_pages(isolated_office,monkeypatch):
    turn,_=isolated_office;turn=start(turn,'step-cancel')
    workspace.write_file(turn['project_id'],'report.json',json.dumps(spec(),ensure_ascii=False))
    original=office_slide_progress.SlideProgress.page
    def page(self, number,total,slide):
        original(self,number,total,slide)
        if number==1:store.patch('turn',turn['id'],state='done',outcome='cancelled')
    monkeypatch.setattr(office_slide_progress.SlideProgress,'page',page)
    with pytest.raises(ValueError,match='已停止'):office.render(turn,'report.json','取消报告')
    current=store.get('turn',turn['id'])
    assert current['outcome']=='cancelled' and current['state']=='done'
    assert len(current['office_slide_progress']['pages'])==1
    assert not store.all_items('office_artifact')


def test_preview_omits_whole_long_values_and_discloses_it():
    from pptx import Presentation
    deck=Presentation();slide=deck.slides.add_slide(deck.slide_layouts[5]);slide.shapes.title.text='标题'
    from pptx.util import Inches
    shape=slide.shapes.add_textbox(Inches(1),Inches(1),Inches(2),Inches(1));shape.name='nexus:text:0';shape.text='9'*2100
    preview=office_slide_progress._slide_preview(1,slide)
    assert preview['lines']==[] and preview['omitted'] is True


@pytest.mark.parametrize('mutation', [
    {'state':'done','outcome':'cancelled'},
    {'agent_trace':[{'id':'new-step','tool':'render_office_artifact','status':'started'}]},
    {'session_id':'other-session'},
    {'actor_id':'u_'+'f'*64},
])
def test_concurrent_turn_change_cannot_write_stale_preview(isolated_office, mutation):
    import threading
    from sqlalchemy import update
    from pptx import Presentation
    turn,_=isolated_office;turn=start(turn,'step-concurrent')
    recorder=office_slide_progress.for_turn(turn);recorder.begin()
    slide=Presentation().slides.add_slide(Presentation().slide_layouts[5])
    conn=store.engine.connect();conn.exec_driver_sql('BEGIN IMMEDIATE')
    current=store.get('turn',turn['id']);current.update(mutation)
    conn.execute(update(store.items).where(store.items.c.id==turn['id']).values(
        body=store._encode('turn',current,conn)))
    entered=threading.Event();result=[]
    def attempt():
        entered.set()
        try:recorder.page(1,1,slide)
        except ValueError as error:result.append(str(error))
    thread=threading.Thread(target=attempt);thread.start();assert entered.wait(3)
    conn.commit();conn.close();thread.join(5)
    assert not thread.is_alive() and result and '已停止' in result[0]
    assert store.get('turn',turn['id'])['office_slide_progress'] is None


def test_cancellation_after_build_is_detected_before_initial_publish(isolated_office,monkeypatch):
    turn,_=isolated_office;turn=start(turn,'step-before-publish')
    workspace.write_file(turn['project_id'],'report.json',json.dumps(spec(),ensure_ascii=False))
    original=office._verify_structure
    def cancel_after_verify(*args,**kwargs):
        result=original(*args,**kwargs)
        store.patch('turn',turn['id'],state='done',outcome='cancelled')
        return result
    monkeypatch.setattr(office,'_verify_structure',cancel_after_verify)
    with pytest.raises(ValueError,match='已停止'):office.render(turn,'report.json','中止报告')
    records=store.all_items('office_artifact')
    assert len(records)==1 and records[0]['state']=='pending'
    with pytest.raises(ValueError,match='尚未完成'):office.artifact(turn['project_id'],records[0]['id'])


def test_http_observes_constructed_slide_while_file_is_still_building(isolated_office,monkeypatch):
    import threading
    from fastapi.testclient import TestClient
    from agent_service.api import app
    from agent_service.config import TOKEN
    turn,_=isolated_office;turn=start(turn,'step-http')
    workspace.write_file(turn['project_id'],'report.json',json.dumps(spec(),ensure_ascii=False))
    first=threading.Event();resume=threading.Event();errors=[]
    original=office_slide_progress.SlideProgress.page
    def page(self,number,total,slide):
        original(self,number,total,slide)
        if number==1:
            first.set()
            if not resume.wait(10):raise RuntimeError('test progress reader timed out')
    monkeypatch.setattr(office_slide_progress.SlideProgress,'page',page)
    def work():
        try:office.render(turn,'report.json','流式报告')
        except Exception as error:errors.append(error)
    thread=threading.Thread(target=work);thread.start()
    try:
        assert first.wait(10)
        client=TestClient(app)
        response=client.get('/api/projects/'+turn['project_id']+'/turns/'+turn['id'],
                            headers={'Authorization':'Bearer '+TOKEN})
        assert response.status_code==200,response.text
        progress=response.json()['office_slide_progress']
        assert progress['phase']=='building' and progress['total']==4
        assert len(progress['pages'])==1 and progress['pages'][0]['title']=='季度经营'
        assert not store.all_items('office_artifact')
    finally:
        resume.set();thread.join(15)
    assert not thread.is_alive() and not errors
    assert store.get('turn',turn['id'])['office_slide_progress']['phase']=='verified'
