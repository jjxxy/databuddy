import json
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from cryptography.fernet import Fernet
from .locks import FileLock
from sqlalchemy import create_engine, MetaData, Table, Column, String, Text, select, update
from .config import DATA, DATABASE_URL

def now():
    return datetime.now(timezone.utc).isoformat()

def uid():
    return secrets.token_hex(12)

engine = create_engine(DATABASE_URL, connect_args={'check_same_thread': False} if DATABASE_URL.startswith('sqlite:') else {'connect_timeout':5}, pool_pre_ping=True, pool_recycle=300, pool_size=int(__import__('os').getenv('DB_POOL_SIZE','3')), max_overflow=int(__import__('os').getenv('DB_MAX_OVERFLOW','2')), pool_timeout=5)
metadata = MetaData()
items = Table('items', metadata, Column('id', String(64), primary_key=True), Column('kind', String(32), index=True), Column('body', Text, nullable=False))
def create_tables():
    with engine.begin() as c:
        if engine.dialect.name=='postgresql':c.exec_driver_sql('SELECT pg_advisory_xact_lock(91826634)')
        metadata.create_all(c)
create_tables()

def _decode(body):
    value=json.loads(body)
    return unseal(value['__company_private_v1']) if '__company_private_v1' in value else value

def _encode(kind,value,connection):
    if kind in ('turn','collab_task','collab_batch','gateway_task','work_execution') and value.get('project_id'):
        row=connection.execute(select(items.c.body).where(items.c.id==value['project_id'],items.c.kind=='project')).first()
        if row and (json.loads(row[0]).get('company_private') or json.loads(row[0]).get('work_private')):
            value={'id':value['id'],'project_id':value['project_id'],'__company_private_v1':seal(value)}
    return json.dumps(value,ensure_ascii=False,default=str)

def put(kind, value, item_id=None):
    if kind=='run':
        from .jobs import save
        return save(value,item_id)
    from .locks import assert_owned
    assert_owned()
    item_id = item_id or value.get('id') or uid()
    value = dict(value, id=item_id)
    with engine.begin() as c:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        encoded=_encode(kind,value,c)
        stmt=(pg_insert if engine.dialect.name=='postgresql' else sqlite_insert)(items).values(id=item_id,kind=kind,body=encoded)
        c.execute(stmt.on_conflict_do_update(index_elements=['id'],set_={'body':encoded}))
    return value

def get(kind, item_id):
    if kind=='run':
        from .jobs import get_job
        return get_job(item_id)
    with engine.connect() as c:
        row = c.execute(select(items.c.body).where(items.c.id == item_id, items.c.kind == kind)).first()
    if not row:
        raise KeyError(item_id)
    return _decode(row[0])

def all_items(kind):
    if kind=='run':
        from .jobs import list_jobs
        return list(reversed(list_jobs(limit=100000)))
    with engine.connect() as c:
        rows = c.execute(select(items.c.body).where(items.c.kind == kind)).all()
    return [_decode(x[0]) for x in rows]

def delete(kind, item_id):
    from .locks import assert_owned
    assert_owned()
    with engine.begin() as c:
        c.execute(items.delete().where(items.c.id == item_id, items.c.kind == kind))

# All processes share the persistent encryption key. Exclusive creation avoids startup races.
def cipher():
    keypath = DATA / 'connector.key'
    with FileLock(str(DATA / 'connector-key.lock')):
        if not keypath.exists():
            with keypath.open('xb') as f:
                keypath.chmod(0o600)
                f.write(Fernet.generate_key())
                f.flush()
    return Fernet(keypath.read_bytes())

def seal(value):
    return cipher().encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()

def unseal(value):
    return json.loads(cipher().decrypt(value.encode()))

def patch(kind, item_id, **values):
    """Update selected fields without losing concurrent fields."""
    from .locks import assert_owned
    assert_owned()
    with engine.connect() as c:
        if DATABASE_URL.startswith('sqlite:'):
            c.exec_driver_sql('BEGIN IMMEDIATE')
        else:
            c.begin()
        row=c.execute(select(items.c.body).where(items.c.id==item_id,items.c.kind==kind).with_for_update()).first()
        if not row:
            c.rollback();raise KeyError(item_id)
        body=_decode(row[0]);body.update(values)
        c.execute(update(items).where(items.c.id==item_id).values(body=_encode(kind,body,c)))
        c.commit()
    return body

@contextmanager
def transaction():
    from .locks import assert_owned
    assert_owned()
    with engine.connect() as c:
        if DATABASE_URL.startswith('sqlite:'):c.exec_driver_sql('BEGIN IMMEDIATE')
        else:c.begin()
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback();raise
