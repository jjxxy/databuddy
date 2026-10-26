"""One-way, atomic SQLite -> PostgreSQL metadata cutover. Never deletes SQLite."""
import json,os,sqlite3,time
from pathlib import Path
from sqlalchemy import text,select,func
from .config import DATA

def maybe_migrate():
    if os.getenv('ATLAS_MIGRATE_SQLITE')!='1':return None
    from .store import engine,metadata,items
    from . import jobs,audit,activity
    source=DATA/'metadata.sqlite'
    if engine.dialect.name!='postgresql':raise ValueError('迁移目标必须为PostgreSQL')
    if not source.is_file():raise ValueError('没有原SQLite元数据库，拒绝自动创建空项目')
    with engine.begin() as c:
        c.execute(text('SELECT pg_advisory_xact_lock(913448261)'))
        if c.execute(select(items.c.id).where(items.c.id=='migration-sqlite-v13')).first():return {'reused':True}
        if c.execute(select(func.count()).select_from(items).where(items.c.id!='audit-lock')).scalar():raise ValueError('目标数据库非空，拒绝覆盖')
        folder=DATA/'migrations';folder.mkdir(exist_ok=True);backup=folder/'pre-postgres.sqlite'
        with sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True) as src,sqlite3.connect(backup) as out:src.backup(out)
        backup.chmod(0o600)
        with sqlite3.connect('file:'+str(backup)+'?mode=ro',uri=True) as src:
            src.row_factory=sqlite3.Row;names={r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")};counts={}
            # Validate the original audit chain with the existing shared encryption key.
            previous='0'*64
            if 'audit_events' in names:
                for r in src.execute('SELECT * FROM audit_events ORDER BY sequence'):
                    if r['previous_hash']!=previous or r['event_hash']!=audit.digest(r['created'],r['action'],r['subject'],r['body'],previous):raise ValueError('原审计链校验失败')
                    previous=r['event_hash']
            for name in ('items','jobs','audit_events','activity_events','activity_reads','push_outbox'):
                if name not in names:continue
                table=metadata.tables[name];count=0
                if name=='items':c.execute(items.delete().where(items.c.id=='audit-lock'))
                query=src.execute('SELECT * FROM '+name)
                while batch:=query.fetchmany(500):
                    c.execute(table.insert(),[dict(r) for r in batch]);count+=len(batch)
                actual=c.execute(select(func.count()).select_from(table)).scalar()
                if actual!=count:raise ValueError('迁移行数校验失败')
                counts[name]=count
            for table,col in (('audit_events','sequence'),('activity_events','seq')):
                c.execute(text(f"SELECT setval(pg_get_serial_sequence('{table}','{col}'),COALESCE((SELECT max({col}) FROM {table}),1),EXISTS(SELECT 1 FROM {table}))"))
            value={'id':'migration-sqlite-v13','counts':counts,'finished_at':time.time(),'direction':'sqlite_to_postgresql'}
            c.execute(items.insert().values(id=value['id'],kind='system',body=json.dumps(value)))
    return {'ok':True,'counts':counts}
