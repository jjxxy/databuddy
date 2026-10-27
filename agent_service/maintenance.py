"""Consistent application backups, offline restore, and conservative run retention."""
import argparse,contextlib,hashlib,hmac,json,os,shutil,sys,tempfile,time,zipfile
from pathlib import Path
from sqlalchemy import select,func
from .config import DATA
from .store import engine,metadata,items,get,patch
from .jobs import jobs,list_jobs,ACTIVE
from .audit import events,record
from .activity import events as activity_events,reads as activity_reads,outbox as push_outbox
BACKUP_TABLES=(items,jobs,events,activity_events,activity_reads,push_outbox)

def require_server_backup_layout():
    # Format 2 contains DATA only. A local workspace also has live Git/code
    # and visible deliverables beside DATA, and has a different restore layout.
    # Refuse before touching an archive, destination, lock or database so that
    # a partial snapshot can never be advertised as a complete local backup.
    if os.getenv('ATLAS_LOCAL_MODE')=='1' or os.getenv('ATLAS_LOCAL_WORKSPACE'):
        raise ValueError('本地文件夹模式不支持旧format2备份或恢复。请先停止Atlas服务，备份或还原整个所选工作空间目录（含.atlas、Atlas工作区和Atlas成果），并另外保存部署目录中的llm.json、.env等配置文件；恢复前核对原挂载路径与文件权限')

@contextlib.contextmanager
def guard(exclusive=False,blocking=False):
    from .platform_files import file_guard
    with file_guard(DATA/'maintenance.lock',exclusive=exclusive,blocking=blocking):
        yield

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        while chunk:=f.read(1024*1024):h.update(chunk)
    return h.hexdigest()

def backup(output):
    require_server_backup_layout()
    output=Path(output).resolve()
    if output.is_relative_to(DATA):raise ValueError('备份文件必须位于数据目录外')
    output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists():raise ValueError('目标备份已存在，拒绝覆盖')
    with guard(exclusive=True,blocking=True),tempfile.TemporaryDirectory() as temp:
        temp=Path(temp);database=temp/'metadata.jsonl'
        # The same cross-process shared lock covers API mutations and worker executions.
        with engine.connect() as c,database.open('w',encoding='utf-8') as out:
            for table in BACKUP_TABLES:
                for row in c.execution_options(stream_results=True).execute(select(table)).mappings():out.write(json.dumps({'table':table.name,'row':dict(row)},ensure_ascii=False,default=str)+'\n')
        files=[]
        for p in DATA.rglob('*'):
            if not p.is_file() or p.is_symlink():continue
            rel=p.relative_to(DATA)
            if engine.dialect.name=='sqlite' and p.resolve()==Path(engine.url.database).resolve():continue
            if p.name.endswith(('.lock','-wal','-shm','-journal')) or p.name in ('metadata.sqlite',):continue
            if rel.parts[0]=='dagster':continue # Dagster is reconstructible; its server DB is not mixed into app snapshots.
            files.append((p,'data/'+rel.as_posix()))
        files.append((database,'metadata.jsonl'))
        manifest={'format':2,'created_at':time.time(),'data_root':str(DATA),'files':{name:sha(p) for p,name in files}}
        partial=output.with_suffix(output.suffix+'.partial')
        try:
            with zipfile.ZipFile(partial,'w',zipfile.ZIP_DEFLATED) as z:
                for p,name in files:z.write(p,name)
                z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False))
            partial.chmod(0o600);os.replace(partial,output)
        finally:partial.unlink(missing_ok=True)
    record('maintenance.backup','workspace',{'files':len(files),'archive_hash':sha(output)})
    return {'path':str(output),'files':len(files),'sha256':sha(output)}

def validate_staged_backup(rows,tmp):
    audits=sorted((entry['row'] for entry in rows if entry['table']=='audit_events'),key=lambda x:x['sequence'])
    key_path=tmp/'data'/'connector.key'
    if audits and not key_path.exists():raise ValueError('备份缺少审计密钥')
    previous='0'*64;last=0
    for event in audits:
        if not isinstance(event['sequence'],int) or event['sequence']<=last:raise ValueError('备份审计序号无效')
        payload=json.dumps([event['created'],event['action'],event['subject'],event['body'],previous],ensure_ascii=False,separators=(',',':')).encode()
        expected=hmac.new(key_path.read_bytes(),payload,hashlib.sha256).hexdigest()
        if event['previous_hash']!=previous or not hmac.compare_digest(event['event_hash'],expected):raise ValueError('备份审计链无效，未写入目标空间')
        previous=event['event_hash'];last=event['sequence']
    for entry in rows:
        row=entry['row']
        if entry['table'] in ('items','jobs') and (not isinstance(row.get('id'),str) or '/' in row['id'] or '\\' in row['id'] or row['id'] in ('.','..')):raise ValueError('备份记录 ID 无效')
        if entry['table']=='items' and row['kind']=='pipeline':
            body=json.loads(row['body'])
            if body['id']!=row['id']:raise ValueError('任务 ID 不一致')

def restore(archive):
    require_server_backup_layout()
    with guard(exclusive=True,blocking=False):
        with engine.connect() as c:
            occupied=sum(c.execute(select(func.count()).select_from(table).where(items.c.id!='audit-lock') if table is items else select(func.count()).select_from(table)).scalar() for table in BACKUP_TABLES)
        if occupied:raise ValueError('恢复只允许空工作空间。停止服务并使用新的数据库与数据卷')
        with tempfile.TemporaryDirectory() as tmp,zipfile.ZipFile(archive) as z:
            tmp=Path(tmp)
            if len(z.namelist())!=len(set(z.namelist())):raise ValueError('备份含重复条目')
            maximum=int(os.getenv('MAX_BACKUP_UNCOMPRESSED_MB','4096'))*1024*1024
            if sum(info.file_size for info in z.infolist())>maximum:raise ValueError('备份解压体积超过限制')
            manifest=json.loads(z.read('manifest.json'))
            if manifest.get('format')!=2 or not isinstance(manifest.get('files'),dict) or 'metadata.jsonl' not in manifest['files']:raise ValueError('不支持的备份格式')
            for name,expected in manifest['files'].items():
                rel=Path(name)
                if rel.is_absolute() or '..' in rel.parts or '\\' in name or (name!='metadata.jsonl' and not name.startswith('data/')) or name not in z.namelist():raise ValueError('备份路径无效')
                info=z.getinfo(name)
                if (info.external_attr>>16)&0o170000==0o120000:raise ValueError('备份不允许符号链接')
                dest=tmp/rel;dest.parent.mkdir(parents=True,exist_ok=True)
                with z.open(name) as src,dest.open('wb') as out:shutil.copyfileobj(src,out)
                if sha(dest)!=expected:raise ValueError('备份完整性校验失败：'+name)
            rows=[]
            with (tmp/'metadata.jsonl').open(encoding='utf-8') as f:
                for line in f:
                    value=json.loads(line)
                    if value['table'] not in tuple(t.name for t in BACKUP_TABLES):raise ValueError('备份包含未知表')
                    if value['table']=='items' and value['row']['kind']=='pipeline':
                        body=json.loads(value['row']['body']);body['repository']=str(DATA/'repositories'/body['id']);value['row']['body']=json.dumps(body,ensure_ascii=False)
                    rows.append(value)
            validate_staged_backup(rows,tmp)
            # Empty-only restore: stage all bytes first, then commit metadata in one transaction.
            copied=[]
            try:
                for src in (tmp/'data').rglob('*'):
                    if src.is_file():
                        dest=DATA/src.relative_to(tmp/'data')
                        if engine.dialect.name=='sqlite' and dest.resolve()==Path(engine.url.database).resolve():raise ValueError('备份不得覆盖目标元数据库文件')
                        dest.parent.mkdir(parents=True,exist_ok=True)
                        if not dest.resolve().is_relative_to(DATA):raise ValueError('恢复路径指向工作空间外')
                        if dest.exists():raise ValueError('恢复目标存在同名文件，拒绝覆盖')
                        copied.append(dest);shutil.copy2(src,dest)
                with engine.begin() as c:
                    c.execute(items.delete().where(items.c.id=='audit-lock'))
                    for entry in rows:c.execute(metadata.tables[entry['table']].insert().values(**entry['row']))
                    if engine.dialect.name=='postgresql':c.exec_driver_sql("SELECT setval(pg_get_serial_sequence('audit_events','sequence'), COALESCE((SELECT max(sequence) FROM audit_events),1), EXISTS(SELECT 1 FROM audit_events))")
                    if engine.dialect.name=='postgresql':c.exec_driver_sql("SELECT setval(pg_get_serial_sequence('activity_events','seq'), COALESCE((SELECT max(seq) FROM activity_events),1), EXISTS(SELECT 1 FROM activity_events))")
            except BaseException:
                for p in copied:p.unlink(missing_ok=True)
                raise
        from .audit import verify
        if not verify()['ok']:raise ValueError('恢复后审计完整性验证失败')
    record('maintenance.restore','workspace',{'format':2})
    return {'ok':True,'rows':len(rows)}

def _retention(days=30,keep_success=20,apply=False):
    from .store import all_items
    from datetime import datetime,timezone
    from . import workspace
    runs=list_jobs(100000);by_id={run['id']:run for run in runs}
    cutoff=time.time()-days*86400
    protected=set()
    workflows=all_items('data_workflow')
    for p in all_items('pipeline')+workflows:
        protected.update(x for x in (p.get('published_run'),p.get('candidate_run')) if x)
        protected.update(r['id'] for r in list_jobs(keep_success,target=p['id'],status='succeeded'))
        protected.update(r['id'] for r in list_jobs(keep_success,target=p['id'],status='validated'))

    def references(document):
        if document is None:return set()
        if not isinstance(document,dict) or not isinstance(document.get('bindings'),dict):
            raise ValueError('工作流依赖信息无法核对，未执行历史清理')
        result=set()
        for binding in document['bindings'].values():
            if not isinstance(binding,dict):raise ValueError('工作流依赖信息无法核对，未执行历史清理')
            ident=binding.get('result_id')
            if ident is not None:
                if not isinstance(ident,str) or not ident:raise ValueError('工作流依赖信息无法核对，未执行历史清理')
                result.add(ident)
        return result

    for workflow in workflows:
        protected.update(references(workflow.get('active_document')))
        # A saved draft is a reusable dependency even before its next execution.
        # If it is temporarily invalid, fail closed rather than deleting inputs
        # which may be needed when the owner repairs it.
        if workflow.get('path'):
            try:draft=json.loads(workspace.read_file(workflow['project_id'],workflow['path']))
            except (OSError,ValueError,KeyError,TypeError):
                raise ValueError('工作流草稿暂时无法核对，未执行历史清理') from None
            protected.update(references(draft))
    current=datetime.now(timezone.utc)
    for share in all_items('data_share'):
        if share.get('state')!='active':continue
        try:
            expires=datetime.fromisoformat(share['expires_at'])
            expired=expires.tzinfo is not None and expires<=current
        except (KeyError,TypeError,ValueError):expired=False
        if not expired and share.get('result_id'):protected.add(share['result_id'])
    for report in all_items('business_report'):
        if report.get('run_id'):protected.add(report['run_id'])

    # Retained results, in-flight frozen plans, and referenced snapshots keep the
    # complete transitive input chain. References can point beyond the scan page.
    retained={run['id'] for run in runs if run['status'] in ACTIVE or datetime.fromisoformat(run['created_at']).timestamp()>cutoff}
    pending=list(protected|retained);visited=set()
    while pending:
        ident=pending.pop()
        if ident in visited:continue
        visited.add(ident)
        try:run=by_id.get(ident) or get('run',ident)
        except KeyError:raise ValueError('结果依赖记录无法核对，未执行历史清理') from None
        if run.get('kind')=='data_workflow':
            upstream=references(run.get('workflow_document'))
            protected.update(upstream);pending.extend(upstream-visited)
    candidates=[]
    for run in runs:
        if run['id'] in protected or run['status'] in ACTIVE:continue
        if datetime.fromisoformat(run['created_at']).timestamp()>cutoff:continue
        folder=DATA/'runs'/run['id']
        if folder.exists():candidates.append({'run_id':run['id'],'folder':str(folder)})
    if apply:
        for value in candidates:
            shutil.rmtree(value['folder']);run=get('run',value['run_id']);run['artifact_removed']=True
            from .jobs import save
            save(run)
        record('maintenance.retention','workspace',{'removed':len(candidates),'days':days})
    return {'apply':apply,'candidates':candidates}

def retention(days=30,keep_success=20,apply=False):
    if days<0 or keep_success<1:raise ValueError('保留天数必须非负且至少保留一版成功结果')
    with guard(exclusive=apply,blocking=False):return _retention(days,keep_success,apply)

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='action',required=True)
    b=sub.add_parser('backup');b.add_argument('output')
    r=sub.add_parser('restore');r.add_argument('archive')
    c=sub.add_parser('cleanup');c.add_argument('--days',type=int,default=30);c.add_argument('--keep-success',type=int,default=20);c.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    result=backup(args.output) if args.action=='backup' else restore(args.archive) if args.action=='restore' else retention(args.days,args.keep_success,args.apply)
    print(json.dumps(result,ensure_ascii=False))
