import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI,Header,HTTPException,Depends,Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete as sql_delete

from agent_service import workspace_lifecycle as life,conversation,jobs
from agent_service.config import DATA
from agent_service.store import put,get,patch,all_items,delete,now,uid,engine


@pytest.fixture
def project():
    kinds=('project','conversation','turn','source','pipeline','data_workflow','work_action','collab_task','data_share')
    previous={kind:{row['id'] for row in all_items(kind)} for kind in kinds}
    previous_jobs={row['id'] for row in jobs.list_jobs(limit=100000)}
    row=put('project',{'name':'收入分析','created_at':now(),'updated_at':now()},'p_'+uid())
    yield row
    with engine.begin() as connection:
        for row in jobs.list_jobs(limit=100000):
            if row['id'] not in previous_jobs:connection.execute(sql_delete(jobs.jobs).where(jobs.jobs.c.id==row['id']))
    for kind in reversed(kinds):
        for row in all_items(kind):
            if row['id'] not in previous[kind]:delete(kind,row['id'])


def session(project,**extra):
    return put('conversation',{'project_id':project['id'],'name':'本月收入','created_at':now(),**extra},'c_'+uid())


def turn(project,sid=None,**extra):
    return put('turn',{'project_id':project['id'],'actor_id':'owner','state':'agent','created_at':now(),
        **({'session_id':sid} if sid else {}),**extra},'t_'+uid())


def test_rename_revision_compare_and_swap_and_invalid_name(project):
    initial=life.public_project(project)
    assert initial['revision']==0 and type(initial['revision']) is int
    changed=life.rename_project(project['id'],'  合并收入  ',0)
    assert changed['name']=='合并收入' and changed['revision']==1
    assert life.rename_project(project['id'],'合并收入',1)['revision']==1
    with pytest.raises(life.LifecycleConflict):life.rename_project(project['id'],'旧页面覆盖',0)
    for name in ('  ','非法\n名称','A'*81):
        with pytest.raises(ValidationError):life.rename_project(project['id'],name,1)
    for revision in (-1,True,'1'):
        with pytest.raises(ValueError):life.delete_project(project['id'],revision)
    assert get('project',project['id'])['name']=='合并收入'


def test_concurrent_rename_cannot_silently_overwrite(project):
    def rename(name):
        try:return life.rename_project(project['id'],name,0)
        except life.LifecycleConflict:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(rename,['一组','二组']))
    assert results.count('conflict')==1
    assert get('project',project['id'])['lifecycle_revision']==1


def test_delete_default_session_tombstone_cancels_legacy_and_internal_child_only(project):
    pid=project['id'];sid=conversation.default_session(pid)
    legacy=turn(project)
    child_session=session(project,internal=True)
    child=turn(project,child_session['id'],collaboration_parent_turn_id=legacy['id'])
    other=session(project);sibling=turn(project,other['id'])
    task=put('collab_task',{'project_id':pid,'parent_turn_id':legacy['id'],'child_turn_id':child['id'],
        'transport':'local','state':'working'},'ct_'+uid())
    result=life.delete_session(pid,sid,0)
    assert result['revision']==1 and get('conversation',sid)['deleted_at']
    assert get('project',pid)['context_visibility_revision']==1
    assert sid not in {row['id'] for row in conversation.sessions(pid)}
    assert get('turn',legacy['id'])['outcome']=='cancelled'
    assert get('turn',child['id'])['outcome']=='cancelled'
    assert get('collab_task',task['id'])['state']=='cancelled'
    assert get('turn',sibling['id'])['state']=='agent'
    with pytest.raises(life.WorkspaceDeleted):life.assert_turn_live(legacy)
    assert not life.visible_turn(legacy) and not life.visible_turn(child)
    assert life.visible_turn(sibling)
    life.restore_session(pid,sid,1)
    assert get('project',pid)['context_visibility_revision']==2
    assert get('turn',legacy['id'])['outcome']=='cancelled'
    assert sid in {row['id'] for row in conversation.sessions(pid)}


def test_project_delete_preserves_files_and_stops_jobs_schedules_shares(project):
    pid=project['id'];source=put('source',{'project_id':pid,'name':'收入.csv'},'s_'+uid())
    pipeline=put('pipeline',{'project_id':pid,'name':'汇总','cron':'0 9 * * 1','watch_sources':True,
        'pending_schedule':'pending','next_due':'2030-01-01','published_run':'old-result'},'pl_'+uid())
    workflow=put('data_workflow',{'project_id':pid,'cron':'0 10 * * 1'},'wf_'+uid())
    share=put('data_share',{'project_id':pid,'state':'active','version':1},'share_'+uid())
    queued=jobs.save({'kind':'profile','target':source['id'],'status':'queued'},'j_'+uid())
    running=jobs.save({'kind':'pipeline','target':pipeline['id'],'status':'running','lease_token':'still-running'},'j_'+uid())
    artifact=DATA/('lifecycle-artifact-'+uid()+'.txt');artifact.write_text('金额 120.30；保留输出',encoding='utf-8')
    try:
        result=life.delete_project(pid,0)
        assert result['revision']==1 and result['stopped_work']['external_operations_recalled'] is False
        assert artifact.read_text(encoding='utf-8')=='金额 120.30；保留输出'
        assert get('source',source['id'])['name']=='收入.csv'
        assert jobs.get_job(queued['id'])['status']=='cancelled'
        assert jobs.get_job(running['id'])['status']=='running' and jobs.get_job(running['id'])['cancel_requested']
        assert not get('pipeline',pipeline['id'])['cron'] and not get('data_workflow',workflow['id'])['cron']
        assert get('pipeline',pipeline['id'])['published_run']=='old-result'
        assert get('data_share',share['id'])['state']=='revoked'
        with pytest.raises(life.WorkspaceDeleted):life.assert_project_live(pid)
        restored=life.restore_project(pid,1)
        assert restored['revision']==2
        assert get('data_share',share['id'])['state']=='revoked'
        assert not get('pipeline',pipeline['id'])['cron']
        assert jobs.get_job(queued['id'])['status']=='cancelled'
    finally:artifact.unlink(missing_ok=True)


def test_sent_external_actions_are_unknown_and_are_not_claimed_recalled(project):
    chat=session(project);parent=turn(project,chat['id'])
    pending=put('work_action',{'project_id':project['id'],'turn_id':parent['id'],'state':'approved'},'act_'+uid())
    sent=put('work_action',{'project_id':project['id'],'turn_id':parent['id'],'state':'executing'},'act_'+uid())
    remote=put('collab_task',{'project_id':project['id'],'parent_turn_id':parent['id'],
        'transport':'a2a','state':'working','remote_task_id':'external-42'},'ct_'+uid())
    result=life.delete_session(project['id'],chat['id'])
    assert get('work_action',pending['id'])['state']=='cancelled'
    assert get('work_action',sent['id'])['state']=='unknown'
    assert get('collab_task',remote['id'])['state']=='unknown'
    assert result['stopped_work']['external_operations_recalled'] is False


def test_delete_is_atomic_including_task_and_share_changes(project,monkeypatch):
    child=turn(project)
    share=put('data_share',{'project_id':project['id'],'state':'active','version':1},'share_'+uid())
    original=life._write
    def fail_on_project(connection,kind,value):
        if kind=='project':raise RuntimeError('simulated storage failure')
        return original(connection,kind,value)
    monkeypatch.setattr(life,'_write',fail_on_project)
    with pytest.raises(RuntimeError):life.delete_project(project['id'])
    assert not get('project',project['id']).get('deleted_at')
    assert get('turn',child['id'])['state']=='agent'
    assert get('data_share',share['id'])['state']=='active'


def test_parent_project_must_be_restored_first_and_chats_restore_independently(project):
    pid=project['id'];chat=session(project);hidden=session(project,internal=True)
    life.delete_session(pid,chat['id'])
    life.delete_project(pid)
    with pytest.raises(life.WorkspaceDeleted):life.restore_session(pid,chat['id'])
    rows=life.trash()
    assert pid in {row['id'] for row in rows['projects']}
    assert chat['id'] in {row['id'] for row in rows['conversations']}
    assert hidden['id'] not in {row['id'] for row in rows['conversations']}
    life.restore_project(pid)
    assert get('conversation',chat['id'])['deleted_at']
    life.restore_session(pid,chat['id'])
    assert not get('conversation',chat['id'])['deleted_at']


def test_session_ownership_internal_records_and_missing_default_cas(project):
    other=put('project',{'name':'其他项目','created_at':now()},'p_'+uid())
    foreign=session(other);hidden=session(project,internal=True)
    for sid in (foreign['id'],hidden['id'],'c_missing'):
        with pytest.raises(KeyError):life.delete_session(project['id'],sid)
    default=conversation.default_session(project['id'])
    with pytest.raises(life.LifecycleConflict):life.delete_session(project['id'],default,9)
    with pytest.raises(KeyError):get('conversation',default)


def test_old_resource_and_download_urls_cannot_bypass_project_trash(project):
    pid=project['id']
    source=put('source',{'project_id':pid},'s_'+uid())
    pipeline=put('pipeline',{'project_id':pid},'pl_'+uid())
    view=put('view',{'pipeline_id':pipeline['id']},'v_'+uid())
    run=jobs.save({'kind':'pipeline','target':pipeline['id'],'status':'succeeded'},'j_'+uid())
    try:
        paths=['/api/projects/'+pid+'/data-results/anything/download',
            '/api/sources/'+source['id']+'/original','/api/pipelines/'+pipeline['id']+'/export',
            '/api/views/'+view['id']+'/run','/api/runs/'+run['id']]
        for path in paths:life.assert_request_live(path,'GET')
        life.delete_project(pid)
        for path in paths:
            with pytest.raises(life.WorkspaceDeleted):life.assert_request_live(path,'GET')
        life.assert_request_live('/api/projects/'+pid,'DELETE')
        life.assert_request_live('/api/projects/'+pid+'/restore','POST')
        with pytest.raises(life.WorkspaceDeleted):life.assert_request_live('/api/projects/'+pid+'/sessions/default/restore','POST')
        with pytest.raises(KeyError):life.assert_request_live('/api/sources/not-present/original','GET')
    finally:delete('view',view['id'])


def test_legacy_pipeline_and_all_join_inputs_follow_source_project(project):
    other=put('project',{'name':'查找表项目','created_at':now()},'p_'+uid())
    primary=put('source',{'project_id':project['id']},'s_'+uid())
    lookup=put('source',{'project_id':other['id']},'s_'+uid())
    legacy=put('pipeline',{'source_id':primary['id'],'source_ids':[lookup['id']],'cron':'0 9 * * 1'},'pl_'+uid())
    with_join=put('pipeline',{'project_id':project['id'],'source_id':primary['id'],
        'spec':{'joins':[{'source_id':lookup['id']}]},'watch_sources':True},'pl_'+uid())
    view=put('view',{'pipeline_id':legacy['id']},'v_'+uid())
    legacy_job=jobs.save({'kind':'pipeline','target':legacy['id'],'status':'running'},'j_'+uid())
    lookup_job=jobs.save({'kind':'pipeline','target':with_join['id'],'status':'queued'},'j_'+uid())
    try:
        life.assert_request_live('/api/pipelines/'+legacy['id']+'/export','GET')
        life.delete_project(other['id'])
        assert jobs.get_job(legacy_job['id'])['cancel_requested']
        assert jobs.get_job(lookup_job['id'])['status']=='cancelled'
        assert not get('pipeline',legacy['id'])['cron']
        assert not get('pipeline',with_join['id'])['watch_sources']
        for path in ('/api/pipelines/'+legacy['id']+'/export','/api/pipelines/'+with_join['id']+'/code.zip','/api/views/'+view['id']+'/run'):
            with pytest.raises(life.WorkspaceDeleted):life.assert_request_live(path,'GET')
    finally:delete('view',view['id'])


@pytest.mark.parametrize('job_kind,target_kind',[('probe','source'),('profile','source'),('pipeline','pipeline'),
    ('data_workflow','data_workflow'),('report_fetch','report_connector'),('report_acquisition','report_connector'),
    ('work_action','work_action'),('business_report','business_report'),('turn','turn')])
def test_every_worker_kind_resolves_actual_target_ancestry(project,job_kind,target_kind):
    target=put(target_kind,{'project_id':project['id'],'created_at':now()},'target_'+uid())
    run=jobs.save({'kind':job_kind,'target':target['id'],'status':'succeeded'},'j_'+uid())
    try:
        life.assert_request_live('/api/runs/'+run['id'],'GET')
        life.delete_project(project['id'])
        with pytest.raises(life.WorkspaceDeleted):life.assert_request_live('/api/runs/'+run['id'],'GET')
    finally:delete(target_kind,target['id'])


def test_delete_wins_queue_race_and_restore_cannot_run_late_job(project,monkeypatch):
    source=put('source',{'project_id':project['id']},'s_'+uid())
    inspected=threading.Event();resume=threading.Event();original=life._enqueue_projects
    def blocked(target,extra):
        result=original(target,extra)
        if not inspected.is_set():
            inspected.set();assert resume.wait(5)
        return result
    monkeypatch.setattr(life,'_enqueue_projects',blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        queued=pool.submit(jobs.enqueue,'probe',source['id'])
        assert inspected.wait(5)
        life.delete_project(project['id'])
        resume.set()
        with pytest.raises(life.WorkspaceDeleted):queued.result(timeout=5)
    life.restore_project(project['id'])
    assert jobs.list_jobs(target=source['id'])==[]


def test_delete_and_restore_while_queue_waits_cannot_replay_old_admission(project,monkeypatch):
    source=put('source',{'project_id':project['id']},'s_'+uid())
    inspected=threading.Event();resume=threading.Event();original=life._enqueue_projects
    def blocked(target,extra):
        result=original(target,extra)
        if not inspected.is_set():
            inspected.set();assert resume.wait(5)
        return result
    monkeypatch.setattr(life,'_enqueue_projects',blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        queued=pool.submit(jobs.enqueue,'probe',source['id'])
        assert inspected.wait(5)
        life.delete_project(project['id']);life.restore_project(project['id'])
        resume.set()
        with pytest.raises(ValueError,match='未将旧操作重新入队'):queued.result(timeout=5)
    assert jobs.list_jobs(target=source['id'])==[]


def test_queue_wins_delete_race_and_new_row_is_cancelled_atomically(project,monkeypatch):
    source=put('source',{'project_id':project['id']},'s_'+uid())
    admitted=threading.Event();resume=threading.Event();deleting=threading.Event();original=jobs._enqueue
    def blocked(*args,**kwargs):
        admitted.set();assert resume.wait(5)
        return original(*args,**kwargs)
    def remove():deleting.set();return life.delete_project(project['id'])
    monkeypatch.setattr(jobs,'_enqueue',blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        queued=pool.submit(jobs.enqueue,'probe',source['id'])
        assert admitted.wait(5)
        deleted=pool.submit(remove);assert deleting.wait(5)
        resume.set()
        row=queued.result(timeout=5);deleted.result(timeout=5)
    assert jobs.get_job(row['id'])['status']=='cancelled'
    life.restore_project(project['id'])
    assert jobs.get_job(row['id'])['status']=='cancelled'


def test_queue_guard_supports_existing_lock_and_unscoped_system_target(project):
    source=put('source',{'project_id':project['id']},'s_'+uid())
    with life._project_lock(project['id']):
        scoped=jobs.enqueue('probe',source['id'])
    assert scoped['status']=='queued'
    system=jobs.enqueue('maintenance','no-entity-'+uid())
    assert system['status']=='queued'
    life.delete_project(project['id'])
    with pytest.raises(life.WorkspaceDeleted):jobs.enqueue('maintenance','other-system-'+uid(),extra={'project_id':project['id']})


def test_queue_guard_follows_snapshot_and_action_session(project):
    source=put('source',{'project_id':project['id']},'s_'+uid())
    chat=session(project);action=put('work_action',{'project_id':project['id'],'session_id':chat['id']},'act_'+uid())
    life.delete_session(project['id'],chat['id'])
    # A work action retains its originating session; a late confirmation must
    # not queue it after the conversation's cancellation sweep.
    with pytest.raises(life.WorkspaceDeleted):jobs.enqueue('work_action',action['id'])
    life.delete_project(project['id'])
    with pytest.raises(life.WorkspaceDeleted):jobs.enqueue('pipeline','no-entity-'+uid(),extra={'source_snapshot':{source['id']:source}})


def test_real_auth_request_cannot_enqueue_after_delete_restore_during_preparation(project):
    source=put('source',{'project_id':project['id']},'s_'+uid())
    prepared=threading.Event();resume=threading.Event();app=FastAPI()
    async def auth(request:Request):
        with life.request_scope():
            life.assert_request_live(request.url.path,request.method)
            yield
    life.install(app,auth)
    @app.post('/api/projects/{pid}/queue-prepared',dependencies=[Depends(auth)])
    def queue_prepared(pid:str):
        # This sync endpoint runs in a copied Context from async auth. The
        # shared first-seen map must survive without ContextVar.set here.
        prepared.set();assert resume.wait(5)
        return jobs.enqueue('probe',source['id'])
    client=TestClient(app);url='/api/projects/'+project['id']+'/queue-prepared'
    with ThreadPoolExecutor(max_workers=1) as pool:
        response=pool.submit(client.post,url)
        assert prepared.wait(5)
        life.delete_project(project['id']);life.restore_project(project['id'])
        resume.set();blocked=response.result(timeout=5)
    assert blocked.status_code==409 and '未重放删除前' in blocked.json()['detail']
    assert jobs.list_jobs(target=source['id'])==[]
    fresh=client.post(url)
    assert fresh.status_code==200 and fresh.json()['status']=='queued'
    assert life._request_visibility.get() is None


def test_request_scope_captures_body_resource_first_check_and_is_reset(project):
    with life.request_scope():
        # A global API validates IDs from its body before lengthy preparation.
        life.assert_request_live('/api/projects','POST')
        life.assert_project_live(project['id'])
        life.delete_project(project['id']);life.restore_project(project['id'])
        with pytest.raises(life.LifecycleConflict):jobs.enqueue('maintenance','unscoped-system-'+uid())
    assert life._request_visibility.get() is None
    assert jobs.enqueue('maintenance','fresh-system-'+uid())['status']=='queued'


def test_routes_enforce_auth_revision_and_deleted_status(project):
    app=FastAPI()
    def auth(authorization:str|None=Header(default=None)):
        if authorization!='Bearer owner-test':raise HTTPException(401,'authentication required')
    @app.exception_handler(KeyError)
    async def missing(request,exc):return JSONResponse(status_code=404,content={'detail':'missing'})
    @app.exception_handler(ValueError)
    async def invalid(request,exc):return JSONResponse(status_code=400,content={'detail':str(exc)})
    life.install(app,auth)
    client=TestClient(app);url='/api/projects/'+project['id'];headers={'Authorization':'Bearer owner-test'}
    assert client.get('/api/trash').status_code==401
    assert client.patch(url,json={'name':'预算分析','expected_revision':0},headers=headers).json()['revision']==1
    assert client.patch(url,json={'name':'旧页面','expected_revision':0},headers=headers).status_code==409
    assert client.patch(url,json={'name':'新名字','unexpected':'rejected'},headers=headers).status_code==422
    assert client.request('DELETE',url,json={'expected_revision':1},headers=headers).status_code==200
    assert client.patch(url,json={'name':'已删','expected_revision':2},headers=headers).status_code==410
    assert client.post(url+'/restore',json={'expected_revision':2},headers=headers).json()['revision']==3
    assert client.request('DELETE',url+'?expected_revision=2',json={'expected_revision':3},headers=headers).status_code==400
    assert client.get('/api/trash',headers=headers).json()['permanent_deletion_supported'] is False
