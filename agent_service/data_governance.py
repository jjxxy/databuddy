"""Project scoped catalog and quality observations; no raw rows or credentials."""
from .store import get,all_items
from .jobs import list_jobs,get_job
from .releases import manifest_for


def owned(project_id,pipeline_id):
    p=get('pipeline',pipeline_id)
    if p.get('project_id')!=project_id:raise ValueError('应用不属于当前项目')
    return p


def quality(project_id,pipeline_id):
    p=owned(project_id,pipeline_id)
    rows=list_jobs(limit=5,target=pipeline_id)
    return {'application_id':pipeline_id,'published_run':p.get('published_run'),'candidate_run':p.get('candidate_run'),
            'runs':[{'id':r['id'],'status':r['status'],'commit':r.get('commit'),'error_code':r.get('error_code'),
                     'data_contract':r.get('data_contract'),'quality':r.get('quality',[]),'finished_at':r.get('finished_at')} for r in rows],
            'scope':'aggregate_checks_only; no_raw_records'}


def _application_asset(p):
    metric=None;run=None;state='unpublished';manifest={}
    if p.get('published_run'):
        try:
            run=get_job(p['published_run'])
            folder,manifest=manifest_for(run)
            if p.get('repository'):
                from .releases import verify_semantics
                verify_semantics(p,run,manifest,folder)
            state='published'
        except (KeyError,OSError,ValueError):
            state='published_artifact_unavailable';manifest={}
    spec=manifest.get('spec',{})
    if state=='published' and spec.get('amount_field'):
        from .metric_registry import status
        metric={'label':spec.get('amount_label'),'field':spec['amount_field'],'group_by':spec.get('group_by'),
                'definition':spec.get('metric_definition'),'status':status(p,dict(run,id=run.get('id','')),manifest),
                'currency':spec.get('currency'),'commit':run['commit'],'run_id':run.get('id')}
    return {'application_id':p['id'],'name':p['name'],'state':state,'draft_commit':p.get('commit'),
            'published_commit':run.get('commit') if run else None,'published_metric':metric,
            'sources':p.get('source_ids') or [p['source_id']],
            'published_sources':[x['source_id'] for x in manifest.get('inputs',[])],
            'data_as_of':manifest.get('data_as_of'),'records':manifest.get('records'),
            'data_contract':manifest.get('data_contract'),
            'lineage_level':'generated_column_rules' if manifest.get('column_lineage') else 'declared_source_to_application; not_column_level'}


def catalog(project_id,offset=0,limit=20,asset_type='applications'):
    get('project',project_id)
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=40:raise ValueError('无效分页参数')
    if asset_type not in ('applications','sources','metrics','all'):raise ValueError('无效资产类型')
    pipelines={p['id']:p for p in all_items('pipeline') if p.get('project_id')==project_id and not p.get('archived')}
    descriptors=[]
    if asset_type in ('applications','all'):
        descriptors.extend(('application',p['id'],p) for p in pipelines.values())
    if asset_type in ('sources','all'):
        descriptors.extend(('source',s['id'],s) for s in all_items('source')
                           if s.get('project_id')==project_id and not s.get('archived'))
    if asset_type in ('metrics','all'):
        descriptors.extend(('metric',m['id'],m) for m in all_items('metric')
                           if m.get('project_id')==project_id and m.get('application_id') in pipelines
                           and pipelines[m['application_id']].get('published_run')
                           and m['commit']==pipelines[m['application_id']].get('active_commit'))
    descriptors.sort(key=lambda d:(d[0],d[1]))
    assets=[]
    # Fetch manifests/Git only for the requested page, not every project asset.
    for kind,key,value in descriptors[offset:offset+limit]:
        if kind=='application':
            row=_application_asset(value)
        elif kind=='source':
            row={'source_id':value['id'],'name':value['name'],'state':value.get('status'),
                 'revision':value.get('revision'),'field_count':len(value.get('profile',{}).get('fields',[]))}
        else:
            from .metric_registry import current
            try:row=current(project_id,value['application_id'])
            except (KeyError,OSError,ValueError):
                row={'id':key,'application_id':value['application_id'],'state':'published_artifact_unavailable'}
        assets.append(dict(row,asset_type=kind) if asset_type!='applications' else row)
    return {'assets':assets,'total':len(descriptors),
            'next_offset':offset+limit if offset+limit<len(descriptors) else None,
            'scope':'current_project; published_semantics_and_registered_metrics_only; paginated; no_raw_records'}


def lineage(project_id,pipeline_id,offset=0,limit=20,section='columns'):
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=40 or section not in ('columns','summary'):
        raise ValueError('无效血缘分页参数')
    p=owned(project_id,pipeline_id)
    if not p.get('published_run'):raise ValueError('尚无正式版本，不能查询正式字段血缘')
    run=get_job(p['published_run'])
    if run.get('target')!=p['id']:raise ValueError('运行不属于当前应用')
    folder,manifest=manifest_for(run)
    from .releases import verify_semantics
    verify_semantics(p,run,manifest,folder)
    graph=manifest.get('column_lineage')
    if not graph:return {'application_id':p['id'],'run_id':run['id'],'available':False,'reason':'历史版本未生成字段血缘，请构建新候选；未按当前草稿推测历史规则'}
    rows=graph[section]
    tracked={r['column'] for r in graph['columns']}
    untracked=[f['name'] for f in manifest.get('output_schema',[]) if f['name'] not in tracked]
    return {'application_id':p['id'],'run_id':run['id'],'commit':run['commit'],'available':True,
            'sha256':manifest['column_lineage_sha256'],'section':section,
            'columns':rows[offset:offset+limit],'total':len(rows),
            'next_offset':offset+limit if offset+limit<len(rows) else None,
            'row_rules':graph['row_rules'],'untracked_output_columns':untracked,'scope':graph['scope']}
