"""Owner-facing facts for conversation decisions, never model-generated status."""
import hashlib,json
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
from .store import get,all_items
from .jobs import list_jobs,get_job
from .releases import manifest_for


def freshness(p,manifest=None):
    m=manifest or {};stamp=m.get('data_as_of') or p.get('data_as_of')
    threshold=(m.get('runtime') or p.get('active_runtime') or p.get('runtime') or {}).get('freshness_minutes',1440)
    try:age=max(0,(datetime.now(timezone.utc)-datetime.fromisoformat(stamp)).total_seconds()/60)
    except (ValueError,TypeError):age=None
    return {'data_as_of':stamp,'age_minutes':round(age,1) if age is not None else None,'threshold_minutes':threshold,'stale':age is None or age>threshold}


def provenance(m):
    result=[]
    for entry in m.get('inputs',[]):
        try:s=get('source',entry['source_id'])
        except KeyError:continue
        result.append({'source_id':s['id'],'name':s['name'],'kind':s['kind'],'revision':entry.get('revision',entry.get('source_revision')),'collected_at':entry.get('collected_at'),'strategy':'全量快照'})
    return result


def _fingerprints(folder,m):
    from .applications import connection,quoted
    fields=[f['name'] for f in m['fields']]
    fingerprints={}
    with connection(folder) as con:
        cur=con.execute('select object_id,'+','.join(quoted(f) for f in fields)+' from business.business_objects')
        while rows:=cur.fetchmany(512):
            for row in rows:
                fingerprints[row[0]]=hashlib.sha256(json.dumps(list(zip(fields,row[1:])),ensure_ascii=False,default=str,separators=(',',':')).encode()).digest()
    return fingerprints


def _metric_total(folder):
    from .applications import connection
    with connection(folder) as con:
        value=con.execute('select sum(amount_total) from business.summary').fetchone()[0]
    return Decimal(str(value)) if value is not None else Decimal(0)


def comparison(p,run_id=None):
    rid=run_id or p.get('candidate_run') or p.get('published_run')
    if not rid:return None
    run=get_job(rid)
    if run['target']!=p['id']:raise ValueError('对比结果不属于当前应用')
    folder,new=manifest_for(run);previous=p.get('published_run')
    if not previous or previous==rid:return {'kind':'initial' if not previous else 'current','records':new['records'],'run_id':rid}
    old_run=get_job(previous)
    if old_run.get('artifact_removed'):return {'kind':'unavailable','reason':'旧结果已清理，无法完整对比','run_id':rid}
    old_folder,old=manifest_for(old_run)
    before=_fingerprints(old_folder,old);after=_fingerprints(folder,new)
    shared=before.keys()&after.keys()
    changed=sum(before[k]!=after[k] for k in shared)
    differences=[]
    labels={'filters':'筛选条件','computations':'计算口径','group_by':'汇总维度','amount_field':'汇总数值','primary_key':'记录标识','metric_definition':'指标说明','currency':'币种/单位','joins':'关联关系'}
    for key,label in labels.items():
        if old['spec'].get(key)!=new['spec'].get(key):differences.append(label)
    if old.get('application')!=new.get('application'):differences.append('应用页面')
    result={'kind':'compared','run_id':rid,'previous_run_id':previous,'before_records':old['records'],'after_records':new['records'],'added':len(after.keys()-before.keys()),'removed':len(before.keys()-after.keys()),'changed':changed,'definition_changes':differences,'identity_changed':old['spec'].get('primary_key')!=new['spec'].get('primary_key'),'identity_note':'使用稳定主键匹配；没有主键时内容变化可能显示为删除及新增' if not new['spec'].get('primary_key') else '按稳定主键匹配，字段变化计为修改'}
    same_metric=all(old['spec'].get(k)==new['spec'].get(k) for k in ('amount_field','group_by','currency','computations','filters','metric_definition'))
    if same_metric and new['spec'].get('amount_field'):
        try:
            a=_metric_total(old_folder)
            b=_metric_total(folder)
            result['metric_delta']=str(b-a)
        except (InvalidOperation,KeyError):pass
    else:result['metric_comparable']=False
    return result


def attention(pid):
    rows=[]
    for p in all_items('pipeline'):
        if p.get('project_id')!=pid or p.get('archived'):continue
        last=list_jobs(limit=1,target=p['id']);last=last[0] if last else None
        issues=[]
        if p.get('schedule_error'):issues.append('更新计划需要修复，当前未正常调度')
        if last and last['status']=='failed':issues.append('最近一次更新失败，旧结果保留' if p.get('published_run') else '生成尚未成功')
        f=freshness(p)
        if p.get('published_run') and f['stale']:issues.append('数据已超过设定时效')
        if p.get('candidate_run'):issues.append('有新结果等待确认')
        if issues:rows.append({'application_id':p['id'],'name':(p.get('application_spec') or {}).get('title',p['name']),'issues':issues,'schedule':p.get('cron'),'next_due':p.get('next_due'),'freshness':f})
    return rows


def health_answer(pid):
    notices=attention(pid)
    if not notices:return '当前没有发现更新失败、超时效或待确认的应用。'
    return '需要关注：\n'+'\n'.join('• '+n['name']+'：'+'；'.join(n['issues']) for n in notices)
