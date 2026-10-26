"""Reversible project/conversation removal with atomic task cancellation.

Soft removal preserves bytes. It stops new work, revokes project shares and
pauses schedules; restoring the record never silently replays old operations.
Projects retain the application's common-authorized workspace ACL.
"""
import hashlib
import json
import time
import contextvars
from contextlib import contextmanager,ExitStack,nullcontext
from fastapi import APIRouter,Depends
from fastapi.responses import JSONResponse
from pydantic import Field,field_validator
from sqlalchemy import select,update

from .actor_context import current_actor
from .config import DATA
from .locks import FileLock
from .models import StrictModel
from .store import items,engine,get,all_items,now,transaction,_decode,_encode
from .jobs import jobs,ACTIVE


class WorkspaceDeleted(ValueError):pass
class LifecycleConflict(ValueError):pass


_request_visibility=contextvars.ContextVar('atlas_request_workspace_visibility',default=None)


@contextmanager
def request_scope():
    # A mutable map intentionally travels through Starlette's copied sync
    # worker Context. Setting a new ContextVar inside that worker would not
    # propagate its first-seen generation back to the request's async scope.
    token=_request_visibility.set({})
    try:yield
    finally:_request_visibility.reset(token)


class RevisionRequest(StrictModel):
    expected_revision:int|None=Field(default=None,ge=0)


class RenameRequest(RevisionRequest):
    name:str=Field(min_length=1,max_length=80)

    @field_validator('name')
    @classmethod
    def valid_name(cls,value):
        value=value.strip()
        if not value or any(ord(char)<32 or ord(char)==127 for char in value):raise ValueError('名称不能为空或含控制字符')
        return value


def public_project(value):
    keys=('id','name','created_at','updated_at','source_id','pipeline_id','deleted_at','deleted_by')
    return {key:value[key] for key in keys if key in value}|{'revision':int(value.get('lifecycle_revision',0))}


def public_session(value):
    keys=('id','project_id','name','created_at','last_mode','model_id','application_id','deleted_at','deleted_by')
    return {key:value[key] for key in keys if key in value}|{'revision':int(value.get('lifecycle_revision',0))}


def assert_project_live(pid):
    project=get('project',pid)
    if project.get('deleted_at'):raise WorkspaceDeleted('项目已移至回收站，请先恢复；未继续读取或执行')
    scope=_request_visibility.get()
    if scope is not None:
        revision=int(project.get('context_visibility_revision',0))
        if scope.setdefault(pid,revision)!=revision:
            raise LifecycleConflict('工作空间可见性已变化，请重新发起操作；未重放删除前的请求')
    return project


def assert_turn_live(turn):
    assert_project_live(turn['project_id'])
    from .conversation import default_session
    session_id=turn.get('session_id') or default_session(turn['project_id'])
    try:session=get('conversation',session_id)
    except KeyError:
        if session_id!=default_session(turn['project_id']):raise WorkspaceDeleted('对话不可用，未继续执行') from None
        return turn
    if session.get('project_id')!=turn['project_id']:raise ValueError('对话不属于任务项目')
    if session.get('deleted_at'):raise WorkspaceDeleted('对话已移至回收站，请先恢复；未继续执行')
    return turn


def assert_request_live(path,method):
    """Guard authenticated legacy download/API paths as well as project APIs.

    The project root DELETE is intentionally repeatable after removal. Only its
    explicit restore endpoint may otherwise touch a deleted project. Child
    session restore still requires its parent project to be live.
    """
    parts=path.strip('/').split('/')
    if len(parts)<3 or parts[0]!='api':return
    family,identity=parts[1:3];method=method.upper()
    if family=='projects':
        if len(parts)==3 and method=='DELETE':get('project',identity);return
        if len(parts)==4 and parts[3]=='restore' and method=='POST':get('project',identity);return
        assert_project_live(identity);return
    kinds={'sources':'source','pipelines':'pipeline','views':'view'}
    if family in kinds:
        resource=get(kinds[family],identity)
    elif family=='runs':resource=get('run',identity)
    else:return
    _assert_resource_live('run' if family=='runs' else kinds[family],resource,set())


def _assert_resource_live(kind,resource,seen,projects=None):
    """Resolve stored ancestry, including pre-project records and join inputs.

    Checking every input is deliberate: a live pipeline's own project field
    cannot make a removed lookup source visible. Job targets are resolved using
    their actual stored kind so new/legacy worker names do not drop this check.
    """
    identity=resource.get('id');key=(kind,identity)
    if key in seen:return
    if len(seen)>=256:raise ValueError('资源关联过多，无法安全检查工作空间状态')
    seen.add(key)
    if kind=='project':
        project=assert_project_live(identity)
        if projects is not None:_capture_visibility(projects,project)
        return
    pid=resource.get('project_id')
    if pid:
        project=assert_project_live(pid)
        if projects is not None:_capture_visibility(projects,project)
    if kind=='turn':assert_turn_live(resource)
    if resource.get('session_id') and pid:
        assert_turn_live({'project_id':pid,'session_id':resource['session_id']})
    references=[]
    if resource.get('pipeline_id'):references.append(('pipeline',resource['pipeline_id']))
    if resource.get('application_id'):references.append(('pipeline',resource['application_id']))
    if resource.get('source_id'):references.append(('source',resource['source_id']))
    references.extend(('source',value) for value in resource.get('source_ids',[]) or [])
    if kind=='source' and resource.get('parent_source_id'):references.append(('source',resource['parent_source_id']))
    if kind=='pipeline':
        for join in (resource.get('spec') or {}).get('joins',[]) or []:
            if isinstance(join,dict) and join.get('source_id'):references.append(('source',join['source_id']))
    for refkind,refid in references:
        _assert_resource_live(refkind,get(refkind,refid),seen,projects)
    if kind=='run' and resource.get('target'):
        # IDs are globally unique in items. Resolve the actual record instead
        # of guessing a target kind from the worker's versioned kind string.
        with engine.connect() as connection:
            row=connection.execute(select(items.c.kind,items.c.body).where(items.c.id==resource['target'])).first()
        if not row:raise KeyError(resource['target'])
        _assert_resource_live(row[0],_decode(row[1]),seen,projects)


def _capture_visibility(projects,project):
    revision=int(project.get('context_visibility_revision',0));pid=project['id']
    if pid in projects and projects[pid]!=revision:raise ValueError('工作空间可见性已变化，未将旧操作重新入队')
    projects[pid]=revision


def _enqueue_projects(target,extra):
    projects={};seen=set()
    for pid in list(_request_visibility.get() or {}):
        _capture_visibility(projects,assert_project_live(pid))
    with engine.connect() as connection:
        row=connection.execute(select(items.c.kind,items.c.body).where(items.c.id==target)).first()
    # System/dedup queue tests may use a target without an items entity. Such
    # targets retain their existing behavior; declared project scope never does.
    if row:_assert_resource_live(row[0],_decode(row[1]),seen,projects)
    if extra.get('project_id'):
        _capture_visibility(projects,assert_project_live(extra['project_id']))
    for identity,snapshot in (extra.get('source_snapshot') or {}).items():
        _assert_resource_live('source',get('source',identity),seen,projects)
        if isinstance(snapshot,dict) and snapshot.get('project_id'):
            _capture_visibility(projects,assert_project_live(snapshot['project_id']))
    for key,kind in (('turn_id','turn'),('pipeline_id','pipeline'),('source_id','source')):
        if extra.get(key):_assert_resource_live(kind,get(kind,extra[key]),seen,projects)
    return projects


def _project_lock(pid):
    path=str(DATA/('lifecycle-'+hashlib.sha256(pid.encode()).hexdigest()[:32]+'.lock'))
    from .locks import _owned,assert_owned
    assert_owned()
    # FileLock is reentrant per object, not per pathname. Reuse the ownership
    # already carried by a synchronous caller instead of self-deadlocking.
    if any(lock.path==path for lock,_ in _owned.get()):return nullcontext()
    return FileLock(path,timeout=10)


@contextmanager
def enqueue_guard(target,extra=None):
    """Serialize final queue admission with removal before any jobs transaction.

    Resource ancestry is re-read under all project locks. If another operation
    changed it while locks were being acquired, retry with the new sorted set.
    Existing callers must not enter this gate inside an open DB transaction.
    """
    extra=extra or {}
    for _ in range(3):
        projects=_enqueue_projects(target,extra)
        with ExitStack() as stack:
            for pid in sorted(projects):stack.enter_context(_project_lock(pid))
            checked=_enqueue_projects(target,extra)
            if any(checked[pid]!=revision for pid,revision in projects.items() if pid in checked):
                raise ValueError('工作空间可见性已变化，未将旧操作重新入队')
            if checked.keys()!=projects.keys():continue
            yield
            return
    raise ValueError('任务关联的工作空间已变化，请刷新后重试；未晚入队')


def visible_turn(turn):
    try:
        assert_turn_live(turn)
        from .conversation import default_session
        sid=turn.get('session_id') or default_session(turn['project_id'])
        try:session=get('conversation',sid)
        except KeyError:session={}
        return not session.get('internal') and not turn.get('collaboration_parent_turn_id')
    except (KeyError,ValueError):return False


def _row(connection,kind,identity):
    value=connection.execute(select(items.c.body).where(items.c.id==identity,items.c.kind==kind).with_for_update()).first()
    if not value:raise KeyError(identity)
    return _decode(value[0])


def _write(connection,kind,value):
    connection.execute(update(items).where(items.c.id==value['id'],items.c.kind==kind).values(body=_encode(kind,value,connection)))


def _all(connection,kind):
    return [_decode(row[0]) for row in connection.execute(select(items.c.body).where(items.c.kind==kind)).all()]


def _cancel(connection,pid,sid=None):
    stamp=now();who=current_actor();turns=_all(connection,'turn')
    from .conversation import default_session
    scoped={t['id'] for t in turns if t.get('project_id')==pid and (sid is None or (t.get('session_id') or default_session(pid))==sid)}
    # Internal child conversations use separate session IDs, so follow the
    # server-owned parent link when removing the coordinator's conversation.
    scoped.update(t['id'] for t in turns if t.get('project_id')==pid and t.get('collaboration_parent_turn_id') in scoped)
    active=[];job_ids=set()
    for turn in turns:
        if turn['id'] not in scoped:continue
        job_ids.update(turn[key] for key in ('run_id','data_workflow_job') if turn.get(key))
        if turn.get('state') not in ('done','failed'):
            turn.update(state='done',outcome='cancelled',answer='项目或对话已移至回收站，未完成的处理已停止；已有资料和成果保留。',
                        finished_at=stamp,progress='',resume_available=False,secret_text=None,lifecycle_cancelled_at=stamp)
            _write(connection,'turn',turn);active.append(turn['id'])
    action_ids=set()
    for action in _all(connection,'work_action'):
        if action.get('project_id')!=pid or (sid is not None and action.get('turn_id') not in scoped and action.get('session_id')!=sid):continue
        action_ids.add(action['id'])
        if action.get('state') in ('pending','approved'):
            action.update(state='cancelled',finished_at=stamp,cancel_requested=True)
            _write(connection,'work_action',action)
        elif action.get('state')=='executing':
            action.update(state='unknown',cancel_requested=True,finished_at=stamp,
                error='操作已经发出，删除对话不能撤回；请到原系统核对，未自动重发')
            _write(connection,'work_action',action)
    cancelled_tasks=[]
    for task in _all(connection,'collab_task'):
        if task.get('project_id')!=pid or (sid is not None and task.get('parent_turn_id') not in scoped and task.get('child_turn_id') not in scoped):continue
        if task.get('state') in ('succeeded','failed','input_required','auth_required','cancelled','unknown','rejected'):continue
        if task.get('transport')=='local' or (task.get('state')=='queued' and not task.get('remote_task_id')):
            task.update(state='cancelled',cancel_requested=True,lifecycle_cancelled_at=stamp,updated_at=stamp,error='项目或对话已移至回收站，未继续执行')
        else:
            task.update(state='unknown',cancel_requested=True,lifecycle_cancelled_at=stamp,updated_at=stamp,
                error='远端请求已发出，删除本地记录不能证明远端已停止；请到远端核对，未重复提交')
        _write(connection,'collab_task',task);cancelled_tasks.append(task['id'])
    target_ids=set(action_ids)
    paused=[];revoked=[]
    if sid is None:
        for kind in ('source','pipeline','data_workflow'):
            for record in _all(connection,kind):
                related=record.get('project_id')==pid
                if kind=='pipeline' and not related:
                    inputs=set(record.get('source_ids',[]) or [])
                    if record.get('source_id'):inputs.add(record['source_id'])
                    inputs.update(join['source_id'] for join in (record.get('spec') or {}).get('joins',[]) or [] if isinstance(join,dict) and join.get('source_id'))
                    related=bool(inputs&target_ids)
                if not related:continue
                target_ids.add(record['id'])
                if kind in ('pipeline','data_workflow') and (record.get('cron') or record.get('watch_sources') or record.get('pending_schedule')):
                    record.update(lifecycle_paused_schedule={key:record.get(key) for key in ('cron','watch_sources','pending_schedule','next_due')},
                        cron=None,watch_sources=False,pending_schedule=None,next_due=None)
                    _write(connection,kind,record);paused.append(record['id'])
        for share in _all(connection,'data_share'):
            if share.get('project_id')==pid and share.get('state')=='active':
                share.update(state='revoked',revoked_at=stamp,revoked_by_hash=hashlib.sha256(who.encode()).hexdigest(),
                    version=share.get('version',0)+1,lifecycle_revoked=True)
                _write(connection,'data_share',share);revoked.append(share['id'])
    cancelled_jobs=[]
    rows=connection.execute(select(jobs).where(jobs.c.status.in_(ACTIVE)).with_for_update()).mappings().all()
    for row in rows:
        body=json.loads(row['body'])
        if row['id'] not in job_ids and row['target'] not in target_ids and not (sid is None and body.get('project_id')==pid):continue
        body.update(cancel_requested=True,lifecycle_cancelled_at=stamp)
        state='cancelled' if row['status'] in ('queued','retrying') else row['status']
        body['status']=state
        connection.execute(update(jobs).where(jobs.c.id==row['id']).values(status=state,body=json.dumps(body,ensure_ascii=False),updated=time.time()))
        cancelled_jobs.append(row['id'])
    return {'cancelled_turn_ids':active,'cancel_requested_job_ids':cancelled_jobs,'stopped_collaboration_ids':cancelled_tasks,
            'paused_schedule_ids':paused,'revoked_share_ids':revoked,
            'external_operations_recalled':False}


def _change(pid,sid,action,expected_revision=None,name=None):
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision<0):raise ValueError('版本编号无效')
    lock=FileLock(str(DATA/('lifecycle-'+hashlib.sha256(pid.encode()).hexdigest()[:32]+'.lock')),timeout=10)
    with lock,transaction() as connection:
        project=_row(connection,'project',pid)
        if sid is None:resource=project;kind='project'
        else:
            if project.get('deleted_at'):raise WorkspaceDeleted('请先恢复所属项目，再整理或恢复对话')
            from .conversation import default_session
            try:resource=_row(connection,'conversation',sid)
            except KeyError:
                if sid!=default_session(pid):raise
                resource={'id':sid,'project_id':pid,'name':'项目对话','created_at':project['created_at']}
                connection.execute(items.insert().values(id=sid,kind='conversation',body=json.dumps(resource,ensure_ascii=False)))
            if resource.get('project_id')!=pid or resource.get('internal'):raise KeyError(sid)
            kind='conversation'
        revision=int(resource.get('lifecycle_revision',0))
        if expected_revision is not None and expected_revision!=revision:raise LifecycleConflict('内容已被其他操作修改，请刷新后重试')
        deleted=bool(resource.get('deleted_at'));changes=None
        if action=='rename':
            if deleted:raise WorkspaceDeleted('内容已移至回收站，请先恢复后改名')
            checked=RenameRequest(name=name).name
            if resource.get('name')==checked:return public_project(resource) if sid is None else public_session(resource)
            resource['name']=checked
        elif action=='delete':
            if deleted:return public_project(resource) if sid is None else public_session(resource)
            resource.update(deleted_at=now(),deleted_by=current_actor())
            changes=_cancel(connection,pid,sid)
        elif action=='restore':
            if not deleted:return public_project(resource) if sid is None else public_session(resource)
            resource.update(deleted_at=None,deleted_by=None,restored_at=now(),restored_by=current_actor())
        else:raise ValueError('不支持的工作空间操作')
        resource.update(lifecycle_revision=revision+1,updated_at=now())
        if action in ('delete','restore'):
            if sid is None:resource['context_visibility_revision']=int(resource.get('context_visibility_revision',0))+1
            else:
                project['context_visibility_revision']=int(project.get('context_visibility_revision',0))+1
                _write(connection,'project',project)
        _write(connection,kind,resource)
        result=public_project(resource) if sid is None else public_session(resource)
        if changes is not None:result['stopped_work']=changes
        return result


def rename_project(pid,name,expected_revision=None):return _change(pid,None,'rename',expected_revision,name)
def delete_project(pid,expected_revision=None):return _change(pid,None,'delete',expected_revision)
def restore_project(pid,expected_revision=None):return _change(pid,None,'restore',expected_revision)
def rename_session(pid,sid,name,expected_revision=None):return _change(pid,sid,'rename',expected_revision,name)
def delete_session(pid,sid,expected_revision=None):return _change(pid,sid,'delete',expected_revision)
def restore_session(pid,sid,expected_revision=None):return _change(pid,sid,'restore',expected_revision)


def trash():
    projects=[public_project(p) for p in all_items('project') if p.get('deleted_at')]
    conversations=[public_session(c) for c in all_items('conversation') if c.get('deleted_at') and not c.get('internal')]
    projects.sort(key=lambda p:(p.get('deleted_at',''),p['id']),reverse=True)
    conversations.sort(key=lambda c:(c.get('deleted_at',''),c['id']),reverse=True)
    return {'projects':projects,'conversations':conversations,'permanent_deletion_supported':False}


def _expected(value,query):
    body=value.expected_revision if value else None
    if body is not None and query is not None and body!=query:raise ValueError('请求中的版本编号不一致')
    return body if body is not None else query


def install(app,auth):
    @app.exception_handler(WorkspaceDeleted)
    async def deleted_handler(request,exc):return JSONResponse(status_code=410,content={'detail':str(exc)})
    @app.exception_handler(LifecycleConflict)
    async def conflict_handler(request,exc):return JSONResponse(status_code=409,content={'detail':str(exc)})
    router=APIRouter(dependencies=[Depends(auth)])
    @router.get('/api/trash')
    def get_trash():return trash()
    @router.patch('/api/projects/{project_id}')
    def project_rename(project_id:str,value:RenameRequest):return rename_project(project_id,value.name,value.expected_revision)
    @router.delete('/api/projects/{project_id}')
    def project_delete(project_id:str,value:RevisionRequest|None=None,expected_revision:int|None=None):return delete_project(project_id,_expected(value,expected_revision))
    @router.post('/api/projects/{project_id}/restore')
    def project_restore(project_id:str,value:RevisionRequest|None=None,expected_revision:int|None=None):return restore_project(project_id,_expected(value,expected_revision))
    @router.patch('/api/projects/{project_id}/sessions/{session_id}')
    def session_rename(project_id:str,session_id:str,value:RenameRequest):return rename_session(project_id,session_id,value.name,value.expected_revision)
    @router.delete('/api/projects/{project_id}/sessions/{session_id}')
    def session_delete(project_id:str,session_id:str,value:RevisionRequest|None=None,expected_revision:int|None=None):return delete_session(project_id,session_id,_expected(value,expected_revision))
    @router.post('/api/projects/{project_id}/sessions/{session_id}/restore')
    def session_restore(project_id:str,session_id:str,value:RevisionRequest|None=None,expected_revision:int|None=None):return restore_session(project_id,session_id,_expected(value,expected_revision))
    app.include_router(router)
