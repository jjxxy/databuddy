import json,hashlib,os,sys
from pathlib import Path
import dlt,duckdb
from .connectors import read_source,SourceError
from .store import get,now
from .errors import TaskError

def ingest(run_dir,code_dir):
    run_dir=Path(run_dir);plan=json.loads((Path(code_dir)/'plan.json').read_text(encoding='utf-8'))
    from .rules import validate_legacy_numeric,NUMERIC_POLICY
    try:validate_legacy_numeric(plan['spec'],current_policy=plan.get('numeric_policy')==NUMERIC_POLICY)
    except ValueError as error:raise TaskError(str(error),'SQL_INEXACT_NUMERIC') from None
    frozen=json.loads((run_dir/'sources.json').read_text(encoding='utf-8')) if (run_dir/'sources.json').exists() else {}
    resources=[];inputs=[]
    all_sources=[{'source_id':plan['source_id'],'table':'records','schema_labels':plan['schema_labels'],'fields':[{'name':x} for x in plan['schema_names']]}]+plan.get('joins',[])
    for entry in all_sources:
        s=frozen.get(entry['source_id']) or get('source',entry['source_id'])
        records,fields,evidence,_=read_source(s,allow_empty=plan['spec'].get('allow_empty_source',False),expected_labels=entry['schema_labels'])
        if [x['name'] for x in fields]!=[x['name'] for x in entry['fields']] or [x['label'] for x in fields]!=entry['schema_labels']:
            raise TaskError('来源字段发生变化。重新探查、检查影响并生成新版本。正式结果保留。','SCHEMA_CHANGED')
        # Preserve entirely-null columns with explicit nullable type hints.
        hints={'record_hash':{'data_type':'text','nullable':False}}
        for field in fields:
            if all(r.get(field['name']) is None for r in records):hints[field['name']]={'data_type':'text','nullable':True}
        resources.append(dlt.resource(records,name=entry['table'],write_disposition='replace',columns=hints))
        inputs.append({'source_id':s['id'],'source_revision':s.get('revision',1),'collected_at':now(),'records':len(records),'fields':fields,'evidence':evidence,'input_hash':hashlib.sha256(json.dumps(records,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()})
    dest=dlt.destinations.duckdb(str(run_dir/'warehouse.duckdb'))
    pipeline_name='personal_'+run_dir.parent.name+'_'+run_dir.name
    pipelines_dir=str(run_dir/'dlt')
    if os.name=='nt':
        # dlt adds its own normalization/load directory hierarchy. Keeping it
        # inside runs/<id>/attempt-<n> exceeds Win32 MAX_PATH even for an ordinary
        # user profile. Use an app-owned short, attempt-specific working path;
        # Never use a verbatim-path prefix here: dlt appends relative dot
        # components which Windows deliberately rejects in verbatim paths.
        from .config import DATA
        identity=hashlib.sha256(str(run_dir.resolve()).encode('utf-8')).hexdigest()[:16]
        pipeline_name='p_'+identity
        work=str((DATA/'dlt-work'/identity).resolve())
        pipelines_dir=work
    pipe=dlt.pipeline(pipeline_name=pipeline_name,pipelines_dir=pipelines_dir,destination=dest,dataset_name='raw')
    pipe.run(resources)
    with duckdb.connect(str(run_dir/'warehouse.duckdb')) as con:
        con.execute('create schema if not exists raw')
        previous=json.loads((run_dir/'previous_schema.json').read_text(encoding='utf-8')) if (run_dir/'previous_schema.json').exists() else {}
        for entry,info in zip(all_sources,inputs):
            if info['records']==0:
                columns=[f'"{f["name"]}" varchar' for f in entry['fields']]+['record_hash varchar','_source_index bigint','_dlt_id varchar','_dlt_load_id varchar']
                if entry['source_id'] in previous:
                    columns=['"'+c['name']+'" '+c['type'] for c in previous[entry['source_id']]]
                con.execute('drop table if exists raw.'+entry['table'])
                con.execute('create table raw.'+entry['table']+' ('+', '.join(columns)+')')
    (run_dir/'input.json').write_text(json.dumps({'sources':inputs,'source_id':plan['source_id'],'input_hash':hashlib.sha256(json.dumps(inputs,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),'strategy':'full_refresh'},ensure_ascii=False,indent=2),encoding='utf-8')

def main():
    folder=Path(os.environ['RUN_DIR'])
    try:ingest(folder,os.environ['CODE_DIR'])
    except Exception as e:
        error={'message':str(e) if isinstance(e,(SourceError,TaskError)) else '采集失败，请检查只读连接和来源格式','code':getattr(e,'code','SOURCE_FAILED'),'retryable':getattr(e,'retryable',False)}
        (folder/'error.json').write_text(json.dumps(error,ensure_ascii=False),encoding='utf-8');sys.exit(3)

if __name__=='__main__':main()
