import csv
from decimal import Decimal
from contextlib import asynccontextmanager
import hashlib
import io
import json
import os
import secrets
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import duckdb
import sqlglot
from sqlglot import exp
from croniter import croniter
from fastapi import FastAPI, Depends, File, Form, UploadFile, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field,ValidationError
from .config import DATA, TOKEN, MAX_UPLOAD, TIMEZONE, LLM_BASE, LLM_MODEL
from .store import get,put,all_items,delete,now,uid,seal,patch
from .models import SourceInput,PipelineInput,ScheduleInput,NoteInput,QueryInput,AskInput,SourceUpdate,ViewInput,PublishInput,ReleaseInput
from .connectors import validate_config,SourceError
from .codegen import generate
from .execution import enqueue
from .jobs import list_jobs,get_job,request_cancel,ACTIVE
from .releases import artifact_folder,manifest_for,publish
from .sources import revision
from .audit import record,list_events,verify
from .analysis import read_query,ask as answer_question
from .process_control import executable

@asynccontextmanager
async def lifespan(app):
    if len(TOKEN)<16:raise RuntimeError('APP_TOKEN 至少 16 字符。请运行 bootstrap.py 生成部署配置')
    from .conversation import initialize
    from .runtime import preflight
    from .identity import validate_config
    validate_config()
    preflight()
    initialize()
    import threading
    from .runtime import heartbeat
    stop=threading.Event()
    def beat():
        while not stop.is_set():
            try:heartbeat('api')
            except Exception:pass
            stop.wait(5)
    thread=threading.Thread(target=beat,daemon=True);thread.start()
    def media_reaper():
        from .media_tools import reconcile_calls
        from .maintenance import guard
        while not stop.wait(5):
            try:
                with guard():reconcile_calls()
            except Exception:pass
    media_thread=threading.Thread(target=media_reaper,daemon=True);media_thread.start()
    try:yield
    finally:stop.set();thread.join(timeout=1);media_thread.join(timeout=1)

app=FastAPI(title='个人数据 Agent',version='0.20.0',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)

class SourceReviewPublishInput(PublishInput):
    confirm_source_review:bool=Field(default=False,strict=True)
    expected_commit:str|None=Field(default=None,min_length=1,max_length=128)
    expected_parquet_sha256:str|None=Field(default=None,pattern=r'^[a-f0-9]{64}$')

async def auth(request:Request):
    from .identity import mode,principal
    from .actor_context import actor_from_identity,actor_scope
    if mode()=='oidc':
        from starlette.concurrency import run_in_threadpool
        value=await run_in_threadpool(principal,request)
    else:
        token=request.headers.get('Authorization','').removeprefix('Bearer ')
        if not TOKEN or not secrets.compare_digest(token.encode(),TOKEN.encode()):raise HTTPException(401,'请填写部署时生成的工作台访问口令')
        value={'authenticated':True,'mode':'token','scope':'owner_workspace'}
    request.state.identity=value
    from .workspace_lifecycle import request_scope
    with actor_scope(actor_from_identity(value)),request_scope():
        from .workspace_lifecycle import assert_request_live
        assert_request_live(request.url.path,request.method)
        yield value

def operator_auth(request:Request, identity=Depends(auth)):
    """Configuration needs a separate operator credential, never the UI token.

    An unset operator token intentionally leaves configuration file-only.
    OIDC business identities also need the separate operator credential.
    """
    expected=os.getenv('ATLAS_OPERATOR_TOKEN','')
    supplied=request.headers.get('X-Atlas-Operator-Token','')
    if len(expected)<16 or secrets.compare_digest(expected.encode(),TOKEN.encode()) or not secrets.compare_digest(supplied.encode(),expected.encode()):
        raise HTTPException(403,'此配置仅供后台管理员维护')
    return identity

_requests={}
@app.middleware('http')
async def boundary(request:Request,call_next):
    import time
    from collections import deque
    from contextlib import nullcontext
    from .maintenance import guard
    request_id=uid();ip=request.client.host if request.client else 'local'
    api_surface=request.url.path.startswith(('/api/','/a2a','/mcp/atlas'))
    key=(ip,api_surface)
    if key[1]:
        bucket=_requests.setdefault(key,deque())
        while bucket and bucket[0]<time.time()-60:bucket.popleft()
        if len(bucket)>=3000:return JSONResponse(status_code=429,content={'detail':'请求过于频繁，请稍后重试'},headers={'Retry-After':'60'})
        bucket.append(time.time())
    mutating=request.method in ('POST','PUT','PATCH','DELETE')
    if mutating and any(j['state']=='applying' for j in all_items('restore')):
        return JSONResponse(status_code=503,content={'detail':'项目回退正在恢复，请稍后重试'},headers={'Retry-After':'3'})
    try:
        with guard() if mutating else nullcontext():
            response=await call_next(request)
            response.headers['X-Request-ID']=request_id
            response.headers['X-Content-Type-Options']='nosniff'
            response.headers['Referrer-Policy']='no-referrer'
            response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"
            if api_surface:response.headers['Cache-Control']='no-store'
            if mutating and api_surface and response.status_code<400:record('api.'+request.method,request.url.path,{'request_id':request_id,'actor':hashlib.sha256(str(getattr(request.state,'identity',{}).get('subject','owner_token')).encode()).hexdigest()[:20]})
            return response
    except BlockingIOError:return JSONResponse(status_code=503,content={'detail':'工作空间正在备份或维护，请稍后重试'},headers={'Retry-After':'30'})

@app.exception_handler(KeyError)
def missing(request,exc):return JSONResponse(status_code=404,content={'detail':'资源不存在'})

@app.exception_handler(RequestValidationError)
def invalid_request(request,exc):
    # Pydantic errors include the original input, including SecretStr values
    # before parsing. Messages and locations can also contain dynamic input.
    return JSONResponse(status_code=422,content={'detail':'请求字段格式或取值不正确，请检查必填字段、数据类型和允许的范围'})

@app.exception_handler(ValidationError)
def invalid_model(request,exc):
    return JSONResponse(status_code=400,content={'detail':'配置或数据格式不正确，请检查输入'})

@app.exception_handler(ValueError)
def invalid(request,exc):return JSONResponse(status_code=400,content={'detail':str(exc)[:1000]})

@app.get('/api/platform',dependencies=[Depends(auth)])
def platform_status():
    from .runtime import readiness,nodes
    from .store import engine
    roles={r:nodes(r) for r in ('api','agent','worker')}
    counts={r:sum(n['healthy'] for n in values) for r,values in roles.items()}
    gaps=[]
    if engine.dialect.name!='postgresql':gaps.append('metadata_is_not_postgresql')
    if os.getenv('ATLAS_STORAGE_MODE')!='rwx':gaps.append('storage_is_not_shared_rwx')
    for role,count in counts.items():
        if count<2:gaps.append(role+'_has_fewer_than_two_healthy_nodes')
    if os.getenv('ATLAS_AUTH_MODE','token')!='oidc':gaps.append('oidc_not_enabled')
    try:backup_state=get('system','backup-health')
    except KeyError:backup_state={'ok':False}
    return {'backup':backup_state,'version':'0.20.0','readiness':readiness(),'metadata':engine.dialect.name,'healthy_nodes':counts,'nodes':roles,'ha_configuration_gaps':gaps,'high_availability_verified':False,'scope':'owner_workspace','note':'配置检查不能证明故障切换或SLO，需执行故障验收'}

@app.get('/api/identity')
def identity(value=Depends(auth)):return value

@app.get('/ready')
def ready():
    from .runtime import readiness
    value=readiness()
    return JSONResponse(status_code=200 if value['ready'] else 503,content=value)

@app.get('/health')
def health():return {'status':'ok'}

@app.get('/api/status',dependencies=[Depends(auth)])
def status():
    try:
        worker=get('system','worker-health');worker['age_seconds']=(datetime.now(timezone.utc)-datetime.fromisoformat(worker['heartbeat'])).total_seconds();worker['healthy']=worker['age_seconds']<20
    except KeyError:worker={'healthy':False,'age_seconds':None}
    try:
        agent=get('system','agent-health');agent['age_seconds']=(datetime.now(timezone.utc)-datetime.fromisoformat(agent['heartbeat'])).total_seconds();agent['healthy']=agent['age_seconds']<20
    except KeyError:agent={'healthy':os.getenv('AGENT_ORCHESTRATION_MODE')!='separate','mode':'inline' if os.getenv('AGENT_ORCHESTRATION_MODE')!='separate' else 'separate'}
    from .runtime import healthy,nodes
    worker.update(healthy=healthy('worker'),nodes=nodes('worker'))
    if os.getenv('AGENT_ORCHESTRATION_MODE')=='separate':agent.update(healthy=healthy('agent'),nodes=nodes('agent'))
    recent=list_jobs(100)
    return {'version':'0.20.0','planner_mode':'llm' if model_status()['configured'] else 'rules','multimodal':os.getenv('USE_DOCLING','0')=='1','timezone':TIMEZONE,'scheduler':os.getenv('SCHEDULER_MODE','worker'),'sources':len(all_items('source')),'pipelines':len(all_items('pipeline')),'worker':worker,'agent':agent,'code_execution':__import__('agent_service.isolation',fromlist=['status']).status(),'attention':sum(r['status']=='failed' for r in {r['target']:next(x for x in recent if x['target']==r['target']) for r in recent}.values()),'waiting_release':sum(bool(p.get('candidate_run')) for p in all_items('pipeline'))}

def public_source(s):return {k:v for k,v in s.items() if k!='secret_config'}
def public_run(r):return {k:v for k,v in r.items() if k not in ('source_snapshot','lease_token','secret_report_snapshot')}

def browser_scalar(value):
    # JavaScript cannot represent integers beyond 2**53-1. Preserve identifiers
    # and exact financial values before JSON serialization, not after parsing.
    if isinstance(value,Decimal) or (isinstance(value,int) and not isinstance(value,bool) and abs(value)>9007199254740991):return str(value)
    return value

@app.get('/api/sources',dependencies=[Depends(auth)])
def sources():
    from .workspace_lifecycle import assert_request_live,WorkspaceDeleted
    visible=[]
    for source in all_items('source'):
        try:assert_request_live('/api/sources/'+source['id'],'GET')
        except (KeyError,WorkspaceDeleted):continue
        visible.append(public_source(source))
    return visible

@app.post('/api/sources',dependencies=[Depends(auth)])
def create_source(value:SourceInput):
    try:validate_config(value.kind,value.config)
    except Exception:raise HTTPException(400,'连接配置不完整或格式不正确，请检查地址、表名、分页配置和只读确认')
    source=put('source',{'name':value.name,'kind':value.kind,'secret_config':seal(value.config),'goal':value.goal,'created_at':now(),'status':'queued'})
    enqueue('probe',source['id'])
    revision(source)
    return public_source(get('source',source['id']))

async def receive_file(file):
    suffix=Path(file.filename or '').suffix.lower()
    if suffix not in ('.csv','.xlsx','.json','.jsonl','.pdf','.docx','.pptx','.png','.jpg','.jpeg','.tiff','.webp','.html','.htm','.txt','.md'):raise HTTPException(400,'不支持的文件格式')
    folder=DATA/'uploads';folder.mkdir(exist_ok=True);stored=uid()+suffix;path=folder/stored
    size=0;hasher=hashlib.sha256()
    try:
        with path.open('xb') as out:
            while chunk:=await file.read(1024*1024):
                size+=len(chunk)
                if size>MAX_UPLOAD:raise HTTPException(413,'文件超过上传体积限制')
                hasher.update(chunk);out.write(chunk)
        if not size:raise HTTPException(400,'文件为空')
        if suffix in ('.xlsx','.docx','.pptx'):
            with zipfile.ZipFile(path) as z:
                if sum(x.file_size for x in z.infolist())>200*1024*1024:raise HTTPException(400,'Office 解压体积超过限制')
    except zipfile.BadZipFile:
        path.unlink(missing_ok=True);raise HTTPException(400,'Office 文件格式损坏')
    except BaseException:
        path.unlink(missing_ok=True);raise
    return {'stored_name':stored,'name':Path(file.filename).name[:120],'bytes':size,'content_hash':hasher.hexdigest()}

@app.post('/api/upload',dependencies=[Depends(auth)])
async def upload(file:UploadFile=File(...),goal:str=Form('')):
    if len(goal)>2000:raise HTTPException(400,'分析目标过长')
    info=await receive_file(file)
    duplicate=next((s for s in all_items('source') if s.get('content_hash')==info['content_hash'] and s['name'].lower().endswith(Path(info['name']).suffix.lower()) and not s.get('archived')),None)
    if duplicate:
        (DATA/'uploads'/info['stored_name']).unlink();return dict(public_source(duplicate),duplicate=True)
    source=put('source',{'name':info['name'],'kind':'file','secret_config':seal({'stored_name':info['stored_name']}),'content_hash':info['content_hash'],'bytes':info['bytes'],'goal':goal,'created_at':now(),'status':'queued'})
    revision(source);enqueue('probe',source['id'])
    return public_source(get('source',source['id']))

@app.post('/api/sources/{source_id}/replace-file',dependencies=[Depends(auth)])
async def replace_file(source_id:str,file:UploadFile=File(...)):
    s=get('source',source_id)
    if s['kind']!='file':raise HTTPException(400,'只有文件来源可替换文件')
    if any(r['status'] in ACTIVE for r in list_jobs(target=source_id)):raise HTTPException(409,'请等待探查结束')
    info=await receive_file(file)
    s=patch('source',source_id,name=info['name'],secret_config=seal({'stored_name':info['stored_name']}),content_hash=info['content_hash'],bytes=info['bytes'],status='queued')
    revision(s);enqueue('probe',source_id)
    return public_source(get('source',source_id))

@app.post('/api/sources/{source_id}/probe',dependencies=[Depends(auth)])
def probe(source_id:str):
    s=get('source',source_id);s['status']='queued';s.pop('error',None);put('source',s)
    return public_run(enqueue('probe',source_id))

@app.post('/api/sources/{source_id}/sheet',dependencies=[Depends(auth)])
def choose_sheet(source_id:str,payload:dict):
    from .store import unseal
    s=get('source',source_id)
    if s['kind']!='file' or not s['name'].lower().endswith('.xlsx'):raise HTTPException(400,'只有 Excel 支持工作表选择')
    sheets=s.get('profile',{}).get('evidence',{}).get('sheets',[])
    if payload.get('sheet') not in sheets:raise HTTPException(400,'工作表不存在')
    cfg=unseal(s['secret_config']);cfg['sheet']=payload['sheet'];s.update(secret_config=seal(cfg),status='queued');put('source',s);revision(s)
    return public_run(enqueue('probe',source_id))

@app.get('/api/pipelines',dependencies=[Depends(auth)])
def pipelines():
    from .workspace_lifecycle import assert_request_live,WorkspaceDeleted
    results=[]
    for p in all_items('pipeline'):
        try:assert_request_live('/api/pipelines/'+p['id'],'GET')
        except (KeyError,WorkspaceDeleted):continue
        as_of=p.get('data_as_of') or p.get('last_success')
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(as_of)).total_seconds()/60 if as_of else None
        threshold=(p.get('active_runtime') or p.get('runtime') or {}).get('freshness_minutes',1440)
        results.append(dict(p,freshness={'age_minutes':round(age,1) if age is not None else None,'threshold_minutes':threshold,'stale':age is None or age>threshold}))
    return results

@app.post('/api/pipelines',dependencies=[Depends(auth)])
def pipeline(value:PipelineInput):
    from .workspace_lifecycle import assert_request_live
    for source_id in {value.source_id,*[join.source_id for join in value.spec.joins]}:
        assert_request_live('/api/sources/'+source_id,'POST')
    s=get('source',value.source_id)
    if s.get('status')!='ready' or s.get('archived'):raise HTTPException(409,'先等待数据探查成功')
    from .company_search import basis_from_ids
    basis=basis_from_ids(s.get('project_id'),value.knowledge_evidence_ids)
    p=generate(s,value.name,value.spec.model_dump(),release_mode=value.release_mode,runtime=value.runtime.model_dump(),application=value.application.model_dump() if value.application else None,knowledge_basis=basis)
    enqueue('pipeline',p['id'],p['commit'])
    return p

@app.put('/api/pipelines/{pipeline_id}',dependencies=[Depends(auth)])
def revise_pipeline(pipeline_id:str,value:PipelineInput):
    from .workspace_lifecycle import assert_request_live
    for source_id in {value.source_id,*[join.source_id for join in value.spec.joins]}:
        assert_request_live('/api/sources/'+source_id,'PUT')
    existing=get('pipeline',pipeline_id)
    if value.source_id!=existing['source_id']:raise HTTPException(400,'调整任务时保留原来源；不同来源请创建新任务')
    if any(r['status'] in ACTIVE for r in list_jobs(target=pipeline_id)):raise HTTPException(409,'请等待当前执行或重试结束后调整任务')
    source=get('source',value.source_id)
    if source.get('status')!='ready':raise HTTPException(409,'先等待数据探查成功')
    from .company_search import basis_from_ids
    basis=basis_from_ids(source.get('project_id'),value.knowledge_evidence_ids or [v['evidence_id'] for v in existing.get('knowledge_basis',[])])
    p=generate(source,value.name,value.spec.model_dump(),existing=existing,release_mode=value.release_mode,runtime=value.runtime.model_dump(),application=value.application.model_dump() if value.application else None,knowledge_basis=basis)
    enqueue('pipeline',pipeline_id,p['commit'])
    return p

@app.post('/api/pipelines/{pipeline_id}/run',dependencies=[Depends(auth)])
def run_pipeline(pipeline_id:str):
    p=get('pipeline',pipeline_id)
    if p.get('archived'):raise HTTPException(409,'任务已归档')
    active=[r for r in list_jobs(target=pipeline_id) if r['status'] in ACTIVE]
    if active:raise HTTPException(409,'该任务正在等待或执行')
    return public_run(enqueue('pipeline',pipeline_id,p['commit']))

def _schedule(pipeline_id:str,value:ScheduleInput):
    p=get('pipeline',pipeline_id)
    if value.cron:
        if not croniter.is_valid(value.cron) or len(value.cron.split())!=5:raise HTTPException(400,'需要有效的五段 cron，例如 0 9 * * *')
        p['next_due']=croniter(value.cron,datetime.now(ZoneInfo(TIMEZONE))).get_next(datetime).astimezone(timezone.utc).isoformat()
    else:p.pop('next_due',None)
    return patch('pipeline',pipeline_id,cron=value.cron,next_due=p.get('next_due'),pending_schedule=None)

@app.post('/api/pipelines/{pipeline_id}/schedule',dependencies=[Depends(auth)])
def schedule(pipeline_id:str,value:ScheduleInput):
    from .locks import FileLock
    with FileLock(str(DATA/'scheduler.lock'),timeout=5):return _schedule(pipeline_id,value)

@app.get('/api/runs',dependencies=[Depends(auth)])
def runs(limit:int=100,offset:int=0,target:str|None=None,status:str|None=None):
    if limit<1 or limit>200 or offset<0:raise HTTPException(400,'分页参数超出范围')
    from .workspace_lifecycle import assert_request_live,WorkspaceDeleted
    visible=[];cursor=0;skipped=0
    while len(visible)<limit:
        page=list_jobs(200,cursor,target,status)
        for run in page:
            try:assert_request_live('/api/runs/'+run['id'],'GET')
            except (KeyError,WorkspaceDeleted):continue
            if skipped<offset:skipped+=1;continue
            visible.append(public_run(run))
            if len(visible)==limit:break
        if len(page)<200:break
        cursor+=len(page)
    return visible

def published(pipeline_id):
    p=get('pipeline',pipeline_id)
    if not p.get('published_run'):raise HTTPException(409,'该任务还没有通过测试的发布结果')
    folder=artifact_folder(get_job(p['published_run']))
    return p,folder,json.loads((folder/'output.json').read_text(encoding='utf-8'))

@app.get('/api/pipelines/{pipeline_id}/result',dependencies=[Depends(auth)])
def result(pipeline_id:str,offset:int=0,limit:int=50,search:str="",sort:str="object_id",descending:bool=False):
    if offset<0 or limit<1 or limit>200:raise HTTPException(400,'分页参数超出范围')
    p,folder,manifest=published(pipeline_id)
    allowed={'object_id'}|{f['name'] for f in manifest['fields']}
    if sort not in allowed:raise HTTPException(400,'排序字段不存在')
    if len(search)>200:raise HTTPException(400,'搜索文字过长')
    where='';params=[]
    if search:
        where=' where '+' or '.join('cast("'+f['name']+'" as varchar) ilike ?' for f in manifest['fields'])
        params=['%'+search+'%']*len(manifest['fields'])
    with duckdb.connect(str(folder/'warehouse.duckdb'),read_only=True,config={'enable_external_access':False}) as con:
        total=con.execute('select count(*) from business.business_objects'+where,params).fetchone()[0]
        cur=con.execute('select * from business.business_objects'+where+' order by "'+sort+'" '+('desc' if descending else 'asc')+', object_id limit ? offset ?',params+[limit,offset])
        rows=[dict(zip([x[0] for x in cur.description],[browser_scalar(v) for v in row])) for row in cur.fetchall()]
    notes={n['object_id']:n for n in all_items('note') if n['pipeline_id']==pipeline_id}
    return {'manifest':manifest,'rows':rows,'notes':notes,'offset':offset,'limit':limit,'total':total}

@app.put('/api/pipelines/{pipeline_id}/objects/{object_id}/note',dependencies=[Depends(auth)])
def note(pipeline_id:str,object_id:str,value:NoteInput):
    _,folder,_=published(pipeline_id)
    with duckdb.connect(str(folder/'warehouse.duckdb'),read_only=True,config={'enable_external_access':False}) as con:
        if not con.execute('select 1 from business.business_objects where object_id=? limit 1',[object_id]).fetchone():raise HTTPException(404,'业务记录不存在')
    key=hashlib.sha256((pipeline_id+':'+object_id).encode()).hexdigest()
    return put('note',{'pipeline_id':pipeline_id,'object_id':object_id,'note':value.note,'tags':value.tags,'updated_at':now()},key)

@app.get('/api/pipelines/{pipeline_id}/export',dependencies=[Depends(auth)])
def export(pipeline_id:str,run_id:str|None=None):
    if run_id:
        from .releases import manifest_for
        run=get_job(run_id)
        if run['target']!=pipeline_id or not run.get('validated') or run.get('artifact_removed') or run['status'] not in ('validated','succeeded'):raise HTTPException(400,'该数据版本不属于此应用或已不可用')
        folder,manifest=manifest_for(run)
    else:
        pipeline,folder,manifest=published(pipeline_id)
        run=get_job(pipeline['published_run'])
    from .releases import ensure_candidate_bulk_allowed
    ensure_candidate_bulk_allowed(run,manifest)
    labels={x['name']:x['label'] for x in manifest['fields']}
    def records():
        with duckdb.connect(str(folder/'warehouse.duckdb'),read_only=True,config={'enable_external_access':False}) as con:
            cursor=con.execute('select * from business.business_objects order by object_id')
            columns=[x[0] for x in cursor.description if not x[0].startswith('_dlt')]
            buf=io.StringIO();writer=csv.writer(buf)
            writer.writerow([labels.get(x,x) for x in columns]);yield '\ufeff'+buf.getvalue();buf.seek(0);buf.truncate(0)
            names=[x[0] for x in cursor.description]
            for row in iter(lambda:cursor.fetchone(),None):
                record=dict(zip(names,row))
                # CSV formula injection prevention for data exported to office applications.
                writer.writerow([("'"+str(record[x])) if isinstance(record[x],str) and record[x].startswith(('=','+','-','@','\t','\r')) else record[x] for x in columns])
                yield buf.getvalue();buf.seek(0);buf.truncate(0)
    return StreamingResponse(records(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="business_objects.csv"'})

def code_version(p, requested):
    if requested is None:return p['commit']
    allowed={p['commit']} | {r.get('commit') for r in all_items('run') if r['target']==p['id']}
    if requested not in allowed:raise HTTPException(400,'该代码版本不属于此任务的运行历史')
    return requested

@app.get('/api/pipelines/{pipeline_id}/code',dependencies=[Depends(auth)])
def code(pipeline_id:str,commit:str|None=None):
    from .codegen import git
    p=get('pipeline',pipeline_id);repo=Path(p['repository']);version=code_version(p,commit)
    files=git(repo,'ls-tree','-r','--name-only',version).splitlines()
    return {'commit':version,'repository':str(repo),'files':[{'path':name,'content':git(repo,'show',version+':'+name)} for name in files]}

@app.get('/api/pipelines/{pipeline_id}/code.zip',dependencies=[Depends(auth)])
def code_zip(pipeline_id:str,commit:str|None=None):
    import subprocess
    p=get('pipeline',pipeline_id);version=code_version(p,commit)
    data=subprocess.run([executable('git'),'-C',p['repository'],'archive','--format=zip',version],capture_output=True,check=True,timeout=30).stdout
    return StreamingResponse(iter([data]),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="generated-code.zip"'})

@app.get('/api/sources/{source_id}/original',dependencies=[Depends(auth)])
def original(source_id:str,revision:int|None=None):
    from .store import unseal
    s=get('source',source_id)
    if s['kind']!='file':raise HTTPException(400,'只有上传文件保留原件')
    if revision is not None:
        versions=[v for v in all_items('source_version') if v['source_id']==source_id and v['revision']==revision]
        if not versions:raise HTTPException(404,'来源版本不存在')
        s=versions[0]
    path=DATA/'uploads'/unseal(s['secret_config'])['stored_name']
    return FileResponse(path,filename=s['name'],media_type='application/octet-stream')

@app.post('/api/pipelines/{pipeline_id}/query',dependencies=[Depends(auth)])
def query(pipeline_id:str,value:QueryInput):
    _,folder,manifest=published(pipeline_id)
    try:r=read_query(folder,value.sql)
    except ValueError:raise
    except Exception:raise HTTPException(400,'查询失败，请检查字段、表名及查询复杂度')
    return dict(r,run_id=manifest['run_id'],commit=manifest['commit'])

@app.post('/api/pipelines/{pipeline_id}/ask',dependencies=[Depends(auth)])
def ask(pipeline_id:str,value:AskInput):
    _,folder,m=published(pipeline_id)
    from .provider import project_scope
    with project_scope(get('pipeline',pipeline_id).get('project_id')):return dict(answer_question(folder,m,value.question),run_id=m['run_id'],commit=m['commit'])

@app.get('/api/memory',dependencies=[Depends(auth)])
def memory():raise HTTPException(410,'旧全局记忆接口已停用，请使用项目内的个人/项目记忆')

@app.post('/api/memory',dependencies=[Depends(auth)])
def save_memory(value:NoteInput):raise HTTPException(410,'旧全局记忆接口已停用，请使用项目内的个人/项目记忆')

@app.delete('/api/memory/{memory_id}',dependencies=[Depends(auth)])
def remove_memory(memory_id:str):raise HTTPException(410,'旧全局记忆接口已停用，请使用项目内的个人/项目记忆')

@app.patch('/api/sources/{source_id}',dependencies=[Depends(auth)])
def update_source(source_id:str,value:SourceUpdate):
    s=get('source',source_id)
    if any(r['target']==source_id and r['status'] in ACTIVE for r in list_jobs(target=source_id)):raise HTTPException(409,'等待来源探查结束后再修改')
    values={}
    if value.name is not None:values['name']=value.name
    if value.archived is not None:values['archived']=value.archived
    if value.config is not None:
        if s['kind']=='file':raise HTTPException(400,'文件来源请使用替换文件操作')
        try:validate_config(s['kind'],value.config)
        except Exception:raise HTTPException(400,'连接配置不完整或格式不正确，请检查地址、表名、分页配置和只读确认') from None
        values.update(secret_config=seal(value.config),status='queued')
    updated=patch('source',source_id,**values);revision(updated)
    if value.config is not None:enqueue('probe',source_id)
    return public_source(get('source',source_id))

@app.post('/api/pipelines/{pipeline_id}/publish',dependencies=[Depends(auth)])
def release(pipeline_id:str,value:SourceReviewPublishInput,request:Request):
    from .actor_context import actor_from_identity
    return publish(pipeline_id,value.run_id,confirm_source_review=value.confirm_source_review,
                   expected_commit=value.expected_commit,expected_parquet_sha256=value.expected_parquet_sha256,
                   review_actor=actor_from_identity(request.state.identity))

@app.post('/api/pipelines/{pipeline_id}/rollback',dependencies=[Depends(auth)])
def rollback(pipeline_id:str,value:SourceReviewPublishInput,request:Request):
    from .actor_context import actor_from_identity
    return publish(pipeline_id,value.run_id,rollback=True,confirm_source_review=value.confirm_source_review,
                   expected_commit=value.expected_commit,expected_parquet_sha256=value.expected_parquet_sha256,
                   review_actor=actor_from_identity(request.state.identity))

@app.get('/api/pipelines/{pipeline_id}/preview',dependencies=[Depends(auth)])
def preview(pipeline_id:str):
    p=get('pipeline',pipeline_id)
    if not p.get('candidate_run'):raise HTTPException(409,'还没有验证通过的候选结果')
    run=get_job(p['candidate_run']);folder,m=manifest_for(run)
    with duckdb.connect(str(folder/'warehouse.duckdb'),read_only=True,config={'enable_external_access':False}) as con:
        cur=con.execute('select * from business.business_objects order by object_id limit 25')
        rows=[dict(zip([x[0] for x in cur.description],[browser_scalar(v) for v in r])) for r in cur.fetchall()]
    from .releases import _source_review_details,_requires_fact_alignment
    needs_review,_=_source_review_details(run,m)
    return {'manifest':m,'rows':rows,'run_id':run['id'],'requires_source_review':needs_review,
            'fact_semantic_alignment_required':_requires_fact_alignment(run,m)}

@app.patch('/api/pipelines/{pipeline_id}/release-mode',dependencies=[Depends(auth)])
def release_mode(pipeline_id:str,value:ReleaseInput):return patch('pipeline',pipeline_id,release_mode=value.mode)

@app.post('/api/pipelines/{pipeline_id}/archive',dependencies=[Depends(auth)])
def archive_pipeline(pipeline_id:str):return patch('pipeline',pipeline_id,archived=True,cron=None)

@app.get('/api/runs/{run_id}',dependencies=[Depends(auth)])
def run_detail(run_id:str):
    r=get_job(run_id);folder=artifact_folder(r)
    result={'run':public_run(r)}
    if (folder/'input.json').exists():result['inputs']=json.loads((folder/'input.json').read_text(encoding='utf-8'))
    if (folder/'output.json').exists():result['manifest']=json.loads((folder/'output.json').read_text(encoding='utf-8'))
    return result

@app.post('/api/runs/{run_id}/cancel',dependencies=[Depends(auth)])
def cancel(run_id:str):
    r=get_job(run_id)
    if r['status'] not in ACTIVE:raise HTTPException(409,'任务已经结束')
    return public_run(request_cancel(run_id))

@app.post('/api/runs/{run_id}/retry',dependencies=[Depends(auth)])
def retry(run_id:str):
    r=get_job(run_id)
    if r['status'] not in ('failed','cancelled'):raise HTTPException(409,'仅重试失败或取消的任务')
    return public_run(enqueue(r['kind'],r['target'],r.get('commit')))

@app.get('/api/views',dependencies=[Depends(auth)])
def views():
    from .workspace_lifecycle import assert_request_live,WorkspaceDeleted
    visible=[]
    for view in all_items('view'):
        try:assert_request_live('/api/views/'+view['id'],'GET')
        except (KeyError,WorkspaceDeleted):continue
        visible.append(view)
    return visible

@app.post('/api/views',dependencies=[Depends(auth)])
def save_view(value:ViewInput):
    from .workspace_lifecycle import assert_request_live
    assert_request_live('/api/pipelines/'+value.pipeline_id,'POST')
    _,folder,m=published(value.pipeline_id)
    read_query(folder,value.sql)
    return put('view',dict(value.model_dump(),created_at=now(),created_commit=m['commit']))

@app.post('/api/views/{view_id}/run',dependencies=[Depends(auth)])
def execute_view(view_id:str):
    v=get('view',view_id);_,folder,m=published(v['pipeline_id'])
    try:r=read_query(folder,v['sql'])
    except Exception:raise HTTPException(409,'数据版本或字段发生变化，修改保存的分析后再运行')
    return dict(r,name=v['name'],chart=v['chart'],run_id=m['run_id'],commit=m['commit'])

@app.delete('/api/views/{view_id}',dependencies=[Depends(auth)])
def remove_view(view_id:str):delete('view',view_id);return {'ok':True}

@app.get('/api/audit',dependencies=[Depends(auth)])
def audits(limit:int=100,offset:int=0):return list_events(max(1,min(limit,200)),max(offset,0))

@app.get('/api/audit/verify',dependencies=[Depends(auth)])
def audit_verify():return verify()

@app.get('/api/pipelines/{pipeline_id}/lineage',dependencies=[Depends(auth)])
def lineage(pipeline_id:str):
    p=get('pipeline',pipeline_id)
    result={'pipeline_id':pipeline_id,'active_commit':p.get('active_commit'),'candidate_commit':p['commit'],'source_ids':p.get('source_ids',[p['source_id']]),'saved_views':[v['id'] for v in all_items('view') if v['pipeline_id']==pipeline_id]}
    if p.get('published_run'):
        _,folder,m=published(pipeline_id)
        # A newer candidate may change joins before it is published. Every
        # source drawn beside the formal result must come from that frozen run.
        inputs=m.get('inputs',[])
        result.update(source_ids=list(dict.fromkeys(entry['source_id'] for entry in inputs)),inputs=inputs,relationships=m.get('relationships',[]),fields=m['fields'],quality=m.get('quality',[]),rules={'filters':m['spec'].get('filters',[]),'computations':m['spec'].get('computations',[])},run_id=m['run_id'])
    return result

# The normal product surface is projects + conversation; technical services
# remain authenticated APIs for the Agent, integrations and maintenance.
from .conversation import ProjectInput,TurnInput,SessionInput

@app.get('/api/projects',dependencies=[Depends(auth)])
def projects():
    from .conversation import initialize
    from .workspace_lifecycle import public_project
    initialize()
    return sorted([public_project(p) for p in all_items('project') if not p.get('deleted_at')],key=lambda p:p.get('updated_at',p['created_at']),reverse=True)

@app.post('/api/projects',dependencies=[Depends(auth)])
def new_project(value:ProjectInput):
    from .conversation import create_project
    return create_project(value)

@app.get('/api/projects/{project_id}/conversation',dependencies=[Depends(auth)])
def conversation(project_id:str,session_id:str|None=None,before:str|None=None,limit:int=150):
    from .conversation import thread
    return thread(project_id,session_id,before,limit)

@app.post('/api/projects/{project_id}/messages',dependencies=[Depends(auth)])
def message(project_id:str,value:TurnInput):
    from .conversation import submit
    return submit(project_id,value)

@app.post('/api/projects/{project_id}/attachments',dependencies=[Depends(auth)])
async def attachment(project_id:str,file:UploadFile=File(...),request_id:str=Form(...),local_processing:bool=Form(False)):
    from .conversation import stable
    from .locks import FileLock
    import re
    get('project',project_id)
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',request_id):raise HTTPException(400,'上传标识无效')
    sid=stable('s_',project_id,request_id)
    # Save outside the critical section so a slow upload cannot lock a conversation.
    info=await receive_file(file)
    with FileLock(str(DATA/('attachment-'+sid+'.lock')),timeout=5):
        try:
            source=get('source',sid)
            if source.get('content_hash')!=info['content_hash']:raise HTTPException(409,'同一上传标识不能更换文件')
        except KeyError:
            source=put('source',{'name':info['name'],'kind':'file','secret_config':seal({'stored_name':info['stored_name']}),'content_hash':info['content_hash'],'bytes':info['bytes'],'goal':'','created_at':now(),'status':'queued','project_id':project_id,**({'skip_model_planning':True} if local_processing else {})},sid)
            revision(source)
        finally:
            from .store import unseal
            try:
                if unseal(get('source',sid)['secret_config'])['stored_name']!=info['stored_name']:(DATA/'uploads'/info['stored_name']).unlink(missing_ok=True)
            except KeyError:pass
        if source['status']!='ready':enqueue('probe',sid)
    return {'id':source['id'],'name':source['name']}

from .provider import ProviderInput
from .company_search import CompanySearchConfig,SearchInput as CompanyQuery

@app.get('/api/projects/{project_id}/company-search',dependencies=[Depends(operator_auth)])
def company_search_settings(project_id:str):
    from .company_search import status,configuration
    return {'status':status(project_id),'configuration':configuration(project_id)}

@app.put('/api/projects/{project_id}/company-search',dependencies=[Depends(operator_auth)])
def company_search_connect(project_id:str,value:CompanySearchConfig):
    from .company_search import configure
    return configure(project_id,value)

@app.delete('/api/projects/{project_id}/company-search',dependencies=[Depends(operator_auth)])
def company_search_disconnect(project_id:str):
    from .company_search import disconnect
    return disconnect(project_id)

@app.post('/api/projects/{project_id}/company-search/search',dependencies=[Depends(auth)])
def company_search_query(project_id:str,value:CompanyQuery):
    from .company_search import search
    return search(project_id,value)

@app.get('/api/projects/{project_id}/company-search/evidence/{evidence_id}',dependencies=[Depends(auth)])
def company_search_document(project_id:str,evidence_id:str,offset:int=0,limit:int=8000):
    from .company_search import EvidenceInput,read
    args=EvidenceInput(evidence_id=evidence_id,offset=offset,limit=limit)
    value=read(project_id,evidence_id)
    size=len(value['content']);value['content']=value['content'][args.offset:args.offset+args.limit]
    return dict(value,content_offset=args.offset,next_offset=args.offset+args.limit if size>args.offset+args.limit else None)

@app.get('/api/models',dependencies=[Depends(auth)])
def available_models():
    from .model_catalog import public_catalog
    return public_catalog()

@app.get('/api/model',dependencies=[Depends(operator_auth)])
def model_status():
    from .provider import status
    return status()

@app.get('/api/agent/capabilities',dependencies=[Depends(auth)])
def agent_capabilities():
    from .agent import capabilities
    return capabilities()

@app.put('/api/model',dependencies=[Depends(operator_auth)])
def connect_model(value:ProviderInput):
    from .provider import configure
    return configure(value)

@app.get('/api/projects/{project_id}/sessions',dependencies=[Depends(auth)])
def project_sessions(project_id:str):
    from .conversation import sessions
    return sessions(project_id)

@app.post('/api/projects/{project_id}/sessions',dependencies=[Depends(auth)])
def new_session(project_id:str,value:SessionInput):
    from .conversation import create_session
    return create_session(project_id,value)

@app.get('/api/projects/{project_id}/versions',dependencies=[Depends(auth)])
def project_versions(project_id:str,limit:int=100):
    from .workspace import history
    return history(project_id,max(1,min(limit,200)))

@app.get('/api/projects/{project_id}/files',dependencies=[Depends(auth)])
def project_files(project_id:str):
    from .workspace import list_files
    return list_files(project_id)

@app.get('/api/projects/{project_id}/assets',dependencies=[Depends(auth)])
def project_assets(project_id:str,offset:int=0,limit:int=20,asset_type:str='all'):
    from .data_governance import catalog
    return catalog(project_id,offset,limit,asset_type)

@app.get('/api/projects/{project_id}/applications/{application_id}/lineage',dependencies=[Depends(auth)])
def application_lineage(project_id:str,application_id:str,offset:int=0,limit:int=20,section:str='columns'):
    from .data_governance import lineage
    return lineage(project_id,application_id,offset,limit,section)

@app.get('/api/projects/{project_id}/applications/{application_id}/metric',dependencies=[Depends(auth)])
def application_metric(project_id:str,application_id:str):
    from .metric_registry import current
    return current(project_id,application_id)

@app.get('/api/projects/{project_id}/file',dependencies=[Depends(auth)])
def project_file(project_id:str,path:str,commit:str|None=None):
    from .workspace import path_for
    target=path_for(project_id,path)
    if commit:
        from .workspace import resolve,folder,git_content
        known=resolve(project_id,commit)
        try:content=git_content(folder(project_id),known['commit'],'files/'+path)
        except Exception:raise HTTPException(404,'该版本里没有此文件')
        return StreamingResponse(iter([content]),media_type='application/octet-stream')
    if not target.is_file():raise HTTPException(404,'文件不存在')
    return FileResponse(target,filename=target.name,media_type='application/octet-stream')

@app.get('/api/projects/{project_id}/repository.zip',dependencies=[Depends(auth)])
def project_repository(project_id:str):
    from .workspace import folder,history
    import subprocess
    if not history(project_id,1):raise HTTPException(409,'项目还没有版本')
    archive=subprocess.run([executable('git'),'-C',str(folder(project_id)),'archive','--format=zip','HEAD'],capture_output=True,check=True,timeout=30).stdout
    return StreamingResponse(iter([archive]),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="project-repository.zip"'})

@app.get('/api/projects/{project_id}/applications',dependencies=[Depends(auth)])
def data_applications(project_id:str):
    from .applications import catalog
    return catalog(project_id)

@app.get('/api/projects/{project_id}/applications/{application_id}',dependencies=[Depends(auth)])
def data_application(project_id:str,application_id:str,preview:bool=False,run_id:str|None=None):
    from .applications import info
    return info(project_id,application_id,preview,run_id)

@app.get('/api/projects/{project_id}/applications/{application_id}/data',dependencies=[Depends(auth)])
def application_data(project_id:str,application_id:str,preview:bool=False,search:str='',offset:int=0,limit:int=25,sort:str|None=None,descending:bool=False,run_id:str|None=None):
    from .applications import data
    return data(project_id,application_id,preview,search,offset,limit,sort,descending,run_id)

@app.get('/api/projects/{project_id}/applications/{application_id}/export',dependencies=[Depends(auth)])
def application_export(project_id:str,application_id:str,preview:bool=False,search:str='',run_id:str|None=None):
    from .applications import csv_records
    return StreamingResponse(csv_records(project_id,application_id,preview,search,run_id),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="application-data.csv"'})

@app.get('/api/projects/{project_id}/applications/{application_id}/code.zip',dependencies=[Depends(auth)])
def application_code(project_id:str,application_id:str,preview:bool=False,run_id:str|None=None):
    from .applications import snapshot
    p,run,_,_,_=snapshot(project_id,application_id,preview,run_id)
    return code_zip(p['id'],run['commit'])

# Browser state is authenticated and separate from project Git snapshots.
from . import activity
@app.exception_handler(activity.DeviceNotRegistered)
def browser_device_missing(request,exc):
    return JSONResponse(status_code=404,content={'detail':'当前浏览器设备尚未注册，请重新注册后继续','code':'browser_device_missing'})

@app.exception_handler(activity.ViewConflict)
def view_conflict(request,exc):return JSONResponse(status_code=409,content={'detail':str(exc)})

@app.post('/api/browser/register',dependencies=[Depends(auth)])
def register_browser(value:activity.DeviceInput):return activity.register(value.device_id)

@app.get('/api/activity',dependencies=[Depends(auth)])
def browser_activity(device_id:str,cursor:int=0,limit:int=50):return activity.feed(device_id,cursor,limit)

@app.post('/api/browser/{device_id}/read',dependencies=[Depends(auth)])
def read_activity(device_id:str,value:activity.ReadInput):return activity.mark_read(device_id,value)

@app.put('/api/browser/{device_id}/preferences',dependencies=[Depends(auth)])
def browser_preferences(device_id:str,value:activity.Preferences):return activity.preferences(device_id,value)

@app.put('/api/browser/{device_id}/subscription',dependencies=[Depends(auth)])
def browser_subscribe(device_id:str,value:activity.SubscriptionInput):return activity.subscribe(device_id,value)

@app.delete('/api/browser/{device_id}/subscription',dependencies=[Depends(auth)])
def browser_unsubscribe(device_id:str):return activity.unsubscribe(device_id)

@app.get('/api/browser/{device_id}/view',dependencies=[Depends(auth)])
def browser_view(device_id:str,project_id:str,session_id:str):return activity.read_view(device_id,project_id,session_id)

@app.put('/api/browser/{device_id}/view',dependencies=[Depends(auth)])
def save_browser_view(device_id:str,value:activity.ViewInput):return activity.save_view(device_id,value)

@app.get('/api/projects/{project_id}/message-status',dependencies=[Depends(auth)])
def message_status(project_id:str,request_id:str):
    import re
    from .conversation import turn_identity,public_turn
    get('project',project_id)
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',request_id):raise ValueError('消息编号无效')
    from .workspace_lifecycle import assert_turn_live
    try:
        turn=get('turn',turn_identity(project_id,request_id));assert_turn_live(turn)
        return {'accepted':True,'turn':public_turn(turn)}
    except KeyError:return {'accepted':False}

@app.get('/api/projects/{project_id}/turns/{turn_id}',dependencies=[Depends(auth)])
def browser_turn(project_id:str,turn_id:str):
    from .conversation import public_turn
    t=get('turn',turn_id)
    if t['project_id']!=project_id:raise KeyError(turn_id)
    from .workspace_lifecycle import assert_turn_live
    assert_turn_live(t)
    return public_turn(t)

from . import work_tools,office
@app.get('/api/projects/{project_id}/work-connectors',dependencies=[Depends(operator_auth)])
def work_connectors(project_id:str):return work_tools.list_connectors(project_id)

@app.post('/api/projects/{project_id}/work-connectors',dependencies=[Depends(operator_auth)])
def new_work_connector(project_id:str,value:work_tools.ConnectorInput):return work_tools.configure(project_id,value)

@app.put('/api/projects/{project_id}/work-connectors/{connector_id}',dependencies=[Depends(operator_auth)])
def update_work_connector(project_id:str,connector_id:str,value:work_tools.ConnectorInput):return work_tools.configure(project_id,value,connector_id)

@app.delete('/api/projects/{project_id}/work-connectors/{connector_id}',dependencies=[Depends(operator_auth)])
def disable_work_connector(project_id:str,connector_id:str):
    c=work_tools.owned(project_id,connector_id)
    patch('project',project_id,work_model_approval=None)
    patch('work_connector',connector_id,enabled=False,secret_config=seal({}),revision=uid())
    return {'ok':True}

@app.get('/api/projects/{project_id}/work-actions',dependencies=[Depends(auth)])
def work_actions(project_id:str):return work_tools.actions(project_id)

@app.post('/api/projects/{project_id}/work-actions/{action_id}/decision',dependencies=[Depends(auth)])
def decide_work_action(project_id:str,action_id:str,value:work_tools.ActionDecision):return work_tools.decide(project_id,action_id,value)

@app.get('/api/projects/{project_id}/deliverables/{artifact_id}',dependencies=[Depends(auth)])
def office_download(project_id:str,artifact_id:str):
    file,value=office.artifact(project_id,artifact_id)
    return FileResponse(file,filename=value['name'],headers={'Cache-Control':'no-store'})

from .data_api import install as install_data_api
install_data_api(app,auth)
from .work_runtime import install as install_work_runtime
install_work_runtime(app,auth)
from .browser_tools import install as install_browser_tools
from .media_tools import install as install_media_tools
install_browser_tools(app,auth)
install_media_tools(app,auth)



# Personal capabilities are scoped by the authenticated dependency, while
# administrator presets keep the separate operator credential.
from .memory_api import install as install_memory_api
from .extensions_api import install as install_extensions_api
from .local_api import router as local_workspace_router
install_memory_api(app,auth)
install_extensions_api(app,auth,operator_auth)
app.include_router(local_workspace_router(auth))
from .agent_gateway import install as install_agent_gateway
from .agent_mesh import install as install_agent_mesh
install_agent_gateway(app,auth,operator_auth)
install_agent_mesh(app,auth,operator_auth)
from .workspace_lifecycle import install as install_workspace_lifecycle
from .workspace_artifacts import install as install_workspace_artifacts
install_workspace_lifecycle(app,auth)
install_workspace_artifacts(app,auth)

@app.get('/api/desktop/runtime',dependencies=[Depends(auth)])
def desktop_runtime_status():
    from .model_catalog import public_catalog
    from .local_workspace import enabled as local_folder_enabled
    desktop=os.getenv('ATLAS_DESKTOP_MODE')=='1'
    location='local' if local_folder_enabled() else 'cloud' if os.getenv('ATLAS_WORKSPACE_LOCATION')=='cloud' else 'unbound'
    return {'desktop':desktop,'local_capabilities_available':True,
            'workspace_location':location,'cloud_workspace_configured':location=='cloud',
            'model_configured':public_catalog()['configured'],'connection_checked_at_startup':False,
            'notice':os.getenv('ATLAS_DESKTOP_MODEL_NOTICE','云端模型按需连接；断网时本地导入、加工、查看和导出仍可用。')}

app.mount('/',StaticFiles(directory=Path(__file__).parent/'static',html=True),name='workbench')
