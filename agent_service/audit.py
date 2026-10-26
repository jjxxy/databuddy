import hashlib,hmac,json,time
from sqlalchemy import Table,Column,Integer,String,Text,select
from .store import metadata,engine,transaction,cipher

events=Table('audit_events',metadata,Column('sequence',Integer,primary_key=True,autoincrement=True),Column('created',String(64),nullable=False),Column('action',String(100),nullable=False),Column('subject',String(100)),Column('body',Text,nullable=False),Column('previous_hash',String(64),nullable=False),Column('event_hash',String(64),nullable=False))
from .store import create_tables
create_tables()

# Serialize the initial sentinel creation as well as subsequent append operations.
from .store import items
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
with engine.begin() as c:
    stmt=(pg_insert if engine.dialect.name=='postgresql' else sqlite_insert)(items).values(id='audit-lock',kind='system',body='{}').on_conflict_do_nothing(index_elements=['id'])
    c.execute(stmt)

def digest(created,action,subject,body,previous):
    from .config import DATA
    key=(DATA/'connector.key').read_bytes()
    payload=json.dumps([created,action,subject,body,previous],ensure_ascii=False,separators=(',',':')).encode()
    return hmac.new(key,payload,hashlib.sha256).hexdigest()

def record(action,subject='',details=None):
    from .store import now,items
    cipher()
    # No credentials or original row values in the audit payload.
    details=details or {}
    safe={k:v for k,v in details.items() if not any(s in k.lower() for s in ('password','token','secret','connection','config'))}
    body=json.dumps(safe,ensure_ascii=False,sort_keys=True,default=str);created=now()
    with transaction() as c:
        # A sentinel row supplies a cross-process lock on PostgreSQL; SQLite uses BEGIN IMMEDIATE.
        row=c.execute(select(items.c.id).where(items.c.id=='audit-lock').with_for_update()).first()
        if not row:c.execute(items.insert().values(id='audit-lock',kind='system',body='{}'))
        old=c.execute(select(events.c.event_hash).order_by(events.c.sequence.desc()).limit(1)).first()
        previous=old[0] if old else '0'*64
        value=digest(created,action,subject,body,previous)
        c.execute(events.insert().values(created=created,action=action,subject=subject,body=body,previous_hash=previous,event_hash=value))

def list_events(limit=100,offset=0):
    with engine.connect() as c:return [dict(x) for x in c.execute(select(events).order_by(events.c.sequence.desc()).limit(limit).offset(offset)).mappings()]

def verify():
    previous='0'*64;count=0
    with engine.connect() as c:
        for row in c.execute(select(events).order_by(events.c.sequence)).mappings():
            if row['previous_hash']!=previous or not hmac.compare_digest(row['event_hash'],digest(row['created'],row['action'],row['subject'],row['body'],previous)):return {'ok':False,'sequence':row['sequence']}
            previous=row['event_hash'];count+=1
    return {'ok':True,'events':count,'head':previous}
