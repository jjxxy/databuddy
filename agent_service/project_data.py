"""Project-scoped versioned data workflows, source bindings and immutable results.

Only the queue executes a frozen plan. Draft changes cannot alter the published
plan, and a failed refresh never replaces the last verified result.
"""
import hashlib,json,re,threading,time,zipfile
from functools import lru_cache
from pathlib import Path
from typing import Annotated,Literal
from pydantic import Field,field_validator,model_serializer
from sqlalchemy import select,update
from .models import StrictModel
from .config import DATA,MAX_ROWS
from .store import get,put,patch,all_items,uid,now,unseal,transaction,items
from . import workspace
from .data_workflows import WorkflowError,WorkflowPlan,execute,digest

_TEXT_PROJECTION_MAX_BYTES=20*1024*1024


@lru_cache(maxsize=1)
def processing_fingerprint():
    """Conservative process build identity; unknown older engines cannot reuse."""
    import importlib.metadata,platform,sys
    root=Path(__file__).resolve().parent
    return digest({'version':1,'python':sys.version,'platform':platform.platform(),
        'max_rows':MAX_ROWS,
        'code':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob('*.py'))},
        'packages':sorted((d.metadata.get('Name',''),d.version) for d in importlib.metadata.distributions())})


class ReadOptions(StrictModel):
    sheet:str|None=Field(default=None,max_length=100)
    table:str|None=Field(default=None,max_length=100)
    header_rows:list[int]|None=Field(default=None,max_length=20)
    cell_range:str|None=Field(default=None,max_length=100)
    data_start_row:int|None=Field(default=None,ge=1)
    data_end_row:int|None=Field(default=None,ge=1)
    merged_label_columns:list[str]=Field(default_factory=list,max_length=50)
    percentage_columns:dict[str,Literal['fraction','points']]=Field(default_factory=dict,max_length=50)
    text_columns:list[Annotated[str,Field(pattern=r'^c_(?:[1-9][0-9]?|1[0-9]{2}|2[0-4][0-9]|25[0-6])$')]]=Field(
        default_factory=list,max_length=200,json_schema_extra={'uniqueItems':True},
        description='仅XLSX：明确列出原始c_N列，将Decimal/整数无损投影为非科学记数精确文本，原字符串与空值保持；供混合金额后续normalize使用。不推断单位，不可用于百分比列；float/bool拒绝。')
    html_table:int=Field(default=0,ge=0,le=200)
    hidden_rows:Literal['include','exclude','error']='error'
    subtotal_policy:Literal['preserve','error']='error'

    @field_validator('text_columns',mode='before')
    @classmethod
    def unique_text_columns(cls,value):
        if not isinstance(value,list) or any(not isinstance(item,str) for item in value) or len(set(value))!=len(value):
            raise ValueError('text_columns必须是唯一原始c_N标识的列表')
        return value

    @model_serializer(mode='wrap')
    def stable_empty_projection(self,handler):
        # Adding an opt-in reader option must not change old frozen plan hashes.
        value=handler(self)
        if not self.text_columns:value.pop('text_columns',None)
        return value


class InputBinding(StrictModel):
    source_id:str|None=Field(default=None,max_length=64)
    result_id:str|None=Field(default=None,max_length=64)
    options:ReadOptions=Field(default_factory=ReadOptions)
    column_map:dict[str,str]=Field(default_factory=dict,max_length=200)


class WorkflowDocument(StrictModel):
    version:Literal[1]=1
    plan:WorkflowPlan
    bindings:dict[str,InputBinding]=Field(min_length=1,max_length=10)
    contract:dict|None=None
    reviewed_mapping_ids:list[str]=Field(default_factory=list,max_length=20)


class RegisterWorkflow(StrictModel):
    path:str=Field(min_length=1,max_length=200)
class WorkflowTarget(StrictModel):
    workflow_id:str=Field(min_length=1,max_length=64)
class RunTarget(StrictModel):
    run_id:str=Field(min_length=1,max_length=64)
class SourceTable(StrictModel):
    source_id:str=Field(min_length=1,max_length=64)
    options:ReadOptions=Field(default_factory=ReadOptions)
    offset:int=Field(default=0,ge=0)
    limit:int=Field(default=25,ge=1,le=100)
class ExportResult(RunTarget):
    precision_policy:Literal['reject','text_exact']='text_exact'
class PublishWorkflow(RunTarget):
    expected_plan_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
class ScheduleWorkflow(WorkflowTarget):
    cron:str|None=None
    watch_sources:bool=False


def owned(pid,kind,ident):
    value=get(kind,ident)
    if value.get('project_id')!=pid:raise ValueError('数据对象不属于当前项目')
    return value


def source_path(source):
    from .connectors import verified_file_source
    return verified_file_source(source)[0]


def inspect_source(pid,sid):
    s=owned(pid,'source',sid)
    if s['kind']=='file' and s['name'].lower().endswith('.xlsx'):
        from .workbook_reader import inspect_workbook
        report=inspect_workbook(source_path(s));source_path(s)
        return dict(report,source_id=sid,source_revision=s.get('revision',1))
    return {'source_id':sid,'source_revision':s.get('revision',1),'fields':s.get('profile',{}).get('fields',[]),
            'evidence':s.get('profile',{}).get('evidence',{}),'status':s.get('status')}


def _workbook_semantic_review_reasons(table):
    cells=list(table.get('header_cells',{}).values())+[cell for row in table.get('special_cells',{}).values() for cell in row.values()]
    reasons=[]
    if table.get('row_roles'):reasons.append('ROW_ROLE_REVIEW_REQUIRED')
    if any('formula' in cell for cell in cells):reasons.append('FORMULA_CACHE_FRESHNESS_UNKNOWN')
    if any(w.get('code')=='EXCEL_PRECISION_LIMIT' for w in table.get('warnings',[])):reasons.append('EXCEL_PRECISION_LIMIT')
    return reasons


def _project_text_columns(rows,fields,options):
    """Explicit storage projection only; source semantics and raw cells stay put."""
    from decimal import Decimal
    from .workbook_reader import WorkbookReadError
    columns=options.text_columns
    if not columns:return rows,None
    known={field['name']:field for field in fields}
    for column in columns:
        if column not in known:
            raise WorkbookReadError('text_columns包含当前读取范围外的列，未进行投影','TEXT_PROJECTION_INVALID',{'column':column})
        field=known[column]
        if (column in options.percentage_columns or field.get('stored_semantics') in
                ('ratio','percent_points','ambiguous_percentage_representation')):
            raise WorkbookReadError('百分比字段不能投影为普通文本以隐藏表示约束；请保留显式百分比策略和数值语义',
                                    'TEXT_PROJECTION_PERCENTAGE_CONFLICT',{'column':column})
    projected=[];projected_bytes=0;counts={column:{'Decimal':0,'int':0,'str':0,'NoneType':0} for column in columns}
    for index,row in enumerate(rows,1):
        copied=dict(row)
        for column in columns:
            value=row[column];kind=type(value)
            if value is None:counts[column]['NoneType']+=1
            elif kind is str:counts[column]['str']+=1
            elif kind is int or kind is Decimal and value.is_finite():
                # Check the projected allocation before formatting. A finite
                # Decimal('1e1000000000') must not allocate a gigabyte of zeros.
                maximum=4096;too_large=False
                if kind is int:
                    too_large=value.bit_length()>maximum*3322//1000+1
                    estimate=max(1,value.bit_length()*30103//100000+1)+int(value<0)
                else:
                    adjusted=value.adjusted()
                    if adjusted>=maximum or adjusted < -maximum:too_large=True
                    else:
                        components=value.as_tuple();exponent=components.exponent
                        estimate=components.sign+max(1,len(components.digits)+exponent)+(1-exponent if exponent<0 else 0)
                        too_large=estimate>maximum
                if too_large:
                    raise WorkbookReadError('精确数值文本超过单元格投影的内存上限，未转换','TEXT_PROJECTION_LIMIT',{'column':column,'row_index':index})
                if projected_bytes+estimate>_TEXT_PROJECTION_MAX_BYTES:
                    raise WorkbookReadError('精确数值文本累计超过投影内存上限，未继续转换','TEXT_PROJECTION_LIMIT',{'column':column,'row_index':index})
                text=str(value) if kind is int else format(value,'f')
                if len(text)>maximum:
                    raise WorkbookReadError('精确数值文本超过单元格投影的内存上限，未转换','TEXT_PROJECTION_LIMIT',{'column':column,'row_index':index})
                copied[column]=text;counts[column]['int' if kind is int else 'Decimal']+=1
                projected_bytes+=len(text)
            else:
                actual={float:'float',bool:'bool',Decimal:'Decimal'}.get(kind,'other')
                raise WorkbookReadError('显式文本投影只接受有限Decimal、整数、字符串及空值，未转换不受支持的原值',
                    'TEXT_PROJECTION_UNSUPPORTED_VALUE',{'column':column,'row_index':index,'expected_data_type':'text','actual_type':actual})
        projected.append(copied)
    return projected,{'version':1,'policy':'explicit_text_columns','columns':list(columns),
                      'numeric_conversion':'Decimal/int to exact fixed-point text; strings/null unchanged',
                      'lossless':True,'original_cell_evidence_preserved':True,'type_counts':counts}


def source_requires_fact_alignment(source,evidence):
    """Fact text review does not approve mixed row meanings or aggregation.

    No currently accepted workflow receipt binds every fact, semantic meaning,
    period, unit and aggregation to this exact run. Until one exists, these
    sources remain inspectable candidates but cannot be promoted to reports or
    upstream results by a generic source review click.
    """
    source=source if isinstance(source,dict) else {}
    evidence=evidence if isinstance(evidence,dict) else {}
    def marked(value,proof):
        profile=value.get('profile')
        profile_evidence=profile.get('evidence') if isinstance(profile,dict) else None
        return bool(value.get('fact_review_id') or value.get('visual_fact_review_id')
                    or value.get('semantic_alignment_required') is True
                    or 'origin_agent_handoff' in value or 'external_provenance' in value
                    or proof.get('fact_review_id') or proof.get('visual_fact_review_id')
                    or proof.get('semantic_alignment_required') is True
                    or isinstance(profile_evidence,dict) and profile_evidence.get('semantic_alignment_required') is True)
    if marked(source,evidence):return True
    # Derived sources must not launder an ancestor's fact-review obligation.
    # Follow the immutable version referenced by each edge, not the current
    # source (which may have since been replaced). Missing or ambiguous history
    # is not evidence of safety; preserve the restriction in that case.
    if 'origin_source_id' not in source and 'origin_source_revision' not in source:return False
    project=source.get('project_id');ident=source.get('id');revision=source.get('revision',1)
    if not isinstance(project,str) or not project or not isinstance(ident,str) or type(revision) is not int or revision<1:return True
    versions={}
    for version in all_items('source_version'):
        versions.setdefault((version.get('source_id'),version.get('revision')),[]).append(version)
    seen={(ident,revision)};current=source
    for _ in range(512):
        if 'origin_source_id' not in current and 'origin_source_revision' not in current:return False
        origin=current.get('origin_source_id');number=current.get('origin_source_revision')
        if not isinstance(origin,str) or not origin or type(number) is not int or number<1:return True
        key=(origin,number)
        if key in seen:return True
        seen.add(key)
        found=versions.get(key,[])
        if len(found)!=1:return True
        parent=found[0]
        if parent.get('project_id')!=project or not isinstance(parent.get('kind'),str):return True
        if marked(parent,{}):return True
        current=parent
    return True


def source_interpretation_needs_review(source,evidence):
    """Unstructured and explicitly tentative sources need an owner decision."""
    source=source if isinstance(source,dict) else {}
    evidence=evidence if isinstance(evidence,dict) else {}
    name=str(source.get('name') or '').lower()
    unstructured=source.get('kind')=='file' and name.endswith(
        ('.txt','.md','.pdf','.docx','.pptx','.png','.jpg','.jpeg','.tif','.tiff','.webp','.html','.htm'))
    return bool(source_requires_fact_alignment(source,evidence) or
                source.get('requires_semantic_review') is True or
                evidence.get('semantic_review_required') is True or
                evidence.get('requires_semantic_review') is True or
                evidence.get('parser')=='docling' or unstructured)


def read_input(pid,binding,*,frozen_source=None):
    binding=InputBinding.model_validate(binding)
    if bool(binding.source_id)==bool(binding.result_id):raise ValueError('输入必须指定一个来源或已验证结果')
    if binding.result_id:
        if binding.options.text_columns:
            from .workbook_reader import WorkbookReadError
            raise WorkbookReadError('text_columns只支持明确选择的XLSX来源，不适用于已验证结果','TEXT_PROJECTION_NOT_XLSX')
        folder,meta=result_folder(pid,binding.result_id)
        from .result_delivery import read_all_handoff
        page=read_all_handoff(folder/'handoff',expected_manifest_sha256=meta['handoff_sha256'],max_rows=MAX_ROWS)
        require_safe_numeric_lineage(pid,binding.result_id,root_meta=meta,root_page=page)
        rows=page['rows'];fields=page['schema']
        evidence={'result_id':binding.result_id,'manifest_sha256':meta['handoff_sha256'],'complete':True,
                  'requires_semantic_review':meta.get('requires_semantic_review',False),
                  'requires_source_review':meta.get('requires_source_review',False),
                  'semantic_review_reasons':page.get('context',{}).get('semantic_review_reasons',[]),
                  'output_semantics':page.get('context',{}).get('output_semantics')}
        provenance={'upstream_result':binding.result_id,'handoff_manifest':meta['handoff_sha256']}
    else:
        s=frozen_source or owned(pid,'source',binding.source_id)
        if s.get('project_id')!=pid or s.get('archived'):raise ValueError('来源不可用或不属于项目')
        file_path=source_path(s) if s['kind']=='file' else None
        o=binding.options
        if o.text_columns and not (s['kind']=='file' and s['name'].lower().endswith('.xlsx')):
            from .workbook_reader import WorkbookReadError
            raise WorkbookReadError('text_columns只支持明确选择的XLSX来源','TEXT_PROJECTION_NOT_XLSX')
        if s['kind']=='file' and s['name'].lower().endswith('.xlsx'):
            from .workbook_reader import read_table
            options=o.model_dump(exclude={'html_table','text_columns'},exclude_none=True)
            table=read_table(file_path,limit=MAX_ROWS,**options)
            if table.get('truncated') or table.get('complete') is False or table.get('executable_values_complete') is False:raise ValueError('表格读取未完成，不能按完整输入执行')
            rows,projection=_project_text_columns(table['rows'],table['fields'],o)
            fields=table['fields'];evidence={k:v for k,v in table.items() if k not in ('rows','provenance')}
            reasons=_workbook_semantic_review_reasons(table)
            if reasons:
                evidence['requires_semantic_review']=True
                evidence['semantic_review_reasons']=reasons
            provenance={k:table[k] for k in ('source_rows','special_cells','coordinate_rule','selection','row_roles') if k in table}
            if projection:evidence['text_projection']=projection;provenance['text_projection']=projection
        elif s['kind']=='file' and s['name'].lower().endswith(('.html','.htm')):
            from .evidence_tables import parse_html_tables
            if o.header_rows and o.header_rows!=list(range(1,len(o.header_rows)+1)):
                raise ValueError('网页表格表头行必须从第一行连续指定；不能将偏移行误读为同一表头')
            tables=parse_html_tables(file_path.read_text(encoding='utf-8'),source_id=s['id'],
                                     header_rows=len(o.header_rows) if o.header_rows else None)
            if o.html_table>=len(tables):raise ValueError('网页表格编号不存在')
            table=tables[o.html_table]
            if not table['complete']:raise ValueError('网页明细有跨行跨列歧义，需明确标签传播规则或使用源报告导出')
            rows=table['rows'];fields=table['fields'];provenance=table['provenance'];evidence={k:v for k,v in table.items() if k not in ('rows','provenance')}
        else:
            from .connectors import read_source
            rows,fields,evidence,truncated=read_source(s)
            if evidence.get('document_only'):
                raise ValueError('此 JSON 是完整文档，不是已确定的记录表；请明确记录路径并转换为对象数组后再进行表格加工，原文仍可用于对话和工作。')
            if truncated:raise ValueError('来源返回的是样本，不能继续完整加工')
            rows=[{k:v for k,v in r.items() if k not in ('record_hash','_source_index')} for r in rows]
            provenance={'source_id':s['id'],'evidence':evidence,'level':'source_and_row_order; no_invented_cell_coordinates'}
        # Interpretation of unstructured content and generated layouts is not
        # a verified financial fact. Preserve this obligation across source
        # snapshots, handoffs and later refreshes; parser names and source flags
        # must not silently disappear during normalization.
        source_review=source_interpretation_needs_review(s,evidence)
        if source_review:
            reasons=list(evidence.get('semantic_review_reasons') or [])
            if 'SOURCE_INTERPRETATION_REVIEW_REQUIRED' not in reasons:
                reasons.append('SOURCE_INTERPRETATION_REVIEW_REQUIRED')
            if source_requires_fact_alignment(s,evidence):
                reasons.append('FACT_SEMANTIC_ALIGNMENT_REQUIRED')
                evidence['semantic_alignment_required']=True
            evidence.update(requires_semantic_review=True,requires_source_review=True,
                            semantic_review_reasons=reasons)
        if s['kind']=='file':
            # Readers must never attach a frozen revision to bytes which were
            # replaced while parsing. CSV also verifies the exact parsed buffer.
            source_path(s)
            private_cfg=unseal(s['secret_config'])
            receipt=private_cfg.get('report_receipt')
            if receipt:
                evidence['report_context']={k:receipt.get(k) for k in ('filters_requested','filter_verification','filters_verified','context_declared','context_verification','context_verified','content_sha256','completion_verified','rows','format')}
            if s.get('fact_review_id'):evidence['fact_review_id']=s['fact_review_id']
        evidence.update(source_id=s['id'],source_revision=s.get('revision',1),content_hash=s.get('content_hash'),rows=len(rows))
    if binding.column_map:
        names={f['name'] for f in fields}
        if not set(binding.column_map)<=names or len({binding.column_map.get(n,n) for n in names})!=len(names):raise ValueError('输入列映射无效或目标重复')
        rows=[{binding.column_map.get(k,k):v for k,v in r.items()} for r in rows]
        fields=[dict(f,name=binding.column_map.get(f['name'],f['name'])) for f in fields]
    if len(rows)>MAX_ROWS:raise ValueError('输入超过部署行数预算，未截断或发布')
    return rows,fields,evidence,provenance


def preview_source(pid,value):
    value=SourceTable.model_validate(value)
    # Inspection must reveal questionable rows before the agent can author a
    # complete-data policy; this never relaxes a later execution's defaults.
    options=value.options.model_copy(update={'hidden_rows':'include','subtotal_policy':'preserve'})
    source=owned(pid,'source',value.source_id)
    if source['kind']=='file' and source['name'].lower().endswith('.xlsx'):
        from .workbook_reader import read_table
        table=read_table(source_path(source),limit=MAX_ROWS,require_formula_cache=False,
                         **options.model_dump(exclude={'html_table','text_columns'},exclude_none=True))
        source_path(source)
        rows,projection=_project_text_columns(table['rows'],table['fields'],options);fields=table['fields']
        evidence={k:v for k,v in table.items() if k not in ('rows','provenance')}
        if projection:evidence['text_projection']=projection
        reasons=_workbook_semantic_review_reasons(table)
        evidence.update(source_id=source['id'],source_revision=source.get('revision',1),
                        requires_semantic_review=bool(reasons),semantic_review_reasons=reasons)
        return {'source_id':source['id'],'fields':fields,
                'rows':json.loads(json.dumps(rows[value.offset:value.offset+value.limit],ensure_ascii=False,default=str)),
                'total':len(rows),'offset':value.offset,'next_offset':value.offset+value.limit if value.offset+value.limit<len(rows) else None,
                'sample_only':True,'complete_source_read':bool(table.get('executable_values_complete') and table.get('metadata_complete') and not table.get('truncated')),
                'available_rows_only':bool(table.get('truncated')),'evidence':evidence}
    rows,fields,evidence,_=read_input(pid,InputBinding(source_id=value.source_id,options=options))
    return {'source_id':value.source_id,'fields':fields,'rows':json.loads(json.dumps(rows[value.offset:value.offset+value.limit],ensure_ascii=False,default=str)),
            'total':len(rows),'offset':value.offset,'next_offset':value.offset+value.limit if value.offset+value.limit<len(rows) else None,
            'sample_only':True,'complete_source_read':True,'evidence':evidence}


def load_document(pid,path):
    text=workspace.read_file(pid,path)
    doc=WorkflowDocument.model_validate_json(text)
    if set(doc.bindings)!=set(doc.plan.inputs):raise ValueError('每个输入别名必须有明确来源绑定')
    for b in doc.bindings.values():
        if bool(b.source_id)==bool(b.result_id):raise ValueError('输入必须绑定一个来源或结果')
        if b.source_id:owned(pid,'source',b.source_id)
        else:result_folder(pid,b.result_id)
    if doc.contract:
        from .data_contracts import validate
        validate(doc.contract)
    return doc


def register(pid,path):
    doc=load_document(pid,path);wid='wf_'+hashlib.sha256((pid+':'+path).encode()).hexdigest()[:40]
    try:value=owned(pid,'data_workflow',wid)
    except KeyError:value={'project_id':pid,'path':path,'created_at':now(),'published_run':None,'active_document':None,'cron':None,'watch_sources':False}
    value.update(name=doc.plan.name,draft_sha256=digest(doc.model_dump()),updated_at=now())
    put('data_workflow',value,wid)
    return inspect_workflow(pid,wid)


def inspect_workflow(pid,wid):
    w=owned(pid,'data_workflow',wid);doc=load_document(pid,w['path'])
    return {'workflow_id':wid,'name':doc.plan.name,'path':w['path'],'draft_sha256':digest(doc.model_dump()),
            'published_sha256':w.get('active_sha256'),'published_run':w.get('published_run'),'candidate_run':w.get('candidate_run'),
            'inputs':{k:{'binding':b.model_dump(),'schema':doc.plan.inputs[k].model_dump()} for k,b in doc.bindings.items()},
            'steps':[s.model_dump() for s in doc.plan.stages],'output':doc.plan.output,'contract':doc.contract,'document':doc.model_dump(),
            'cron':w.get('cron'),'watch_sources':w.get('watch_sources',False),'next_due':w.get('next_due'),
            'refresh_error':w.get('refresh_error'),'validation':'typed_plan_valid; not_executed',
            'latest_run':workflow_run_diagnostic(pid,wid,draft_sha256=digest(doc.model_dump())),
            'edit':'告诉Agent需要修改的业务要求，Agent生成新草稿并重新校验；正式计划保持固定版本'}


_FAILURE_MESSAGES={
 'SQL_INEXACT_NUMERIC':'SQL中的数值运算可能经过二进制浮点，CAST回DECIMAL不能恢复精度；本次未返回未验证结果',
 'PROCESSING_ENGINE_CHANGED':'运行引擎版本已变化，本次没有混用版本执行；请重新发起加工',
 'MIXED_PERCENTAGE_REPRESENTATION':'同一列混合百分比与其他表示法，需明确逐类转换规则后再执行，不能统一当作比例',
 'INPUT_SCHEMA_MISMATCH':'输入字段与冻结计划不一致，请修改方案后重新运行',
 'INPUT_PERCENTAGE_FORMAT_MISMATCH':'输入百分比的数值类型或value_format与来源已明确的表示不一致',
 'INPUT_UNIT_MISMATCH':'输入单位与原表明确标识不一致，需要显式转换或重新核对',
 'UPSTREAM_SEMANTICS_MISMATCH':'下游输入必须保留已验证上游的含义、粒度、单位、期间和表示，不能直接重标',
 'MISSING_COLUMN':'Operation references missing or duplicate columns',
 'SCHEMA_MISMATCH':'输入行字段与冻结方案声明不一致，未自动删除或补齐',
 'TYPE_MISMATCH':'实际字段类型与冻结方案不一致，需检查输入声明或显式转换步骤',
 'VALUE_FORMAT_MISMATCH':'数值表示不一致，比例与百分点需要明确转换',
 'UNIT_MISMATCH':'运算字段单位不一致，需要明确转换规则',
 'SEMANTIC_MISMATCH':'字段含义、期间或粒度不一致，需核对声明',
 'GRAIN_DUPLICATE':'声明的行粒度键存在重复，未忽略或自动合并',
 'NULL_VALUE':'非空字段存在缺失值，未自动补零',
 'DIVIDE_BY_ZERO':'存在零分母，需明确业务处理规则',
 'DATA_CONTRACT_FAILED':'结果未通过数据验收契约，之前正式结果保留',
 'DATA_CONTRACT_EXACTNESS':'数值校验精度需要核对：检查原始资料和阈值，使用整数或固定十进制字符串后重新运行',
 'TEXT_PROJECTION_INVALID':'显式文本投影列不属于当前XLSX读取范围，未转换',
 'TEXT_PROJECTION_NOT_XLSX':'显式文本投影只支持XLSX来源，不适用于其他来源或已验证结果',
 'TEXT_PROJECTION_PERCENTAGE_CONFLICT':'百分比数值与表示不能通过文本投影隐藏，需保留显式百分比语义',
 'TEXT_PROJECTION_UNSUPPORTED_VALUE':'显式文本投影只允许有限Decimal、整数、字符串和空值，未转换其他类型',
 'TEXT_PROJECTION_LIMIT':'精确文本投影超过单元格内存上限，未分配超长文本',
}


def _safe_failure_details(value):
    if not isinstance(value,dict):return {}
    result={}
    for key in ('input_alias','column','source_column'):
        item=value.get(key)
        if isinstance(item,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',item):result[key]=item
    column=value.get('excel_column')
    if isinstance(column,str) and re.fullmatch(r'[A-Z]{1,3}',column):result['excel_column']=column
    if value.get('required_value_format') in ('percent_fraction','percent_points'):
        result['required_value_format']=value['required_value_format']
    if type(value.get('row_index')) is int and 1<=value['row_index']<=MAX_ROWS:result['row_index']=value['row_index']
    if value.get('expected_data_type') in ('text','decimal','integer','boolean','date'):
        result['expected_data_type']=value['expected_data_type']
    if value.get('actual_type') in ('NoneType','str','int','float','bool','Decimal','date','datetime','list','dict','bytes','other'):
        result['actual_type']=value['actual_type']
    formats=value.get('formats')
    if isinstance(formats,list):
        safe=[item for item in formats if isinstance(item,str) and len(item)<=32
              and re.fullmatch(r'(?:General|[0#][0#.,]*%|[0#][0#.,]*"%")',item)]
        if safe:result['formats']=safe[:10]
    return result


def workflow_run_diagnostic(pid,wid,rid=None,*,draft_sha256=None):
    """An owned frozen run's safe status is a diagnostic, never result rows."""
    from .jobs import get_job,jobs,render,engine as job_engine
    w=owned(pid,'data_workflow',wid)
    if rid is None:
        with job_engine.connect() as connection:
            row=connection.execute(select(jobs).where(jobs.c.kind=='data_workflow',jobs.c.target==wid)
                                   .order_by(jobs.c.created.desc(),jobs.c.id.desc()).limit(1)).mappings().first()
        if row is None:return None
        run=render(row)
    else:run=get_job(rid)
    if run.get('project_id')!=pid or run.get('kind')!='data_workflow' or run.get('target')!=wid:
        raise ValueError('运行诊断不属于当前项目的加工流程')
    try:frozen=WorkflowDocument.model_validate(run['workflow_document'])
    except (KeyError,ValueError,TypeError):raise ValueError('运行诊断缺少有效冻结方案，未返回内容') from None
    if not re.fullmatch(r'[a-f0-9]{64}',run.get('commit') or '') or digest(frozen.model_dump())!=run['commit']:
        raise ValueError('运行诊断与冻结方案版本不一致，未返回内容')
    available=run['status'] in ('validated','succeeded') and bool(run.get('validated'))
    result={'run_id':run['id'],'workflow_id':wid,'status':run['status'],'plan_sha256':run['commit'],
            'matches_current_draft':run['commit']==(draft_sha256 or w.get('draft_sha256')),
            'result_available':available}
    if run['status'] not in ('failed','cancelled'):return result
    raw=run.get('workflow_failure')
    if not isinstance(raw,dict):raw={}
    code=raw.get('code') or run.get('error_code') or 'WORKFLOW_FAILED'
    if not isinstance(code,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',code):code='WORKFLOW_FAILED'
    stage=raw.get('stage_id');stage=stage if isinstance(stage,str) and re.fullmatch(r'(?:input:)?[A-Za-z][A-Za-z0-9_]{0,63}',stage) else None
    details=_safe_failure_details(raw.get('details'))
    if stage and stage.startswith('input:'):details['input_alias']=stage[6:]
    failure={'stage_id':stage,'code':code,'message':_FAILURE_MESSAGES.get(code,'数据加工未通过验证；请核对输入、字段声明和处理规则，未返回未验证结果')}
    if details:failure['details']=details
    alias=details.get('input_alias');path='bindings.'+alias+'.options.percentage_columns' if alias else 'bindings.<输入别名>.options.percentage_columns'
    if code=='MIXED_PERCENTAGE_REPRESENTATION':
        next_step='按来源字段、数字格式及业务口径在 '+path+' 对原始c_N列明确fraction或points；不能猜测裸数含义。同步核对plan.inputs的数值类型、unit及value_format，保存后重新登记并运行。'
    elif code=='INPUT_PERCENTAGE_FORMAT_MISMATCH':
        next_step='核对plan.inputs.'+(alias or '<输入别名>')+'.columns中对应列；百分比规范化结果必须声明数值类型、unit=%及明确的percent_fraction或percent_points，不能保留text/native。修复草稿后重新登记运行。'
    elif (code=='TYPE_MISMATCH' and alias and details.get('expected_data_type')=='text'
            and details.get('actual_type') in ('Decimal','int')):
        next_step='先核对该输入列的来源类型与单位。若它是后续需要normalize的混合原始金额，可在bindings.'+alias+'.options.text_columns显式列出原始c_N列，仅将精确Decimal/整数无损投影为文本；原字符串和空值保持，保留来源单位并显式normalize。不得投影比例/百分比、float或bool，也不得自动猜测单位；否则修正输入类型声明。保存后重新登记运行。'
    elif code=='SQL_INEXACT_NUMERIC':
        next_step='不要仅将SQL结果CAST回DECIMAL或重复运行原SQL。均值先用aggregate求和与计数，除法和比率改用compute的精确Decimal运算并明确rounding_scale；使用不含科学计数的精确十进制字面量，核对单位和分母为零的业务处理后重新登记运行。'
    elif code=='DATA_CONTRACT_EXACTNESS':
        next_step='历史数值范围或对账记录需要从原始资料重新加工；新契约的范围阈值使用安全整数或固定十进制字符串（最多26位整数、12位小数），不要用浮点数、科学计数或将浮点结果CAST回十进制。核对口径后重新运行，再确认采用。'
    elif code in ('TASK_FAILED','WORKFLOW_FAILED'):
        next_step='此运行没有保存可核对的具体失败阶段。保留run_id并核对输入声明或服务状态；不要盲目重跑、分页读取失败结果，或声称已经完成。'
    else:next_step='查看本流程输入与步骤的完整声明，按失败阶段修复草稿，再register_data_workflow、run_data_workflow；失败运行不能用read_data_workflow_result分页读取，不能发布或当成完成。'
    result.update(workflow_failure=failure,next_step=next_step,repairable=code not in ('TASK_FAILED','WORKFLOW_FAILED'))
    return result


def enqueue_workflow(pid,wid,*,use_published=False,scheduled_key=None,originating_turn_id=None):
    from .job_authority import for_turn,client_guard
    from contextlib import nullcontext
    if originating_turn_id is None:
        from .provider import current_turn_id
        originating_turn_id=current_turn_id()
    authority=for_turn(originating_turn_id)
    with client_guard(authority["client_id"]) if authority else nullcontext():
        # Re-read after locking: revoke/cancel may have won admission.
        authority=for_turn(originating_turn_id)
        return _enqueue_workflow(pid,wid,use_published=use_published,scheduled_key=scheduled_key,authority=authority,
                                 originating_turn_id=originating_turn_id)


def _reusable_workflow_run(pid,wid,doc,frozen,turn_id):
    """Reuse an actual receipt in this owner task, never a cross-task cache."""
    if not turn_id or doc.reviewed_mapping_ids or any(b.result_id for b in doc.bindings.values()):return None
    from .actor_context import current_actor
    from .conversation import default_session,stable
    from .jobs import get_job
    from .result_delivery import verify_handoff,_read_json
    from .connectors import verified_file_source
    turn=get('turn',turn_id)
    def eligible(value):
        return (value.get('project_id')==pid and value.get('actor_id','owner')==current_actor()=='owner'
            and value.get('interaction_mode','work')=='work' and value.get('outcome')!='cancelled'
            and not any(value.get(key) for key in ('gateway_client_id','gateway_limits','collaboration_parent_turn_id',
                                                   'collaboration_task_id','collaboration_depth')))
    if not eligible(turn) or turn.get('state') not in ('agent','agent_tool'):return None
    family={};member=turn;session=turn.get('session_id') or default_session(pid)
    for _ in range(50):
        if (member['id'] in family or not eligible(member)
                or (member.get('session_id') or default_session(pid))!=session):return None
        family[member['id']]=member
        if not member.get('resumed_from'):break
        try:member=get('turn',member['resumed_from'])
        except KeyError:return None
    else:return None
    origin=turn.get('agent_origin',turn['id'])
    if origin!=member['id']:return None
    candidates=[step for step in reversed(turn.get('agent_trace',[]))
        if step.get('tool')=='run_data_workflow' and step.get('status')=='done'
        and step.get('arguments',{}).get('workflow_id')==wid and isinstance(step.get('observation'),dict)]
    if not candidates:return None
    # Reuse has the same current authorization boundary as a new tool effect.
    from .agent import authorize
    authorize(turn,'run_data_workflow',{'workflow_id':wid})
    for source in frozen.values():
        if (source.get('kind')!='file' or source.get('fact_review_id')
                or type(source.get('revision')) is not int or source['revision']<1
                or not source.get('revision_id') or not re.fullmatch(r'[a-f0-9]{64}',source.get('content_hash') or '')):return None
        verified_file_source(source)
    engine=processing_fingerprint();plan_hash=digest(doc.model_dump())
    for step in candidates:
        rid=step['observation'].get('run_id')
        if not isinstance(rid,str) or not step.get('id'):continue
        try:
            run=get_job(rid)
            if (run.get('project_id')!=pid or run.get('kind')!='data_workflow' or run.get('target')!=wid
                    or run.get('originating_turn_id') not in family or run.get('gateway_authority')
                    or run.get('authority_origin')!={'version':1,'kind':'workspace'}
                    or run.get('status') not in ('validated','succeeded') or not run.get('validated')
                    or run.get('cancel_requested') or run.get('commit')!=plan_hash
                    or run.get('scheduled_key')!=stable('dataop_',origin,step['id'])
                    or run.get('runtime_snapshot',{}).get('processing_fingerprint')!=engine
                    or digest(run.get('workflow_document'))!=plan_hash
                    or digest(run.get('source_snapshot'))!=digest(frozen)):continue
            folder,meta=result_folder(pid,rid)
            if meta.get('processing_fingerprint')!=engine:continue
            verified=verify_handoff(folder/'handoff',meta['handoff_sha256'])
            context=_read_json(folder/'handoff'/'context.json')
            if context.get('processing_fingerprint')!=engine:continue
            require_safe_numeric_lineage(pid,rid,root_meta=meta,root_page={'context':context,'total':verified.get('row_count')})
            # Check again after complete result verification, before admitting reuse.
            from .sources import snapshot
            if digest(snapshot(list(frozen)))!=digest(frozen):return None
            for source in frozen.values():verified_file_source(source)
            return dict(run,reused_verified_result=True)
        except (KeyError,ValueError,OSError,TypeError):continue
    return None


def _enqueue_workflow(pid,wid,*,use_published=False,scheduled_key=None,authority=None,originating_turn_id=None):
    if authority and authority['project_id']!=pid:raise ValueError('加工任务不属于原调用授权项目')
    w=owned(pid,'data_workflow',wid)
    doc=WorkflowDocument.model_validate(w['active_document']) if use_published and w.get('active_document') else load_document(pid,w['path'])
    if use_published and not w.get('active_document'):raise ValueError('流程尚未正式发布')
    from .sources import snapshot
    from .jobs import enqueue
    ids=[b.source_id for b in doc.bindings.values() if b.source_id]
    frozen=snapshot(ids)
    if any(s.get('project_id')!=pid for s in frozen.values()):raise ValueError('输入来源跨项目')
    if not authority and not use_published:
        reused=_reusable_workflow_run(pid,wid,doc,frozen,originating_turn_id)
        if reused:return reused
    queued=enqueue('data_workflow',wid,commit=digest(doc.model_dump()),scheduled_key=scheduled_key,max_attempts=1,
        extra={**({'gateway_authority':authority} if authority else {}),'project_id':pid,'workflow_document':doc.model_dump(),'source_snapshot':frozen,
               'originating_turn_id':originating_turn_id,
               'runtime_snapshot':{'timeout_seconds':doc.plan.budget.timeout_seconds,'processing_fingerprint':processing_fingerprint()},
               'source_revisions':{sid:s.get('revision',1) for sid,s in frozen.items()}})
    if queued.get('project_id')!=pid or queued.get('kind')!='data_workflow' or queued.get('commit')!=digest(doc.model_dump()):raise ValueError('已有不同版本的加工任务，请等待完成后运行新方案')
    return queued


def result_folder(pid,rid,*,lease_token=None):
    from .jobs import get_job
    run=get_job(rid)
    if run.get('project_id')!=pid or run.get('kind')!='data_workflow':raise ValueError('结果不属于当前项目')
    leased=lease_token and run['status']=='running' and run.get('lease_token')==lease_token and (run.get('lease_until') or 0)>=time.time()
    if (run['status'] not in ('validated','succeeded') and not leased) or not run.get('validated'):raise ValueError('完整结果尚未验证成功')
    folder=(DATA/run['artifact_dir']).resolve()
    if not folder.is_relative_to((DATA/'runs').resolve()):raise ValueError('结果路径无效')
    meta=json.loads((folder/'data-result.json').read_text())
    if run.get('data_result')!=meta:raise ValueError('结果元数据与服务端验证回执不一致')
    if meta['run_id']!=rid or meta['plan_sha256']!=run['commit']:raise ValueError('结果与执行版本不一致')
    actual=hashlib.sha256((folder/'handoff'/'manifest.json').read_bytes()).hexdigest()
    if actual!=meta['handoff_sha256']:raise ValueError('交接清单完整性校验失败')
    return folder,meta


def require_current_numeric_evidence(rid,meta,context,total):
    """Keep old results readable, but do not reuse unproven financial checks."""
    from .data_contracts import require_exact_numeric_evidence,ExactDataContractError
    from .jobs import get_job
    run=get_job(rid)
    document=run.get('workflow_document')
    contract=document.get('contract') if isinstance(document,dict) else None
    report=meta.get('contract')
    checks=report.get('checks',[]) if isinstance(report,dict) else []
    if not isinstance(checks,list):checks=[]
    numeric_in_report=any(isinstance(item,dict) and isinstance(item.get('name'),str)
                          and item['name'].startswith(('range:','total:')) for item in checks)
    numeric_in_contract=isinstance(contract,dict) and bool(contract.get('ranges') or contract.get('totals'))
    if not numeric_in_report and not numeric_in_contract:return
    if (type(total) is not int or total < 0 or type(meta.get('records')) is not int or meta['records']!=total
            or not isinstance(context,dict) or context.get('data_contract')!=report
            or not isinstance(document,dict) or digest(document)!=meta['plan_sha256']):
        raise ExactDataContractError('历史数值校验记录与冻结结果不一致，请从原始资料重新运行并核对后再采用')
    require_exact_numeric_evidence(report,contract,expected_rows=total)


def _source_review_receipt_valid(pid, rid, run, meta, context):
    """An adoption receipt is explicit evidence of a review, not run success."""
    receipt=run.get('source_review_receipt')
    if not isinstance(receipt,dict) or run.get('status')!='succeeded':return False
    required={'version','decision','method','project_id','run_id','plan_sha256','handoff_sha256',
              'source_snapshot_sha256','input_evidence_sha256','reviewer','reviewed_at','receipt_sha256'}
    if set(receipt)!=required:return False
    content={k:v for k,v in receipt.items() if k!='receipt_sha256'}
    return (type(receipt['version']) is int and receipt['version']==1 and receipt['decision']=='confirmed' and
            receipt['method']=='authenticated_user_source_review' and receipt['project_id']==pid and
            receipt['run_id']==rid and receipt['plan_sha256']==meta.get('plan_sha256') and
            receipt['handoff_sha256']==meta.get('handoff_sha256') and
            receipt['source_snapshot_sha256']==digest(run.get('source_snapshot',{})) and
            receipt['input_evidence_sha256']==digest(context.get('input_evidence')) and
            isinstance(receipt['reviewer'],str) and re.fullmatch(r'owner|u_[a-f0-9]{64}',receipt['reviewer']) is not None and
            isinstance(receipt['reviewed_at'],str) and bool(receipt['reviewed_at']) and
            receipt['receipt_sha256']==digest(content))


def _verify_review_originals(pid,run):
    """Recheck actual frozen file evidence before recording a new review.

    Derived manual evidence is not the original image. Follow its separate
    explicit visual-review receipt back to the authenticated source and page.
    Other derived inputs require a verifiable source revision and content record.
    This function never re-queries an external connector.
    """
    from .connectors import verified_file_source
    document=WorkflowDocument.model_validate(run['workflow_document'])
    snapshot=run.get('source_snapshot')
    if not isinstance(snapshot,dict):raise ValueError('来源快照不可核对，请重新运行')
    for binding in document.bindings.values():
        if not binding.source_id:continue
        frozen=snapshot.get(binding.source_id)
        if not isinstance(frozen,dict) or frozen.get('id')!=binding.source_id or frozen.get('project_id')!=pid:
            raise ValueError('来源快照不可核对，请重新运行')
        if frozen.get('kind')!='file':
            raise ValueError('该外部来源没有可复核的本地版本快照，请先保存并核对原始资料')
        verified_file_source(frozen)
        origin=frozen.get('origin_source_id')
        if not origin:continue
        if not isinstance(origin,str):raise ValueError('派生资料的原件记录不可核对')
        visual_id=frozen.get('visual_fact_review_id')
        if visual_id:
            from .visual_fact_service import _owned as visual_owned,_original as visual_original
            record,payload=visual_owned(pid,visual_id)
            reviewed=record.get('review')
            if (record.get('status')!='approved' or record.get('output_source_id')!=binding.source_id
                    or record.get('source_id')!=origin or type(payload.get('page')) is not int
                    or payload['page']<1 or record.get('page')!=payload['page']
                    or not isinstance(reviewed,dict) or reviewed.get('decision')!='approved'
                    or reviewed.get('visual_review_confirmed') is not True
                    or reviewed.get('content_sha256')!=record.get('content_sha256')
                    or payload.get('source_id')!=origin):
                raise ValueError('人工视觉资料缺少可核对的原件复核回执')
            source,content_hash,_=visual_original(pid,origin,payload.get('page'))
            if (source.get('revision',1)!=payload.get('source_revision')
                    or content_hash!=payload.get('source_file_sha256')):
                raise ValueError('人工视觉资料的原始图片或 PDF 版本已变化，请重新核对')
            continue
        fact_id=frozen.get('fact_review_id')
        if fact_id:
            from .fact_service import _owned as facts_owned,_document
            record,payload=facts_owned(pid,fact_id);reviewed=record.get('review')
            if (record.get('status')!='approved' or record.get('output_source_id')!=binding.source_id
                    or not isinstance(reviewed,dict) or reviewed.get('decision')!='approved'
                    or reviewed.get('content_sha256')!=record.get('content_sha256')
                    or payload.get('source_id')!=origin):
                raise ValueError('派生事实缺少可核对的原文复核回执')
            original=_document(pid,origin)
            if any(original.get(key)!=payload.get(key) for key in ('source_revision','source_file_sha256','source_text_sha256')):
                raise ValueError('派生事实的原始资料版本已变化，请重新核对')
            continue
        cfg=unseal(frozen['secret_config']);extraction=cfg.get('extraction_evidence')
        origin_revision=frozen.get('origin_source_revision')
        if not isinstance(extraction,dict) or type(origin_revision) is not int or origin_revision<1:
            raise ValueError('派生资料缺少可核对的原件版本，请重新处理原件')
        original=owned(pid,'source',origin)
        if original.get('kind')!='file' or original.get('archived') or original.get('revision',1)!=origin_revision:
            raise ValueError('派生资料的原件版本已变化，请重新处理原件')
        versions=[entry for entry in all_items('source_version') if entry.get('source_id')==origin and entry.get('revision')==origin_revision]
        if len(versions)!=1 or versions[0].get('content_hash')!=original.get('content_hash'):
            raise ValueError('派生资料的原件版本证据不完整，请重新处理原件')
        path,content,_=verified_file_source(original)
        try:text=content.decode('utf-8').replace('\r\n','\n').replace('\r','\n')
        except UnicodeDecodeError:raise ValueError('派生资料的原文编码已变化，请重新处理原件') from None
        if hashlib.sha256(text.encode('utf-8')).hexdigest()!=extraction.get('source_sha256'):
            raise ValueError('派生资料的原文内容已变化，请重新处理原件')


def _new_source_review_receipt(pid,rid,run,meta,context,review_actor):
    if not isinstance(review_actor,str) or not re.fullmatch(r'owner|u_[a-f0-9]{64}',review_actor):
        raise ValueError('来源需要在工作台由已认证用户核对并确认，不能由后台或模型代确认')
    content={'version':1,'decision':'confirmed','method':'authenticated_user_source_review',
             'project_id':pid,'run_id':rid,'plan_sha256':meta['plan_sha256'],
             'handoff_sha256':meta['handoff_sha256'],
             'source_snapshot_sha256':digest(run.get('source_snapshot',{})),
             'input_evidence_sha256':digest(context.get('input_evidence')),
             'reviewer':review_actor,'reviewed_at':now()}
    return dict(content,receipt_sha256=digest(content))


def require_safe_numeric_lineage(pid,rid,*,root_meta=None,root_page=None,lease_token=None,allow_root_review_pending=False):
    """Check frozen SQL and all result-input ancestors without materializing rows.

    Every ancestor's handoff is verified, not merely its declared link. An
    iterative DFS catches cycles while allowing shared ancestors; missing or
    unsupported evidence is not a safe result. The root parameters are for
    internal callers which already verified that exact handoff in this request.
    """
    from .data_contracts import ExactDataContractError,NumericLineageError
    from .data_workflows import validate_sql
    from .jobs import get_job
    from .result_delivery import read_handoff
    unknown='历史加工链的精度依据不完整或无法核对，请从原始资料重新运行并复核'
    unsafe='历史加工链包含可能丢失精度的数值运算，不能通过事后转换恢复；请从原始资料重新运行并复核'
    states={};hashes={};stack=[(rid,None,False)];count=0
    while stack:
        current,expected,leaving=stack.pop()
        if leaving:
            states[current]='done'
            continue
        if states.get(current)=='visiting':raise NumericLineageError('unknown',unknown)
        if states.get(current)=='done':
            if expected is not None and hashes[current]!=expected:raise NumericLineageError('unknown',unknown)
            continue
        count+=1
        if count>512:raise NumericLineageError('unknown','历史加工链记录过多，未能完整核对；请分段核对或从原始资料重新运行')
        states[current]='visiting'
        try:
            run=get_job(current)
            if current==rid and root_meta is not None and root_page is not None:
                meta,page=root_meta,root_page
                if (run.get('project_id')!=pid or run.get('kind')!='data_workflow'
                        or run.get('data_result')!=meta or meta.get('run_id')!=current
                        or run.get('commit')!=meta.get('plan_sha256')):
                    raise NumericLineageError('unknown',unknown)
            else:
                folder,meta=result_folder(pid,current,lease_token=lease_token if current==rid else None)
                page=read_handoff(folder/'handoff',limit=1,expected_manifest_sha256=meta['handoff_sha256'])
            handoff=meta.get('handoff_sha256');context=page.get('context');total=page.get('total')
            if (not isinstance(handoff,str) or not re.fullmatch(r'[a-f0-9]{64}',handoff)
                    or expected is not None and expected!=handoff
                    or type(total) is not int or type(meta.get('records')) is not int or total!=meta['records']
                    or not isinstance(context,dict) or context.get('project_id')!=pid
                    or context.get('plan_sha256')!=meta.get('plan_sha256')
                    or meta.get('workflow_id')!=run.get('target')):
                raise NumericLineageError('unknown',unknown)
            hashes[current]=handoff
            require_current_numeric_evidence(current,meta,context,total)
            raw=run.get('workflow_document')
            if not isinstance(raw,dict) or digest(raw)!=meta['plan_sha256']:
                raise NumericLineageError('unknown',unknown)
            document=WorkflowDocument.model_validate(raw)
            for stage in document.plan.stages:
                if stage.kind=='sql':
                    try:validate_sql(stage.sql,set(stage.inputs))
                    except WorkflowError as error:
                        raise NumericLineageError('unsafe_sql' if error.code=='SQL_INEXACT_NUMERIC' else 'unknown',
                                                  unsafe if error.code=='SQL_INEXACT_NUMERIC' else unknown) from None
            evidence=context.get('input_evidence')
            if not isinstance(evidence,dict):raise NumericLineageError('source_review_unknown','历史来源证据不完整，请从原始资料重新运行并核对')
            evidence_needs_review=any(isinstance(proof,dict) and proof.get('requires_source_review') is True for proof in evidence.values())
            meta_needs_review=meta.get('requires_source_review') is True
            context_needs_review=context.get('requires_source_review') is True
            needs_review=evidence_needs_review or meta_needs_review or context_needs_review
            if needs_review and not (evidence_needs_review and meta_needs_review and context_needs_review):
                raise NumericLineageError('source_review_unknown','来源复核标记不一致，请从原始资料重新运行并核对')
            if needs_review and not (current==rid and allow_root_review_pending):
                if not _source_review_receipt_valid(pid,current,run,meta,context):
                    raise NumericLineageError('source_review_unknown','历史来源解释缺少已认证的人工复核记录，请先在工作台核对该候选')
            links=[]
            for alias,binding in document.bindings.items():
                proof=evidence.get(alias) if isinstance(evidence,dict) else None
                if binding.source_id:
                    snapshot=run.get('source_snapshot')
                    frozen=snapshot.get(binding.source_id) if isinstance(snapshot,dict) else None
                    if (not isinstance(frozen,dict) or frozen.get('id')!=binding.source_id
                            or not isinstance(proof,dict) or proof.get('source_id')!=binding.source_id
                            or not isinstance(frozen.get('kind'),str) or not isinstance(frozen.get('name'),str)
                            or (frozen.get('kind')=='file' and
                                (not isinstance(frozen.get('content_hash'),str)
                                 or not re.fullmatch(r'[a-f0-9]{64}',frozen['content_hash'])
                                 or proof.get('content_hash')!=frozen['content_hash']))):
                        raise NumericLineageError('source_review_unknown',
                            '历史来源证据不完整，请从原始资料重新运行并核对')
                    if source_requires_fact_alignment(frozen,proof):
                        raise NumericLineageError('fact_semantic_alignment_missing',
                            '文本或视觉事实尚未完成逐项业务语义对齐，不能自动汇总、采用或生成报告；请先核对指标、单位、期间及可加性')
                    # Historical receipts predate the review marker. If the
                    # frozen source or parser proves an interpretation was
                    # involved, do not retroactively invent an approval.
                    if (source_interpretation_needs_review(frozen,proof)
                            and (not isinstance(proof,dict) or proof.get('requires_source_review') is not True)):
                        raise NumericLineageError('source_review_unknown',
                            '历史来源解释缺少完整的复核记录，请从原始资料重新运行并核对')
                    if proof.get('requires_source_review') is True:
                        revision=frozen.get('revision',1)
                        revisions=run.get('source_revisions')
                        if (type(revision) is not int or revision<1 or type(proof.get('source_revision')) is not int
                                or proof.get('source_revision')!=revision or not isinstance(revisions,dict)
                                or type(revisions.get(binding.source_id)) is not int or revisions.get(binding.source_id)!=revision
                                or meta.get('source_revisions')!=revisions):
                            raise NumericLineageError('source_review_unknown','来源版本复核依据不完整，请重新运行并核对')
                    continue
                if not binding.result_id:continue
                checksum=proof.get('manifest_sha256') if isinstance(proof,dict) else None
                if (not isinstance(proof,dict) or proof.get('result_id')!=binding.result_id or proof.get('complete') is not True
                        or not isinstance(checksum,str) or not re.fullmatch(r'[a-f0-9]{64}',checksum)):
                    raise NumericLineageError('unknown',unknown)
                links.append((binding.result_id,checksum))
        except NumericLineageError:
            raise
        except ExactDataContractError as error:
            raise NumericLineageError('unknown',str(error)) from None
        except (KeyError,ValueError,OSError,TypeError):
            raise NumericLineageError('unknown',unknown) from None
        stack.append((current,None,True))
        for child,checksum in reversed(links):stack.append((child,checksum,False))


def read_result(pid,rid,offset=0,limit=100):
    folder,meta=result_folder(pid,rid)
    from .result_delivery import read_handoff
    page=read_handoff(folder/'handoff',offset=offset,limit=limit,expected_manifest_sha256=meta['handoff_sha256'])
    # The display flag is conservative even if a legacy receipt and metadata
    # disagree. Adoption still validates the original immutable context.
    context=dict(page.get('context') or {})
    evidence=context.get('input_evidence')
    if (meta.get('requires_source_review') is True or context.get('requires_source_review') is True
            or isinstance(evidence,dict) and any(isinstance(proof,dict) and proof.get('requires_source_review') is True for proof in evidence.values())):
        context['requires_source_review']=True
    page['context']=context
    return dict(page,run_id=rid,workflow_id=meta['workflow_id'],plan_sha256=meta['plan_sha256'],data_as_of=meta['created_at'])


def execute_job(run,folder):
    from .execution import patch_run,checkpoint
    from .jobs import heartbeat
    from .result_delivery import write_handoff,verify_handoff
    from .errors import TaskError
    doc=WorkflowDocument.model_validate(run['workflow_document']);pid=run['project_id']
    if digest(doc.model_dump())!=run['commit']:raise ValueError('冻结的流程版本不一致')
    engine=processing_fingerprint()
    if run.get('runtime_snapshot',{}).get('processing_fingerprint') not in (None,engine):
        raise TaskError('运行引擎版本已变化，请重新发起加工；未混用版本执行','PROCESSING_ENGINE_CHANGED')
    stop=threading.Event()
    def beat():
        while not stop.wait(3):
            try:
                if not heartbeat(run['id'],run['lease_token']):return
            except Exception:return
    thread=threading.Thread(target=beat,daemon=True);thread.start()
    try:
        inputs={};evidence={};provenance={};stage_receipts=[]
        (folder/'workflow.json').write_text(json.dumps(doc.model_dump(),ensure_ascii=False,indent=2))
        for alias,b in doc.bindings.items():
            checkpoint(run)
            from .workbook_reader import WorkbookReadError
            from .connectors import SourceError
            from .data_contracts import ExactDataContractError
            def fail_input(code,message,details=None):
                failure={'stage_id':'input:'+alias,'code':code,'message':message}
                safe=_safe_failure_details(details)
                if safe:failure['details']=safe
                patch_run(run,workflow_failure=failure,stage='来源读取或输入声明需要核对')
                raise TaskError(message,code) from None
            try:rows,fields,proof,prov=read_input(pid,b,frozen_source=run.get('source_snapshot',{}).get(b.source_id))
            except ExactDataContractError as error:
                fail_input('DATA_CONTRACT_EXACTNESS',str(error))
            except (WorkbookReadError,SourceError) as error:
                details={};original=getattr(error,'details',{})
                if isinstance(original,dict):
                    details=_safe_failure_details(original)
                    details['formats']=original.get('formats')
                    excel_column=original.get('column')
                    if isinstance(excel_column,str) and re.fullmatch(r'[A-Z]{1,3}',excel_column):
                        details['excel_column']=excel_column
                        from openpyxl.utils.cell import column_index_from_string,range_boundaries
                        try:
                            start,end=(range_boundaries(b.options.cell_range)[0],range_boundaries(b.options.cell_range)[2]) if b.options.cell_range else (1,16384) if not b.options.table else (None,None)
                            actual=column_index_from_string(excel_column)
                            if start is not None and start<=actual<=end:
                                key='c_'+str(actual-start+1);details['source_column']=key
                                details['column']=b.column_map.get(key,key)
                        except (ValueError,TypeError):pass
                fail_input(error.code,' '.join(str(error).split())[:1000],details)
            names={f['name'] for f in fields};expected={f.name for f in doc.plan.inputs[alias].columns}
            if names!=expected:fail_input('INPUT_SCHEMA_MISMATCH','输入字段与冻结计划不一致，请修改方案后重新运行')
            upstream=proof.get('output_semantics')
            declared=doc.plan.inputs[alias].model_dump()
            if upstream:
                renames=b.column_map
                old_columns={renames.get(c['name'],c['name']):c for c in upstream['columns']}
                if sorted(renames.get(k,k) for k in upstream['grain'])!=sorted(declared['grain']):fail_input('UPSTREAM_SEMANTICS_MISMATCH','下游不能静默修改上游行粒度')
                if upstream['meaning']!=declared['meaning']:fail_input('UPSTREAM_SEMANTICS_MISMATCH','下游不能静默修改上游表含义')
                for c in declared['columns']:
                    old=old_columns[c['name']]
                    if any(c.get(k)!=old.get(k) for k in ('data_type','meaning','unit','period','aggregation','value_format')):fail_input('UPSTREAM_SEMANTICS_MISMATCH','下游输入语义与上游不一致，不能直接重标单位、期间或口径',{'column':c['name']})
            else:
                observed={f['name']:f for f in fields}
                for c in declared['columns']:
                    f=observed[c['name']]
                    unit=f.get('unit')
                    if f.get('stored_semantics')=='ratio' and c.get('value_format')!='percent_fraction':fail_input('INPUT_PERCENTAGE_FORMAT_MISMATCH','百分比原值是比率，需明确percent_fraction并通过显式步骤转换为百分点，不能只重命名',{'column':c['name'],'required_value_format':'percent_fraction'})
                    if f.get('stored_semantics')=='percent_points' and c.get('value_format')!='percent_points':fail_input('INPUT_PERCENTAGE_FORMAT_MISMATCH','百分比原值是百分点，需明确percent_points，不能静默重标为比率',{'column':c['name'],'required_value_format':'percent_points'})
                    # Text is a storage representation, not permission to erase
                    # a source-declared unit before a later normalization step.
                    if unit and unit!=c.get('unit'):fail_input('INPUT_UNIT_MISMATCH','输入单位与原表明确标识不一致，需要显式转换或重新核对',{'column':c['name']})
            inputs[alias]=rows;evidence[alias]=proof;provenance[alias]=prov
        reviewed_stages=set()
        if doc.reviewed_mapping_ids:
            from .semantic_service import get_reviewed_mapping
            for ident in doc.reviewed_mapping_ids:
                review=get_reviewed_mapping(pid,ident);mapping=review.mapping
                expected={e.source:e.target for e in mapping.entries}
                matched=[stage for stage in doc.plan.stages if stage.kind=='reclassify'
                         and stage.column==mapping.source_field_id and stage.output.name==mapping.target_field_id
                         and stage.mapping==expected and stage.unmapped==({'error':'error','keep':'preserve'}.get(mapping.unmapped_policy))]
                if len(matched)!=1:raise ValueError('已核对映射未与本次具体步骤和枚举内容一致绑定，不能借用其他映射的批准')
                reviewed_stages.add(matched[0].id)
        def on_stage(receipt):
            checkpoint(run);stage_receipts.append(receipt)
            (folder/'stage-receipts.json').write_text(json.dumps(stage_receipts,ensure_ascii=False,indent=2,default=str))
            patch_run(run,stage='数据加工：'+str(receipt.get('stage_id',receipt.get('id',''))),workflow_steps=len(stage_receipts))
        try:
            output=execute(doc.plan,inputs,on_stage=on_stage)
        except WorkflowError as error:
            # Executor diagnostics are controlled messages, never raw data values.
            # Keep the precise failure on the fenced run so an agent can repair the
            # failed step without treating completed dependency receipts as output.
            message=' '.join(str(error).split())[:1000]
            failure={'stage_id':error.stage_id,'code':error.code,'message':message}
            details=_safe_failure_details(getattr(error,'details',{}))
            if error.stage_id and error.stage_id.startswith('input:'):
                alias=error.stage_id[6:]
                if alias in doc.bindings:
                    inverse={target:source for source,target in doc.bindings[alias].column_map.items()}
                    source_column=inverse.get(details.get('column'),details.get('column'))
                    if isinstance(source_column,str) and re.fullmatch(r'c_[1-9][0-9]{0,2}',source_column):details['source_column']=source_column
            if details:failure['details']=details
            patch_run(run,workflow_failure=failure,stage='数据加工步骤未完成')
            raise TaskError(message,error.code) from None
        checkpoint(run)
        report=None
        if doc.contract:
            import duckdb
            from .data_contracts import evaluate,ExactDataContractError
            with duckdb.connect() as con:
                # Use the same typed values and schema as the workflow executor.
                con.execute('CREATE SCHEMA business')
                schema=output['output']['schema'];cols=schema['columns']
                types={'text':'VARCHAR','decimal':'DECIMAL(38,12)','integer':'BIGINT','date':'DATE','boolean':'BOOLEAN'}
                definitions=','.join('"'+c['name']+'" '+types[c['data_type']] for c in cols)
                con.execute('CREATE TABLE business.business_objects ('+definitions+')')
                rows=output['output']['rows']
                if rows:con.executemany('INSERT INTO business.business_objects VALUES ('+','.join('?' for _ in cols)+')',[[r[c['name']] for c in cols] for r in rows])
                try:report=evaluate(con,doc.contract)
                except ExactDataContractError as error:raise TaskError(str(error),'DATA_CONTRACT_EXACTNESS') from None
            if not report['passed']:raise TaskError('加工结果未通过数据验收契约，保留之前的正式结果','DATA_CONTRACT_FAILED')
        source_review=any(p.get('requires_source_review') for p in evidence.values())
        context={'project_id':pid,'workflow_id':run['target'],'plan_sha256':run['commit'],'processing_fingerprint':engine,'input_evidence':evidence,'data_contract':report,
                 'lineage_scope':'input_cell_or_source_evidence_plus_stage_dependencies; output_row_lineage_not_inferred',
                 'semantic_review_reasons':sorted({reason for proof in evidence.values() for reason in proof.get('semantic_review_reasons',[])}),
                 'output_semantics':output['output']['schema'],'requires_source_review':source_review,
                 'requires_semantic_review':bool(output.get('requires_semantic_review') or any(p.get('requires_semantic_review') for p in evidence.values()) or any(st.kind in ('reclassify','lookup') and st.id not in reviewed_stages for st in doc.plan.stages))}
        manifest=write_handoff(folder/'handoff',output['output']['rows'],output['output']['schema'],context=context,provenance=provenance,stages=stage_receipts)
        verify_handoff(folder/'handoff');checkpoint(run)
        meta={'run_id':run['id'],'workflow_id':run['target'],'project_id':pid,'plan_sha256':run['commit'],'processing_fingerprint':engine,
              'handoff_sha256':hashlib.sha256((folder/'handoff'/'manifest.json').read_bytes()).hexdigest(),
              'records':output['output']['row_count'],'created_at':now(),'source_revisions':run.get('source_revisions',{}),'complete':True,'contract':report,
              'requires_semantic_review':context['requires_semantic_review'],'requires_source_review':source_review,
              'semantic_review_reasons':context['semantic_review_reasons']}
        (folder/'data-result.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2))
        patch_run(run,validated=True,records=meta['records'],stage='完整结果验证通过',data_result=meta)
        w=owned(pid,'data_workflow',run['target'])
        if w.get('published_run') and w.get('active_sha256')==run['commit'] and not source_review:
            publish_result(pid,run['id'],run['commit'],lease_token=run['lease_token'])
        else:
            patch_run(run,status='validated',finished_at=now(),stage='完整结果验证通过，等待采用',
                target_patch={'kind':'data_workflow','values':{'candidate_run':run['id']}})
    finally:stop.set();thread.join(timeout=4)


def publish_result(pid,rid,expected_sha256,*,lease_token=None,review_actor=None,review_handoff_sha256=None):
    from .job_authority import guard
    from .jobs import get_job
    from contextlib import nullcontext
    # Explicit owner adoption is a new authorization; worker auto-publication
    # must retain its originating machine grant through the database commit.
    with guard(get_job(rid)) if lease_token else nullcontext():
        return _publish_result(pid,rid,expected_sha256,lease_token=lease_token,review_actor=review_actor,review_handoff_sha256=review_handoff_sha256)


def _publish_result(pid,rid,expected_sha256,*,lease_token=None,review_actor=None,review_handoff_sha256=None):
    from .jobs import get_job,jobs,render
    from .result_delivery import read_handoff
    folder,meta=result_folder(pid,rid,lease_token=lease_token)
    before_review=get_job(rid)
    snapshot_hash=digest(before_review.get('source_snapshot',{}))
    page=read_handoff(folder/'handoff',limit=1,expected_manifest_sha256=meta['handoff_sha256'])
    needs_review=meta.get('requires_source_review') is True or page['context'].get('requires_source_review') is True
    if needs_review:
        if lease_token or review_handoff_sha256!=meta['handoff_sha256']:
            raise ValueError('来源解释需要在工作台对照原件并明确确认，请刷新结果后核对')
        if not isinstance(review_actor,str) or not re.fullmatch(r'owner|u_[a-f0-9]{64}',review_actor):
            raise ValueError('来源解释需要由已认证用户在工作台明确确认，不能由后台或模型代确认')
    require_safe_numeric_lineage(pid,rid,root_meta=meta,root_page=page,lease_token=lease_token,
                                 allow_root_review_pending=bool(needs_review and not lease_token))
    if needs_review and not _source_review_receipt_valid(pid,rid,before_review,meta,page['context']):
        try:_verify_review_originals(pid,before_review)
        except ValueError:raise
        except Exception:raise ValueError('来源原件或复核记录不可用，请重新打开并核对') from None
    if meta['plan_sha256']!=expected_sha256:raise ValueError('加工方案版本已变化，请重新核对')
    with transaction() as c:
        row=c.execute(select(jobs).where(jobs.c.id==rid).with_for_update()).mappings().first();run=render(row)
        leased=lease_token and run['status']=='running' and row['lease_token']==lease_token and (row['lease_until'] or 0)>=time.time()
        if run.get('cancel_requested') or (run['status'] not in ('validated','succeeded') and not leased):raise ValueError('结果不可发布或执行租约失效')
        row_job_until=row['lease_until']
        row=c.execute(select(items.c.body).where(items.c.id==run['target'],items.c.kind=='data_workflow').with_for_update()).first()
        w=json.loads(row[0])
        if w.get('project_id')!=pid:raise ValueError('流程不属于当前项目')
        doc=WorkflowDocument.model_validate(run['workflow_document'])
        if run['commit']!=digest(doc.model_dump()):raise ValueError('流程内容完整性校验失败')
        current=load_document(pid,w['path'])
        if run['commit']!=digest(current.model_dump()) and run['commit']!=w.get('active_sha256'):raise ValueError('该候选已被新方案替代')
        if lease_token:
            from .job_authority import check
            check(run)
        w.update(published_run=rid,active_document=doc.model_dump(),active_sha256=run['commit'],source_revisions=run.get('source_revisions',{}),updated_at=now())
        if w.get('candidate_run')==rid:w['candidate_run']=None
        if lease_token and (row_job_until or 0)<time.time():raise ValueError('执行租约失效，禁止采用结果')
        c.execute(update(items).where(items.c.id==w['id'],items.c.kind=='data_workflow').values(body=json.dumps(w,ensure_ascii=False)))
        if run.get('data_result')!=meta or digest(run.get('source_snapshot',{}))!=snapshot_hash:
            raise ValueError('结果或来源记录已变化，请重新核对')
        if needs_review and not _source_review_receipt_valid(pid,rid,run,meta,page['context']):
            run['source_review_receipt']=_new_source_review_receipt(pid,rid,run,meta,page['context'],review_actor)
        run.update(stage='已采用完整结果',finished_at=now())
        c.execute(update(jobs).where(jobs.c.id==rid).values(status='succeeded',body=json.dumps(run,ensure_ascii=False,default=str)))
    return {'workflow_id':w['id'],'published_run':rid,'plan_sha256':run['commit']}


def schedule(pid,wid,cron=None,watch_sources=False):
    from croniter import croniter
    from datetime import datetime,timezone
    from zoneinfo import ZoneInfo
    from .config import TIMEZONE
    w=owned(pid,'data_workflow',wid)
    if not w.get('published_run'):raise ValueError('先核对并采用结果，再启用持续刷新')
    if cron and (not croniter.is_valid(cron) or len(cron.split())!=5):raise ValueError('需要有效五段cron')
    next_due=croniter(cron,datetime.now(ZoneInfo(TIMEZONE))).get_next(datetime).astimezone(timezone.utc).isoformat() if cron else None
    patch('data_workflow',wid,cron=cron,watch_sources=watch_sources,next_due=next_due)
    return {'workflow_id':wid,'cron':cron,'watch_sources':watch_sources,'next_due':next_due,
            'scope':'file_revision_events_or_scheduled_full_snapshot; database_CDC_not_configured'}


def queue_due():
    from datetime import datetime,timezone
    from croniter import croniter
    from .locks import FileLock
    with FileLock(str(DATA/'data-workflow-scheduler.lock'),timeout=2):
        for w in all_items('data_workflow'):
            if not w.get('active_document') or not w.get('published_run'):continue
            cron=w.get('cron');due=cron and w.get('next_due') and datetime.fromisoformat(w['next_due'])<=datetime.now(timezone.utc)
            ids=[b['source_id'] for b in w['active_document']['bindings'].values() if b.get('source_id')]
            revisions={sid:get('source',sid).get('revision',1) for sid in ids}
            changed=w.get('watch_sources') and revisions!=w.get('source_revisions',{})
            if due or changed:
                key=w['id']+':'+(w['next_due'] if due else digest(revisions))
                try:enqueue_workflow(w['project_id'],w['id'],use_published=True,scheduled_key=key)
                except (ValueError,KeyError):patch('data_workflow',w['id'],refresh_error='来源不可用，之前正式结果保留')
                if due:patch('data_workflow',w['id'],next_due=croniter(cron,datetime.now(timezone.utc)).get_next(datetime).isoformat())


def ensure_candidate_bulk_allowed(pid,rid):
    """Reject bulk output of unaligned fact candidates, including ancestors.

    Already-succeeded historical artifacts remain readable. This does not
    assert their financial correctness or grant permission to publish anew.
    """
    from .jobs import get_job
    from .result_delivery import read_handoff
    root=get_job(rid)
    if root.get('project_id')!=pid or root.get('kind')!='data_workflow':
        raise ValueError('结果不属于当前项目')
    if root.get('status') not in ('validated','succeeded'):raise ValueError('候选结果尚未完成验证')
    try:parent=owned(pid,'data_workflow',root['target'])
    except KeyError:parent=None
    if parent and parent.get('published_run')==rid and root.get('status')=='succeeded':
        folder,meta=result_folder(pid,rid)
        read_handoff(folder/'handoff',limit=1,expected_manifest_sha256=meta['handoff_sha256'])
        return
    stack=[(rid,None)];seen={}
    while stack:
        current,expected=stack.pop()
        if current in seen:
            if expected is not None and seen[current]!=expected:raise ValueError('候选来源链版本不一致，不能下载完整结果')
            continue
        if len(seen)>=512:raise ValueError('候选来源链过长，不能下载未经完整核对的结果')
        run=get_job(current);folder,meta=result_folder(pid,current)
        handoff=meta.get('handoff_sha256')
        if expected is not None and handoff!=expected:raise ValueError('候选来源链版本不一致，不能下载完整结果')
        page=read_handoff(folder/'handoff',limit=1,expected_manifest_sha256=handoff)
        context=page.get('context');evidence=context.get('input_evidence') if isinstance(context,dict) else None
        document=run.get('workflow_document')
        if not isinstance(document,dict) or digest(document)!=meta.get('plan_sha256') or not isinstance(evidence,dict):
            raise ValueError('候选来源链无法核对，不能下载完整结果')
        document=WorkflowDocument.model_validate(document)
        reasons=context.get('semantic_review_reasons') or []
        if not isinstance(reasons,list):raise ValueError('候选来源链无法核对，不能下载完整结果')
        if 'FACT_SEMANTIC_ALIGNMENT_REQUIRED' in reasons:
            raise ValueError('事实业务语义尚未对齐，候选只可分页预览，暂不能下载完整结果或生成图表')
        seen[current]=handoff
        for alias,binding in document.bindings.items():
            proof=evidence.get(alias)
            if not isinstance(proof,dict):raise ValueError('候选来源链无法核对，不能下载完整结果')
            if binding.source_id:
                snapshot=run.get('source_snapshot');source=snapshot.get(binding.source_id) if isinstance(snapshot,dict) else None
                if not isinstance(source,dict) or source.get('id')!=binding.source_id or proof.get('source_id')!=binding.source_id:
                    raise ValueError('候选来源链无法核对，不能下载完整结果')
                if source_requires_fact_alignment(source,proof):
                    raise ValueError('事实业务语义尚未对齐，候选只可分页预览，暂不能下载完整结果或生成图表')
                if source.get('kind')=='file' and source.get('content_hash')!=proof.get('content_hash'):
                    raise ValueError('候选来源链版本不一致，不能下载完整结果')
            elif binding.result_id:
                checksum=proof.get('manifest_sha256')
                if (proof.get('result_id')!=binding.result_id or proof.get('complete') is not True
                        or not isinstance(checksum,str) or not re.fullmatch(r'[a-f0-9]{64}',checksum)):
                    raise ValueError('候选上游结果依据不完整，不能下载完整结果')
                stack.append((binding.result_id,checksum))


def export_result(pid,rid,precision_policy='text_exact',*,format='xlsx',expected_handoff_sha256=None):
    if precision_policy not in ('reject','text_exact'):raise ValueError('不支持的Excel精度策略')
    if format not in ('xlsx','csv'):raise ValueError('不支持的结果导出格式')
    ensure_candidate_bulk_allowed(pid,rid)
    from .result_delivery import read_all_handoff,export_xlsx,export_csv,durable_cached_artifact
    from .locks import FileLock,assert_owned
    folder,meta=result_folder(pid,rid)
    if expected_handoff_sha256 is not None and meta['handoff_sha256']!=expected_handoff_sha256:
        raise ValueError('结果版本已变化，请刷新后重新核对')
    key=('dc_'+digest([pid,rid,meta['handoff_sha256'],'csv-literal-v2'])[:40] if format=='csv'
         else 'dx_'+digest([pid,rid,meta['handoff_sha256'],precision_policy])[:40])
    target=folder/('result-csv-v2.csv' if format=='csv' else 'result-'+precision_policy+'.xlsx')
    with FileLock(str(folder/('export-'+('csv' if format=='csv' else precision_policy)+'.lock')),timeout=30):
        try:record=owned(pid,'data_export',key)
        except KeyError:record={}
        if record and (record.get('run_id')!=rid or record.get('source_manifest')!=meta['handoff_sha256']):
            raise ValueError('导出回执与冻结结果不一致')
        def build(temporary):
            table=read_all_handoff(folder/'handoff',expected_manifest_sha256=meta['handoff_sha256'],native_types=format=='csv')
            if format=='csv':
                export_csv(table['rows'],table['schema'],temporary)
                return
            export_xlsx(table['rows'],table['schema'],temporary,precision_policy=precision_policy,
                context=table['context'],provenance={'handoff_sha256':meta['handoff_sha256']},controls=[{'kind':'row_count','expected':table['total']}]+[{'kind':'sum','column':k,**bounds} for k,bounds in (get('run',rid).get('workflow_document',{}).get('contract') or {}).get('totals',{}).items()])
        def save_receipt(checksum,state):
            put('data_export',{'project_id':pid,'run_id':rid,'sha256':checksum,
                'source_manifest':meta['handoff_sha256'],'state':state},key)
        pending=record.get('state')=='pending'
        return durable_cached_artifact(target,expected_sha256=record.get('sha256') if not pending else None,
            pending_sha256=record.get('sha256') if pending else None,build=build,
            save_pending=lambda sha:save_receipt(sha,'pending'),
            save_committed=lambda sha:save_receipt(sha,'committed'),before_publish=assert_owned)


def result_download(pid,rid,kind='handoff',*,expected_handoff_sha256=None):
    folder,meta=result_folder(pid,rid)
    if expected_handoff_sha256 is not None and meta['handoff_sha256']!=expected_handoff_sha256:
        raise ValueError('结果版本已变化，请刷新后重新核对')
    from .result_delivery import verify_handoff,durable_cached_artifact
    from .locks import FileLock,assert_owned
    if kind not in ('handoff','xlsx','csv'):raise ValueError('不支持的结果下载格式')
    if kind=='handoff':ensure_candidate_bulk_allowed(pid,rid)
    verify_handoff(folder/'handoff',meta['handoff_sha256'])
    if kind=='xlsx':return export_result(pid,rid,expected_handoff_sha256=expected_handoff_sha256)
    if kind=='csv':return export_result(pid,rid,format='csv',expected_handoff_sha256=expected_handoff_sha256)
    path=folder/'handoff.zip';key='dz_'+digest([pid,rid,meta['handoff_sha256']])[:40]
    with FileLock(str(folder/'handoff-zip.lock'),timeout=30):
        try:record=owned(pid,'data_export',key)
        except KeyError:record={}
        if record and (record.get('run_id')!=rid or record.get('source_manifest')!=meta['handoff_sha256']):
            raise ValueError('交接下载回执与冻结结果不一致')
        def build(temporary):
            # Include the verified package's exact file set, never unrelated
            # files which happen to appear in the immutable handoff directory.
            manifest=verify_handoff(folder/'handoff',meta['handoff_sha256'])
            with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as z:
                for name in sorted(set(manifest['files'])|{'manifest.json'}):
                    z.write(folder/'handoff'/name,name)
            with zipfile.ZipFile(temporary) as z:
                if z.testzip() is not None:raise ValueError('交接下载件压缩校验失败')
                for name in set(manifest['files'])|{'manifest.json'}:
                    expected=meta['handoff_sha256'] if name=='manifest.json' else manifest['files'][name]['sha256']
                    if hashlib.sha256(z.read(name)).hexdigest()!=expected:
                        raise ValueError('交接下载件内容与冻结结果不一致')
        def save_receipt(checksum,state):
            put('data_export',{'project_id':pid,'run_id':rid,'sha256':checksum,
                'source_manifest':meta['handoff_sha256'],'state':state},key)
        pending=record.get('state')=='pending'
        return durable_cached_artifact(path,expected_sha256=record.get('sha256') if not pending else None,
            pending_sha256=record.get('sha256') if pending else None,build=build,
            save_pending=lambda sha:save_receipt(sha,'pending'),
            save_committed=lambda sha:save_receipt(sha,'committed'),before_publish=assert_owned)


class IndentedSource(StrictModel):
    source_id:str=Field(min_length=1,max_length=64)
    columns:list[str]=Field(min_length=2,max_length=40)


def extract_indented_source(pid,source_id,columns):
    from .evidence_tables import parse_indented_table
    from .sources import revision
    from .execution import enqueue
    from .store import seal
    s=owned(pid,'source',source_id)
    if s['kind']!='file' or not s['name'].lower().endswith(('.txt','.md')):raise ValueError('缩进表格需提供保留空格或制表符的原始文本文件')
    parsed=parse_indented_table(source_path(s).read_text(encoding='utf-8'),columns,source_id=source_id)
    key='it_'+digest([pid,source_id,s.get('content_hash'),columns])[:40]
    try:
        old=owned(pid,'source',key)
        return {'source_id':old['id'],'records':len(parsed['rows']),'requires_semantic_review':True}
    except KeyError:pass
    raw=json.dumps(parsed['rows'],ensure_ascii=False).encode();name=key+'.json';target=DATA/'uploads'/name
    temporary=target.with_suffix('.json.part');temporary.write_bytes(raw);temporary.replace(target)
    cfg={'stored_name':name,'extraction_evidence':{k:v for k,v in parsed.items() if k not in ('rows','fields')}}
    child=put('source',{'project_id':pid,'name':'缩进表格-'+s['name']+'.json','kind':'file','secret_config':seal(cfg),
        'content_hash':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'status':'queued','created_at':now(),
        'origin_source_id':source_id,'origin_source_revision':s.get('revision',1),'requires_semantic_review':True,
        'semantic_alignment_required':source_requires_fact_alignment(s,{})},key)
    revision(child);enqueue('probe',key)
    return {'source_id':key,'records':len(parsed['rows']),'requires_semantic_review':True,
            'scope':'explicit_delimiters_and_layout_hierarchy; no_invented_business_ontology'}
