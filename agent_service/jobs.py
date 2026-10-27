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

ACTIVE=('queued','running','retrying')

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

def claim(lease_seconds=30):
    recover_expired()
    t=time.time()
    with transaction() as c:
        q=select(jobs).where(jobs.c.status.in_(('queued','retrying')),jobs.c.available_at<=t).order_by(jobs.c.created).limit(1).with_for_update(skip_locked=True)
        row=c.execute(q).mappings().first()
        if not row:return None
        token=uid()
        c.execute(update(jobs).where(jobs.c.id==row['id']).values(status='running',attempts=row['attempts']+1,lease_token=token,lease_until=t+lease_seconds,updated=t))
    return get_job(row['id'])

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
    with transaction() as c:
        row=c.execute(select(jobs).where(jobs.c.id==job_id).with_for_update()).mappings().first()
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
    return get_job(job_id)

def request_cancel(job_id):
    with transaction() as c:
        row=c.execute(select(jobs).where(jobs.c.id==job_id).with_for_update()).mappings().first()
        if not row:raise KeyError(job_id)
        body=render(row);body['cancel_requested']=True
        status='cancelled' if row['status'] in ('queued','retrying') else row['status']
        c.execute(update(jobs).where(jobs.c.id==job_id).values(status=status,body=json.dumps(body,ensure_ascii=False),updated=time.time()))
    return get_job(job_id)

def recover_expired():
    with transaction() as c:
        rows=c.execute(select(jobs).where(jobs.c.status=='running',jobs.c.lease_until<time.time()).with_for_update(skip_locked=True)).mappings().all()
        for row in rows:
            body=render(row);cancel=body.get('cancel_requested')
            status='cancelled' if cancel else 'retrying' if row['attempts']<row['max_attempts'] else 'failed'
            body.update(error='执行进程失联，按重试策略恢复',error_code='WORKER_LOST',stage='等待恢复' if status=='retrying' else '已结束',status=status)
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
