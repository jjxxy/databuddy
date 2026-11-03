"""Version-pinned report handlers for authorized business flows.

Inputs are explicit JSON data, never executable templates or code. The engine
must persist the approved flow and its resolved inputs before invoking these
handlers; this module does not grant access or invent approval by itself.
"""
from __future__ import annotations

import hashlib
import html
import json
import re

from pydantic import Field, model_validator

from . import office, workspace
from .actor_context import current_actor
from .business_flow_engine import Handler
from .models import StrictModel
from .store import get, put, now, seal, unseal
from .workspace_lifecycle import assert_turn_live

HTML_RENDERER = 'business-html-v2'
HTML_RENDERER_LEGACY = 'business-html-v1'
MAX_SPEC_BYTES = 256 * 1024
_NAME = r'^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff _-]{0,90}$'


class DeliveryConfig(StrictModel):
    name: str = Field(pattern=_NAME, min_length=1, max_length=91)
    renderer_version: str = Field(min_length=1, max_length=100)


class HtmlTable(StrictModel):
    columns: list[str] = Field(min_length=1,max_length=20)
    rows: list[list[str]] = Field(max_length=1000)

    @model_validator(mode='after')
    def complete(self):
        if any(not isinstance(col,str) or not col.strip() or len(col)>300 for col in self.columns):
            raise ValueError('报告表头无效')
        if any(len(row)!=len(self.columns) or any(len(cell)>2000 for cell in row) for row in self.rows):
            raise ValueError('报告表格行不完整或单元格过长')
        return self


class HtmlSection(StrictModel):
    heading: str = Field(default='', max_length=300)
    body: str = Field(default='', max_length=20000)
    bullets: list[str] = Field(default_factory=list, max_length=30)
    table: HtmlTable | None = None


class HtmlSpec(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    sections: list[HtmlSection] = Field(min_length=1, max_length=30)


class TableReportConfig(StrictModel):
    title: str = Field(min_length=1,max_length=300)
    format: str = Field(pattern=r'^(html|presentation)$')


def _config(value, version):
    config=DeliveryConfig.model_validate(value)
    if config.renderer_version != version:
        raise ValueError('已确认的成果渲染器版本不可用，请重新验证后再运行')
    return config


def validate_office_config(value):
    _office_config(value)


def _office_config(value):
    config=DeliveryConfig.model_validate(value)
    if config.renderer_version not in (office.RENDERER,office.LEGACY_RENDERER):
        raise ValueError('已确认的成果渲染器版本不可用，请重新验证后再运行')
    return config


def validate_html_config(value):
    _html_config(value)


def _html_config(value):
    config=DeliveryConfig.model_validate(value)
    if config.renderer_version not in (HTML_RENDERER,HTML_RENDERER_LEGACY):
        raise ValueError('已确认的成果渲染器版本不可用，请重新验证后再运行')
    return config


def validate_table_report_config(value):
    TableReportConfig.model_validate(value)


def _turn(context,tool):
    if not isinstance(context,dict):raise ValueError('工作流程缺少执行身份')
    pid,tid,actor=(context.get('project_id'),context.get('turn_id') or context.get('originating_turn_id'),context.get('actor_id'))
    if context.get('turn_id') and context.get('originating_turn_id') and context['turn_id']!=context['originating_turn_id']:
        raise ValueError('工作流程与原始任务不一致')
    if not all(isinstance(item,str) and item for item in (pid,tid,actor)) or actor!=current_actor():
        raise ValueError('工作流程缺少有效的项目或使用者身份')
    from .workflow_model_skill import _turn as authorized_turn
    turn=authorized_turn(dict(context,turn_id=tid))
    from . import agent_context,agent_gateway,agent_team
    agent_gateway.assert_turn_allowed(turn)
    agent_team.assert_turn_allowed(turn,tool,{})
    if tool not in agent_context.allowed(turn):raise ValueError('当前任务无权生成该成果')
    from .intents import require
    require(turn,'write')
    return turn


def _spec(inputs):
    if not isinstance(inputs,dict) or set(inputs)!={'spec'} or not isinstance(inputs['spec'],dict):
        raise ValueError('成果节点需要唯一的结构化规格输入')
    value=inputs['spec']
    if value.get('kind')=='table_report_spec_v1':
        value=value.get('spec')
        if not isinstance(value,dict):raise ValueError('数据报告规格不完整')
    try:raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    except (TypeError,ValueError,RecursionError,UnicodeError):raise ValueError('成果规格不是有效 JSON') from None
    if len(raw)>MAX_SPEC_BYTES:raise ValueError('成果规格超过大小限制，请拆分或引用固定版本文件')
    if re.search(r'(?i)(?:postgres(?:ql)?|mysql)(?:\+\w+)?://[^\s]*:|\bsk-[A-Za-z0-9_-]{12,}|(?:password|api_key|authorization|connection_string)["\x27]?\s*[:=]',raw.decode('utf-8')):
        raise ValueError('连接凭证不能写入业务成果')
    return raw,hashlib.sha256(raw).hexdigest()


def _review_notice(inputs):
    value=inputs.get('spec') if isinstance(inputs,dict) else None
    if isinstance(value,dict) and value.get('kind')=='table_report_spec_v1':
        return {'requires_business_review':True,
                'source_review_declared':value.get('requires_source_review') if type(value.get('requires_source_review')) is bool else None,
                'semantic_review_declared':value.get('requires_semantic_review') if type(value.get('requires_semantic_review')) is bool else None,
                'source_table_sha256_declared':value.get('table_sha256') if isinstance(value.get('table_sha256'),str) and re.fullmatch(r'[a-f0-9]{64}',value['table_sha256']) else None,
                'notice':'来源状态来自所提供的报告规格，不代替原始运行回执或业务核对。'}
    return {'requires_business_review':True,'notice':'成果内容来自提供的规格，尚未核验其财经依据。'}


def render_office(context,config,inputs):
    cfg=_office_config(config);turn=_turn(context,'render_office_artifact')
    raw,spec_hash=_spec(inputs)
    # Existing renderer validates all fields, office structure and spreadsheet
    # precision. The source checksum is checked again while holding its lock.
    office._load_spec(raw.decode('utf-8'))
    path='business-flow-inputs/'+spec_hash+'.json'  # Identity only; no file is written.
    result=office.render(turn,path,cfg.name,expected_sha256=spec_hash,frozen_spec=raw.decode('utf-8'),renderer_version=cfg.renderer_version)
    file,record=office.artifact(turn['project_id'],result['artifact_id'])
    if record.get('source_sha256')!=spec_hash or record.get('renderer')!=cfg.renderer_version or record.get('sha256')!=result.get('sha256'):
        raise ValueError('Office 成果与固定规格的回执不一致')
    from .workspace_artifacts import _register
    registered=_register(turn['project_id'],'office',result['artifact_id'],record['name'],record['sha256'],
                         bytes=record.get('bytes'),updated_at=record.get('created_at'),origin=turn['id'],
                         private={'office_id':result['artifact_id']})
    if not registered:raise ValueError('成果已归档，不能重新采用')
    return {'artifact_id':registered['id'],'format':result['format'],'sha256':result['sha256'],
            'spec_sha256':spec_hash,'renderer_version':cfg.renderer_version,'download_url':registered['download_url'],
            'structural_validation':True,'visual_validation':False,'financial_verified':False,'review':_review_notice(inputs)}


def _render_html_v1(spec):
    parts=['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">',
           '<title>'+html.escape(spec.title,quote=True)+'</title>',
           '<style>body{font-family:system-ui,sans-serif;max-width:960px;margin:3rem auto;padding:0 1.5rem;line-height:1.7;color:#263c35}h1,h2{color:#165d48}section{margin:2rem 0}li{margin:.3rem 0}</style>',
           '</head><body><main><h1>'+html.escape(spec.title,quote=True)+'</h1>']
    for section in spec.sections:
        parts.append('<section>')
        if section.heading:parts.append('<h2>'+html.escape(section.heading,quote=True)+'</h2>')
        for line in section.body.splitlines():parts.append('<p>'+html.escape(line,quote=True)+'</p>')
        if section.bullets:
            parts.append('<ul>')
            for item in section.bullets:parts.append('<li>'+html.escape(item,quote=True)+'</li>')
            parts.append('</ul>')
        # v1 documents previously had no table field. Preserve their exact
        # bytes while allowing newly generated structured specifications to
        # be rendered for an explicitly retained v1 flow.
        if section.table is not None:
            parts.append('<table><thead><tr>')
            parts.extend('<th>'+html.escape(c,quote=True)+'</th>' for c in section.table.columns)
            parts.append('</tr></thead><tbody>')
            for row in section.table.rows:
                parts.append('<tr>');parts.extend('<td>'+html.escape(c,quote=True)+'</td>' for c in row);parts.append('</tr>')
            parts.append('</tbody></table>')
        parts.append('</section>')
    parts.append('</main></body></html>')
    return ''.join(parts)


def _render_html(spec,version=HTML_RENDERER):
    if version==HTML_RENDERER_LEGACY:return _render_html_v1(spec)
    if version!=HTML_RENDERER:raise ValueError('报告渲染器版本不可用')
    esc=lambda value:html.escape(value,quote=True)
    css=('''
:root{color-scheme:light;--ink:#172c3d;--muted:#536676;--line:#d9e2e8;--accent:#08645e;--paper:#ffffff}
*{box-sizing:border-box}body{margin:0;background:#f1f4f6;color:var(--ink);font:16px/1.7 system-ui,-apple-system,"Microsoft YaHei",sans-serif}
.report{max-width:1120px;margin:40px auto;padding:0 24px}.report-head{background:var(--paper);border-top:5px solid var(--accent);padding:30px 36px;border-radius:4px 4px 0 0}
.eyebrow{color:var(--accent);font-size:12px;font-weight:700;letter-spacing:.12em}h1{margin:8px 0 0;font-size:clamp(26px,3vw,36px);line-height:1.28;overflow-wrap:anywhere}
main{background:var(--paper);padding:8px 36px 36px}section{padding:22px 0;border-top:1px solid var(--line)}section:first-child{border-top:0}h2{font-size:20px;line-height:1.4;margin:0 0 12px}p{margin:8px 0;white-space:pre-wrap;overflow-wrap:anywhere}li{margin:5px 0;overflow-wrap:anywhere}
.table-wrap{overflow-x:auto;border:1px solid var(--line);margin-top:12px}table{border-collapse:collapse;width:100%;font-size:14px;line-height:1.55}caption{text-align:left;padding:12px 14px;background:#f5f9f8;font-weight:600;color:var(--muted)}th,td{text-align:left;vertical-align:top;padding:10px 14px;border-bottom:1px solid var(--line);overflow-wrap:anywhere;min-width:100px}th{background:#ecf3f2;color:#203f40;font-weight:700}tbody tr:nth-child(even){background:#f8fafb}tbody tr:last-child td{border-bottom:0}.end{color:var(--muted);font-size:12px;padding-top:20px;border-top:1px solid var(--line)}
@media(max-width:600px){.report{margin:0;padding:0}.report-head{padding:24px 20px}main{padding:8px 20px 28px}th,td{padding:9px 10px}}
@media print{@page{size:A4;margin:15mm}body{background:#fff;font-size:10pt}.report{max-width:none;margin:0;padding:0}.report-head{padding:0 0 12pt;border:0;border-bottom:2pt solid #08645e}main{padding:0}section{padding:12pt 0}h1{font-size:22pt}h2{font-size:15pt;break-after:avoid}.table-wrap{overflow:visible;border:0}table{font-size:9pt;table-layout:fixed}thead{display:table-header-group}tr{break-inside:avoid}th,td{min-width:0;padding:5pt;border:1px solid #c9d3d8}tbody tr:nth-child(even){background:#fff}}
''')
    if any(section.table is not None and len(section.table.columns)>6 for section in spec.sections):
        css+='@media print{@page{size:A4 landscape}}'
    parts=['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">',
           '<title>'+esc(spec.title)+'</title><style>'+css+'</style></head><body>',
           '<div class="report"><header class="report-head"><div class="eyebrow">业务报告</div><h1>'+esc(spec.title)+'</h1></header><main>']
    for index,section in enumerate(spec.sections):
        ident='section-'+str(index+1)
        parts.append('<section'+(' aria-labelledby="'+ident+'"' if section.heading else '')+'>')
        if section.heading:parts.append('<h2 id="'+ident+'">'+esc(section.heading)+'</h2>')
        for line in section.body.splitlines():parts.append('<p>'+esc(line)+'</p>')
        if section.bullets:
            parts.append('<ul>');parts.extend('<li>'+esc(item)+'</li>' for item in section.bullets);parts.append('</ul>')
        if section.table is not None:
            table=section.table
            parts.append('<div class="table-wrap" role="region" aria-label="'+esc(section.heading or '数据表')+'" tabindex="0"><table>')
            parts.append('<caption>'+esc(section.heading or '数据明细')+' · 共 '+str(len(table.rows))+' 行</caption><thead><tr>')
            parts.extend('<th scope="col">'+esc(label)+'</th>' for label in table.columns);parts.append('</tr></thead><tbody>')
            for row in table.rows:
                parts.append('<tr>');parts.extend('<td>'+esc(cell)+'</td>' for cell in row);parts.append('</tr>')
            parts.append('</tbody></table></div>')
        parts.append('</section>')
    parts.append('<div class="end">报告内容以实际资料和业务口径核对为准。</div></main></div></body></html>')
    return ''.join(parts)


def render_html(context,config,inputs):
    cfg=_html_config(config);turn=_turn(context,'write_file')
    raw,spec_hash=_spec(inputs)
    spec=HtmlSpec.model_validate(json.loads(raw))
    content=_render_html(spec,cfg.renderer_version);content_hash=hashlib.sha256(content.encode('utf-8')).hexdigest()
    if len(content.encode('utf-8'))>2*1024*1024:raise ValueError('完整报告超过安全容量，未截断，请使用完整数据结果')
    # Business values must not enter the project's Git working tree. Save the
    # verified output in the protected artifact store; the content itself is
    # not a source file or a versioned workflow definition.
    from .config import DATA
    from .locks import FileLock
    from .agent_handoff import _atomic_bytes
    identity=hashlib.sha256(json.dumps([turn['project_id'],turn['id'],turn.get('actor_id','owner'),cfg.name,
                                       content_hash,cfg.renderer_version],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    artifact_id='bfa_'+identity[:40]
    path=DATA/'business-flow-artifacts'/artifact_id/(cfg.name+'.html')
    record={'id':artifact_id,'project_id':turn['project_id'],'actor_id':turn.get('actor_id','owner'),
            'turn_id':turn['id'],'name':cfg.name+'.html','relative_path':str(path.relative_to(DATA)),
            'format':'html','sha256':content_hash,'bytes':len(content.encode('utf-8')),
            'spec_sha256':spec_hash,'renderer_version':cfg.renderer_version}
    with FileLock(str(DATA/('business-flow-artifact-'+identity+'.lock')),timeout=20):
        try:old=get('business_flow_artifact',artifact_id)
        except KeyError:old=None
        if old and any(old.get(key)!=value for key,value in record.items()):
            raise ValueError('报告回执与已保存的成果不一致')
        _atomic_bytes(path,content.encode('utf-8'))
        if not old:put('business_flow_artifact',dict(record,created_at=now(),secret_preview=seal(json.loads(raw))),artifact_id)
        elif not old.get('secret_preview'):
            put('business_flow_artifact',dict(old,secret_preview=seal(json.loads(raw))),artifact_id)
    from .workspace_artifacts import _register
    registered=_register(turn['project_id'],'work',artifact_id,record['name'],content_hash,format='html',bytes=record['bytes'],
                         origin=turn['id'],status='saved',semantic_status='not_confirmed',metric_status='not_confirmed',
                         private={'flow_artifact_id':artifact_id})
    if not registered:raise ValueError('报告已归档，不能重新采用')
    return {'artifact_id':registered['id'],'format':'html','sha256':content_hash,'spec_sha256':spec_hash,
            'renderer_version':cfg.renderer_version,'download_url':registered['download_url'],
            'content_escaped':True,'visual_validation':False,'financial_verified':False,'review':_review_notice(inputs)}


def artifact(pid,identity):
    """Read an exact project-and-actor-owned report with a verified checksum."""
    from .config import DATA
    from .agent_handoff import _read_bytes
    from .platform_files import assert_plain_path
    if not isinstance(identity,str) or not re.fullmatch(r'bfa_[a-f0-9]{40}',identity):raise KeyError(identity)
    value=get('business_flow_artifact',identity)
    if value.get('project_id')!=pid or value.get('actor_id')!=current_actor() or value.get('archived') or value.get('deleted_at'):
        raise ValueError('该业务成果不可用')
    turn=get('turn',value.get('turn_id'))
    if turn.get('project_id')!=pid or turn.get('actor_id','owner')!=current_actor():raise ValueError('成果与原任务不一致')
    assert_turn_live(turn)
    relative=value.get('relative_path')
    if not isinstance(relative,str):raise ValueError('成果存储路径无效')
    root=DATA/'business-flow-artifacts';path=DATA/relative
    assert_plain_path(path,missing_ok=False)
    if not path.resolve().is_relative_to(root.resolve()):raise ValueError('成果存储路径越界')
    raw=_read_bytes(path,2*1024*1024)
    if hashlib.sha256(raw).hexdigest()!=value.get('sha256') or len(raw)!=value.get('bytes'):
        raise ValueError('成果内容与固定回执不一致')
    return path,value


def preview_blocks(pid,identity):
    """Return text-only report structure tied to verified stored bytes."""
    _,value=artifact(pid,identity)
    if value.get('renderer_version') not in (HTML_RENDERER,HTML_RENDERER_LEGACY) or not value.get('secret_preview'):
        raise ValueError('此报告没有可验证的结构化预览，请下载原件')
    try:
        source=unseal(value['secret_preview'])
        raw=json.dumps(source,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
        if len(raw)>MAX_SPEC_BYTES or hashlib.sha256(raw).hexdigest()!=value['spec_sha256']:
            raise ValueError('报告预览规格与固定回执不一致')
        spec=HtmlSpec.model_validate(source)
        if hashlib.sha256(_render_html(spec,value['renderer_version']).encode('utf-8')).hexdigest()!=value['sha256']:
            raise ValueError('报告预览与保存的成果不一致')
    except Exception:
        raise ValueError('报告预览无法验证，请下载原件') from None
    blocks=[{'type':'heading','text':spec.title}]
    for section in spec.sections:
        if section.heading:blocks.append({'type':'heading','text':section.heading})
        for line in section.body.splitlines():
            blocks.append({'type':'paragraph','text':line})
        for item in section.bullets:blocks.append({'type':'paragraph','text':'• '+item})
        if section.table is not None:
            for start in range(0,max(1,len(section.table.rows)),100):
                blocks.append({'type':'table','table':{'columns':section.table.columns,'rows':section.table.rows[start:start+100]}})
    return blocks


def table_report_spec(context,config,inputs):
    """Project every row of a completed typed table, never model-made totals.

    The graph validator must require this input to come from an actual
    table_workflow node; the digest check independently catches corruption.
    This deliberately fails, rather than silently truncating, if the complete
    table cannot be represented within the small report output budget.
    """
    cfg=TableReportConfig.model_validate(config)
    _turn(context,'render_office_artifact' if cfg.format=='presentation' else 'write_file')
    if not isinstance(inputs,dict) or set(inputs) not in ({'table'},{'table','commentary'}):raise ValueError('报告需要一份已完成的数据加工结果')
    table=inputs['table']
    if (not isinstance(table,dict) or table.get('kind')!='table_workflow_v1' or table.get('complete') is not True
            or table.get('financial_evidence_status')!='requires_business_review'
            or type(table.get('requires_source_review')) is not bool or type(table.get('requires_semantic_review')) is not bool):
        raise ValueError('报告输入不是完整且待业务核验的数据加工结果')
    max_rows=100 if cfg.format=='presentation' else 1000
    if 'rows_ref' in table:
        if type(table.get('row_count')) is not int or table['row_count']>max_rows:
            raise ValueError('完整结果超出报告展示容量；未截断，请使用完整表格交付')
        # References are bound to the authenticated project, actor, turn and
        # run; the existing output digest check below independently verifies
        # the complete table after decryption.
        from .business_flow_data import materialize_table
        table=materialize_table(context,table)
    rows=table.get('rows');schema=table.get('schema')
    if not isinstance(rows,list) or type(table.get('row_count')) is not int or table['row_count']!=len(rows):
        raise ValueError('数据结果行数不完整，未生成报告')
    from .data_workflows import TableSchema, digest
    parsed=TableSchema.model_validate(schema)
    if not isinstance(table.get('output_sha256'),str) or digest({'schema':schema,'rows':rows})!=table['output_sha256']:
        raise ValueError('数据结果摘要不一致，未生成报告')
    if len(rows)>max_rows or len(parsed.columns)>20:
        raise ValueError('完整结果超出报告展示容量；未截断，请使用完整表格交付')
    keys=[column.name for column in parsed.columns]
    labels=[(column.label or column.name)+(('（'+column.unit+'）') if column.unit else '')+
            ((' · '+column.period) if column.period else '') for column in parsed.columns]
    display_rows=[]
    for row in rows:
        if not isinstance(row,dict) or set(row)!=set(keys):raise ValueError('数据结果字段与声明不一致')
        values=[]
        for key,label in zip(keys,labels):
            value=row[key]
            if value is not None and type(value) not in (str,int,bool):raise ValueError('报告数值需要精确文本或整数，不接受浮点数')
            display='未提供' if value is None else ('是' if value is True else '否' if value is False else str(value))
            values.append(display)
        # The report retains each value as text, including exact decimal
        # strings. It never infers totals, rounding, or missing-as-zero.
        display_rows.append(values)
    notes=['以下为实际数据加工结果，完整 '+str(len(rows))+' 行；不代表业务口径或财经数字已审核。']
    if parsed.meaning:notes.append('结果含义：'+parsed.meaning)
    if isinstance(table.get('output_sha256'),str) and re.fullmatch(r'[a-f0-9]{64}',table['output_sha256']):
        notes.append('数据结果摘要：'+table['output_sha256'])
    if isinstance(table.get('plan_sha256'),str) and re.fullmatch(r'[a-f0-9]{64}',table['plan_sha256']):
        notes.append('处理方案摘要：'+table['plan_sha256'])
    refs=table.get('source_refs')
    lineage_complete=table.get('source_lineage_complete') is True
    selection_complete=table.get('selection_lineage_complete') is True
    if not isinstance(refs,list):refs=[];lineage_complete=False
    for ref in refs:
        if (not isinstance(ref,dict) or not isinstance(ref.get('source_id'),str) or not ref['source_id']
                or type(ref.get('revision')) is not int or ref['revision']<1
                or not isinstance(ref.get('content_hash'),str) or not re.fullmatch(r'[a-f0-9]{64}',ref['content_hash'])
                or not isinstance(ref.get('name'),str) or not ref['name']):
            lineage_complete=False
            selection_complete=False
            continue
        notes.append('文件来源：'+ref['name']+'；资料 ID：'+ref['source_id']+'；版本：'+str(ref['revision'])+'；内容摘要：'+ref['content_hash'])
        basis=ref.get('read_basis');basis_hash=ref.get('read_basis_sha256')
        try:
            from .business_flow_data import _read_basis
            if not isinstance(basis_hash,str) or not re.fullmatch(r'[a-f0-9]{64}',basis_hash):raise ValueError('缺少读取依据摘要')
            _read_basis(basis,basis_hash,ref['content_hash'])
        except (ValueError,TypeError,UnicodeError,RecursionError):
            selection_complete=False
            continue
        # Keep the actual selection, reader options and mapping together with
        # this specific source version. Names are untrusted and all renderers
        # must encode them as text; no inference of business approval is made.
        field_labels={'kind':'读取方式','file_sha256':'原文件摘要','sheet':'工作表','table':'表格',
                'range':'范围（起始列、行及结束列、行）','header_rows':'表头行','data_rows':'数据行',
                'merged_label_columns':'合并标签列','hidden_rows_policy':'隐藏行处理',
                'subtotal_policy':'小计处理','percentage_columns':'百分比列','cell_range':'指定单元格范围',
                'data_start_row':'指定数据起始行','data_end_row':'指定数据结束行','hidden_rows':'隐藏行处理',
                'text_columns':'按文本读取的列','html_table':'网页表格序号','table_id':'网页表格标识',
                'records_path':'JSON 记录路径','selection_method':'记录选择方式','wrapper_context_fields':'记录之外的上下文字段',
                'wrapper_context_status':'记录之外的上下文状态'}
        kinds={'workbook_table':'Excel 工作表','html_table':'网页表格','whole_source_table':'完整来源表',
               'json_records':'JSON 记录'}
        methods={'explicit':'明确指定','inferred':'自动识别，需核对','root_array':'顶层数组','root_object':'顶层对象作为一条记录'}
        def describe(mapping):
            parts=[]
            for key,value in sorted(mapping.items()):
                shown=kinds.get(value,value) if key=='kind' and isinstance(value,str) else methods.get(value,value) if key=='selection_method' and isinstance(value,str) else '未记录，需回查原始资料' if key=='wrapper_context_status' and value=='not_recorded' else value
                text=shown if isinstance(shown,str) else json.dumps(shown,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
                parts.append(field_labels.get(key,key)+'：'+text)
            return '；'.join(parts) or '无'
        selection=basis['selection']
        if ref['name'].lower().endswith('.json') and selection.get('kind')=='whole_source_table':
            selection_complete=False
            notes.append('实际读取选择：旧版 JSON 记录路径未记录，请回查原始资料与运行记录。')
        else:
            notes.append('实际读取选择：'+describe(selection))
        notes.append('请求的读取配置（实际范围以读取选择为准）：'+describe(basis['options']))
        mapping=basis['column_map']
        notes.append('字段映射：'+('；'.join(str(key)+' → '+str(value) for key,value in sorted(mapping.items())) if mapping else '沿用原字段'))
        notes.append('读取依据摘要：'+basis_hash)
    if not refs or not lineage_complete:notes.append('文件来源记录不完整，需核对原始资料及版本。')
    if not refs or not selection_complete:notes.append('读取范围或字段映射记录不完整，请回查流程运行记录并核对原始资料。')
    if table['requires_source_review']:notes.append('来源资料仍需业务核对。')
    if table['requires_semantic_review']:notes.append('数据语义或处理口径仍需业务核对。')
    review_body='\n'.join(notes)
    if len(review_body)>20000:raise ValueError('完整来源与读取依据超过报告容量；未截断，请查阅流程运行记录')
    sections=[{'heading':'数据与核对状态','body':review_body,'bullets':[]}]
    sections.append({'heading':'数据明细','body':'','bullets':[],
                     'table':{'columns':labels,'rows':display_rows}})
    commentary=inputs.get('commentary')
    if commentary is not None:
        if (not isinstance(commentary,dict) or commentary.get('evidence_status')!='unverified'
                or commentary.get('nondeterministic') is not True or not isinstance(commentary.get('text'),str)
                or not commentary['text'].strip() or len(commentary['text'])>1000
                or hashlib.sha256(commentary['text'].encode('utf-8')).hexdigest()!=commentary.get('output_sha256')):
            raise ValueError('AI 说明回执无效或超出展示容量，未生成报告')
        sections.append({'heading':'AI 生成说明（未经核验）',
                         'body':'以下内容由 AI 生成，包含的数字和判断均未经核验，不能作为财务计算依据。\n'+commentary['text'],
                         'bullets':[]})
    spec={'title':cfg.title,'sections':sections}
    if cfg.format=='presentation':spec['kind']='presentation'
    try:raw=json.dumps(spec,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    except (TypeError,ValueError,RecursionError,UnicodeError):raise ValueError('报告规格无效') from None
    if len(raw)>MAX_SPEC_BYTES:raise ValueError('完整结果超出报告容量；未截断，请使用完整表格交付')
    return {'kind':'table_report_spec_v1','spec':spec,'table_sha256':table['output_sha256'],
            'row_count':len(rows),'requires_source_review':table['requires_source_review'],
            'requires_semantic_review':table['requires_semantic_review'],
            'financial_evidence_status':'requires_business_review',
            'ai_commentary_unverified':commentary is not None}


def handlers():
    return {'office_document':Handler(render_office,deterministic=False,replay_safe=False,
                                      validate_config=validate_office_config,version=office.RENDERER),
            'html_report':Handler(render_html,deterministic=True,replay_safe=False,
                                  validate_config=validate_html_config,version=HTML_RENDERER),
            'table_report_spec':Handler(table_report_spec,deterministic=True,replay_safe=True,
                                        validate_config=validate_table_report_config,version='table-report-v5')}
