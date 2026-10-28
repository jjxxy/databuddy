import json,hashlib,re
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

def _source_review_details(run,manifest):
    """Derive review scope from frozen inputs, never a model-authored claim."""
    from .project_data import source_interpretation_needs_review
    frozen=run.get('source_snapshot') or {}
    inputs=manifest.get('inputs') or []
    flagged=manifest.get('requires_source_review') is True
    details=[]
    for item in inputs:
        if not isinstance(item,dict):raise ValueError('来源核对依据不完整，请从原始资料重新运行')
        sid=item.get('source_id');source=frozen.get(sid)
        if not isinstance(source,dict) or source.get('id')!=sid:
            raise ValueError('来源版本未冻结，请从原始资料重新运行并核对')
        if source.get('revision',1)!=item.get('source_revision'):
            raise ValueError('来源版本与运行记录不一致，请从原始资料重新运行')
        proof=item.get('evidence') if isinstance(item.get('evidence'),dict) else {}
        if source.get('kind')=='file':
            digest=source.get('content_hash')
            if not isinstance(digest,str) or len(digest)!=64 or proof.get('content_sha256')!=digest:
                raise ValueError('来源原件摘要与运行记录不一致，请从原始资料重新运行')
        else:digest=None
        flagged=flagged or source_interpretation_needs_review(source,proof)
        details.append({'source_id':sid,'revision':source.get('revision',1),'content_sha256':digest})
    if flagged and not details:raise ValueError('来源核对依据缺失，请从原始资料重新运行')
    return flagged,details


def _requires_fact_alignment(run,manifest):
    """Evidence review does not establish additivity of financial facts."""
    from .project_data import source_requires_fact_alignment
    frozen=run.get('source_snapshot') or {}
    for item in manifest.get('inputs') or []:
        if not isinstance(item,dict):continue
        source=frozen.get(item.get('source_id')) or {}
        evidence=item.get('evidence') if isinstance(item.get('evidence'),dict) else {}
        if source_requires_fact_alignment(source,evidence):
            return True
    return False


def semantic_warning(run,manifest):
    """A stored or published output does not establish financial accuracy."""
    if manifest.get('fact_semantic_alignment_required') is True or _requires_fact_alignment(run,manifest):
        return '这份结果的事实业务语义尚未逐项对齐；不同指标、单位或期间不能默认相加。'
    if not isinstance(manifest.get('inputs'),list) or not manifest['inputs']:
        return '这份结果的来源依据无法完整核对，不能据此认定财务口径已确认。'
    try:needed,details=_source_review_details(run,manifest)
    except (KeyError,TypeError,ValueError):
        return '这份结果的来源依据无法完整核对，不能据此认定财务口径已确认。'
    if needed:
        receipt=run.get('source_review_receipt')
        if (not isinstance(receipt,dict) or receipt.get('confirmed_source_review') is not True
                or receipt.get('pipeline_id')!=run.get('target') or receipt.get('run_id')!=run.get('id')
                or receipt.get('commit')!=run.get('commit') or receipt.get('parquet_sha256')!=manifest.get('parquet_hash')
                or receipt.get('sources')!=details or not isinstance(receipt.get('actor_hash'),str)
                or not re.fullmatch(r'[a-f0-9]{64}',receipt['actor_hash'])
                or not isinstance(receipt.get('reviewed_at'),str) or not receipt['reviewed_at']):
            return '这份结果的来源解释缺少完整人工复核记录，不能据此认定财务口径已确认。'
    return None


def ensure_candidate_bulk_allowed(run,manifest,project_id=None):
    """Check selected version integrity before allowing an adopted or safe export."""
    if not isinstance(run,dict) or run.get('kind')!='pipeline':raise ValueError('数据版本不可用')
    if run.get('status') not in ('validated','succeeded') or not run.get('validated') or run.get('artifact_removed'):
        raise ValueError('数据版本尚未完成验证')
    pipeline=get('pipeline',run['target'])
    if project_id is not None and pipeline.get('project_id')!=project_id:raise ValueError('数据版本不属于当前项目')
    folder,stored=manifest_for(run)
    if (stored!=manifest or manifest.get('run_id')!=run.get('id') or manifest.get('pipeline_id')!=run.get('target')
            or manifest.get('commit')!=run.get('commit')):
        raise ValueError('结果版本与应用记录不一致，不能下载')
    path=folder/'business_objects.parquet'
    if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=manifest.get('parquet_hash'):
        raise ValueError('结果摘要不一致，不能下载')
    verify_semantics(pipeline,run,manifest,folder)
    if pipeline.get('published_run')==run.get('id'):return
    if manifest.get('fact_semantic_alignment_required') is True or _requires_fact_alignment(run,manifest):
        raise ValueError('事实业务语义尚未对齐，候选只可分页预览，暂不能下载完整结果或生成图表')


def _verify_review_sources(connection,pipeline,run,details):
    """Recheck current original bytes for a new owner review receipt."""
    from .connectors import verified_file_source
    from .store import unseal
    from .semantic_alignment import canonical_sha256
    frozen=run.get('source_snapshot') or {}
    pid=pipeline.get('project_id')
    originals=[]
    def locked(kind,ident):
        row=connection.execute(select(items.c.body).where(items.c.id==ident,items.c.kind==kind).with_for_update()).first()
        if not row:raise ValueError('来源原件已不存在，请重新导入并核对')
        return json.loads(row[0])
    def check(sid,revision,digest):
        current=locked('source',sid)
        if (current.get('archived') or (pid and current.get('project_id')!=pid)
                or current.get('revision',1)!=revision or current.get('content_hash')!=digest):
            raise ValueError('来源原件版本已变化，请重新运行并核对')
        if current.get('kind')=='file':
            try:verified_file_source(current,require_hash=True)
            except Exception:raise ValueError('来源原件缺失或摘要已变化，请重新导入并核对') from None
        return current
    for entry in details:
        sid=entry['source_id'];source=frozen[sid]
        current=check(sid,entry['revision'],entry['content_sha256'])
        origin=source.get('origin_source_id')
        if not origin:continue
        version=source.get('origin_source_revision');digest=None
        if version is not None:
            original=locked('source',origin)
            version_record=locked('source_version',original.get('revision_id'))
            if (version_record.get('source_id')!=origin or version_record.get('revision')!=version
                    or (pid and version_record.get('project_id')!=pid)):
                raise ValueError('原始资料版本依据不完整，请重新导入并核对')
            digest=version_record.get('content_hash')
        else:
            visual=source.get('visual_fact_review_id');fact=source.get('fact_review_id')
            if not visual and not fact:raise ValueError('派生资料缺少原始版本依据，请重新导入并核对')
            kind='visual_fact_proposal' if visual else 'fact_proposal'
            proposal=locked(kind,visual or fact);payload=unseal(proposal.get('secret_payload',''))
            if (proposal.get('project_id')!=pid or proposal.get('status')!='approved'
                    or proposal.get('output_source_id')!=sid or payload.get('source_id')!=origin
                    or canonical_sha256(payload)!=proposal.get('content_sha256')):
                raise ValueError('派生资料缺少可信核对依据，请重新导入并核对')
            version=payload.get('source_revision');digest=payload.get('source_file_sha256')
        if type(version) is not int or not isinstance(digest,str) or len(digest)!=64:
            raise ValueError('原始资料版本依据不完整，请重新导入并核对')
        check(origin,version,digest)
        originals.append({'source_id':origin,'revision':version,'content_sha256':digest})
    return originals


def publish(pipeline_id,run_id,lease_token=None,rollback=False,*,confirm_source_review=False,
            expected_commit=None,expected_parquet_sha256=None,review_actor=None):
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
        source_review,source_details=_source_review_details(run,manifest)
        if manifest.get('fact_semantic_alignment_required') is True or _requires_fact_alignment(run,manifest):
            raise ValueError('来源事实尚缺少逐条业务语义和可加性的可信核对，不能发布；请保留候选并核对来源')
        if lease_token:
            # A background refresh cannot approve an unreviewed interpretation.
            # Recheck the frozen source and parser receipt here as a guard even
            # when a caller bypasses the worker's normal candidate branch.
            if source_review:
                raise ValueError('来源解释需要人工核对，请查看候选结果后确认发布')
        elif source_review:
            if (confirm_source_review is not True or not isinstance(review_actor,str) or not review_actor
                    or expected_commit!=run['commit'] or expected_parquet_sha256!=manifest['parquet_hash']):
                raise ValueError('请对照原始资料核对来源和业务口径，并确认当前候选的版本后再发布')
            if not rollback and p.get('candidate_run')!=run_id:
                raise ValueError('候选结果已变化，请重新查看并确认最新结果')
        frozen_plan=verify_semantics(p,run,manifest,folder)
        from .rules import validate_legacy_numeric,NUMERIC_POLICY
        from .data_contracts import ExactDataContractError
        try:
            validate_legacy_numeric(frozen_plan['spec'],current_policy=frozen_plan.get('numeric_policy')==NUMERIC_POLICY)
        except ValueError:
            raise ExactDataContractError('历史结果包含无法确保精度的除法或数值筛选，请从原始资料重新运行并核对') from None
        # Re-evaluate frozen contract at publication, including manual publish/rollback.
        import subprocess
        contract_file=folder/'code'/'data-contract.json'
        from .process_control import executable
        tracked_contract=subprocess.run([executable('git'),'-C',p['repository'],'ls-tree','--name-only',run['commit'],'--','data-contract.json'],capture_output=True,text=True,encoding='utf-8',check=True,timeout=15).stdout.strip()
        if tracked_contract:
            if not contract_file.is_file():raise ValueError('数据契约产物缺失，禁止发布')
            import duckdb
            from .data_contracts import evaluate,evaluate_verified_parquet,require_exact_numeric_evidence,ExactDataContractError
            frozen=subprocess.run([executable('git'),'-C',p['repository'],'show',run['commit']+':data-contract.json'],capture_output=True,text=True,encoding='utf-8',check=True,timeout=15).stdout
            contract=json.loads(frozen)
            if json.loads(contract_file.read_text(encoding='utf-8'))!=contract:raise ValueError('数据契约与代码版本不一致')
            if contract.get('ranges') or contract.get('totals'):
                previous_report=run.get('data_contract')
                if manifest.get('data_contract')!=previous_report:
                    raise ExactDataContractError('数值校验记录与运行结果不一致，请从原始资料重新运行并核对')
                expected_rows=manifest.get('records')
                if type(expected_rows) is not int or type(run.get('records')) is not int or run['records']!=expected_rows:
                    raise ExactDataContractError('数值校验记录与完整结果行数不一致，请从原始资料重新运行并核对')
                require_exact_numeric_evidence(previous_report,contract,expected_rows=expected_rows)
                result=evaluate_verified_parquet(contract,folder/'business_objects.parquet',manifest['parquet_hash'])
                if result.get('rows')!=expected_rows:
                    raise ExactDataContractError('数值校验记录与完整结果行数不一致，请从原始资料重新运行并核对')
            else:
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
        if source_review and not lease_token:
            expected={'pipeline_id':pipeline_id,'run_id':run_id,'commit':run['commit'],
                      'parquet_sha256':manifest['parquet_hash'],'sources':source_details,
                      'confirmed_source_review':True}
            previous=run.get('source_review_receipt')
            if previous is not None:
                if (not isinstance(previous,dict) or any(previous.get(k)!=v for k,v in expected.items())
                        or not isinstance(previous.get('actor_hash'),str) or len(previous['actor_hash'])!=64
                        or not isinstance(previous.get('reviewed_at'),str)):
                    raise ValueError('历史来源复核回执与当前结果不一致，请重新运行')
            else:
                originals=_verify_review_sources(c,p,run,source_details)
                run['source_review_receipt']=dict(expected,original_sources=originals,
                    actor_hash=hashlib.sha256(review_actor.encode('utf-8')).hexdigest(),reviewed_at=now())
            c.execute(update(jobs).where(jobs.c.id==run_id).values(body=json.dumps(run,ensure_ascii=False,default=str)))
    record('release.rollback' if rollback else 'release.publish',pipeline_id,{'run_id':run_id,'commit':run['commit']})
    return p
