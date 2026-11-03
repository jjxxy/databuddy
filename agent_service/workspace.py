"""A project-wide Git timeline, with encrypted state outside Git and forward-only restore.

Raw inputs, connector credentials, conversation transcripts and runtime warehouses
never enter this repository. They are immutable/versioned objects referenced by
checkpoints. Restores retain newer versions and conversations and are journalled.
"""
import hashlib,json,os,re,shutil,subprocess
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
from .platform_files import native_path,assert_plain_path,fsync_directory,open_regular_read,atomic_replace,create_regular_exclusive
from .process_control import executable

PROJECT_KEYS=('name','source_id','pipeline_id')
PIPE_KEYS=('name','spec','commit','release_mode','runtime','source_id','source_ids','candidate_run','published_run','active_commit','active_runtime','status','cron','data_as_of','last_success','archived','strategy','schedule_error','application_spec','application_customized','pending_schedule','knowledge_basis','business_plan_confirmation_required')
SOURCE_KEYS=('name','kind','secret_config','revision','revision_id','content_hash','bytes','status','profile','proposed_spec','planner_mode','archived','error','error_code','warning','schema_diff','suggestions','goal')
FLOW_KEYS=('name','actor_id','turn_id','published_turn_id','path','candidate_digest','candidate_run','published_digest','published_commit','published_run','pending_refresh_run','created_at','updated_at','archived','cron','next_due','schedule_inputs','schedule_source_bindings','schedule_timezone','refresh_error')

def folder(pid):
    get('project',pid)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',pid):raise ValueError('项目标识无效')
    if __import__('os').getenv('NEXUS_LOCAL_WORKSPACE'):
        from .local_workspace import project_folder
        return native_path(project_folder(pid))
    return native_path(DATA/'workspaces'/pid)

def path_for(pid,name):
    root=folder(pid);p=Path(name)
    if not name or len(name)>200 or p.is_absolute() or '\\' in name or any(x.startswith('.') or x in ('..','pipelines','project-state.json') for x in p.parts):raise ValueError('只能操作项目内普通文件，不能修改内部目录')
    if any(':' in part or part.rstrip(' .')!=part or re.fullmatch(r'(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',part) for part in p.parts):
        raise ValueError('项目文件名不能包含设备名称、数据流或歧义字符')
    if p.suffix.lower() not in ('.md','.txt','.csv','.json','.yaml','.yml','.sql','.py',
                                '.js','.mjs','.cjs','.jsx','.ts','.tsx','.html','.htm','.css','.svg','.toml','.sh','.r','.ipynb'):
        raise ValueError('只支持可版本化的文本和代码文件')
    out=root/'files'/p
    if not out.resolve().is_relative_to((root/'files').resolve()) or any(x.is_symlink() for x in [out,*out.parents]):raise ValueError('不允许链接或越界路径')
    assert_plain_path(out)
    return out

def generated_flow_file(name):
    parts=[part.rstrip(' .') for part in Path(name).as_posix().casefold().split('/')]
    return len(parts)>=2 and bool(_FLOW_DIR.fullmatch('/'.join(parts[:2])))


def _editable(name):
    if generated_flow_file(name) or re.fullmatch(r'business-flows/bf_[a-f0-9]{40}(?:\.lock\.json|\.json|\.py)',Path(name).as_posix().casefold()):
        raise ValueError('此文件随流程版本生成，请通过对话修改对应流程')


_FLOW_DIR=re.compile(r'(?:data-flows/wf_|business-flows/bf_)[a-f0-9]{40}')


def _validate_text(content):
    if not isinstance(content,str) or len(content.encode())>256*1024:raise ValueError('单个文件不能超过256KB')
    if re.search(r'(?i)(?:postgres(?:ql)?|mysql)(?:\+\w+)?://[^\s]*:|\bsk-[A-Za-z0-9_-]{12,}|(?:password|api_key|authorization|connection_string)["\x27]?\s*[:=]',content):raise ValueError('连接凭证不能写入项目代码仓')


def write_file(pid,name,content):
    _editable(name);_validate_text(content)
    files=list_files(pid)
    if sum(f['bytes'] for f in files if f['path']!=name)+len(content.encode())>8*1024*1024:raise ValueError('项目文本工作区超过8MB限制')
    return _write_text(pid,name,content)


def _write_text(pid,name,content):
    from .locks import assert_owned
    assert_owned()
    target=path_for(pid,name)
    if __import__('os').getenv('NEXUS_LOCAL_WORKSPACE'):
        from .local_workspace import write_project_file
        return write_project_file(pid,name,content)
    target.parent.mkdir(parents=True,exist_ok=True)
    import os
    tmp=target.with_name('.'+uid()+'.tmp')
    try:
        with os.fdopen(create_regular_exclusive(tmp),'w',encoding='utf-8',newline='\n') as f:
            f.write(content);f.flush();os.fsync(f.fileno())
        atomic_replace(tmp,target)
        fsync_directory(target.parent)
    finally:tmp.unlink(missing_ok=True)
    return {'path':name,'bytes':len(content.encode())}


def _delete_text(pid,name):
    from .locks import assert_owned
    assert_owned()
    if __import__('os').getenv('NEXUS_LOCAL_WORKSPACE'):
        from .local_workspace import delete_project_file
        return delete_project_file(pid,name)
    path=path_for(pid,name)
    existed=path.exists();path.unlink(missing_ok=True)
    return {'deleted':name,'already_absent':not existed}


def delete_file(pid,name):
    _editable(name)
    return _delete_text(pid,name)


def write_flow_assets(pid,prefix,files,*,siblings=None):
    """Replace one generated file set. Callers serialize registration and checkpoints."""
    if not _FLOW_DIR.fullmatch(prefix) or not files:raise ValueError('流程文件目录无效')
    directory=prefix+'/'
    wanted={}
    for rel,value in files.items():
        if not re.fullmatch(r'(?:[a-zA-Z0-9_-]+/)*[a-zA-Z0-9_-]+\.(?:json|py|sql)',rel):
            raise ValueError('流程代码文件名无效')
        path=directory+rel;path_for(pid,path)
        content=value.decode('utf-8') if isinstance(value,bytes) else value
        _validate_text(content);wanted[path]=content
    adjacent=set()
    if siblings:
        if not prefix.startswith('business-flows/bf_') or set(siblings)!={'.json','.lock.json','.py'}:
            raise ValueError('流程入口文件无效')
        for suffix,content in siblings.items():
            path=prefix+suffix;path_for(pid,path);_validate_text(content)
            adjacent.add(path);wanted[path]=content
    if len({path.casefold() for path in wanted})!=len(wanted):raise ValueError('流程文件名冲突')
    entries=list_files(pid)
    outside=lambda p:not p.startswith(directory) and p not in adjacent
    external_bytes=sum(f['bytes'] for f in entries if outside(f['path']))
    if external_bytes+sum(len(content.encode()) for content in wanted.values())>8*1024*1024:
        raise ValueError('项目文本工作区超过8MB限制')
    previous={f['path']:read_file(pid,f['path']) for f in entries if not outside(f['path'])}
    changed=[]
    try:
        for path,content in wanted.items():
            if previous.get(path)!=content:
                changed.append(path);_write_text(pid,path,content)
        for path in previous.keys()-wanted.keys():
            changed.append(path);_delete_text(pid,path)
    except Exception:
        for path in reversed(changed):
            if path in previous:_write_text(pid,path,previous[path])
            elif path_for(pid,path).exists():_delete_text(pid,path)
        raise

def read_file(pid,name):
    path=path_for(pid,name)
    if __import__('os').getenv('NEXUS_LOCAL_WORKSPACE'):
        from .local_workspace import read_project_file
        return read_project_file(pid,name)
    import os
    with os.fdopen(open_regular_read(path),'r',encoding='utf-8') as stream:
        return stream.read(256*1024)

def list_files(pid):
    if __import__('os').getenv('NEXUS_LOCAL_WORKSPACE'):
        from .local_workspace import list_project_files
        return list_project_files(pid)
    root=folder(pid)/'files'
    assert_plain_path(root)
    return [{'path':p.relative_to(root).as_posix(),'bytes':p.stat().st_size} for p in sorted(root.rglob('*')) if p.is_file() and not p.is_symlink()]

def git_content(repo,ref,name):
    return subprocess.run([executable('git'),'-C',str(repo),'cat-file','blob',ref+':'+name],capture_output=True,check=True,timeout=30).stdout


def flow_blob(text):
    raw=text.encode('utf-8')
    return hashlib.sha1(b'blob '+str(len(raw)).encode('ascii')+b'\0'+raw).hexdigest()


def verify_flow_tree(repo,prefix,commit,expected):
    tree=git(repo,'ls-tree','-r','--full-tree',commit,'--',prefix)
    if expected is None:
        if tree:raise ValueError('流程代码目录缺少对应的版本锁')
        return
    actual={}
    for line in tree.splitlines():
        metadata,path=line.split('\t',1)
        mode,kind,oid=metadata.split()
        if mode!='100644' or kind!='blob' or not path.startswith(prefix):
            raise ValueError('流程代码目录包含无效文件')
        actual[path[len(prefix):]]=oid
    if actual!=expected:raise ValueError('项目节点代码或业务流程图与固定版本不一致')


def state(pid):
    p=get('project',pid)
    return {'project':{k:p.get(k) for k in PROJECT_KEYS},'sources':{s['id']:{k:s[k] for k in SOURCE_KEYS if k in s} for s in all_items('source') if s.get('project_id')==pid},
            'pipelines':{p['id']:{k:p[k] for k in PIPE_KEYS if k in p} for p in all_items('pipeline') if p.get('project_id')==pid},
            'business_flows':{f['id']:{k:f[k] for k in FLOW_KEYS if k in f} for f in all_items('business_flow') if f.get('project_id')==pid},
            # This stays inside encrypted checkpoint state. Without the full
            # durable record a restored draft could execute a newer formal
            # document or schedule while appearing to have been rolled back.
            'data_workflows':{w['id']:w for w in all_items('data_workflow') if w.get('project_id')==pid}}

def lock(pid):return FileLock(str(DATA/('workspace-'+pid+'.lock')),timeout=10)

def _init(pid):
    repo=folder(pid);repo.mkdir(parents=True,exist_ok=True)
    if not (repo/'.git').exists():
        git(repo,'-c','core.longpaths=true','init','-b','main');git(repo,'config','user.name','Nexus WorkStudio Project Agent');git(repo,'config','user.email','agent@localhost')
    if os.name=='nt':
        # This is the managed project repository, including ones created by
        # older releases. config --default also works when the key is absent.
        if git(repo,'config','--local','--type=bool','--default=false','--get','core.longpaths')!='true':
            git(repo,'config','--local','core.longpaths','true')
    return repo

def _mirror(repo,snapshot):
    # Git contains reproducible code and nonsecret references only.
    public={'project':snapshot['project'],'sources':{sid:{k:v.get(k) for k in ('name','kind','revision','revision_id','content_hash','archived')} for sid,v in snapshot['sources'].items()},'pipelines':snapshot['pipelines'],
            'business_flows':{fid:{k:v.get(k) for k in ('name','path','turn_id','published_turn_id','candidate_digest','candidate_run','published_digest','published_commit','published_run','cron','schedule_source_bindings','archived')} for fid,v in snapshot.get('business_flows',{}).items()}}
    # Commit a fingerprint, not the potentially sensitive workflow document,
    # so a state-only adoption/schedule change yields a distinct checkpoint.
    public['data_workflows']={wid:hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode('utf-8')).hexdigest()
                              for wid,value in snapshot.get('data_workflows',{}).items()}
    for fid,flow in snapshot.get('business_flows',{}).items():
        public['business_flows'][fid]['schedule_timezone']=flow.get('schedule_timezone')
        # The encrypted inputs remain outside Git but must affect checkpoint
        # identity when users change scheduling parameters.
        encrypted=flow.get('schedule_inputs')
        public['business_flows'][fid]['schedule_inputs_sha256']=hashlib.sha256(encrypted.encode()).hexdigest() if isinstance(encrypted,str) else None
    for sid,source in snapshot['sources'].items():
        public['sources'][sid]['status']=source.get('status')
        public['sources'][sid]['schema_hash']=hashlib.sha256(json.dumps((source.get('profile') or {}).get('fields',[]),sort_keys=True).encode()).hexdigest()
    # Windows' default text encoding can be GBK even though Git readers and
    # project exports use UTF-8. Pin both encoding and line endings so Chinese
    # project metadata round-trips identically on every deployment host.
    (repo/'project-state.json').write_text(json.dumps(public,ensure_ascii=False,sort_keys=True,indent=2),encoding='utf-8',newline='\n')
    target=repo/'pipelines'
    if target.exists():shutil.rmtree(target)
    for pid,p in snapshot['pipelines'].items():
        if not p.get('commit') or p.get('archived'):continue
        original=get('pipeline',pid)
        names=git(original['repository'],'ls-tree','-r','--name-only',p['commit']).splitlines()
        for name in names:
            if not (name in ('plan.json','business-plan.json','README.md','knowledge-evidence.json','data-contract.json','column-lineage.json') or name.startswith(('dbt/','semantics/','connectors/','application/'))):continue
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
    from .workspace_lifecycle import _project_lock
    # Match scheduled business admission's workspace -> scheduler ->
    # lifecycle order. No job may enter between the active scan and restore.
    with lock(pid),FileLock(str(DATA/'scheduler.lock'),timeout=10),FileLock(str(DATA/'data-workflow-scheduler.lock'),timeout=10),_project_lock(pid):
        # All objects and code are verified before altering any current pointer.
        snapshot=unseal(target['secret_state'])
        current_data={w['id']:w for w in all_items('data_workflow') if w.get('project_id')==pid}
        if 'data_workflows' not in snapshot and current_data:
            raise ValueError('这个历史版本未保存数据加工流程状态，无法安全回退；请先核对流程和正式结果')
        saved_data=snapshot.get('data_workflows',{})
        if not isinstance(saved_data,dict):raise ValueError('历史数据加工流程状态无效，未回退')
        current_ids={p['id'] for p in all_items('pipeline') if p.get('project_id')==pid}|{s['id'] for s in all_items('source') if s.get('project_id')==pid}|{f['id'] for f in all_items('business_flow') if f.get('project_id')==pid}|set(current_data)
        with transaction() as c:
            active=c.execute(select(jobs.c.id).where(jobs.c.target.in_(list(current_ids|set(snapshot['pipelines'])|set(snapshot['sources'])|set(snapshot.get('business_flows',{}))|set(saved_data))),jobs.c.status.in_(ACTIVE))).first()
            if active:raise ValueError('项目还有正在处理的任务，先停止或等它完成后再回退')
        from .project_data import WorkflowDocument,result_folder
        from .data_workflows import digest
        from .result_delivery import verify_handoff
        for wid,workflow in saved_data.items():
            if (not isinstance(workflow,dict) or workflow.get('id')!=wid or workflow.get('project_id')!=pid
                    or wid not in current_data):raise ValueError('历史数据加工流程身份不可核验，未回退')
            if workflow.get('active_document'):
                doc=WorkflowDocument.model_validate(workflow['active_document'])
                if workflow.get('active_sha256')!=digest(doc.model_dump()):raise ValueError('历史数据加工方案摘要不一致，未回退')
            for field in ('published_run','candidate_run'):
                rid=workflow.get(field)
                if rid:
                    job=get_job(rid)
                    if job.get('target')!=wid:raise ValueError('历史数据结果不属于当前流程')
                    result,meta=result_folder(pid,rid)
                    verify_handoff(result/'handoff',meta['handoff_sha256'])
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

def _pause_restored_schedule(value,notice_field):
    # Preserve all unresolved side-effect records: a rollback cannot undo an
    # external operation or count as confirmation of its outcome.
    if value.get('cron') or value.get('watch_sources'):
        value['schedule_resume_required']=True
        value[notice_field]=('项目已回退且存在外部操作状态未知的运行；请先核对回执再重新设置' if value.get('schedule_uncertain_run') or value.get('schedule_uncertain_runs') else
                             '项目已回退，自动更新已暂停；请核对方案、期间、时区和资料后重新设置')
        value['schedule_generation']=uid()
        value['refresh_retry_after']=None
        value['schedule_retry_after']=None


def _merge_unresolved_schedule(current,restored):
    pending=[]
    for state in (current,restored):
        entries=state.get('schedule_uncertain_runs')
        if isinstance(entries,list):
            for run_id in entries:
                if isinstance(run_id,str) and run_id and run_id not in pending:pending.append(run_id)
        run_id=state.get('schedule_uncertain_run')
        if isinstance(run_id,str) and run_id and run_id not in pending:pending.append(run_id)
    restored['schedule_uncertain_runs']=pending
    restored['schedule_uncertain_run']=pending[0] if pending else None
    # A missing-period warning is also a live operational fact, not something
    # a project version rollback can silently erase.
    if current.get('schedule_gap_pending') or restored.get('schedule_gap_pending'):
        restored['schedule_gap_pending']=True
        restored['schedule_resume_required']=True
        restored['refresh_error']='存在未核对的历史计划缺口；请核对期间及资料后重新设置自动更新'
    if pending:
        restored['schedule_resume_required']=True
        restored['refresh_error']='存在外部操作状态未知的运行；请逐条核对后重新设置自动更新'


def _apply_restore(journal):
    pid=journal['project_id'];target=resolve(pid,journal['commit']);snapshot=unseal(target['secret_state'])
    # DB pointers update atomically. The journal is replayed after a crash before
    # file restoration/commit; new project turns are held until it completes.
    with transaction() as c:
        project=get('project',pid)
        rows=c.execute(select(items).where(items.c.kind.in_(('project','source','pipeline','business_flow','data_workflow')))).mappings().all()
        for row in rows:
            value=json.loads(row['body']);kind=row['kind'];item_id=row['id']
            if kind=='project' and item_id==pid:
                value.update(snapshot['project'],pending_connection=None,updated_at=now())
                # Invalidate requests prepared against pre-restore versions.
                value['context_visibility_revision']=int(value.get('context_visibility_revision',0))+1
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
                    _pause_restored_schedule(value,'schedule_error')
                else:value.update(archived=True,cron=None,next_due=None)
            elif kind=='business_flow' and value.get('project_id')==pid:
                old=snapshot.get('business_flows',{}).get(item_id)
                if old is None:value.update(archived=True)
                else:
                    for key in FLOW_KEYS:
                        if key in old:value[key]=old[key]
                        else:value.pop(key,None)
                    _pause_restored_schedule(value,'refresh_error')
                    # The restored project commit itself contains the exact
                    # candidate document; no circular commit stored in Git.
                    value['candidate_commit']=target['commit']
            elif kind=='data_workflow' and value.get('project_id')==pid:
                old=snapshot.get('data_workflows',{}).get(item_id)
                if old is None:
                    value.update(archived=True,cron=None,watch_sources=False,next_due=None,schedule_resume_required=True,
                                 refresh_error='该数据加工流程不属于回退版本，已暂停')
                else:
                    previous=value
                    value=dict(old)
                    _pause_restored_schedule(value,'refresh_error')
                    _merge_unresolved_schedule(previous,value)
            else:continue
            c.execute(update(items).where(items.c.id==item_id).values(body=json.dumps(value,ensure_ascii=False)))
    repo=_init(pid)
    files=repo/'files'
    if files.exists():shutil.rmtree(files)
    names=git(repo,'ls-tree','-r','--name-only','-z',target['commit']).split('\0')
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
            # Multiple conversation slots can observe the same applying record
            # before any of them acquires the project lock.  Re-read only after
            # locking, otherwise a stale observation could delete files again
            # after another slot already finished recovery.
            with lock(journal['project_id']):
                try:current=get('restore',journal['id'])
                except KeyError:continue
                if current.get('state')!='applying' or current.get('project_id')!=journal['project_id']:continue
                from .workspace_lifecycle import _project_lock
                with FileLock(str(DATA/'scheduler.lock'),timeout=10),FileLock(str(DATA/'data-workflow-scheduler.lock'),timeout=10),_project_lock(current['project_id']):_apply_restore(current)
