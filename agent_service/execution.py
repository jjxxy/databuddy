import hashlib,importlib.metadata,json,os,signal,subprocess,sys,time
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import duckdb,yaml
from croniter import croniter
from .config import DATA,TIMEZONE
from .store import get,patch,now,transaction,items
from .jobs import enqueue as queue_enqueue,claim,get_job,fenced_update,heartbeat,ACTIVE
from .codegen import checkout
from .errors import TaskError,Cancelled
from .sources import snapshot
from .audit import record
from .releases import publish
from .process_control import spawn,stop,executable

RUN_TIMEOUT=int(os.getenv('MAX_RUN_TIMEOUT_SECONDS','3600'))

def enqueue(kind,target,commit=None,scheduled_key=None,max_attempts=3):
    sources={}
    if kind=='pipeline':
        p=get('pipeline',target)
        # Freeze the source configurations used by this specific execution, outside Git.
        plan=json.loads(subprocess.run([executable('git'),'-C',p['repository'],'show',(commit or p['commit'])+':plan.json'],capture_output=True,text=True,check=True).stdout)
        sources=snapshot([plan['source_id']]+[j['source_id'] for j in plan.get('joins',[])])
    if kind=='pipeline':max_attempts=plan.get('runtime',{}).get('max_attempts',max_attempts)
    return queue_enqueue(kind,target,commit,scheduled_key,max_attempts,extra={'source_snapshot':sources,'runtime_snapshot':plan.get('runtime',{}) if kind=='pipeline' else {}})

def patch_run(run,**values):
    updated=fenced_update(run['id'],run['lease_token'],**values);run.update(updated);return updated

def checkpoint(run):
    current=get_job(run['id'])
    if current.get('cancel_requested'):raise Cancelled()
    from .job_authority import check
    check(current)
    if current.get('lease_token')!=run['lease_token'] or current.get('status')!='running' or (current.get('lease_until') or 0)<=time.time():raise TaskError('执行租约已失效','LEASE_LOST')
    if time.monotonic()>run['_deadline']:raise TaskError('任务超过运行时间限制，请缩小范围或调整预算','TIMEOUT',True)

def invoke(args,run,folder,env=None,stage='execution'):
    logfile=folder/'execution.log'
    with logfile.open('a') as log:
        child_env=dict(env or os.environ,ATLAS_PARENT_PID=str(os.getpid()))
        proc=spawn(args,stdout=log,stderr=log,env=child_env)
        start=time.monotonic();beat=0
        try:
            while proc.poll() is None:
                if time.monotonic()-beat>=3:
                    current=get_job(run['id'])
                    if current.get('cancel_requested'):raise Cancelled()
                    if not heartbeat(run['id'],run['lease_token']):raise TaskError('执行租约已失效','LEASE_LOST')
                    beat=time.monotonic()
                if time.monotonic()>run.get('_deadline',start+min(RUN_TIMEOUT,run.get('runtime_snapshot',{}).get('timeout_seconds',240))):raise TaskError('任务超过运行时间限制，请缩小范围或调整预算','TIMEOUT',True)
                time.sleep(.1)
        except BaseException:
            if proc.poll() is None:
                stop(proc,timeout=3)
            raise
    if proc.returncode:
        if (folder/'error.json').exists():
            e=json.loads((folder/'error.json').read_text());raise TaskError(e['message'],e.get('code','SOURCE_FAILED'),e.get('retryable',False))
        if stage=='dbt':raise TaskError('数据质量或 dbt 构建失败，查看质量报告定位失败规则','QUALITY_FAILED')
        raise TaskError('运行失败，来源权限或文件解析未通过','SOURCE_FAILED')

def quality_report(folder):
    path=folder/'code'/'dbt'/'target'/'run_results.json'
    data=json.loads(path.read_text()) if path.exists() else {}
    result=[{'name':x['unique_id'],'status':x['status'],'failures':x.get('failures'),'execution_time':x.get('execution_time')} for x in data.get('results',[])]
    contract=folder/'data-contract-report.json'
    if contract.exists():
        report=json.loads(contract.read_text())
        result.extend(dict(check,name='contract:'+check['name']) for check in report['checks'])
    return result

def execute(run):
    run['_deadline']=time.monotonic()+min(RUN_TIMEOUT,run.get('runtime_snapshot',{}).get('timeout_seconds',240))
    folder=DATA/'runs'/run['id']/('attempt-'+str(run['attempts']));folder.mkdir(parents=True,exist_ok=True)
    patch_run(run,started_at=now(),stage='探查' if run['kind']=='probe' else '采集',artifact_dir=str(folder.relative_to(DATA)))
    runtime_env=dict(os.environ,RUN_DIR=str(folder),PYTHONPATH=str(Path(__file__).resolve().parent.parent))
    try:
        checkpoint(run)
        if run['kind']=='report_fetch':
            from .project_reports import execute_job as fetch_report_job
            fetch_report_job(run,folder)
            return
        if run['kind']=='data_workflow':
            from .project_data import execute_job
            execute_job(run,folder)
            return
        if run['kind']=='work_action':
            import threading
            from .work_tools import perform
            stopped=threading.Event()
            def beat():
                while not stopped.wait(3):
                    try:
                        if not heartbeat(run['id'],run['lease_token']):return
                    except Exception:return
            thread=threading.Thread(target=beat,daemon=True);thread.start()
            try:result=perform(run['target'],before_call=lambda:checkpoint(run))
            finally:stopped.set();thread.join(timeout=4)
            checkpoint(run)
            patch_run(run,status='succeeded' if result['state']=='succeeded' else 'failed',finished_at=now(),stage='外部操作'+result['state'])
            return
        if run['kind']=='probe':
            runtime_env['ATLAS_MODEL_SOURCE_ID']=run['target']
            invoke([sys.executable,'-m','agent_service.probe',run['target'],str(folder/'error.json')],run,folder,runtime_env)
            checkpoint(run)
            patch_run(run,status='succeeded',finished_at=now(),stage='探查完成')
            record('source.probe',run['target'],{'run_id':run['id'],'attempt':run['attempts']});return
        p=get('pipeline',run['target']);pipeline=dict(p,commit=run['commit'])
        code=checkout(pipeline,folder);plan=json.loads((code/'plan.json').read_text())
        if p.get('published_run'):
            from .releases import manifest_for
            previous_folder,previous_manifest=manifest_for(get_job(p['published_run']))
            source_tables={entry['source_id']:('records' if i==0 else f'join_{i}') for i,entry in enumerate(previous_manifest.get('inputs',[]))}
            previous_schema={}
            with duckdb.connect(str(previous_folder/'warehouse.duckdb'),read_only=True) as previous:
                for source_id,table in source_tables.items():
                    previous_schema[source_id]=[{'name':r[0],'type':r[1]} for r in previous.execute('describe raw.'+table).fetchall()]
            (folder/'previous_schema.json').write_text(json.dumps(previous_schema))
        (folder/'sources.json').write_text(json.dumps(run.get('source_snapshot') or snapshot([plan['source_id']]+[j['source_id'] for j in plan.get('joins',[])]),ensure_ascii=False))
        (folder/'sources.json').chmod(0o600)
        runtime_env['CODE_DIR']=str(code)
        invoke([sys.executable,str(code/'connectors'/'collect.py')],run,folder,runtime_env)
        patch_run(run,stage='dbt 构建与质量测试')
        profile={'personal_agent':{'target':'local','outputs':{'local':{'type':'duckdb','path':str(folder/'warehouse.duckdb'),'schema':'business','threads':1}}}}
        (folder/'profiles.yml').write_text(yaml.safe_dump(profile))
        invoke([sys.executable,'-c','from dbt.cli.main import cli; cli()', 'build','--project-dir',str(code/'dbt'),'--profiles-dir',str(folder),'--no-use-colors'],run,folder,runtime_env,'dbt')
        patch_run(run,stage='生成候选业务结果',quality=quality_report(folder))
        relationships=[]
        with duckdb.connect(str(folder/'warehouse.duckdb'),read_only=True) as con:
            contract_result=None
            if (code/'data-contract.json').exists():
                from .data_contracts import evaluate
                contract_result=evaluate(con,json.loads((code/'data-contract.json').read_text()))
                (folder/'data-contract-report.json').write_text(json.dumps(contract_result,ensure_ascii=False,indent=2))
                patch_run(run,data_contract=contract_result)
                if not contract_result['passed']:raise TaskError('数据验收契约未通过，正式结果保持原版本','DATA_CONTRACT_FAILED')
            count=con.execute('select count(*) from business.business_objects').fetchone()[0]
            schema_rows=con.execute('describe business.business_objects').fetchall()
            columns=[x[0] for x in schema_rows]
            output_schema=[{'name':x[0],'type':x[1]} for x in schema_rows]
            if p.get('published_run') and p.get('active_commit')==run['commit']:
                from .releases import manifest_for
                _,old_manifest=manifest_for(get_job(p['published_run']))
                if old_manifest.get('output_schema') and old_manifest['output_schema']!=output_schema:raise TaskError('正式代码的结果类型发生变化，需重新探查并验证新版本','OUTPUT_SCHEMA_CHANGED')
            summary=[]
            if plan['spec']['amount_field']:
                cur=con.execute('select * from business.summary order by amount_total desc limit 50');summary=[dict(zip([x[0] for x in cur.description],r)) for r in cur.fetchall()]
            for i,j in enumerate(plan.get('joins',[]),1):
                unmatched=con.execute(f'select count(*) from business.business_objects where "j{i}_{j["right_field"]}" is null').fetchone()[0]
                relationships.append({'source_id':j['source_id'],'label':j['label'],'cardinality':'many_to_one','unmatched':unmatched,'records':count})
            file=str(folder/'business_objects.parquet').replace("'","''");con.execute("copy business.business_objects to '"+file+"' (format parquet)")
        inputs=json.loads((folder/'input.json').read_text())
        m={'data_contract':contract_result,'knowledge_basis':plan.get('knowledge_basis',[]),'application':plan.get('application'),'run_id':run['id'],'pipeline_id':p['id'],'commit':run['commit'],'semantic_version':3,'runtime':run.get('runtime_snapshot',{}),'records':count,'columns':columns,'output_schema':output_schema,'summary':summary,'fields':plan['fields'],'spec':plan['spec'],'relationships':relationships,'inputs':inputs['sources'],'quality':quality_report(folder),'published_at':now(),'versions':{x:importlib.metadata.version(x) for x in ['dlt','duckdb','dbt-core','dbt-duckdb','langgraph']},'data_as_of':min((s.get('collected_at',run['started_at']) for s in inputs['sources']),default=run['started_at']),'input_hash':inputs['input_hash'],'parquet_hash':hashlib.sha256((folder/'business_objects.parquet').read_bytes()).hexdigest()}
        if plan.get('column_lineage_version')==1:
            from .column_lineage import digest
            lineage=json.loads((code/'column-lineage.json').read_text())
            m['column_lineage']=lineage
            m['column_lineage_sha256']=digest(lineage)
            m['metric_aggregation']=plan['metric_aggregation']
        (folder/'output.json').write_text(json.dumps(m,ensure_ascii=False,indent=2,default=str))
        checkpoint(run)
        patch_run(run,validated=True,records=count,quality=m['quality'])
        latest=get('pipeline',p['id'])
        if run['commit']==latest['commit']:
            patch_run(run,target_patch={'kind':'pipeline','expected':{'commit':run['commit']},'values':{'candidate_run':run['id']}})
        auto=latest.get('release_mode','auto')=='auto' or run['commit']==latest.get('active_commit')
        if auto:
            publish(p['id'],run['id'],lease_token=run['lease_token'])
            patch_run(run,status='succeeded',finished_at=now(),stage='已发布')
        else:patch_run(run,status='validated',finished_at=now(),stage='验证通过，等待发布')
        record('run.validated',p['id'],{'run_id':run['id'],'commit':run['commit'],'records':count})
    except Exception as e:
        current=get_job(run['id'])
        if current.get('lease_token')!=run['lease_token']:return
        error=Cancelled() if current.get('cancel_requested') else e if isinstance(e,TaskError) else TaskError('执行失败，检查来源、字段及处理规则','TASK_FAILED')
        status='cancelled' if isinstance(error,Cancelled) else 'retrying' if error.retryable and run['attempts']<run['max_attempts'] else 'failed'
        try:patch_run(run,status=status,finished_at=now(),stage='等待重试' if status=='retrying' else '已取消' if status=='cancelled' else '失败',error=str(error),error_code=error.code,available_at=time.time()+min(300,2**run['attempts']),quality=quality_report(folder))
        except RuntimeError:return
        if run['kind']=='probe' and status=='failed':patch('source',run['target'],status='failed',error=str(error),error_code=error.code)
        record('run.'+status,run['target'],{'run_id':run['id'],'code':error.code,'attempt':run['attempts']})

def _queue_due():
    # Updating the schedule row and enqueuing need one logical clock; per-workspace file lock
    # serializes schedulers, while DB uniqueness and active-task suppression remain authoritative.
    from .locks import FileLock
    with FileLock(str(DATA/'scheduler.lock'),timeout=5):
        current=datetime.now(timezone.utc)
        from .store import all_items
        for p in all_items('pipeline'):
            if not p.get('cron') or p.get('archived'):continue
            try:due=datetime.fromisoformat(p['next_due'])
            except (ValueError,TypeError,KeyError):
                patch('pipeline',p['id'],cron=None,schedule_error='定时配置无效，已停用');continue
            if due>current:continue
            commit=p.get('active_commit') or (p.get('commit') if p.get('release_mode','auto')=='auto' else None)
            if commit:
                try:enqueue('pipeline',p['id'],commit,f"{p['id']}:{p['next_due']}")
                except ValueError:patch('pipeline',p['id'],schedule_error='来源已归档，需修复连接')
            patch('pipeline',p['id'],next_due=croniter(p['cron'],current.astimezone(ZoneInfo(TIMEZONE))).get_next(datetime).astimezone(timezone.utc).isoformat())

def process_once():
    from .maintenance import guard
    try:
        with guard():
            run=claim()
            if run:execute(run);return True
    except BlockingIOError:return False
    return False

def queue_due():
    from .maintenance import guard
    try:
        with guard():
            _queue_due()
            from .project_data import queue_due as queue_workflows
            queue_workflows()
    except BlockingIOError:return
