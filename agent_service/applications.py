"""Versioned data applications: generated UI code + validated pipeline + snapshots.

A pipeline ID is the stable application ID. UI configuration is frozen into the
same Git commit and result manifest as its transformations, avoiding draft UI
being accidentally served over an older published schema.
"""
import csv,io,json,threading
from decimal import Decimal
from pathlib import Path
from contextlib import contextmanager
import duckdb
from .models import ApplicationSpec
from .store import get,all_items
from .jobs import get_job,list_jobs,ACTIVE
from .releases import manifest_for
from .config import TIMEZONE


def configuration(name,spec,fields,value=None):
    default={'title':name,'columns':[f['name'] for f in fields][:12],'chart':'bar' if spec.get('group_by') and spec.get('amount_field') else 'none'}
    config=ApplicationSpec.model_validate(value if value is not None else default).model_dump()
    if not config['title']:config['title']=name
    known={f['name'] for f in fields}
    if not config['columns']:config['columns']=[f['name'] for f in fields][:12]
    if len(set(config['columns']))!=len(config['columns']) or any(c not in known for c in config['columns']):raise ValueError('应用界面引用了不存在或重复的字段')
    if config['default_sort'] not in known|{'object_id'}:raise ValueError('应用默认排序字段不存在')
    if config['chart']=='bar' and not (spec.get('group_by') and spec.get('amount_field')):raise ValueError('汇总图需要分组和数值字段；明细应用请选择无图表')
    return config


def write_code(repo,pipeline_id,project_id,name,spec,fields,value=None):
    config=configuration(name,spec,fields,value)
    target=repo/'application';target.mkdir(exist_ok=True)
    static=Path(__file__).parent/'static'
    for filename in ('data-app.html','data-app.js','data-app.css'):
        # The generated, versioned app uses the same reviewed renderer as hosting.
        (target/filename).write_bytes((static/filename).read_bytes())
    payload={'application_id':pipeline_id,'project_id':project_id,'renderer_version':1,'config':config,'fields':[{'name':f['name'],'label':f['label'],'numeric':bool(f.get('numeric'))} for f in fields]}
    (target/'application.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    (target/'README.md').write_text('# '+config['title']+'\n\n本应用的采集入口、dbt规则、语义和页面由同一个Git版本管理。\n\n在Atlas同源运行 data-app.html?project='+str(project_id or '')+'&app='+pipeline_id+'。也可通过application.json读取标识。重跑和调整由Atlas任务队列及Agent执行；凭证需在目标环境单独配置，代码包不包含数据或密钥。此应用代码依赖Atlas运行时，不是脱离后端的独立安装包。\n',encoding='utf-8')
    return config


def owned(project_id,application_id):
    p=get('pipeline',application_id)
    if p.get('project_id')!=project_id:raise ValueError('应用不属于当前项目')
    if p.get('archived'):raise ValueError('应用已经归档')
    if not p.get('application_spec'):raise ValueError('这个历史分析尚未生成应用界面，可通过对话生成应用')
    return p


def card(p):
    jobs=list_jobs(limit=1,target=p['id']);last=jobs[0] if jobs else None
    state='updating' if last and last['status'] in ACTIVE else 'ready' if p.get('published_run') else 'preview' if p.get('candidate_run') else 'failed' if last and last['status']=='failed' else 'building'
    from .experience import freshness
    return {'id':p['id'],'project_id':p['project_id'],'name':p['application_spec']['title'],'description':p['application_spec'].get('description',''),'status':state,'last_run_status':last['status'] if last else None,'has_published':bool(p.get('published_run')),'has_candidate':bool(p.get('candidate_run')),'data_as_of':p.get('data_as_of'),'freshness':freshness(p),'schedule':p.get('cron'),'next_due':p.get('next_due'),'pending_schedule':(p.get('pending_schedule') or {}).get('cron'),'timezone':TIMEZONE,'source_id':p['source_id'],'code_commit':p['commit'],'url':'/data-app.html?project='+p['project_id']+'&app='+p['id']}


def catalog(project_id):
    get('project',project_id)
    return [card(p) for p in all_items('pipeline') if p.get('project_id')==project_id and p.get('application_spec') and not p.get('archived')]


class ApplicationNotReady(ValueError):pass

def snapshot(project_id,application_id,preview=False,run_id=None):
    p=owned(project_id,application_id)
    selected=run_id
    run_id=run_id or ((p.get('candidate_run') or p.get('published_run')) if preview else (p.get('published_run') or p.get('candidate_run')))
    if not run_id:raise ValueError('应用正在生成或还没有可用结果，请稍后重试')
    run=get_job(run_id)
    if run['target']!=p['id'] or not run.get('validated') or run.get('status') not in ('validated','succeeded') or run.get('artifact_removed'):raise ValueError('该应用结果版本不可用')
    folder,m=manifest_for(run)
    if not m.get('application') and not selected and p.get('candidate_run') and p['candidate_run']!=run_id:
        return snapshot(project_id,application_id,True,p['candidate_run'])
    if not m.get('application'):raise ApplicationNotReady('应用页面还在生成；旧分析的数据仍保留，等新版本完成后可预览')
    config=configuration(p['name'],m['spec'],m['fields'],m.get('application'))
    return p,run,folder,m,config


def info(project_id,application_id,preview=False,run_id=None):
    p=owned(project_id,application_id);result=card(p)
    try:
        _,run,_,m,config=snapshot(project_id,application_id,preview,run_id)
        result.update(config=config,fields=[{'name':f['name'],'label':f['label'],'numeric':bool(f.get('numeric'))} for f in m['fields']],run_id=run['id'],result_commit=run['commit'],preview=run['id']!=p.get('published_run'),can_publish=run['id']==p.get('candidate_run') and run['commit']==p['commit'],preview_type='candidate' if run['id']==p.get('candidate_run') else 'published' if run['id']==p.get('published_run') else 'history',metric_status=__import__('agent_service.metric_registry',fromlist=['status']).status(p,run,m),data_as_of=m.get('data_as_of'),records=m['records'])
        from .experience import freshness,provenance
        from .releases import semantic_warning
        warning=semantic_warning(run,m)
        result.update(freshness=freshness(p,m),sources=provenance(m),definition=m['spec'].get('metric_definition',''),semantic_warning=warning)
        if warning:result['metric_status']='requires_review'
    except ApplicationNotReady:
        result['status']='failed' if result.get('last_run_status')=='failed' else 'building'
    except ValueError:
        if p.get('published_run') or p.get('candidate_run'):raise
    return result


def quoted(name):return '"'+name.replace('"','""')+'"'

def condition(m,search):
    if len(search)>200:raise ValueError('筛选文字不能超过200字符')
    if not search:return '',[]
    fields=m['fields']
    return ' where ('+' or '.join('cast('+quoted(f['name'])+' as varchar) ilike ?' for f in fields)+')',['%'+search+'%']*len(fields)

@contextmanager
def connection(folder):
    with duckdb.connect(str(folder/'warehouse.duckdb'),read_only=True,config={'enable_external_access':False}) as con:
        con.execute("set memory_limit='256MB'");con.execute('set threads=1')
        timer=threading.Timer(5,con.interrupt);timer.start()
        try:yield con
        finally:timer.cancel()


def values(cursor):
    names=[x[0] for x in cursor.description]
    return [dict(zip(names,[str(v) if isinstance(v,Decimal) else v for v in row])) for row in cursor.fetchall()]


def data(project_id,application_id,preview=False,search='',offset=0,limit=25,sort=None,descending=False,run_id=None):
    if offset<0 or limit<1 or limit>100:raise ValueError('应用分页超出范围')
    p,run,folder,m,config=snapshot(project_id,application_id,preview,run_id)
    from .releases import ensure_candidate_bulk_allowed,semantic_warning
    blocked=None;warning=semantic_warning(run,m)
    try:ensure_candidate_bulk_allowed(run,m,project_id=project_id)
    except ValueError as error:blocked=str(error)
    where,params=condition(m,search)
    fields={f['name']:f for f in m['fields']};sort=sort or config['default_sort']
    if sort not in set(fields)|{'object_id'}:raise ValueError('应用排序字段不存在')
    columns=config['columns'];order=quoted(sort)
    if fields.get(sort,{}).get('numeric'):order='try_cast(replace(cast('+order+" as varchar), ',', '') as decimal(38,12))"
    with connection(folder) as con:
        total=con.execute('select count(*) from business.business_objects'+where,params).fetchone()[0]
        rows=values(con.execute('select object_id,'+','.join(quoted(c) for c in columns)+' from business.business_objects'+where+' order by '+order+(' desc' if descending else ' asc')+',object_id limit ? offset ?',params+[limit,offset]))
        summary=[];metric_total=None
        # Historic results can remain downloadable even when their provenance
        # is uncertain; that does not make a newly computed total trustworthy.
        if m['spec'].get('amount_field') and not blocked and not warning:
            amount=quoted(m['spec']['amount_field']);group=quoted(m['spec']['group_by'])
            expression='try_cast(replace(cast('+amount+" as varchar), ',', '') as decimal(38,12))"
            value=con.execute('select sum('+expression+') from business.business_objects'+where,params).fetchone()[0]
            metric_total=str(value) if value is not None else None
            summary=values(con.execute('select cast('+group+' as varchar) as category,count(*) as record_count,sum('+expression+') as amount_total from business.business_objects'+where+' group by 1 order by amount_total desc nulls last limit 20',params))
    return {'application':{'id':p['id'],'title':config['title'],'layout':config['layout'],'chart':config['chart']},'fields':[fields[c] for c in columns],'rows':rows,'total':total,'offset':offset,'limit':limit,'summary':summary,'summary_limit':20,'metric':{'label':m['spec'].get('amount_label'),'total':metric_total,'status':'requires_review' if warning else __import__('agent_service.metric_registry',fromlist=['status']).status(p,run,m),'currency':m['spec'].get('currency','')},'run_id':run['id'],'commit':run['commit'],'preview':run['id']!=p.get('published_run'),'data_as_of':m.get('data_as_of'),'search':search,'bulk_blocked_reason':blocked,'semantic_warning':warning}


def csv_records(project_id,application_id,preview=False,search='',run_id=None):
    _,run,folder,m,config=snapshot(project_id,application_id,preview,run_id)
    from .releases import ensure_candidate_bulk_allowed
    ensure_candidate_bulk_allowed(run,m,project_id=project_id)
    where,params=condition(m,search);fields={f['name']:f for f in m['fields']};columns=config['columns']
    def records():
        buf=io.StringIO();writer=csv.writer(buf);writer.writerow([fields[c]['label'] for c in columns]);yield '\ufeff'+buf.getvalue();buf.seek(0);buf.truncate(0)
        with connection(folder) as con:
            cur=con.execute('select '+','.join(quoted(c) for c in columns)+' from business.business_objects'+where+' order by object_id',params)
            for row in iter(lambda:cur.fetchone(),None):
                writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@','\t','\r')) else v for v in row]);yield buf.getvalue();buf.seek(0);buf.truncate(0)
    return records()
