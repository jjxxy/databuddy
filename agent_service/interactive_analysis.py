"""Interactive, deterministic exploration of a fixed adopted result.

Each request verifies and rereads the complete immutable handoff. This is not
an approval, a causal analysis, or evidence that source data is complete.
"""
import re
from datetime import date
from decimal import Decimal, localcontext
from typing import Literal
from pydantic import Field, model_validator

from .models import StrictModel
from .data_workflows import NAME, TableSchema, decimal_value
from .result_delivery import read_all_handoff
from .config import MAX_ROWS

MAX_ANALYSIS_BYTES = 20 * 1024 * 1024
MAX_GROUPS = 500  # Other consumers, including factor and GAP, remain capped.
MAX_EXPLORE_GROUPS = 5000
MAX_GROUP_PAGE = 500


class ExploreFilter(StrictModel):
    column: str = Field(pattern=NAME)
    value: str | bool | None

    @model_validator(mode='after')
    def validate_value(self):
        if isinstance(self.value, str) and len(self.value) > 500:
            raise ValueError('筛选值过长')
        return self


class ExploreRequest(StrictModel):
    expected_handoff_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    group_by: list[str] = Field(min_length=1, max_length=3)
    group_offset: int = Field(default=0, ge=0, strict=True)
    group_limit: int = Field(default=MAX_GROUP_PAGE, ge=1, le=MAX_GROUP_PAGE, strict=True)
    filters: list[ExploreFilter] = Field(default_factory=list, max_length=10)
    metric: Literal['count', 'sum'] = 'count'
    measure: str | None = Field(default=None, pattern=NAME)
    period_column: str | None = Field(default=None, pattern=NAME)
    period: str | None = Field(default=None, pattern=r'^\d{4}-(?:0[1-9]|1[0-2])$')

    @model_validator(mode='after')
    def validate_request(self):
        if len(set(self.group_by)) != len(self.group_by):raise ValueError('分组字段不能重复')
        if len({f.column for f in self.filters}) != len(self.filters):raise ValueError('同一字段不能重复筛选')
        if (self.period is None) != (self.period_column is None):raise ValueError('切月需要同时指定期间字段和月份')
        if self.period:
            try:date.fromisoformat(self.period+'-01')
            except ValueError:raise ValueError('月份不是有效的日历日期') from None
        if self.period_column and any(f.column == self.period_column for f in self.filters):raise ValueError('月份与筛选字段不能重复')
        if self.metric == 'sum' and not self.measure:raise ValueError('求和必须选择数值字段')
        if self.metric == 'count' and self.measure is not None:raise ValueError('计数不接受数值字段')
        return self


def _as_dimension(value, column):
    if value is None:return None
    if column.data_type in ('text', 'date') and isinstance(value, str):return value
    if column.data_type == 'boolean' and type(value) is bool:return value
    raise ValueError('分组或筛选字段的数据类型与声明不一致，不能分析')


def _missing(value):
    return value is None or isinstance(value,str) and not value.strip()


def _month_of(value):
    if not isinstance(value,str):raise ValueError('期间字段存在空值或不明确日期，请先规范期间再切月')
    if re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])',value):
        try:date.fromisoformat(value+'-01');return value
        except ValueError:pass
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
        try:return date.fromisoformat(value).isoformat()[:7]
        except ValueError:pass
    raise ValueError('期间字段存在空值或不明确日期，请先规范期间再切月')


def _checked_grain(rows, schema):
    if not schema.grain:
        if len(rows) > 1:raise ValueError('结果缺少业务粒度，无法确认是否重复计数')
        return
    fields = {c.name:c for c in schema.columns}
    seen = set()
    for row in rows:
        key = []
        for name in schema.grain:
            value = row.get(name)
            if value is None or isinstance(value, str) and not value.strip():raise ValueError('业务粒度存在空值，不能汇总')
            column = fields[name]
            if column.data_type in ('decimal', 'integer'):
                value = decimal_value(value)
                if column.data_type == 'integer' and value != value.to_integral_value():raise ValueError('业务粒度不是有效整数')
            elif column.data_type in ('text', 'date', 'boolean'):
                value = _as_dimension(value, column)
            key.append(value)
        item = tuple(key)
        if item in seen:raise ValueError('业务粒度重复，不能计算可能重复的金额')
        seen.add(item)


def explore(pid, rid, value):
    from . import workspace, project_data as data
    from .workspace_lifecycle import _project_lock, assert_project_live
    from .jobs import get_job
    value = ExploreRequest.model_validate(value)
    # Follow restore's workspace -> project lifecycle lock order. A project
    # deleted while the complete table is read must not produce new results.
    with workspace.lock(pid), _project_lock(pid):
        assert_project_live(pid)
        run = get_job(rid)
        if run.get('project_id') != pid or run.get('kind') != 'data_workflow' or run.get('status') != 'succeeded':
            raise ValueError('请先采用并核对正式结果，再进行完整数据分析')
        workflow = data.owned(pid, 'data_workflow', run['target'])
        if workflow.get('published_run') != rid:raise ValueError('正式结果版本已变化，请刷新后重新分析')
        folder, meta = data.result_folder(pid, rid)
        if meta['handoff_sha256'] != value.expected_handoff_sha256:raise ValueError('结果版本已变化，请刷新后重新分析')
        data.ensure_candidate_bulk_allowed(pid, rid)
        root = folder / 'handoff'
        if sum((root / name).stat().st_size for name in ('rows.jsonl','provenance.jsonl','schema.json','context.json','stages.json')) > MAX_ANALYSIS_BYTES:
            raise ValueError('当前交互分析上限为20 MiB，请先缩小数据范围后生成正式结果；没有截断计算')
        table = read_all_handoff(root, expected_manifest_sha256=meta['handoff_sha256'], max_rows=MAX_ROWS)
        if table['total'] != len(table['rows']) or table['total'] != meta['records'] or not table['complete_result_available']:
            raise ValueError('完整数据与结果记录不一致，不能分析')
        data.require_safe_numeric_lineage(pid, rid, root_meta=meta, root_page=table)
        schema = TableSchema.model_validate(table.get('context',{}).get('output_semantics') or table['schema'])
        fields = {c.name:c for c in schema.columns}
        for name in value.group_by + [f.column for f in value.filters]:
            if name not in fields or fields[name].data_type not in ('text','date','boolean'):
                raise ValueError('分组和筛选目前只支持文本、日期或布尔字段')
        for f in value.filters:
            col = fields[f.column]
            if f.value is not None and ((col.data_type == 'boolean' and type(f.value) is not bool) or (col.data_type != 'boolean' and not isinstance(f.value,str))):
                raise ValueError('筛选值与字段类型不一致')
        rows = table['rows']
        if value.period_column:
            col = fields.get(value.period_column)
            if not col or col.data_type not in ('text','date'):raise ValueError('月份字段必须是文本或日期字段')
            # Ambiguous or missing dates cannot be silently excluded.
            months=[_month_of(r.get(value.period_column)) for r in rows]
            rows = [r for r,month in zip(rows,months) if month == value.period]
        for f in value.filters:
            if not _missing(f.value) and any(_missing(r.get(f.column)) for r in rows):
                raise ValueError('筛选字段存在空值，无法判断是否属于所选范围；请先核对空值')
        rows = [r for r in rows if all(_as_dimension(r.get(f.column), fields[f.column]) == f.value for f in value.filters)]
        measure = fields.get(value.measure) if value.measure else None
        if value.metric == 'sum':
            if not measure or measure.data_type not in ('decimal','integer') or measure.aggregation != 'additive' or measure.value_format != 'native' or not measure.unit:
                raise ValueError('只能汇总已声明单位、可加的原始数字字段；不能直接汇总比例')
            if not measure.period and not value.period:raise ValueError('数值期间未声明，请先选择月份字段和月份')
            if measure.period and value.period and measure.period != value.period:raise ValueError('数值期间与所选月份不一致')
            # The declared grain applies to the full frozen result. Checking
            # only filtered rows can hide duplicates in other offices/months.
            _checked_grain(table['rows'], schema)
        groups = {}
        with localcontext() as ctx:
            ctx.prec = 120
            for row in rows:
                key = tuple(_as_dimension(row.get(name),fields[name]) for name in value.group_by)
                if key not in groups:
                    if len(groups) >= MAX_EXPLORE_GROUPS:raise ValueError(f'分组超过{MAX_EXPLORE_GROUPS}项，请增加筛选后重试；没有截断计算')
                    groups[key] = {'values':list(key), 'count':0, 'amount':Decimal(0)}
                group = groups[key];group['count'] += 1
                if measure:
                    raw = row.get(value.measure)
                    if raw is None:raise ValueError('数值字段存在空值，不能把缺失值当作零')
                    number = decimal_value(raw)
                    if measure.data_type == 'integer' and number != number.to_integral_value():raise ValueError('数值字段不是有效整数')
                    group['amount'] += number
        # Aggregate and sort all groups before paging. The page is a view of
        # the complete calculation, not a summary of just the displayed rows.
        keys = sorted(groups,key=lambda x:tuple('' if v is None else ('1' if v is True else '0' if v is False else '2'+v) for v in x))
        total_groups = len(keys)
        if value.group_offset > total_groups:
            raise ValueError('分组页码超出完整计算结果，请重新开始分析')
        page_keys = keys[value.group_offset:value.group_offset+value.group_limit]
        result = []
        for key in page_keys:
            group=groups[key]
            item={'values':group['values'],'count':group['count']}
            if measure:item.update(amount=format(group['amount'],'f'),unit=measure.unit,period=measure.period or value.period)
            result.append(item)
        # Recheck the adopted pointer after computation; the returned values
        # always refer to the exact requested immutable version.
        assert_project_live(pid)
        if data.owned(pid,'data_workflow',run['target']).get('published_run') != rid:
            raise ValueError('分析期间正式结果已变化，请刷新后重试')
        return {'run_id':rid,'handoff_sha256':meta['handoff_sha256'],'plan_sha256':meta['plan_sha256'],
                'total_rows':table['total'],'selected_rows':len(rows),'group_by':value.group_by,
                'filters':[f.model_dump() for f in value.filters],'period':value.period,
                'period_column':value.period_column,'metric':value.metric,'measure':value.measure,
                'groups':result,'input_complete':True,
                # Old clients use complete to mean all groups returned. Keep
                # that assertion false for any partial result page.
                'complete':value.group_offset==0 and len(result)==total_groups,
                'groups_total':total_groups,'grouped_rows':sum(group['count'] for group in groups.values()),
                'group_offset':value.group_offset,
                'group_limit':value.group_limit,
                'next_group_offset':value.group_offset+len(result) if value.group_offset+len(result)<total_groups else None,
                'notice':'按固定结果的完整记录计算；分组按页展示，不能把本页当作全部分组。这不是财务审批，也不证明原始资料完整或业务口径正确。'}
