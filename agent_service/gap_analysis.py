"""Exact variance between declared metrics in an adopted immutable result.

This is arithmetic comparison, not a business-cause determination or approval.
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Literal
from pydantic import Field, model_validator

from .models import StrictModel
from .data_workflows import NAME, TableSchema, decimal_value
from .result_delivery import read_all_handoff
from .config import MAX_ROWS
from .interactive_analysis import _checked_grain, _as_dimension, MAX_ANALYSIS_BYTES, MAX_GROUPS


class GapRequest(StrictModel):
    expected_handoff_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    baseline_column: str = Field(pattern=NAME)
    current_column: str = Field(pattern=NAME)
    baseline_period: str = Field(pattern=r'^\d{4}-(?:0[1-9]|1[0-2])$')
    current_period: str = Field(pattern=r'^\d{4}-(?:0[1-9]|1[0-2])$')
    group_by: list[str] = Field(default_factory=list, max_length=2)
    ratio_decimals: int = Field(ge=0, le=8, strict=True)
    rounding: Literal['half_even'] = 'half_even'

    @model_validator(mode='after')
    def validate_input(self):
        if self.baseline_column == self.current_column:raise ValueError('比较的两个字段必须不同')
        if len(set(self.group_by)) != len(self.group_by):raise ValueError('分组字段不能重复')
        try:
            date.fromisoformat(self.baseline_period+'-01');date.fromisoformat(self.current_period+'-01')
        except ValueError:raise ValueError('月份必须是有效日历月份') from None
        return self


def evaluate(table, value):
    value=GapRequest.model_validate(value)
    rows=table.get('rows')
    if not isinstance(rows,list) or table.get('total')!=len(rows) or table.get('complete_result_available') is not True:
        raise ValueError('必须读取完整固定结果，不能使用预览数据')
    if not rows:raise ValueError('没有数据，未将缺失结果当作零')
    schema=TableSchema.model_validate(table.get('context',{}).get('output_semantics') or table['schema'])
    fields={c.name:c for c in schema.columns}
    before,after=fields.get(value.baseline_column),fields.get(value.current_column)
    if before is None or after is None:raise ValueError('比较字段不存在')
    if any(c.data_type not in ('decimal','integer') or c.value_format!='native' or c.aggregation!='additive' or not c.unit for c in (before,after)):
        raise ValueError('比较字段必须是明确单位和可加性的原始数值，不能直接相加比例')
    if before.unit!=after.unit:raise ValueError('比较字段单位不一致，不能隐式换算')
    if (before.period,after.period)!=(value.baseline_period,value.current_period):
        raise ValueError('比较字段期间与指定期间不一致，不能推断期间')
    for column in value.group_by:
        if column not in fields or fields[column].data_type not in ('text','date','boolean'):
            raise ValueError('分组仅支持已声明的文本、日期或布尔字段')
    _checked_grain(rows,schema)
    groups={}
    with localcontext() as ctx:
        ctx.prec=120
        for row in rows:
            values=[row.get(value.baseline_column),row.get(value.current_column)]
            if any(v is None for v in values):raise ValueError('比较字段存在空值，不能把缺失值当作零')
            amounts=[decimal_value(v) for v in values]
            if any(c.data_type=='integer' and n!=n.to_integral_value() for c,n in zip((before,after),amounts)):
                raise ValueError('整数比较字段存在非整数值')
            key=tuple(_as_dimension(row.get(name),fields[name]) for name in value.group_by)
            if key not in groups:
                if len(groups)>=MAX_GROUPS:raise ValueError('分组超过500项，请缩小分析范围；没有截断计算')
                groups[key]={'count':0,'baseline':Decimal(0),'current':Decimal(0)}
            item=groups[key];item['count']+=1;item['baseline']+=amounts[0];item['current']+=amounts[1]
        maximum=max(abs(item['current']-item['baseline']) for item in groups.values())
        result=[]
        for key in sorted(groups,key=lambda k:tuple('' if v is None else ('1' if v is True else '0' if v is False else '2'+v) for v in k)):
            item=groups[key];delta=item['current']-item['baseline']
            relative=None if item['baseline']==0 else (delta/item['baseline']*100).quantize(Decimal(1).scaleb(-value.ratio_decimals),rounding=ROUND_HALF_EVEN)
            magnitude=0 if not maximum or not delta else max(1,int((abs(delta)/maximum*10000).to_integral_value(rounding=ROUND_HALF_EVEN)))
            result.append({'values':list(key),'count':item['count'],'baseline':format(item['baseline'],'f'),
                           'current':format(item['current'],'f'),'delta':format(delta,'f'),
                           'relative_percent':None if relative is None else format(relative,'f'),
                           'ratio_unavailable_reason':'基准为0，无法计算变化率' if relative is None else None,
                           'ratio_interpretation_warning':'基准为负，变化率按约定公式计算，其正负不代表改善或恶化，请结合业务口径解读。' if item['baseline']<0 else None,
                           'chart_direction':'increase' if delta>0 else 'decrease' if delta<0 else 'equal',
                           'chart_basis_points':magnitude})
    return {'group_by':value.group_by,'groups':result,'total_rows':len(rows),'unit':before.unit,
            'baseline_column':value.baseline_column,'current_column':value.current_column,
            'baseline_period':value.baseline_period,'current_period':value.current_period,
            'ratio_decimals':value.ratio_decimals,'rounding':'half_even',
            'formulas':{'delta':'本期或实际值－基准值','relative_percent':'（本期或实际值－基准值）÷基准值×100%'},
            'chart_notice':'条形长度为相对本次最大绝对差额的近似比例；准确数值以表格为准。',
            'notice':'按固定版本的完整数据计算；本功能仅支持全表或分组，不支持按筛选字段或月份字段切片，版本更新后请刷新。单位相同不证明业务口径相同，请核对两个字段的含义和范围。差额只是算术比较，不证明业务原因、原始资料完整或财务审批。'}


def analyze(pid,rid,value):
    from . import workspace, project_data as data
    from .workspace_lifecycle import _project_lock, assert_project_live
    from .jobs import get_job
    value=GapRequest.model_validate(value)
    with workspace.lock(pid), _project_lock(pid):
        assert_project_live(pid)
        run=get_job(rid)
        if run.get('project_id')!=pid or run.get('kind')!='data_workflow' or run.get('status')!='succeeded':
            raise ValueError('请先采用并核对正式结果')
        if data.owned(pid,'data_workflow',run['target']).get('published_run')!=rid:raise ValueError('正式结果版本已变化')
        folder,meta=data.result_folder(pid,rid)
        if meta['handoff_sha256']!=value.expected_handoff_sha256:raise ValueError('结果版本已变化')
        data.ensure_candidate_bulk_allowed(pid,rid)
        root=folder/'handoff'
        if sum((root/name).stat().st_size for name in ('rows.jsonl','provenance.jsonl','schema.json','context.json','stages.json'))>MAX_ANALYSIS_BYTES:
            raise ValueError('当前差异分析上限为20 MiB，请缩小范围；没有截断计算')
        table=read_all_handoff(root,expected_manifest_sha256=meta['handoff_sha256'],max_rows=MAX_ROWS)
        if table['total']!=meta['records']:raise ValueError('结果记录数不一致')
        data.require_safe_numeric_lineage(pid,rid,root_meta=meta,root_page=table)
        result=evaluate(table,value)
        assert_project_live(pid)
        if data.owned(pid,'data_workflow',run['target']).get('published_run')!=rid:raise ValueError('分析期间正式结果已变化')
        return dict(result,run_id=rid,handoff_sha256=meta['handoff_sha256'],plan_sha256=meta['plan_sha256'])
