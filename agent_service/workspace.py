"""A project-wide Git timeline, with encrypted state outside Git and forward-only restore.

Raw inputs, connector credentials, conversation transcripts and runtime warehouses
never enter this repository. They are immutable/versioned objects referenced by
checkpoints. Restores retain newer versions and conversations and are journalled.
"""
import hashlib,json,re,shutil,subprocess
from pathlib import Path
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from .locks import FileLock
from croniter import croniter
from sqlalchemy import select,update
from .config import DATA,TIMEZONE
from .store import get,put,patch,all_items,now,seal,unseal,transaction,items,uid
from .codegen import git
from .jobs import ACTIVE,jobs,get_job
from .audit import record
from .platform_files import assert_plain_path,fsync_directory,open_regular_read,atomic_replace,create_regular_exclusive
from .process_control import executable

PROJECT_KEYS=('name','source_id','pipeline_id')
PIPE_KEYS=('name','spec','commit','release_mode','runtime','source_id','source_ids','candidate_run','published_run','active_commit','active_runtime','status','cron','data_as_of','last_success','archived','strategy','schedule_error','application_spec','application_customized','pending_schedule','knowledge_basis')
SOURCE_KEYS=('name','kind','secret_config','revision','revision_id','content_hash','bytes','status','profile','proposed_spec','planner_mode','archived','error','error_code','warning','schema_diff','suggestions','goal')

def folder(pid):
    get('project',pid)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',pid):raise ValueError('项目标识无效')
    if __import__('os').getenv('ATLAS_LOCAL_WORKSPACE'):
        from .local_workspace import project_folder
        return project_folder(pid)
    return DATA/'workspaces'/pid

def path_for(pid,name):
    root=folder(pid);p=Path(name)
    if not name or len(name)>200 or p.is_absolute() or '\\' in name or any(x.startswith('.') or x in ('..','pipelines','project-state.json') for x in p.parts):raise ValueError('只能操作项目内普通文件，不能修改内部目录')
    if p.suffix.lower() not in ('.md','.txt','.csv','.json','.yaml','.yml','.sql','.py'):raise ValueError('只支持可版本化的文本文件')
    out=root/'files'/p
    if not out.resolve().is_relative_to((root/'files').resolve()) or any(x.is_symlink() for x in [out,*out.parents]):raise ValueError('不允许链接或越界路径')
    assert_plain_path(out)
    return out

def write_file(pid,name,content):
    from .locks import assert_owned
    assert_owned()
    if not isinstance(content,str) or len(content.encode())>256*1024:raise ValueError('单个文件不能超过256KB')
    if re.search(r'(?i)(?:postgres(?:ql)?|mysql)(?:\+\w+)?://[^\s]*:|\bsk-[A-Za-z0-9_-]{12,}|(?:password|api_key|authorization|connection_string)["\x27]?\s*[:=]',content):raise ValueError('连接凭证不能写入项目代码仓')
    target=path_for(pid,name)
    files=list_files(pid)
    if sum(f['bytes'] for f in files)+len(content.encode())>8*1024*1024:raise ValueError('项目文本工作区超过8MB限制')
    if __import__('os').getenv('ATLAS_LOCAL_WORKSPACE'):
        from .local_workspace import write_project_file
        return write_project_file(pid,name,content)
    target.parent.mkdir(parents=True,exist_ok=True)
    import os
    tmp=target.with_name('.'+target.name+'.'+uid()+'.tmp')
    try:
        with os.fdopen(create_regular_exclusive(tmp),'w',encoding='utf-8',newline='\n') as f:
            f.write(content);f.flush();os.fsync(f.fileno())
        atomic_replace(tmp,target)
        fsync_directory(target.parent)
    finally:tmp.unlink(missing_ok=True)
    return {'path':name,'bytes':len(content.encode())}

def read_file(pid,name):
    path=path_for(pid,name)
    if __import__('os').getenv('ATLAS_LOCAL_WORKSPACE'):
        from .local_workspace import read_project_file
        return read_project_file(pid,name)
    import os
    with os.fdopen(open_regular_read(path),'r',encoding='utf-8') as stream:
        return stream.read(256*1024)

def list_files(pid):
    if __import__('os').getenv('ATLAS_LOCAL_WORKSPACE'):
        from .local_workspace import list_project_files
        return list_project_files(pid)
    root=folder(pid)/'files'
    assert_plain_path(root)
    return [{'path':p.relative_to(root).as_posix(),'bytes':p.stat().st_size} for p in sorted(root.rglob('*')) if p.is_file() and not p.is_symlink()]

def git_content(repo,ref,name):
    return subprocess.run([executable('git'),'-C',str(repo),'show',ref+':'+name],capture_output=True,check=True,timeout=30).stdout


def state(pid):
    p=get('project',pid)
    return {'project':{k:p.get(k) for k in PROJECT_KEYS},'sources':{s['id']:{k:s[k] for k in SOURCE_KEYS if k in s} for s in all_items('source') if s.get('project_id')==pid},
            'pipelines':{p['id']:{k:p[k] for k in PIPE_KEYS if k in p} for p in all_items('pipeline') if p.get('project_id')==pid}}

def lock(pid):return FileLock(str(DATA/('workspace-'+pid+'.lock')),timeout=10)

def _init(pid):
    repo=folder(pid);repo.mkdir(parents=True,exist_ok=True)
    if not (repo/'.git').exists():
        git(repo,'init','-b','main');git(repo,'config','user.name','Atlas Project Agent');git(repo,'config','user.email','agent@localhost')
    return repo

def _mirror(repo,snapshot):
    # Git contains reproducible code and nonsecret references only.
    public={'project':snapshot['project'],'sources':{sid:{k:v.get(k) for k in ('name','kind','revision','revision_id','content_hash','archived')} for sid,v in snapshot['sources'].items()},'pipelines':snapshot['pipelines']}
    for sid,source in snapshot['sources'].items():
        public['sources'][sid]['status']=source.get('status')
        public['sources'][sid]['schema_hash']=hashlib.sha256(json.dumps((source.get('profile') or {}).get('fields',[]),sort_keys=True).encode()).hexdigest()
    (repo/'project-state.json').write_text(json.dumps(public,ensure_ascii=False,sort_keys=True,indent=2))
    target=repo/'pipelines'
    if target.exists():shutil.rmtree(target)
    for pid,p in snapshot['pipelines'].items():
        if not p.get('commit') or p.get('archived'):continue
        original=get('pipeline',pid)
        names=git(original['repository'],'ls-tree','-r','--name-only',p['commit']).splitlines()
        for name in names:
            if not (name in ('plan.json','README.md','knowledge-evidence.json','data-contract.json','column-lineage.json') or name.startswith(('dbt/','semantics/','connectors/','application/'))):continue
            rel=Path(name)
            if rel.is_absolute() or '..' in rel.parts:raise ValueError('代码路径无效')
            content=git_content(original['repository'],p['commit'],name)
            if len(content)>256*1024:raise ValueError('处理代码文件过大')
            out=target/pid/rel;out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(content)

def checkpoint(pid,turn_id=None,reason='工作变更'):
    with lock(pid):return _checkpoint(pid,turn_id,reason)

def _checkpoint(pid,turn_id,reason):
    repo=_init(pid)
    snapshot=state(pid);_mirror(repo,snapshot)
    try:before=git(repo,'rev-parse','HEAD')
    except Exception:before=None
    git(repo,'add','-A')
    changed=git(repo,'diff','--cached','--name-status').splitlines()
    if changed or not before:git(repo,'commit','-m',reason+(' ['+turn_id+']' if turn_id else ''))
    commit=git(repo,'rev-parse','HEAD')
    key='checkpoint-'+hashlib.sha256((pid+commit).encode()).hexdigest()[:40]
    try:get('checkpoint',key)
    except KeyError:
        put('checkpoint',{'project_id':pid,'commit':commit,'parent':before,'turn_id':turn_id,'reason':reason,'created_at':now(),'changes':[{'status':line.split('\t')[0],'path':line.split('\t')[-1]} for line in changed], 'secret_state':seal(snapshot)},key)
    return {'commit':commit,'parent':before,'changed':bool(changed),'changes':[{'status':line.split('\t')[0],'path':line.split('\t')[-1]} for line in changed]}

def history(pid,limit=100):
    get('project',pid)
    return [{k:v for k,v in c.items() if k!='secret_state'} for c in sorted((c for c in all_items('checkpoint') if c['project_id']==pid),key=lambda c:c['created_at'],reverse=True)[:limit]]

def resolve(pid,commit):
    if not re.fullmatch(r'[0-9a-f]{7,40}',commit):raise ValueError('请使用项目历史中的版本编号')
    matches=[c for c in all_items('checkpoint') if c['project_id']==pid and c['commit'].startswith(commit)]
    if len(matches)!=1:raise ValueError('项目版本不存在或编号不唯一')
    return matches[0]

def begin(turn):
    if turn.get('interaction_mode','work')!='work':return
    if not turn.get('before_version'):
        repo=_init(turn['project_id'])
        try:commit=git(repo,'rev-parse','HEAD')
        except Exception:commit=checkpoint(turn['project_id'],reason='项目初始版本')['commit']
        patch('turn',turn['id'],before_version=commit)

def finalize(turn):
    if turn.get('interaction_mode','work')!='work' or turn.get('work_version'):return
    version=turn.get('restored_version') or checkpoint(turn['project_id'],turn['id'],'工作轮次' if turn['state']=='done' else '未完成轮次的已有变更')
    patch('turn',turn['id'],work_version=version)

def restore(pid,commit,turn_id):
    target=resolve(pid,commit)
    with lock(pid),FileLock(str(DATA/'scheduler.lock'),timeout=10):
        # All objects and code are verified before altering any current pointer.
        snapshot=unseal(target['secret_state'])
        current_ids={p['id'] for p in all_items('pipeline') if p.get('project_id')==pid}|{s['id'] for s in all_items('source') if s.get('project_id')==pid}
        with transaction() as c:
            active=c.execute(select(jobs.c.id).where(jobs.c.target.in_(list(current_ids|set(snapshot['pipelines'])|set(snapshot['sources']))),jobs.c.status.in_(ACTIVE))).first()
            if active:raise ValueError('项目还有正在处理的任务，先停止或等它完成后再回退')
        for sid,source in snapshot['sources'].items():
            current=get('source',sid)
            if current.get('project_id')!=pid:raise ValueError('来源不属于项目')
            if source['kind']=='file':
                cfg=unseal(source['secret_config']);original=DATA/'uploads'/cfg['stored_name']
                if not original.exists() or (source.get('content_hash') and hashlib.sha256(original.read_bytes()).hexdigest()!=source['content_hash']):raise ValueError('历史原件已缺失或被修改，无法完整回退')
        from .releases import manifest_for
        for pipeline_id,p in snapshot['pipelines'].items():
            current=get('pipeline',pipeline_id)
            if current.get('project_id')!=pid:raise ValueError('任务不属于项目')
            git(current['repository'],'cat-file','-e',p['commit']+'^{commit}')
            for rid in (p.get('published_run'),p.get('candidate_run')):
                if rid:
                    run=get_job(rid);folder_,manifest=manifest_for(run)
                    if run['target']!=pipeline_id or hashlib.sha256((folder_/'business_objects.parquet').read_bytes()).hexdigest()!=manifest['parquet_hash']:raise ValueError('历史结果产物不可用，无法完整回退')
        journal_id='restore-'+pid
        journal={'project_id':pid,'commit':target['commit'],'turn_id':turn_id,'state':'applying','created_at':now()}
        put('restore',journal,journal_id)
        return _apply_restore(journal)

def _apply_restore(journal):
    pid=journal['project_id'];target=resolve(pid,journal['commit']);snapshot=unseal(target['secret_state'])
    # DB pointers update atomically. The journal is replayed after a crash before
    # file restoration/commit; new project turns are held until it completes.
    with transaction() as c:
        project=get('project',pid)
        rows=c.execute(select(items).where(items.c.kind.in_(('project','source','pipeline')))).mappings().all()
        for row in rows:
            value=json.loads(row['body']);kind=row['kind'];item_id=row['id']
            if kind=='project' and item_id==pid:value.update(snapshot['project'],pending_connection=None,updated_at=now())
            elif kind=='source' and value.get('project_id')==pid:
                if item_id in snapshot['sources']:
                    for key in SOURCE_KEYS:
                        if key in snapshot['sources'][item_id]:value[key]=snapshot['sources'][item_id][key]
                        else:value.pop(key,None)
                else:value['archived']=True
            elif kind=='pipeline' and value.get('project_id')==pid:
                if item_id in snapshot['pipelines']:
                    for key in PIPE_KEYS:
                        if key in snapshot['pipelines'][item_id]:value[key]=snapshot['pipelines'][item_id][key]
                        else:value.pop(key,None)
                    value['next_due']=croniter(value['cron'],datetime.now(ZoneInfo(TIMEZONE))).get_next(datetime).astimezone(timezone.utc).isoformat() if value.get('cron') else None
                else:value.update(archived=True,cron=None,next_due=None)
            else:continue
            c.execute(update(items).where(items.c.id==item_id).values(body=json.dumps(value,ensure_ascii=False)))
    repo=_init(pid)
    files=repo/'files'
    if files.exists():shutil.rmtree(files)
    names=git(repo,'ls-tree','-r','--name-only',target['commit']).splitlines()
    for name in names:
        if not name.startswith('files/'):continue
        out=path_for(pid,name[6:]);out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(git_content(repo,target['commit'],name))
    version=_checkpoint(pid,journal['turn_id'],'回退至 '+target['commit'][:12])
    patch('restore','restore-'+pid,state='done',version=version,finished_at=now())
    record('project.restore',pid,{'target':target['commit'],'commit':version['commit'],'turn_id':journal['turn_id']})
    return version

def recover():
    for journal in all_items('restore'):
        if journal['state']=='applying':
            with lock(journal['project_id']),FileLock(str(DATA/'scheduler.lock'),timeout=10):_apply_restore(journal)
