"""Publication and an actual Stop must have a single observable order."""
import json
import threading

import pytest

from agent_service import office, result_delivery, store, workspace
from agent_service.conversation import TurnInput, submit
from test_office_integrity_regressions import isolated_office
from test_office_slide_progress import spec, start


def test_stop_waits_for_inflight_final_publish_and_preserves_completed_artifact(isolated_office, monkeypatch):
    turn,_ = isolated_office
    turn=start(turn,'stop-during-publish')
    workspace.write_file(turn['project_id'],'report.json',json.dumps(spec(),ensure_ascii=False))
    entered=threading.Event();release=threading.Event();stopped=threading.Event();out=[];errors=[]
    original=result_delivery.atomic_replace
    def paused(source,target):
        if target.suffix=='.pptx':
            entered.set()
            assert release.wait(10)
        return original(source,target)
    monkeypatch.setattr(result_delivery,'atomic_replace',paused)
    def build():
        try:out.append(office.render(turn,'report.json','发布竞态'))
        except Exception as error:errors.append(error)
    def cancel():
        try:submit(turn['project_id'],TurnInput(text='停止',request_id='stop-concurrent-fixture'))
        except Exception as error:errors.append(error)
        finally:stopped.set()
    worker=threading.Thread(target=build);worker.start()
    stop=None
    try:
        assert entered.wait(10)
        stop=threading.Thread(target=cancel);stop.start()
        assert not stopped.wait(.25), 'Stop returned while final publication was still running'
    finally:
        release.set();worker.join(15)
        if stop:stop.join(15)
    assert not worker.is_alive() and stop and not stop.is_alive() and not errors,errors
    assert len(out)==1 and store.get('turn',turn['id'])['outcome']=='cancelled'
    # Once publishing won the race, a later Stop leaves the already committed
    # artifact intact; it does not claim to retract completed external effects.
    path,record=office.artifact(turn['project_id'],out[0]['artifact_id'])
    assert path.is_file() and record['state']=='committed'


def test_stop_before_publish_never_commits_pending_artifact(isolated_office, monkeypatch):
    turn,_ = isolated_office
    turn=start(turn,'stop-before-publish-guard')
    workspace.write_file(turn['project_id'],'report.json',json.dumps(spec(),ensure_ascii=False))
    original=office.put
    def stop_after_pending(kind,value,*args,**kwargs):
        result=original(kind,value,*args,**kwargs)
        if kind=='office_artifact' and value.get('state')=='pending':
            submit(turn['project_id'],TurnInput(text='停止',request_id='stop-before-publication'))
        return result
    monkeypatch.setattr(office,'put',stop_after_pending)
    with pytest.raises(ValueError,match='已停止'):
        office.render(turn,'report.json','已取消的报告')
    assert store.get('turn',turn['id'])['outcome']=='cancelled'
    record=store.all_items('office_artifact')[0]
    assert record['state']=='pending'
    assert not (office.DATA / record['relative_path']).exists()
    with pytest.raises(ValueError,match='尚未完成'):
        office.artifact(turn['project_id'],record['id'])


def test_cached_artifact_does_not_complete_cancelled_new_step(isolated_office, monkeypatch):
    from agent_service.office_slide_progress import SlideProgress
    turn,_=isolated_office;turn=start(turn,'cache-initial')
    workspace.write_file(turn['project_id'],'report.json',json.dumps(spec(),ensure_ascii=False))
    first=office.render(turn,'report.json','缓存成果')
    turn=start(turn,'cache-cancelled')
    guard=SlideProgress.publication_guard
    def stop_then_guard(self):
        submit(turn['project_id'],TurnInput(text='停止',request_id='stop-cache-publication'))
        return guard(self)
    monkeypatch.setattr(SlideProgress,'publication_guard',stop_then_guard)
    with pytest.raises(ValueError,match='已停止'):
        office.render(turn,'report.json','缓存成果')
    # The old committed artifact remains, but this cancelled step did not
    # receive it as a newly completed result.
    assert store.get('turn',turn['id'])['outcome']=='cancelled'
    assert office.artifact(turn['project_id'],first['artifact_id'])[0].exists()


def test_busy_gateway_client_lock_fails_closed_without_workspace_deadlock(isolated_office, monkeypatch):
    from agent_service import agent_gateway
    from agent_service.job_authority import client_guard
    from agent_service.locks import Timeout
    turn,_=isolated_office;turn=start(turn,'gateway-publish')
    turn=store.patch('turn',turn['id'],gateway_client_id='agc_fixture')
    workspace.write_file(turn['project_id'],'report.json',json.dumps(spec(),ensure_ascii=False))
    monkeypatch.setattr(agent_gateway,'assert_turn_allowed',lambda current:None)
    entered=threading.Event();release=threading.Event();errors=[]
    def hold_client():
        try:
            with client_guard('agc_fixture'):
                entered.set()
                if not release.wait(10):raise RuntimeError('fixture timed out')
        except Exception as error:errors.append(error)
    holder=threading.Thread(target=hold_client);holder.start()
    try:
        assert entered.wait(5)
        with pytest.raises(Timeout):office.render(turn,'report.json','外部报告')
    finally:
        release.set();holder.join(5)
    assert not holder.is_alive() and not errors
    record=store.all_items('office_artifact')[0]
    assert record['state']=='pending' and not (office.DATA/record['relative_path']).exists()


def test_project_removal_waits_for_final_publication(isolated_office, monkeypatch):
    from agent_service.workspace_lifecycle import delete_project,assert_project_live
    turn,_=isolated_office;turn=start(turn,'delete-during-publish')
    workspace.write_file(turn['project_id'],'report.json',json.dumps(spec(),ensure_ascii=False))
    entered=threading.Event();release=threading.Event();deleted=threading.Event();out=[];errors=[]
    original=result_delivery.atomic_replace
    def paused(source,target):
        if target.suffix=='.pptx':
            entered.set()
            assert release.wait(10)
        return original(source,target)
    monkeypatch.setattr(result_delivery,'atomic_replace',paused)
    def build():
        try:out.append(office.render(turn,'report.json','删除前报告'))
        except Exception as error:errors.append(error)
    def remove():
        try:delete_project(turn['project_id'])
        except Exception as error:errors.append(error)
        finally:deleted.set()
    worker=threading.Thread(target=build);worker.start();remover=None
    try:
        assert entered.wait(10)
        remover=threading.Thread(target=remove);remover.start()
        assert not deleted.wait(.25), 'Project removal returned before publication finished'
    finally:
        release.set();worker.join(15)
        if remover:remover.join(15)
    assert not worker.is_alive() and remover and not remover.is_alive() and not errors,errors
    assert len(out)==1 and store.get('turn',turn['id'])['outcome']=='cancelled'
    with pytest.raises(ValueError,match='回收站'):
        assert_project_live(turn['project_id'])
    # Deletion preserves already committed bytes but prevents project access.
    assert store.get('office_artifact',out[0]['artifact_id'])['state']=='committed'


def test_stop_waits_until_delivery_manifest_is_registered(isolated_office, monkeypatch):
    turn,_=isolated_office;turn=start(turn,'stop-during-manifest')
    workspace.write_file(turn['project_id'],'report.json',json.dumps(spec(),ensure_ascii=False))
    entered=threading.Event();release=threading.Event();stopped=threading.Event();out=[];errors=[]
    original=workspace.write_file
    def paused(pid,path,content):
        if path.startswith('deliverables/') and path.endswith('.manifest.json'):
            entered.set()
            assert release.wait(10)
        return original(pid,path,content)
    monkeypatch.setattr(workspace,'write_file',paused)
    def build():
        try:out.append(office.render(turn,'report.json','最终登记'))
        except Exception as error:errors.append(error)
    def cancel():
        try:submit(turn['project_id'],TurnInput(text='停止',request_id='stop-during-manifest'))
        except Exception as error:errors.append(error)
        finally:stopped.set()
    worker=threading.Thread(target=build);worker.start();stop=None
    try:
        assert entered.wait(10)
        stop=threading.Thread(target=cancel);stop.start()
        assert not stopped.wait(.25), 'Stop returned before manifest registration'
    finally:
        release.set();worker.join(15)
        if stop:stop.join(15)
    assert not worker.is_alive() and stop and not stop.is_alive() and not errors,errors
    assert len(out)==1 and store.get('turn',turn['id'])['outcome']=='cancelled'
    assert workspace.read_file(turn['project_id'],'deliverables/最终登记.manifest.json')


def test_stop_blocks_recovery_of_preexisting_pending_destination(isolated_office, monkeypatch):
    from agent_service.office_slide_progress import SlideProgress
    turn,_=isolated_office;turn=start(turn,'pending-interrupt')
    workspace.write_file(turn['project_id'],'report.json',json.dumps(spec(),ensure_ascii=False))
    real_put=office.put
    def crash_before_commit(kind,value,*args,**kwargs):
        if kind=='office_artifact' and value.get('state')=='committed':
            raise RuntimeError('simulated crash after file replacement')
        return real_put(kind,value,*args,**kwargs)
    monkeypatch.setattr(office,'put',crash_before_commit)
    with pytest.raises(RuntimeError,match='simulated crash'):
        office.render(turn,'report.json','恢复成果')
    pending=store.all_items('office_artifact')[0]
    assert pending['state']=='pending' and (office.DATA/pending['relative_path']).exists()
    monkeypatch.setattr(office,'put',real_put)
    turn=start(turn,'pending-cancelled')
    guard=SlideProgress.publication_guard
    def stop_then_guard(self):
        submit(turn['project_id'],TurnInput(text='停止',request_id='stop-pending-recovery'))
        return guard(self)
    monkeypatch.setattr(SlideProgress,'publication_guard',stop_then_guard)
    with pytest.raises(ValueError,match='已停止'):
        office.render(turn,'report.json','恢复成果')
    assert store.get('office_artifact',pending['id'])['state']=='pending'
    with pytest.raises(ValueError,match='尚未完成'):
        office.artifact(turn['project_id'],pending['id'])


def test_revoked_gateway_cannot_publish_pending_presentation(isolated_office, monkeypatch):
    import time
    from agent_service import agent_gateway
    turn,_=isolated_office;turn=start(turn,'gateway-revoked-before-publish')
    client_id='agc_test_'+store.uid();actor='u_'+('a'*64);revision='revision-fixture'
    store.put('agent_client',{'enabled':True,'expires_at':'9999-12-31T00:00:00+00:00',
        'actor_id':actor,'project_ids':[turn['project_id']],'approved_model_ids':['fixture-model'],
        'interaction_mode':'work','revision':revision},client_id)
    turn=store.patch('turn',turn['id'],actor_id=actor,gateway_actor_id=actor,
        gateway_client_id=client_id,gateway_client_revision=revision,model_id='fixture-model',
        gateway_deadline=time.time()+60)
    workspace.write_file(turn['project_id'],'report.json',json.dumps(spec(),ensure_ascii=False))
    original=office.put
    def revoke_after_pending(kind,value,*args,**kwargs):
        result=original(kind,value,*args,**kwargs)
        if kind=='office_artifact' and value.get('state')=='pending':
            agent_gateway.revoke_client(client_id,revision)
        return result
    monkeypatch.setattr(office,'put',revoke_after_pending)
    with pytest.raises(agent_gateway.GatewayError):
        office.render(turn,'report.json','撤权报告')
    record=store.all_items('office_artifact')[0]
    assert record['state']=='pending' and not (office.DATA/record['relative_path']).exists()


def make_template(turn,data,monkeypatch):
    import hashlib
    from pptx import Presentation
    from agent_service import ppt_template
    path=data/'source.pptx';deck=Presentation();slide=deck.slides.add_slide(deck.slide_layouts[0])
    slide.shapes.title.text='旧标题';deck.save(path)
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    source={'id':'s_fixture','project_id':turn['project_id'],'name':'source.pptx','kind':'file',
            'revision':1,'content_hash':digest}
    monkeypatch.setattr(ppt_template.project_data,'owned',lambda pid,kind,sid:source)
    monkeypatch.setattr(ppt_template.project_data,'source_path',lambda _:path)
    return digest,[{'page':1,'shape_id':slide.shapes.title.shape_id,'text':'核对结果'}]


def test_template_stop_before_publication_remains_pending(isolated_office,monkeypatch):
    from agent_service import ppt_template
    turn,data=isolated_office;turn=start(turn,'template-stop')
    turn=store.patch('turn',turn['id'],agent_trace=[{'id':'template-stop','tool':'render_pptx_template','status':'started'}])
    digest,replacements=make_template(turn,data,monkeypatch)
    original=store.put
    def stop_after_pending(kind,value,*args,**kwargs):
        result=original(kind,value,*args,**kwargs)
        if kind=='office_artifact' and value.get('state')=='pending':
            submit(turn['project_id'],TurnInput(text='停止',request_id='template-stop-before-publication'))
        return result
    monkeypatch.setattr(store,'put',stop_after_pending)
    with pytest.raises(ValueError,match='已停止'):
        ppt_template.render(turn,'s_fixture',1,digest,'模板取消',replacements)
    record=store.all_items('office_artifact')[0]
    assert record['state']=='pending' and not (office.DATA/record['relative_path']).exists()


def test_manual_template_render_serializes_project_deletion(isolated_office,monkeypatch):
    from agent_service import ppt_template
    from agent_service.workspace_lifecycle import delete_project
    turn,data=isolated_office;digest,replacements=make_template(turn,data,monkeypatch)
    entered=threading.Event();release=threading.Event();deleted=threading.Event();results=[];errors=[]
    original=ppt_template.render_bytes
    def paused(*args,**kwargs):
        entered.set();assert release.wait(10)
        return original(*args,**kwargs)
    monkeypatch.setattr(ppt_template,'render_bytes',paused)
    def build():
        try:results.append(ppt_template.render_for_project(turn['project_id'],'s_fixture',1,digest,'手工模板',replacements))
        except Exception as error:errors.append(error)
    def remove():
        try:delete_project(turn['project_id'])
        except Exception as error:errors.append(error)
        finally:deleted.set()
    worker=threading.Thread(target=build);worker.start();remover=None
    try:
        assert entered.wait(10)
        remover=threading.Thread(target=remove);remover.start()
        assert not deleted.wait(.25), 'project deletion overtook the manual template render'
    finally:
        release.set();worker.join(15)
        if remover:remover.join(15)
    assert not worker.is_alive() and remover and not remover.is_alive() and not errors,errors
    assert len(results)==1 and store.get('project',turn['project_id'])['deleted_at']


def test_document_stop_before_publication_remains_pending(isolated_office,monkeypatch):
    turn,_=isolated_office;turn=start(turn,'document-stop')
    workspace.write_file(turn['project_id'],'report.json',json.dumps({'kind':'document','title':'报告','sections':[{'body':'正文'}]},ensure_ascii=False))
    original=office.put
    def stop_after_pending(kind,value,*args,**kwargs):
        result=original(kind,value,*args,**kwargs)
        if kind=='office_artifact' and value.get('state')=='pending':
            submit(turn['project_id'],TurnInput(text='停止',request_id='document-stop-before-publication'))
        return result
    monkeypatch.setattr(office,'put',stop_after_pending)
    with pytest.raises(ValueError,match='已停止'):
        office.render(turn,'report.json','取消文档')
    record=store.all_items('office_artifact')[0]
    assert record['state']=='pending' and not (office.DATA/record['relative_path']).exists()


def test_template_source_change_after_build_never_commits(isolated_office,monkeypatch):
    from agent_service import ppt_template
    turn,data=isolated_office;turn=start(turn,'template-source-change')
    turn=store.patch('turn',turn['id'],agent_trace=[{'id':'template-source-change','tool':'render_pptx_template','status':'started'}])
    digest,replacements=make_template(turn,data,monkeypatch)
    source=ppt_template.project_data.owned(turn['project_id'],'source','s_fixture')
    original=store.put
    def change_after_pending(kind,value,*args,**kwargs):
        result=original(kind,value,*args,**kwargs)
        if kind=='office_artifact' and value.get('state')=='pending':source['revision']=2
        return result
    monkeypatch.setattr(store,'put',change_after_pending)
    with pytest.raises(ValueError,match='版本'):
        ppt_template.render(turn,'s_fixture',1,digest,'版本变化',replacements)
    record=store.all_items('office_artifact')[0]
    assert record['state']=='pending' and not (office.DATA/record['relative_path']).exists()


def test_template_agent_success_reuses_lifecycle_lock_without_deadlock(isolated_office,monkeypatch):
    import time
    from agent_service import ppt_template
    turn,data=isolated_office;turn=start(turn,'template-success')
    turn=store.patch('turn',turn['id'],agent_trace=[{'id':'template-success','tool':'render_pptx_template','status':'started'}])
    digest,replacements=make_template(turn,data,monkeypatch)
    started=time.monotonic()
    result=ppt_template.render(turn,'s_fixture',1,digest,'模板成功',replacements)
    assert time.monotonic()-started<5
    assert office.artifact(turn['project_id'],result['artifact_id'])[0].is_file()
