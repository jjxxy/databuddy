"""Strict, non-computational review of a single extracted original value.

This grant is deliberately separate from workflow publication and lineage:
no caller may use it to approve a report, calculation, chart or downstream run.
"""
import json
import re
from . import project_data as data, workspace
from .data_workflows import digest
from .jobs import get_job
from .result_delivery import read_handoff
from .store import get, put, now

KIND='single_fact_value_review'
WARNING='仅核对一条原文数值；不代表财务批准，不允许计算、汇总、关联、图表、报告或下游加工。'


def create_candidate(pid,kind,review_id,expected_fact_sha256,expected_original_revision,expected_original_sha256,retry_run_id=None):
    """Create an idempotent exact-text projection; never grant review or publish."""
    if kind=='text':
        from .fact_service import _owned
    elif kind=='visual':
        from .visual_fact_service import _owned
    else:raise ValueError('事实来源类型无效')
    with workspace.lock(pid):
        record,payload=_owned(pid,review_id)
        if (record.get('status')!='approved' or record.get('content_sha256')!=expected_fact_sha256
                or not record.get('output_source_id') or record.get('fact_count')!=1
                or not isinstance(payload.get('facts'),list) or len(payload['facts'])!=1):
            raise ValueError('仅支持已核对且恰好一条事实的记录，请刷新后核对')
        if (payload.get('source_revision')!=expected_original_revision
                or payload.get('source_file_sha256')!=expected_original_sha256):
            raise ValueError('原件版本或摘要已变化，请刷新后核对')
        sid=record['output_source_id'];source=data.owned(pid,'source',sid)
        if source.get('archived') or source.get('kind')!='file':raise ValueError('已核对来源不可用')
        rows,fields,evidence,_=data.read_input(pid,{'source_id':sid})
        if len(rows)!=1 or evidence.get('rows')!=1:raise ValueError('原值来源必须是完整的单行事实')
        fact=payload['facts'][0];columns=[];fact_id=None
        for field in fields:
            label=field['label'];column={'name':field['name'],'label':label,'meaning':label,'data_type':'text','aggregation':'unknown'}
            if label=='fact_id':fact_id=field['name']
            if label=='value':column.update(meaning=fact['measure'],unit=fact['unit'],period=fact['period'],aggregation='non_additive')
            columns.append(column)
        if not fact_id:raise ValueError('单条事实缺少唯一行标识')
        names=[column['name'] for column in columns]
        raw={'version':1,'plan':{'name':'单条原值核对','inputs':{'facts':{'columns':columns,'grain':[fact_id],
              'meaning':'仅供核对的原文单条事实'}},'stages':[{'id':'keep','kind':'select','input':'facts',
              'description':'原样保留单条事实字段','columns':names}],'output':'keep'},
              'bindings':{'facts':{'source_id':sid}}}
        document=data.WorkflowDocument.model_validate(raw)
        data._verify_review_originals(pid,{'workflow_document':document.model_dump(),'source_snapshot':{sid:source}})
        plan_sha=digest(document.model_dump())
        identity=digest([1,pid,kind,review_id,expected_fact_sha256,source.get('revision',1),source.get('content_hash')])
        path='workflows/single-fact-'+identity[:40]+'.json'
        try:existing=workspace.read_file(pid,path)
        except FileNotFoundError:workspace.write_file(pid,path,json.dumps(document.model_dump(),ensure_ascii=False,indent=2))
        else:
            try:existing_document=data.WorkflowDocument.model_validate_json(existing)
            except ValueError:raise ValueError('单事实候选方案路径已有其他内容，未覆盖') from None
            if digest(existing_document.model_dump())!=plan_sha:raise ValueError('单事实候选方案已被修改，未覆盖')
        wid=data.register(pid,path)['workflow_id']
        key='single_value_'+digest([pid,wid,plan_sha,identity])
        if retry_run_id:
            previous=get_job(retry_run_id)
            if (previous.get('project_id')!=pid or previous.get('target')!=wid or previous.get('kind')!='data_workflow'
                    or previous.get('commit')!=plan_sha or previous.get('status') not in ('failed','cancelled')):
                raise ValueError('仅能明确重试此候选已失败或取消的任务')
            key='single_value_retry_'+digest([key,retry_run_id])
        queued=data.enqueue_workflow(pid,wid,scheduled_key=key)
        frozen=queued.get('source_snapshot',{}).get(sid,{})
        if (queued.get('commit')!=plan_sha or frozen.get('content_hash')!=source.get('content_hash')
                or frozen.get('revision',1)!=source.get('revision',1)):
            raise ValueError('候选来源或方案已变化，请刷新后重试')
        return {'workflow_id':wid,'run_id':queued['id'],'status':queued['status'],'plan_sha256':plan_sha}


def preview(pid,rid):
    folder,meta=data.result_folder(pid,rid)
    run=get_job(rid)
    if run.get('status')!='validated':raise ValueError('只能核对尚未采用的单事实候选；历史成果保持只读')
    raw=run.get('workflow_document')
    if not isinstance(raw,dict) or digest(raw)!=meta.get('plan_sha256') or run.get('commit')!=meta.get('plan_sha256'):
        raise ValueError('冻结方案不可核对，请重新运行')
    document=data.WorkflowDocument.model_validate(raw)
    parent=data.owned(pid,'data_workflow',run['target'])
    if digest(data.load_document(pid,parent['path']).model_dump())!=meta['plan_sha256']:
        raise ValueError('当前方案已变化，请重新运行并核对')
    if (len(document.bindings)!=1 or len(document.plan.inputs)!=1 or len(document.plan.stages)!=1
            or document.contract is not None or document.reviewed_mapping_ids):
        raise ValueError('单事实原值核对仅支持一个来源和一个直接选列步骤')
    alias,binding=next(iter(document.bindings.items()));stage=document.plan.stages[0]
    if (not binding.source_id or binding.result_id or binding.column_map or binding.options!=data.ReadOptions()
            or stage.kind!='select' or stage.input!=alias or document.plan.output!=stage.id
            or set(document.plan.inputs)!={alias}):
        raise ValueError('原值核对不支持计算、筛选、映射或其他处理')
    snapshot=run.get('source_snapshot')
    source=snapshot.get(binding.source_id) if isinstance(snapshot,dict) else None
    if not isinstance(source,dict) or source.get('project_id')!=pid or source.get('id')!=binding.source_id:
        raise ValueError('来源快照不可核对')
    current=data.owned(pid,'source',binding.source_id)
    if current.get('revision',1)!=source.get('revision',1) or current.get('content_hash')!=source.get('content_hash'):
        raise ValueError('来源版本已变化，请重新核对')
    data._verify_review_originals(pid,run)
    if source.get('fact_review_id') and not source.get('visual_fact_review_id'):
        from .fact_service import _owned
        record,payload=_owned(pid,source['fact_review_id']);kind='text'
    elif source.get('visual_fact_review_id') and not source.get('fact_review_id'):
        from .visual_fact_service import _owned
        record,payload=_owned(pid,source['visual_fact_review_id']);kind='visual'
    else:raise ValueError('该候选不是可核对的单条文字或视觉事实')
    facts=payload.get('facts')
    if not isinstance(facts,list) or len(facts)!=1 or record.get('fact_count')!=1:
        raise ValueError('原值核对仅支持提案中恰好一条事实')
    fact=facts[0]
    rows,fields,evidence,_=data.read_input(pid,binding,frozen_source=source)
    if len(rows)!=1 or evidence.get('rows')!=1:raise ValueError('原值核对仅支持完整来源恰好一行')
    labels={field['label']:field['name'] for field in fields}
    required={'fact_id','measure','value','value_as_printed','unit','period','period_as_printed','dimensions','source_id','source_locator','source_file_sha256'}
    if len(labels)!=len(fields) or not required.issubset(labels):raise ValueError('事实字段不完整')
    names=[field['name'] for field in fields]
    schema=document.plan.inputs[alias]
    if stage.columns!=names or [col.name for col in schema.columns]!=names:
        raise ValueError('原值核对要求按原顺序保留所有原始字段')
    row=rows[0]
    from .fact_service import canonical_sha256,_period
    if (row[labels['fact_id']]!=canonical_sha256([record['id'],0]) or
        row[labels['measure']]!=fact.get('measure') or row[labels['value']]!=fact.get('normalized_value') or
        row[labels['value_as_printed']]!=fact.get('value_as_printed') or row[labels['unit']]!=fact.get('unit') or
        row[labels['period']]!=_period(fact.get('period')) or row[labels['period_as_printed']]!=fact.get('period') or
        row[labels['source_id']]!=payload.get('source_id') or row[labels['source_file_sha256']]!=payload.get('source_file_sha256')):
        raise ValueError('单事实行与原始事实不一致')
    try:dimensions=json.loads(row[labels['dimensions']])
    except (ValueError,TypeError):raise ValueError('事实维度不可核对') from None
    if dimensions!=fact.get('dimensions'):raise ValueError('事实维度与原始事实不一致')
    expected_locator=('page:'+str(payload['page']) if kind=='visual'
                      else 'text:'+str(fact['start'])+':'+str(fact['end']))
    if row[labels['source_locator']]!=expected_locator:raise ValueError('事实原件位置不一致')
    review_label='visual_fact_review_id' if kind=='visual' else 'fact_review_id'
    if review_label not in labels or row[labels[review_label]]!=record['id']:
        raise ValueError('事实核对记录不一致')
    if kind=='text' and ('source_text_sha256' not in labels or
                         row[labels['source_text_sha256']]!=payload['source_text_sha256']):
        raise ValueError('原文摘要不一致')
    for name,dimension in dimensions.items():
        if 'dim:'+name not in labels or row[labels['dim:'+name]]!=dimension:
            raise ValueError('事实维度列与原始事实不一致')
    for col,field in zip(schema.columns,fields):
        if col.label!=field['label'] or col.data_type!='text' or col.value_format!='native':
            raise ValueError('原值字段须保留原名称及文本类型')
        if field['label']=='value':
            if (col.meaning!=fact.get('measure') or col.unit!=fact.get('unit') or col.period!=fact.get('period')
                    or col.aggregation not in ('unknown','non_additive')):
                raise ValueError('原值的指标、单位、期间或不可加性不一致')
        elif col.meaning!=field['label'] or col.aggregation not in ('unknown','non_additive'):
            raise ValueError('原始字段含义不一致或被错误标记为可加')
    page=read_handoff(folder/'handoff',limit=2,expected_manifest_sha256=meta['handoff_sha256'])
    if page['total']!=1 or len(page['rows'])!=1 or page['rows'][0]!=row:
        raise ValueError('完整输出与单条原始事实不一致')
    output=page['schema']
    if isinstance(output,dict):output=output.get('columns',output.get('fields'))
    if (not isinstance(output,list) or [x.get('name') for x in output]!=names
            or output!=[col.model_dump() for col in schema.columns]
            or page['context'].get('output_semantics')!=schema.model_dump()):
        raise ValueError('输出字段与原始事实不一致')
    original={'id':payload['source_id'],'revision':payload['source_revision'],'sha256':payload['source_file_sha256']}
    if kind=='visual':original['page']=payload['page']
    else:original.update(text_sha256=payload['source_text_sha256'],text_start=fact['start'],text_end=fact['end'])
    content={'project_id':pid,'run_id':rid,'plan_sha256':meta['plan_sha256'],'handoff_sha256':meta['handoff_sha256'],
             'source':{'id':binding.source_id,'revision':source.get('revision',1),'sha256':source['content_hash']},
             'original':original,'fact_review':{'id':record['id'],'sha256':record['content_sha256'],'kind':kind},
             'fact':fact,'row':row,'schema':page['schema'],'input_schema':schema.model_dump(),
             'source_snapshot_sha256':digest(snapshot),'input_evidence_sha256':digest(page['context'].get('input_evidence')),
             'scope':'single_fact_original_value_only','warning':WARNING}
    return dict(content,preview_sha256=digest(content))


def _valid_receipt(receipt,ident,pid,rid,expected):
    keys={'id','version','method','project_id','run_id','preview_sha256','reviewer','reviewed_at','scope','receipt_sha256'}
    if not isinstance(receipt,dict) or set(receipt)!=keys:return False
    content={k:v for k,v in receipt.items() if k not in ('id','receipt_sha256')}
    return (receipt['id']==ident and type(receipt['version']) is int and receipt['version']==1
            and receipt['method']=='authenticated_single_value_confirmation' and receipt['scope']=='single_fact_original_value_only'
            and receipt['project_id']==pid and receipt['run_id']==rid and receipt['preview_sha256']==expected
            and isinstance(receipt['reviewer'],str) and re.fullmatch(r'owner|u_[a-f0-9]{64}',receipt['reviewer']) is not None
            and isinstance(receipt['reviewed_at'],str) and bool(receipt['reviewed_at']) and receipt['receipt_sha256']==digest(content))


def confirm(pid,rid,expected,actor):
    if not isinstance(actor,str) or not re.fullmatch(r'owner|u_[a-f0-9]{64}',actor):raise ValueError('需要已认证的使用者')
    with workspace.lock(pid):
        value=preview(pid,rid)
        if value['preview_sha256']!=expected:raise ValueError('原文、方案或结果已变化，请重新核对')
        ident='sfv_'+digest([pid,rid,expected])[:40]
        try:receipt=get(KIND,ident)
        except KeyError:
            content={'version':1,'method':'authenticated_single_value_confirmation','project_id':pid,'run_id':rid,
                'preview_sha256':expected,'reviewer':actor,'reviewed_at':now(),'scope':'single_fact_original_value_only'}
            receipt=put(KIND,dict(content,receipt_sha256=digest(content)),ident)
        if not _valid_receipt(receipt,ident,pid,rid,expected):
            raise ValueError('原值核对回执不一致')
        return {'receipt_id':ident,'preview_sha256':expected,'scope':'single_fact_original_value_only',
                'download_url':f'/api/projects/{pid}/data-results/{rid}/single-fact-review/download'}


def export(pid,rid):
    value=preview(pid,rid);ident='sfv_'+digest([pid,rid,value['preview_sha256']])[:40]
    try:receipt=get(KIND,ident)
    except KeyError:raise ValueError('请先明确核对单条原文数值') from None
    if not _valid_receipt(receipt,ident,pid,rid,value['preview_sha256']):
        raise ValueError('原值核对回执不一致')
    return {'scope':value['scope'],'warning':WARNING,'original':value['original'],'fact_review':value['fact_review'],
            'fact':value['fact'],'source':value['source'],'row':value['row'],'schema':value['schema'],
            'plan_sha256':value['plan_sha256'],'handoff_sha256':value['handoff_sha256'],
            'review':{'reviewed_at':receipt['reviewed_at'],'reviewer':receipt['reviewer']}}
