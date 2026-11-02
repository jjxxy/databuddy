"""Transactional, indexed task queue with unique deduplication keys and fenced leases."""
import json
import time
from datetime import datetime,timezone
from sqlalchemy import Table,Column,String,Integer,Float,Text,Index,select,update,or_,and_
from sqlalchemy.exc import IntegrityError
from .store import engine,metadata,items,uid,now,transaction

jobs=Table('jobs',metadata,
    Column('id',String(64),primary_key=True),Column('kind',String(20),nullable=False),
    Column('target',String(64),nullable=False,index=True),Column('commit_sha',String(64)),
    Column('status',String(20),nullable=False,index=True),Column('dedup_key',String(200),unique=True),
    Column('attempts',Integer,nullable=False,default=0),Column('max_attempts',Integer,nullable=False,default=3),
    Column('available_at',Float,nullable=False),Column('created',Float,nullable=False),Column('updated',Float,nullable=False),
    Column('lease_token',String(64)),Column('lease_until',Float),Column('body',Text,nullable=False))
Index('jobs_ready_idx',jobs.c.status,jobs.c.available_at,jobs.c.created)
from .store import create_tables
create_tables()

ACTIVE=('queued','running','retrying','awaiting_review')

def render(row):
    d=json.loads(row['body'])
    d.update(id=row['id'],kind=row['kind'],target=row['target'],commit=row['commit_sha'],status=row['status'],attempts=row['attempts'],max_attempts=row['max_attempts'],lease_token=row['lease_token'],lease_until=row['lease_until'],available_at=row['available_at'])
    d.setdefault('created_at',datetime.fromtimestamp(row['created'],timezone.utc).isoformat())
    return d

def get_job(job_id):
    with engine.connect() as c:
        row=c.execute(select(jobs).where(jobs.c.id==job_id)).mappings().first()
    if not row:raise KeyError(job_id)
    return render(row)

def list_jobs(limit=100,offset=0,target=None,status=None):
    q=select(jobs).order_by(jobs.c.created.desc())
    if target:q=q.where(jobs.c.target==target)
    if status:q=q.where(jobs.c.status==status)
    with engine.connect() as c:rows=c.execute(q.limit(limit).offset(offset)).mappings().all()
    return [render(x) for x in rows]

def save(value,item_id=None):
    job_id=item_id or value.get('id') or uid();value=dict(value,id=job_id)
    t=time.time()
    fields=dict(kind=value['kind'],target=value['target'],commit_sha=value.get('commit'),status=value.get('status','queued'),dedup_key=value.get('scheduled_key') or value.get('dedup_key'),attempts=value.get('attempts',0),max_attempts=value.get('max_attempts',3),available_at=value.get('available_at',t),updated=t,lease_token=value.get('lease_token'),lease_until=value.get('lease_until'),body=json.dumps(value,ensure_ascii=False,default=str))
    with transaction() as c:
        exists=c.execute(select(jobs.c.id).where(jobs.c.id==job_id)).first()
        if exists:c.execute(update(jobs).where(jobs.c.id==job_id).values(**fields))
        else:
            try:created=datetime.fromisoformat(value.get('created_at',now())).timestamp()
            except (ValueError,TypeError):created=t
            c.execute(jobs.insert().values(id=job_id,created=created,**fields))
    return get_job(job_id)

def enqueue(kind,target,commit=None,scheduled_key=None,max_attempts=3,extra=None):
    # Take lifecycle locks before opening the queue transaction. Deletion can
    # then either cancel this admitted row or reject this later admission;
    # restoring a project cannot replay a job that arrived after its sweep.
    from .workspace_lifecycle import enqueue_guard
    from .job_authority import admission_guard
    with enqueue_guard(target,extra):
        with admission_guard(extra):
            return _enqueue(kind,target,commit,scheduled_key,max_attempts,extra)

def _enqueue(kind,target,commit=None,scheduled_key=None,max_attempts=3,extra=None):
    # A DB row lock serializes creation with release/revision updates, across API and scheduler processes.
    with transaction() as c:
        c.execute(select(items.c.id).where(items.c.id==target).with_for_update()).first()
        if scheduled_key:
            old=c.execute(select(jobs).where(jobs.c.dedup_key==scheduled_key)).mappings().first()
            if old:
                from .job_authority import same_owner
                same_owner(render(old),extra)
                return render(old)
        active=c.execute(select(jobs).where(jobs.c.target==target,jobs.c.status.in_(ACTIVE)).order_by(jobs.c.created).limit(1)).mappings().first()
        if active:
            from .job_authority import same_owner
            same_owner(render(active),extra)
            return render(active)
        t=time.time();job_id=uid()
        body=dict(extra or {},id=job_id,kind=kind,target=target,commit=commit,status='queued',created_at=now(),stage='等待执行',scheduled_key=scheduled_key,cancel_requested=False)
        # Queue-owned provenance, always overwrite extra: ordinary new jobs do
        # not need to scan historical turns at every worker checkpoint.
        body['authority_origin']={'version':1,'kind':'gateway' if 'gateway_authority' in body else 'workspace'}
        from .job_authority import check
        check(body)
        c.execute(jobs.insert().values(id=job_id,kind=kind,target=target,commit_sha=commit,status='queued',dedup_key=scheduled_key,attempts=0,max_attempts=max_attempts,available_at=t,created=t,updated=t,body=json.dumps(body,ensure_ascii=False)))
    return get_job(job_id)

def is_scheduler_key(kind,target,key):
    """Recognize only the server-generated scheduler keys, not chat keys."""
    if kind not in ('pipeline','data_workflow') or not isinstance(target,str) or not isinstance(key,str) or not key.startswith(target+':'):
        return False
    suffix=key[len(target)+1:]
    if kind=='data_workflow' and len(suffix)==64 and all(c in '0123456789abcdef' for c in suffix):
        return True
    try:return datetime.fromisoformat(suffix).tzinfo is not None
    except (TypeError,ValueError):return False


def _legacy_snapshot(connection,run):
    if not is_scheduler_key(run.get('kind'),run.get('target',''),run.get('scheduled_key')):return None
    row=connection.execute(select(items.c.body).where(items.c.id==run['target'],items.c.kind==run['kind'])).first()
    if not row:return None
    target=json.loads(row[0]);project=target.get('project_id')
    if not project or (run.get('project_id') and run.get('project_id')!=project):return None
    # Old jobs did not record who configured the schedule. Never invent a
    # user; reconfiguration is required before someone can reconcile it.
    return {'kind':run['kind'],'project_id':project,'actor_id':None,'legacy_unattributed':True,
            'generation':None,'published_run':target.get('published_run'),'candidate_run':target.get('candidate_run')}


def _quarantine_legacy_job(job_id,target_id):
    # This is performed only after identifying the selected job and releasing
    # its queue lock; target then job remains the global mutation lock order.
    with transaction() as c:
        c.execute(select(items.c.id).where(items.c.id==target_id).with_for_update()).first()
        row=c.execute(select(jobs).where(jobs.c.id==job_id,jobs.c.target==target_id,
            jobs.c.status.in_(('queued','retrying'))).with_for_update()).mappings().first()
        if not row:return
        body=render(row)
        if isinstance(body.get('schedule_snapshot'),dict) or not is_scheduler_key(body.get('kind'),target_id,body.get('scheduled_key')):return
        snap=_legacy_snapshot(c,body)
        if not snap:
            # Never start a legacy job whose target or ownership can no longer
            # be checked. Keep its durable failure instead of guessing a scope.
            body.update(status='failed',error='旧版计划的目标或项目无法核对，请联系管理员检查运行记录',
                        error_code='LEGACY_SCHEDULE_TARGET_UNVERIFIABLE',stage='等待管理员核对')
            c.execute(update(jobs).where(jobs.c.id==job_id).values(status='failed',body=json.dumps(body,ensure_ascii=False,default=str),updated=time.time()))
            return
        body['schedule_snapshot']=snap
        attempted=row['attempts']>0
        status='failed' if attempted else 'cancelled'
        body.update(status=status,error='旧版任务的设置者身份未记录；请先核对运行记录并重新设置定时',
                    error_code='LEGACY_SCHEDULE_REVIEW_REQUIRED',stage='等待核对' if attempted else '需重新设置定时')
        if attempted:_notify_scheduled_outcome(c,body,status,lost=True)
        else:
            target=c.execute(select(items.c.body).where(items.c.id==target_id,items.c.kind==body['kind']).with_for_update()).first()
            if target:
                state=json.loads(target[0]);field='schedule_error' if body['kind']=='pipeline' else 'refresh_error'
                state[field]='旧版排队任务未执行；请重新设置定时后启用'
                state['schedule_resume_required']=True
                c.execute(update(items).where(items.c.id==target_id,items.c.kind==body['kind']).values(body=json.dumps(state,ensure_ascii=False,default=str)))
        c.execute(update(jobs).where(jobs.c.id==job_id).values(status=status,body=json.dumps(body,ensure_ascii=False,default=str),updated=time.time()))


def claim(lease_seconds=30):
    recover_expired()
    while True:
        selected=None
        with transaction() as c:
            q=select(jobs).where(jobs.c.status.in_(('queued','retrying')),jobs.c.available_at<=time.time()).order_by(jobs.c.created).limit(1).with_for_update(skip_locked=True)
            row=c.execute(q).mappings().first()
            if not row:return None
            body=render(row)
            legacy=(not isinstance(body.get('schedule_snapshot'),dict) and
                    is_scheduler_key(body.get('kind'),body.get('target'),body.get('scheduled_key')))
            if legacy:selected=(row['id'],row['target'])
            else:
                token=uid();t=time.time()
                c.execute(update(jobs).where(jobs.c.id==row['id']).values(status='running',attempts=row['attempts']+1,lease_token=token,lease_until=t+lease_seconds,updated=t))
                selected=(row['id'],None)
        if selected[1] is None:return get_job(selected[0])
        _quarantine_legacy_job(*selected)


def heartbeat(job_id,token,lease_seconds=30):
    t=time.time()
    with transaction() as c:
        result=c.execute(update(jobs).where(jobs.c.id==job_id,jobs.c.lease_token==token,
            jobs.c.status=='running',jobs.c.lease_until>t).values(lease_until=t+lease_seconds,updated=t))
    return bool(result.rowcount)

def fenced_update(job_id,token,*,target_patch=None,**values):
    from .job_authority import guard
    from contextlib import nullcontext
    # Terminal error/cancellation writes must remain possible after authority ends.
    with guard(get_job(job_id)) if target_patch else nullcontext():
        return _fenced_update(job_id,token,target_patch=target_patch,**values)


def _fenced_update(job_id,token,*,target_patch=None,**values):
    target_id=get_job(job_id)['target']
    with transaction() as c:
        # All writers lock target before job; lifecycle deletion uses that order.
        c.execute(select(items.c.id).where(items.c.id==target_id).with_for_update()).first()
        row=c.execute(select(jobs).where(jobs.c.id==job_id).with_for_update()).mappings().first()
        if row and row['target']!=target_id:raise RuntimeError('任务目标已变更，请重新处理')
        if not row or not token or row['lease_token']!=token or row['status']!='running' or (row['lease_until'] or 0)<=time.time():raise RuntimeError('任务租约已失效，禁止发布')
        body=render(row);body.update(values)
        if target_patch:
            from .job_authority import check
            check(body)
            if body.get('cancel_requested'):raise RuntimeError('任务已请求取消，禁止更新候选结果')
            target=c.execute(select(items.c.body).where(items.c.id==row['target'],
                items.c.kind==target_patch['kind']).with_for_update()).first()
            if not target:raise KeyError(row['target'])
            target_body=json.loads(target[0])
            if all(target_body.get(k)==v for k,v in target_patch.get('expected',{}).items()):
                target_body.update(target_patch['values'])
                if (row['lease_until'] or 0)<=time.time():raise RuntimeError('任务租约已失效，禁止发布')
                c.execute(update(items).where(items.c.id==row['target'],items.c.kind==target_patch['kind']).values(body=json.dumps(target_body,ensure_ascii=False,default=str)))
        columns={'body':json.dumps(body,ensure_ascii=False,default=str),'updated':time.time()}
        if 'status' in values:columns['status']=values['status']
        if 'available_at' in values:columns['available_at']=values['available_at']
        if (row['lease_until'] or 0)<=time.time():raise RuntimeError('任务租约已失效，禁止发布')
        c.execute(update(jobs).where(jobs.c.id==job_id).values(**columns))
        if values.get('status') in ('failed','cancelled'):_notify_scheduled_outcome(c,body,values['status'])
    return get_job(job_id)

def _notify_scheduled_outcome(connection, run, status, *, lost=False):
    """Atomically publish terminal schedule status only to its frozen version."""
    snap=run.get('schedule_snapshot')
    kind=run.get('kind')
    if not isinstance(snap,dict) or snap.get('kind')!=kind or kind not in ('pipeline','data_workflow','business_flow'):
        return
    row=connection.execute(select(items.c.body).where(items.c.id==run['target'],items.c.kind==kind).with_for_update()).first()
    if not row:return
    target=json.loads(row[0])
    if target.get('project_id')!=snap.get('project_id'):return
    if kind=='business_flow' and target.get('actor_id')!=snap.get('actor_id'):return
    # An unknown external outcome survives plan edits, version adoption and
    # pause. A new schedule must not silently erase an unresolved old action.
    uncertain=lost or (status=='cancelled' and run.get('attempts',0)>0)
    field='schedule_error' if kind=='pipeline' else 'refresh_error'
    if uncertain:
        pending=target.get('schedule_uncertain_runs')
        if not isinstance(pending,list):pending=[]
        pending=[value for value in pending if isinstance(value,str) and value]
        old=target.get('schedule_uncertain_run')
        if old and old not in pending:pending.insert(0,old)
        if run['id'] not in pending:pending.append(run['id'])
        target['schedule_uncertain_runs']=pending
        target['schedule_uncertain_run']=pending[0]
        target[field]='自动更新已停止，运行 '+target['schedule_uncertain_run']+' 的外部操作状态可能未知；请核对外部回执并记录结论，之前正式结果保留'
        connection.execute(update(items).where(items.c.id==run['target'],items.c.kind==kind)
                           .values(body=json.dumps(target,ensure_ascii=False,default=str)))
        return
    if (target.get('archived') or not (target.get('cron') or target.get('watch_sources'))
            or target.get('schedule_generation')!=snap.get('generation')
            or target.get('published_run')!=snap.get('published_run')):return
    if kind in ('pipeline','data_workflow') and target.get('candidate_run')!=snap.get('candidate_run'):return
    if kind=='pipeline':
        active=target.get('active_commit')
        if not ((active and active==run.get('commit')) or (not active and not target.get('business_plan_confirmation_required')
                and target.get('release_mode','auto')=='auto' and target.get('commit')==run.get('commit'))):return
        field='schedule_error'
    elif kind=='data_workflow':
        if not run.get('workflow_refresh_published') or target.get('active_sha256')!=run.get('commit'):return
        field='refresh_error'
    else:
        if (target.get('actor_id')!=snap.get('actor_id') or target.get('published_digest')!=run.get('commit')
                or target.get('published_commit')!=snap.get('published_commit')
                or target.get('published_turn_id')!=snap.get('published_turn_id')):return
        field='refresh_error'
    if lost:
        notice='自动更新执行进程失联，外部操作状态可能未知；请核对运行记录及外部回执，之前正式结果保留'
    elif status=='cancelled' and run.get('attempts',0)>0:
        notice='自动更新已停止，之前执行的外部操作状态需核对；之前正式结果保留'
    else:
        notice='自动更新%s；之前正式结果保留，请检查运行记录'%('已取消' if status=='cancelled' else '失败')
    if target.get('schedule_gap_pending'):notice+='；历史计划缺口尚待核对'
    target[field]=notice
    connection.execute(update(items).where(items.c.id==run['target'],items.c.kind==kind)
                       .values(body=json.dumps(target,ensure_ascii=False,default=str)))


def request_cancel(job_id):
    target_id=get_job(job_id)['target']
    with transaction() as c:
        c.execute(select(items.c.id).where(items.c.id==target_id).with_for_update()).first()
        row=c.execute(select(jobs).where(jobs.c.id==job_id).with_for_update()).mappings().first()
        if not row:raise KeyError(job_id)
        if row['target']!=target_id:raise RuntimeError('任务目标已变更，请重新处理')
        body=render(row);body['cancel_requested']=True
        status='cancelled' if row['status'] in ('queued','retrying','awaiting_review') else row['status']
        c.execute(update(jobs).where(jobs.c.id==job_id).values(status=status,body=json.dumps(body,ensure_ascii=False),updated=time.time()))
        if status=='cancelled':_notify_scheduled_outcome(c,body,status)
    return get_job(job_id)

def recover_expired():
    # Discover without locks, then lock target before job in each short
    # transaction; recheck expiry under the lock to avoid racing heartbeat.
    with engine.connect() as c:
        candidates=c.execute(select(jobs.c.id,jobs.c.target).where(jobs.c.status=='running',jobs.c.lease_until<time.time())).all()
    for job_id,target_id in candidates:
        with transaction() as c:
            c.execute(select(items.c.id).where(items.c.id==target_id).with_for_update()).first()
            row=c.execute(select(jobs).where(jobs.c.id==job_id,jobs.c.target==target_id,
                jobs.c.status=='running',jobs.c.lease_until<time.time()).with_for_update(skip_locked=True)).mappings().first()
            if not row:continue
            body=render(row);cancel=body.get('cancel_requested')
            snap=body.get('schedule_snapshot')
            if not isinstance(snap,dict):
                snap=_legacy_snapshot(c,body)
                if snap:body['schedule_snapshot']=snap
            scheduled=isinstance(snap,dict)
            # Pre-upgrade business flows had opaque request keys. Never guess
            # whether they were scheduled: stop and retain their checkpoints.
            legacy_business=body.get('kind')=='business_flow' and not scheduled
            status='cancelled' if cancel else 'failed' if scheduled or legacy_business else 'retrying' if row['attempts']<row['max_attempts'] else 'failed'
            needs_review=scheduled or legacy_business
            body.update(error='执行进程失联，外部状态可能未知，请核对后处理' if needs_review else '执行进程失联，按重试策略恢复',error_code='WORKER_LOST',stage='等待恢复' if status=='retrying' else '已结束',status=status)
            if scheduled:_notify_scheduled_outcome(c,body,status,lost=True)
            c.execute(update(jobs).where(jobs.c.id==row['id']).values(status=status,lease_token=None,lease_until=None,available_at=time.time(),updated=time.time(),body=json.dumps(body,ensure_ascii=False)))

# Idempotent upgrade of v0.1 JSON run records; old records remain read-only for rollback compatibility.
with engine.connect() as c:
    legacy=c.execute(select(items.c.id,items.c.body).where(items.c.kind=='run')).all()
for legacy_id,body in legacy:
    try:get_job(legacy_id)
    except KeyError:
        old=json.loads(body)
        if old.get('status')=='running':old.update(status='retrying',stage='升级后恢复')
        if old.get('kind')=='pipeline' and old.get('status')=='succeeded':
            from .config import DATA
            manifest=DATA/'runs'/legacy_id/'output.json'
            if manifest.exists():
                m=json.loads(manifest.read_text())
                if m.get('commit')==old.get('commit'):old['validated']=True
        try:save(old,legacy_id)
        except IntegrityError:pass
