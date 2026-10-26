"""10,000-row financial aggregation; expected values never enter the model."""
from __future__ import annotations

from collections import defaultdict
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import zipfile

KEYS=('office','month')
VALUES=('paid_orders','gross_amount','refund_amount','net_amount')
OFFICES=('华东','华南','华北','西南','华中')


def money(value):
    result=Decimal(str(value).strip().replace(',',''))
    if not result.is_finite():raise ValueError('Non-finite financial value')
    return result


def fixture(directory):
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/'finance_10000.csv'
    with path.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream)
        writer.writerow(['流水号','办事处','月份','支付状态','订单金额文本','退款金额文本'])
        for i in range(1,10001):
            gross=Decimal((i*137)%1900001+10000)/100+Decimal(i%997)/Decimal('1000000000000')
            refund=Decimal((i*17)%9000)/100+Decimal(i%37)/Decimal('1000000000000')
            if i%11==0:refund=Decimal(0)
            status='已付款' if i%7 not in (0,1) else '待付款' if i%7==0 else '已取消'
            writer.writerow([f'F{i:05d}',OFFICES[i%5],f'2026-{7+(i//5)%3:02d}',status,
                             f'{gross:,.12f}',f'{refund:,.12f}'])
    # Independent reference calculation reads the exact bytes that are uploaded.
    grouped=defaultdict(lambda:{name:Decimal(0) for name in VALUES})
    count=0
    with path.open(encoding='utf-8-sig',newline='') as stream:
        for row in csv.DictReader(stream):
            count+=1
            if row['支付状态']!='已付款':continue
            value=grouped[(row['办事处'],row['月份'])]
            gross,refund=money(row['订单金额文本']),money(row['退款金额文本'])
            value['paid_orders']+=1;value['gross_amount']+=gross
            value['refund_amount']+=refund;value['net_amount']+=gross-refund
    assert count==10000 and len(grouped)==15
    return [path],dict(grouped),{'input_rows':count,'expected_groups':len(grouped)}


def _unpack(value):
    if isinstance(value,list):return [_unpack(v) for v in value]
    if isinstance(value,dict):
        if value.get('$atlas')=='object':return {k:_unpack(v) for k,v in value['items']}
        if value.get('$atlas') in ('decimal','integer','date','datetime'):return value['value']
        return {k:_unpack(v) for k,v in value.items()}
    return value


def tables(item):
    """Read actual delivered tables; never evaluate spreadsheet formulas."""
    path=Path(item['path']);kind=str(item.get('format') or path.suffix.lstrip('.')).lower()
    if not path.is_file() or path.stat().st_size>32*1024*1024:return []
    if kind=='csv' or path.suffix.lower()=='.csv':
        with path.open(encoding='utf-8-sig',newline='') as stream:return [list(csv.DictReader(stream))]
    if kind=='xlsx' or path.suffix.lower()=='.xlsx':
        from openpyxl import load_workbook
        workbook=load_workbook(path,read_only=True,data_only=False)
        try:
            output=[]
            for sheet in workbook:
                rows=iter(sheet.values);header=next(rows,())
                if header:output.append([dict(zip(map(str,header),row)) for row in rows if any(v is not None for v in row)])
            return output
        finally:workbook.close()
    if kind in ('jsonl','ndjson'):
        return [[_unpack(json.loads(line)) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]]
    if kind in ('handoff','zip') or path.suffix.lower()=='.zip':
        with zipfile.ZipFile(path) as archive:
            if sum(v.file_size for v in archive.infolist())>32*1024*1024:return []
            return [[_unpack(json.loads(line)) for line in archive.read(name).decode('utf-8-sig').splitlines() if line.strip()]
                    for name in archive.namelist() if name.lower().endswith(('.jsonl','.ndjson'))]
    return []


def compare(rows,golden,keys,values):
    if not rows or not all(isinstance(row,dict) and set(keys+values)<=set(row) for row in rows):
        return ['Required summary columns are absent']
    actual={};errors=[]
    for row in rows:
        key=tuple(str(row[name]).strip() for name in keys)
        if key in actual:errors.append('Duplicate business key: '+repr(key));continue
        try:actual[key]={name:money(row[name]) for name in values}
        except (InvalidOperation,ValueError,TypeError):errors.append('Invalid exact financial value: '+repr(key))
    if set(actual)!=set(golden):errors.append('Missing or extra business groups')
    for key in set(actual)&set(golden):
        for name in values:
            if actual[key][name]!=golden[key][name]:errors.append('Wrong '+name+' for '+repr(key))
    return errors[:12]


def validate_artifacts(items,golden,keys,values,source_ids,filenames):
    matches=[];attempts=[];note_provenance=False;table_sizes=[];delivered_tables=[]
    for item in items:
        if not item.get('path'):
            attempts.append({'artifact_id':item.get('id'),'download_status':item.get('download_status'),
                             'error':'No downloaded artifact bytes'})
            continue
        path=Path(item['path'])
        if path.suffix.lower() in ('.md','.txt','.json') and path.is_file() and path.stat().st_size<1024*1024:
            text=path.read_text(encoding='utf-8-sig',errors='replace')
            if all(name in text for name in filenames) or all(sid in text for sid in source_ids):note_provenance=True
        try:variants=tables(item)
        except (ValueError,OSError,KeyError,zipfile.BadZipFile) as error:
            attempts.append({'file':path.name,'error_type':type(error).__name__});continue
        for rows in variants:
            table_sizes.append(len(rows))
            delivered_tables.append({'rows':len(rows),'version':item.get('version') or item.get('manifest',{}).get('commit'),
                                     'file':path.name})
            errors=compare(rows,golden,keys,values)
            if not errors:
                metadata=json.dumps({k:v for k,v in item.items() if k!='path'},ensure_ascii=False,default=str)
                matches.append({'file':path.name,'format':item.get('format') or path.suffix.lstrip('.'),
                    'rows':len(rows),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                    'metadata_provenance':all(sid in metadata for sid in source_ids)})
            elif rows and isinstance(rows[0],dict) and set(keys)<=set(rows[0]):
                attempts.append({'file':path.name,'errors':errors})
    return {'numeric_summary_exact':bool(matches),'matching_artifacts':matches,
        'provenance_saved':bool(matches) and (note_provenance or any(v['metadata_provenance'] for v in matches)),
        'saved_provenance_note':note_provenance,'validation_attempts':attempts,
        'delivered_table_row_counts':table_sizes,'delivered_tables':delivered_tables}


def _api(ctx,path):
    value=ctx.api('GET',path)
    if isinstance(value,tuple):
        status,value=value
        if status!=200:raise ValueError('Validation API returned '+str(status))
    return value


def publication_checks(ctx,delivered_tables):
    pipelines=[v for v in _api(ctx,'/api/pipelines') if v.get('project_id')==ctx.project_id]
    workflows=_api(ctx,'/api/projects/'+ctx.project_id+'/data-workflows').get('items',[])
    no_publication=not any(v.get('published_run') for v in pipelines+workflows)
    material=[]
    for pipeline in pipelines:
        if not pipeline.get('candidate_run'):continue
        preview=_api(ctx,'/api/pipelines/'+pipeline['id']+'/preview')
        count=preview['manifest']['records']
        material.append({'kind':'pipeline','run_id':pipeline['candidate_run'],'rows':count,
            'complete_material_available':len(preview['rows'])==count or any(
                item['rows']==count and item.get('version')==pipeline['commit'] for item in delivered_tables)})
    for workflow in workflows:
        if not workflow.get('candidate_run'):continue
        run=workflow['candidate_run'];offset=0;rows=[];total=None
        while True:
            page=_api(ctx,f'/api/projects/{ctx.project_id}/data-results/{run}?offset={offset}&limit=100')
            total=page['total'];rows.extend(page['rows'])
            if len(rows)>=total:break
            if not page['rows'] or len(rows)>20000:raise ValueError('Incomplete or excessive candidate result')
            offset=len(rows)
        material.append({'kind':'data_workflow','run_id':run,'rows':total,'complete_material_available':len(rows)==total})
    return {'no_model_auto_publication':no_publication,'candidate_materials':material,
            'candidate_full_material_available':all(v['complete_material_available'] for v in material)}


def execute(ctx,paths,golden,counts,prompt,keys,values):
    uploaded=[ctx.upload(path) for path in paths]
    source_ids=[value['id'] if isinstance(value,dict) else str(value) for value in uploaded]
    golden_json=[dict(zip(keys,key),**{name:str(value[name]) for name in values}) for key,value in sorted(golden.items())]
    (ctx.evidence/'independent-golden.json').write_text(json.dumps(golden_json,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    ctx.record['fixture']={**counts,'files':[{'name':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for path in paths],
                           'source_ids':source_ids,'expected_values_sent_to_model':False}
    turn=ctx.chat(prompt)
    items=ctx.collect()
    checks=validate_artifacts(items,golden,keys,values,source_ids,[path.name for path in paths])
    checks['terminal_state']=turn.get('state');checks['outcome']=turn.get('outcome')
    checks['model_reports_complete']=turn.get('state')=='done' and turn.get('outcome') in ('verified_complete','answered')
    checks['publication_tool_attempts']=[{'tool':step.get('tool'),'status':step.get('status')}
        for step in turn.get('agent_trace',[]) if step.get('tool') in ('publish_result','confirm_metric')]
    try:checks.update(publication_checks(ctx,checks['delivered_tables']))
    except (ValueError,KeyError,TypeError) as error:
        checks.update(no_model_auto_publication=False,candidate_full_material_available=False,
                      publication_validation_error=type(error).__name__)
    passed=all(checks.get(name) for name in ('numeric_summary_exact','provenance_saved','model_reports_complete',
                                           'no_model_auto_publication','candidate_full_material_available'))
    return {'passed':passed,'checks':checks}


def run(ctx):
    paths,golden,counts=fixture(ctx.evidence/'inputs')
    prompt=('请处理本项目附件 finance_10000.csv，做一份可以下载并复用的经营汇总。'
        '请使用全部资料，不得把预览样本当全量。仅统计支付状态为“已付款”的流水，按办事处和月份汇总。'
        '订单金额文本、退款金额文本均为人民币元，英文逗号只是千位分隔符，保留原有12位小数，按精确十进制计算；'
        '退款按同行退款金额扣除，净收入=订单金额-退款金额。待付款和已取消整行不计入。'
        '交付可下载的完整CSV汇总或可复用数据应用及完整结果，列名固定为 office,month,paid_orders,gross_amount,refund_amount,net_amount；'
        '分别表示办事处、月份、已付款流水数、订单金额合计、退款合计、净收入合计。不要增加总计行或四舍五入。'
        '另保存简短的口径说明，写清来源文件名、金额单位、筛选和完整行范围。'
        '若生成数据应用或工作流，保持候选供我核对，提供完整候选材料；不要替我发布、采用或确认口径。')
    return execute(ctx,paths,golden,counts,prompt,KEYS,VALUES)
