import ast,json,subprocess,re
from pathlib import Path
import yaml
from .locks import FileLock
from .config import DATA
from .models import TransformSpec,RuntimePolicy
from .store import now,put,uid,patch,get
from .rules import filter_sql,computation_sql,numeric,validate_legacy_numeric,NUMERIC_POLICY
from .process_control import executable

_EXPECT_UNSET=object()

def git(repo,*args):return subprocess.run([executable('git'),'-C',str(repo),*args],check=True,capture_output=True,text=True,encoding='utf-8',timeout=30).stdout.strip()

def merged_fields(source,spec,*,source_lookup=None):
    fields=[dict(f,source_id=source['id']) for f in source['profile']['fields']]
    joins=[]
    for i,j in enumerate(spec['joins'],1):
        s=source_lookup(j['source_id']) if source_lookup is not None else get('source',j['source_id'])
        if s.get('status')!='ready' or s.get('archived'):raise ValueError('关联来源必须已探查且未归档')
        right=[dict(f,source_id=s['id'],original_name=f['name'],name=f'j{i}_{f["name"]}',label=j['label']+' · '+f['label']) for f in s['profile']['fields']]
        if j['left_field'] not in {f['name'] for f in source['profile']['fields']}:raise ValueError('关联左字段不属于主来源')
        if j['right_field'] not in {f['name'] for f in s['profile']['fields']}:raise ValueError('关联右字段不存在')
        joins.append(dict(j,table=f'join_{i}',fields=s['profile']['fields'],schema_labels=[f['label'] for f in s['profile']['fields']],source_revision=s.get('revision',1),source_name=s.get('name')))
        fields.extend(right)
    for rule in spec['computations']:
        known={f['name']:f for f in fields}
        if rule['name'] in known:raise ValueError('计算字段名称重复')
        if any(k not in known or not known[k].get('numeric') for k in (rule['left_field'],rule['right_field'])):raise ValueError('计算字段只能引用已知数值字段')
        fields.append({'name':rule['name'],'label':rule['label'],'numeric':True,'derived':True,'source_fields':[rule['left_field'],rule['right_field']]})
    for f in fields:
        if f['name'] in spec['field_labels']:f['label']=spec['field_labels'][f['name']]
    known={f['name']:f for f in fields}
    if any(known.get(k,{}).get('derived') for k in spec['trim_fields']):raise ValueError('计算结果不能使用文本清理，请清理原始字段')
    keys=[spec.get(x) for x in ('primary_key','group_by','amount_field')]+spec['required_fields']+spec['nonnegative_fields']+spec['trim_fields']+list(spec['field_labels'])+[r['field'] for r in spec['filters']]
    if any(k and k not in known for k in keys):raise ValueError('处理规则引用了不存在的字段')
    if spec.get('primary_key') and spec['primary_key'] not in {f['name'] for f in source['profile']['fields']}:raise ValueError('业务主键必须属于主来源')
    for k in spec['nonnegative_fields']+[spec['amount_field']] if spec['amount_field'] else spec['nonnegative_fields']:
        if not known[k]['numeric']:raise ValueError('数值规则字段不是有效数值')
    return fields,joins

def _safe_user_request(project_id, turn_id):
    # Only already-redacted public user text may enter versioned code.
    # Never copy encrypted secrets or revocable personal-memory instructions.
    if not project_id or not isinstance(turn_id,str):return None,None,None
    try:turn=get('turn',turn_id)
    except KeyError:return None,None,None
    if turn.get('project_id')!=project_id:return None,None,None
    from .conversation import redact
    def eligible(value):
        if not isinstance(value,str) or not value.strip() or len(value)>12000:return None
        if value=='已提供连接信息（内容已隐藏并加密保存）' or redact(value)!=value:return None
        if re.search(r'记住|个人偏好|忘记.*记忆|删除.*记忆|\b(?:remember|forget|personal preference)\b',value,re.I):return None
        return value
    latest=eligible(turn.get('text'))
    if not turn.get('resumed_from'):return latest,turn_id if latest else None,None
    # A continuation is not itself the original goal. Resolve the stored
    # originating user turn, binding project, session and actor before using
    # its public text; never trust a free-form work_request as a substitute.
    origin_id=turn.get('agent_origin') or turn.get('resumed_from')
    try:origin=get('turn',origin_id)
    except (KeyError,TypeError):return None,None,latest
    if (origin.get('project_id')!=project_id or origin.get('session_id')!=turn.get('session_id')
            or origin.get('actor_id','owner')!=turn.get('actor_id','owner')):
        return None,None,latest
    request=eligible(origin.get('text'))
    return request,origin_id if request else None,latest


def generate(source,name,spec,existing=None,release_mode='manual',runtime=None, pipeline_id=None, project_id=None, origin_turn=None, application=None,knowledge_basis=None,request_turn_id=None,frozen_sources=None,expected_commit=_EXPECT_UNSET):
    project_id=project_id or (existing or {}).get('project_id') or source.get('project_id')
    source_lookup=None
    if frozen_sources is not None:
        # An approved proposal binds exact source records. Never fall back to
        # live sources if a referenced source is absent from that snapshot.
        if not isinstance(frozen_sources,dict):raise ValueError('已确认来源快照无效')
        def source_lookup(source_id):
            frozen=frozen_sources.get(source_id)
            if not isinstance(frozen,dict) or frozen.get('id')!=source_id or frozen.get('project_id')!=project_id:
                raise ValueError('已确认来源快照缺失或不属于当前项目')
            return frozen
        source=source_lookup(source['id'])
    runtime=RuntimePolicy.model_validate(runtime if runtime is not None else (existing or {}).get('runtime',{})).model_dump()
    spec=TransformSpec.model_validate(spec).model_dump()
    validate_legacy_numeric(spec)
    knowledge_basis=knowledge_basis if knowledge_basis is not None else (existing or {}).get('knowledge_basis',[])
    from .company_search import validate_basis
    validate_basis(project_id or (existing or {}).get('project_id') or source.get('project_id'),knowledge_basis)
    fields,joins=merged_fields(source,spec,source_lookup=source_lookup)
    from .applications import configuration,write_code
    app_value=application if application is not None else (existing.get('application_spec') if existing and existing.get('application_customized') else None)
    app=configuration(name,spec,fields,app_value)
    pipeline_id=existing['id'] if existing else (pipeline_id or uid());repo=Path(existing['repository']) if existing else DATA/'repositories'/pipeline_id
    repo.mkdir(parents=True,exist_ok=True)
    with FileLock(str(DATA/('generate-'+pipeline_id+'.lock')),timeout=30):
        if expected_commit is not _EXPECT_UNSET:
            # Compare under the generation lock, not only when the user
            # approves. A stale proposal may never replace another candidate.
            try:current=get('pipeline',pipeline_id)
            except KeyError:current=None
            if expected_commit is None:
                if current is not None:raise ValueError('目标应用已被创建，请重新查看业务方案')
            elif (current is None or current.get('project_id')!=project_id
                  or current.get('commit')!=expected_commit):
                raise ValueError('应用版本已发生变化，请重新查看业务方案')
        for directory in ['dbt/models','dbt/tests','semantics','connectors']:(repo/directory).mkdir(parents=True,exist_ok=True)
        # Only files owned by this generator are recreated. User-added files are not executed as entrypoints.
        for stale in (repo/'dbt'/'tests').glob('generated_*.sql'):stale.unlink()
        for stale in ['dbt/models/summary.sql','dbt/tests/blank_primary.sql','dbt/tests/valid_amount.sql']:(repo/stale).unlink(missing_ok=True)
        user_request,request_turn,continuation_request=_safe_user_request(project_id,request_turn_id)
        plan={'user_request':user_request,'request_turn_id':request_turn,'continuation_request':continuation_request,'source_id':source['id'],'source_name':source.get('name'),'source_revision':source.get('revision',1),'spec':spec,'fields':fields,'joins':joins,'schema_names':[f['name'] for f in source['profile']['fields']],'schema_labels':[f['label'] for f in source['profile']['fields']],'strategy':'full_refresh','runtime':runtime,'created_at':now(),'generator_version':'0.3.0','numeric_policy':NUMERIC_POLICY,'identity_policy':('business_primary_key_sha256_v1' if spec['primary_key'] else 'preserve_all_rows_occurrence_v1'),'business_plan_version':1}
        write_code(repo,pipeline_id,project_id or (existing or {}).get('project_id') or source.get('project_id'),name,spec,fields,app)
        plan['application']=app
        plan['knowledge_basis']=knowledge_basis
        (repo/'knowledge-evidence.json').write_text(json.dumps({'version':1,'evidence':knowledge_basis,'policy':'frozen_per_code_version; rerun does not regenerate business rules'},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        # Application rules override project defaults; freeze the chosen document in Git.
        contract_pid=project_id or (existing or {}).get('project_id') or source.get('project_id')
        if contract_pid:
            from .data_contracts import select_contract
            contract=select_contract(contract_pid,pipeline_id)
            if contract is not None:
                (repo/'data-contract.json').write_text(json.dumps(contract,ensure_ascii=False,indent=2),encoding='utf-8')
            else:(repo/'data-contract.json').unlink(missing_ok=True)
        from .column_lineage import build
        lineage=build(source,fields,spec,joins)
        (repo/'column-lineage.json').write_text(json.dumps(lineage,ensure_ascii=False,indent=2),encoding='utf-8')
        plan['column_lineage_version']=1
        plan['metric_aggregation']='sum_decimal_38_12'
        (repo/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
        from .business_plan import build as build_business_plan
        business_plan=build_business_plan(plan,pipeline_id,contract_pid,app.get('title') or name,source_name=source.get('name'))
        (repo/'business-plan.json').write_text(json.dumps(business_plan,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        (repo/'.gitignore').write_text('target/\nlogs/\nprofiles.yml\n*.duckdb\n*.parquet\n.env\n',encoding='utf-8')
        collect='from agent_service.runner import main\nif __name__ == "__main__":\n    main()\n'
        ast.parse(collect);(repo/'connectors'/'collect.py').write_text(collect,encoding='utf-8')
        project={'name':'personal_agent','version':'1.0.0','config-version':2,'profile':'personal_agent','model-paths':['models'],'test-paths':['tests'],'models':{'personal_agent':{'+materialized':'table'}}}
        (repo/'dbt'/'dbt_project.yml').write_text(yaml.safe_dump(project,allow_unicode=True,sort_keys=False),encoding='utf-8')
        # Without a business key, repeated identical rows may still be valid
        # individual transactions. Preserve every row; use an occurrence
        # index only for stable identification within the same snapshot.
        base=('select * from raw.records' if spec['primary_key'] else
              'select *, row_number() over (partition by record_hash order by _source_index, _dlt_id) as _atlas_occ from raw.records')
        projections=['r.* exclude (_dlt_id, _dlt_load_id'+('' if spec['primary_key'] else ', _atlas_occ')+')'];join_sql=[]
        for i,j in enumerate(joins,1):
            projections.extend(f'j{i}."{f["name"]}" as "j{i}_{f["name"]}"' for f in j['fields'])
            join_sql.append(f'left join raw.{j["table"]} j{i} on cast(r."{j["left_field"]}" as varchar)=cast(j{i}."{j["right_field"]}" as varchar)')
            (repo/'dbt'/'tests'/f'generated_join_{i}_unique.sql').write_text(f'select "{j["right_field"]}" from raw.{j["table"]} where "{j["right_field"]}" is not null group by 1 having count(*)>1\n',encoding='utf-8')
            if j['required_match']:
                (repo/'dbt'/'tests'/f'generated_join_{i}_matched.sql').write_text('select * from '+"{{ ref('business_objects') }}"+f' where "j{i}_{j["right_field"]}" is null\n',encoding='utf-8')
        identity=(f'sha256(cast(r."{spec["primary_key"]}" as varchar))' if spec['primary_key'] else
                  "case when r._atlas_occ=1 then r.record_hash else sha256(r.record_hash || ':occ:' || cast(r._atlas_occ as varchar)) end")
        joined='select '+identity+' as object_id, '+', '.join(projections)+' from ('+base+') r '+' '.join(join_sql)
        if spec['trim_fields']:
            replacements=', '.join(f'nullif(trim(cast("{f}" as varchar)),\'\') as "{f}"' for f in spec['trim_fields'])
            joined='select * replace ('+replacements+') from ('+joined+') trimmed'
        for rule in spec['computations']:
            joined='select *, '+computation_sql(rule)+' as "'+rule['name']+'" from ('+joined+') computed'
            if rule['operation']=='divide':
                # Reject zero/null denominator rather than emitting Infinity or a misleading missing result.
                (repo/'dbt'/'tests'/('generated_divide_'+rule['name']+'.sql')).write_text("select * from {{ ref('prepared_objects') }} where "+numeric(rule['right_field'])+'=0 or "'+rule['right_field']+'" is null\n',encoding='utf-8')
        (repo/'dbt'/'models'/'prepared_objects.sql').write_text("{{ config(materialized='table') }}\n"+joined+'\n',encoding='utf-8')
        final="select * from {{ ref('prepared_objects') }}"
        if spec['filters']:final+=' where '+' and '.join('('+filter_sql(rule)+')' for rule in spec['filters'])
        (repo/'dbt'/'models'/'business_objects.sql').write_text("{{ config(materialized='table') }}\n"+final+'\n',encoding='utf-8')
        tests={'version':2,'models':[{'name':'business_objects','description':spec['object_label'],'columns':[{'name':'object_id','tests':['not_null','unique']}]}]}
        required=set(spec['required_fields']+([spec['primary_key']] if spec['primary_key'] else []))
        for k in sorted(required):
            tests['models'][0]['columns'].append({'name':k,'tests':['not_null']+(['unique'] if k==spec['primary_key'] else [])})
            (repo/'dbt'/'tests'/f'generated_required_{k}.sql').write_text("select * from {{ ref('business_objects') }} where trim(cast(\""+k+"\" as varchar))=''\n",encoding='utf-8')
        (repo/'dbt'/'models'/'schema.yml').write_text(yaml.safe_dump(tests,allow_unicode=True,sort_keys=False),encoding='utf-8')
        (repo/'dbt'/'tests'/'generated_min_rows.sql').write_text("select count(*) n from {{ ref('business_objects') }} having count(*) < "+str(spec['min_rows'])+'\n',encoding='utf-8')
        for k in spec['nonnegative_fields']:
            checked=f'try_cast(replace(cast("{k}" as varchar), \',\', \'\') as decimal(38,12))'
            (repo/'dbt'/'tests'/f'generated_nonnegative_{k}.sql').write_text("select * from {{ ref('business_objects') }} where "+checked+'<0 or ("'+k+'" is not null and '+checked+' is null)\n',encoding='utf-8')
        if spec['amount_field']:
            a=spec['amount_field'];g=spec['group_by'];expression=f'try_cast(replace(cast("{a}" as varchar), \',\', \'\') as decimal(38,12))'
            (repo/'dbt'/'models'/'summary.sql').write_text(f'select cast("{g}" as varchar) as category, count(*) as record_count, sum({expression}) as amount_total from '+"{{ ref('business_objects') }} group by 1\n",encoding='utf-8')
            (repo/'dbt'/'tests'/'generated_valid_amount.sql').write_text("select * from {{ ref('business_objects') }} where \""+a+'" is not null and (trim(cast("'+a+'" as varchar))=\'\' or '+expression+' is null)\n',encoding='utf-8')
        semantics={'version':2,'object':{'label':spec['object_label'],'id':'object_id','fields':fields,'actions':['note','tag','export']},'relationships':joins,'filters':spec['filters'],'computations':spec['computations'],'metric':{'label':spec['amount_label'],'field':spec['amount_field'],'group_by':spec['group_by'],'status':spec['metric_status'],'definition':spec['metric_definition'],'currency':spec['currency']}}
        (repo/'semantics'/'objects.yml').write_text(yaml.safe_dump(semantics,allow_unicode=True,sort_keys=False),encoding='utf-8')
        (repo/'README.md').write_text('# '+name+'\n\n来源按全量快照采集；多对一关联的右侧键必须唯一，失败不发布。凭证与原件不进入仓库。\n',encoding='utf-8')
        if not (repo/'.git').exists():
            git(repo,'init','-b','main');git(repo,'config','user.name','Personal Data Agent');git(repo,'config','user.email','agent@localhost')
        git(repo,'add','.');git(repo,'commit','-m','Generate release candidate with data quality and semantics')
        commit=git(repo,'rev-parse','HEAD')
        values={'application_spec':app,'application_customized':application is not None or bool(existing and existing.get('application_customized')),'name':name,'spec':spec,'commit':commit,'release_mode':release_mode,'runtime':runtime,'source_ids':[source['id']]+[j['source_id'] for j in joins],'candidate_run':None,'business_plan_confirmation_required':True}
        values['knowledge_basis']=knowledge_basis
        if existing and existing.get('pending_schedule'):values['pending_schedule']=dict(existing['pending_schedule'],commit=commit)
        if project_id:values['project_id']=project_id
        # Bind the idempotency marker to exactly this immutable commit. A later
        # unrelated edit must not inherit an earlier operation's marker.
        values['origin_turn']=origin_turn or None
        values['origin_commit']=commit if origin_turn else None
        if existing:return patch('pipeline',pipeline_id,**values)
        return put('pipeline',dict(values,id=pipeline_id,source_id=source['id'],repository=str(repo),created_at=now(),status='ready',cron=None,published_run=None,active_commit=None,strategy='full_refresh'))

def checkout(pipeline,run_dir):
    out=Path(run_dir)/'code';out.mkdir(parents=True,exist_ok=True)
    proc=subprocess.run([executable('git'),'-C',pipeline['repository'],'archive',pipeline['commit']],capture_output=True,check=True,timeout=30)
    import io,tarfile
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as archive:archive.extractall(out,filter='data')
    return out
