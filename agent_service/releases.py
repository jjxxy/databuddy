import json,hashlib
from pathlib import Path
from sqlalchemy import select,update
from .store import get,transaction,items,now
from .config import DATA,TIMEZONE
from .jobs import jobs,render
from .audit import record

def artifact_folder(run):
    path=DATA/ run.get('artifact_dir',('runs/'+run['id']))
    if not path.resolve().is_relative_to((DATA/'runs').resolve()):raise ValueError('无效运行产物路径')
    return path

def manifest_for(run):
    folder=artifact_folder(run)
    return folder,json.loads((folder/'output.json').read_text(encoding='utf-8'))

def verify_semantics(pipeline,run,manifest,folder):
    """Metadata used for governance must match the immutable Git revision."""
    from .codegen import git
    if manifest.get('run_id')!=run['id'] or manifest.get('pipeline_id')!=pipeline['id'] or manifest.get('commit')!=run['commit']:
        raise ValueError('运行产物版本标识不一致')
    plan=json.loads(git(pipeline['repository'],'show',run['commit']+':plan.json'))
    if manifest.get('spec')!=plan['spec']:raise ValueError('指标口径与冻结代码版本不一致')
    if manifest.get('metric_aggregation')!=plan.get('metric_aggregation'):raise ValueError('指标精度与冻结代码版本不一致')
    if plan.get('column_lineage_version')==1:
        from .column_lineage import digest
        lineage=json.loads(git(pipeline['repository'],'show',run['commit']+':column-lineage.json'))
        path=folder/'code'/'column-lineage.json'
        if not path.is_file():raise ValueError('字段血缘产物缺失，禁止发布')
        if json.loads(path.read_text(encoding='utf-8'))!=lineage or manifest.get('column_lineage')!=lineage or manifest.get('column_lineage_sha256')!=digest(lineage):
            raise ValueError('字段血缘与冻结代码版本不一致')
    return plan

def publish(pipeline_id,run_id,lease_token=None,rollback=False):
    with transaction() as c:
        row=c.execute(select(jobs).where(jobs.c.id==run_id).with_for_update()).mappings().first()
        if not row:raise KeyError(run_id)
        run=render(row)
        if run['target']!=pipeline_id or not run.get('validated') or run.get('cancel_requested'):raise ValueError('只有该任务通过完整质量验证的版本才能发布')
        if lease_token:
            import time
            if row['status']!='running' or row['lease_token']!=lease_token or row['lease_until']<time.time():raise ValueError('执行租约失效，禁止发布')
        elif row['status'] not in ('validated','succeeded'):raise ValueError('运行尚未完成验证')
        p_row=c.execute(select(items.c.body).where(items.c.id==pipeline_id,items.c.kind=='pipeline').with_for_update()).first()
        if not p_row:raise KeyError(pipeline_id)
        p=json.loads(p_row[0])
        if not rollback and run['commit']!=p['commit'] and run['commit']!=p.get('active_commit'):raise ValueError('候选代码已被新版本替代，请重新验证或明确回滚')
        if run.get('artifact_removed'):raise ValueError('该历史产物已按保留策略清理，无法回滚')
        folder,manifest=manifest_for(run)
        actual=hashlib.sha256((folder/'business_objects.parquet').read_bytes()).hexdigest()
        if actual!=manifest['parquet_hash'] or manifest['commit']!=run['commit']:raise ValueError('运行产物完整性校验失败')
        verify_semantics(p,run,manifest,folder)
        # Re-evaluate frozen contract at publication, including manual publish/rollback.
        import subprocess
        contract_file=folder/'code'/'data-contract.json'
        from .process_control import executable
        tracked_contract=subprocess.run([executable('git'),'-C',p['repository'],'ls-tree','--name-only',run['commit'],'--','data-contract.json'],capture_output=True,text=True,encoding='utf-8',check=True,timeout=15).stdout.strip()
        if tracked_contract:
            if not contract_file.is_file():raise ValueError('数据契约产物缺失，禁止发布')
            import duckdb
            from .data_contracts import evaluate
            frozen=subprocess.run([executable('git'),'-C',p['repository'],'show',run['commit']+':data-contract.json'],capture_output=True,text=True,encoding='utf-8',check=True,timeout=15).stdout
            contract=json.loads(frozen)
            if json.loads(contract_file.read_text(encoding='utf-8'))!=contract:raise ValueError('数据契约与代码版本不一致')
            with duckdb.connect() as validation_db:
                validation_db.execute('CREATE SCHEMA business')
                validation_db.read_parquet(str(folder/'business_objects.parquet')).create_view('contract_input')
                validation_db.execute('CREATE VIEW business.business_objects AS SELECT * FROM contract_input')
                result=evaluate(validation_db,contract)
            if not result['passed']:raise ValueError('数据验收契约未通过，禁止发布')
        p.update(published_run=run_id,active_commit=run['commit'],status='published',last_success=now(),data_as_of=manifest.get('data_as_of',manifest['published_at']),active_runtime=manifest.get('runtime',{}))
        if p.get('candidate_run')==run_id:p['candidate_run']=None
        pending=p.get('pending_schedule')
        if pending and pending.get('commit')==run['commit'] and not rollback:
            from croniter import croniter
            from datetime import datetime,timezone
            from zoneinfo import ZoneInfo
            p['cron']=pending['cron'];p['next_due']=croniter(p['cron'],datetime.now(ZoneInfo(TIMEZONE))).get_next(datetime).astimezone(timezone.utc).isoformat();p['pending_schedule']=None
        # Git and contract checks may take time; an expired worker cannot publish.
        if lease_token and row['lease_until']<time.time():raise ValueError('执行租约失效，禁止发布')
        from .metric_registry import register
        register(c,p,run,manifest)
        c.execute(update(items).where(items.c.id==pipeline_id).values(body=json.dumps(p,ensure_ascii=False)))
    record('release.rollback' if rollback else 'release.publish',pipeline_id,{'run_id':run_id,'commit':run['commit']})
    return p
