"""Exact two-factor decomposition over a frozen, adopted result.

The calculation is an explicit arithmetic attribution, not proof of cause.
The period, quantity, price and unit mapping must be declared by the caller.
"""
from decimal import Decimal, localcontext
from datetime import date
from pydantic import Field, model_validator
from .models import StrictModel
from .data_workflows import NAME, TableSchema, decimal_value
from .result_delivery import read_all_handoff
from .config import MAX_ROWS
from .interactive_analysis import ExploreFilter, _checked_grain, _as_dimension, _missing, MAX_ANALYSIS_BYTES, MAX_GROUPS


class FactorRequest(StrictModel):
    expected_handoff_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    quantity_before: str = Field(pattern=NAME)
    quantity_after: str = Field(pattern=NAME)
    price_before: str = Field(pattern=NAME)
    price_after: str = Field(pattern=NAME)
    baseline_period: str = Field(pattern=r'^\d{4}-(?:0[1-9]|1[0-2])$')
    current_period: str = Field(pattern=r'^\d{4}-(?:0[1-9]|1[0-2])$')
    amount_unit: str = Field(min_length=1, max_length=80)
    group_by: list[str] = Field(default_factory=list, max_length=2)
    filters: list[ExploreFilter] = Field(default_factory=list, max_length=10)

    @model_validator(mode='after')
    def validate_fields(self):
        names=[self.quantity_before,self.quantity_after,self.price_before,self.price_after]
        if len(set(names))!=4:raise ValueError('数量及单价的四个字段必须不同')
        if len(set(self.group_by))!=len(self.group_by):raise ValueError('分组字段不能重复')
        if len({f.column for f in self.filters})!=len(self.filters):raise ValueError('同一字段不能重复筛选')
        try:
            before=date.fromisoformat(self.baseline_period+'-01');after=date.fromisoformat(self.current_period+'-01')
        except ValueError:raise ValueError('月份必须是有效日历月份') from None
        if before>=after:raise ValueError('基准月份必须早于当前月份')
        if self.amount_unit!=self.amount_unit.strip() or not self.amount_unit.strip():raise ValueError('金额单位必须明确')
        return self


def evaluate(table, value):
    value=FactorRequest.model_validate(value)
    rows=table['rows']
    if table.get('total')!=len(rows) or not table.get('complete_result_available'):
        raise ValueError('必须读取完整的固定结果，不能使用预览数据')
    if not rows:raise ValueError('没有数据，未将缺失结果当作零')
    schema=TableSchema.model_validate(table.get('context',{}).get('output_semantics') or table['schema'])
    fields={column.name:column for column in schema.columns}
    names=[value.quantity_before,value.quantity_after,value.price_before,value.price_after]
    if any(name not in fields for name in names):raise ValueError('所选因素字段不存在')
    q0,q1,p0,p1=[fields[name] for name in names]
    if any(c.data_type not in ('decimal','integer') or c.value_format!='native' for c in (q0,q1,p0,p1)):
        raise ValueError('数量和单价必须是原生精确数值字段')
    if q0.aggregation!='additive' or q1.aggregation!='additive' or not q0.unit or q0.unit!=q1.unit:
        raise ValueError('数量字段须明确同一单位和可加性')
    if p0.aggregation!='non_additive' or p1.aggregation!='non_additive':
        raise ValueError('单价字段须声明为不可直接相加')
    if p0.unit!=value.amount_unit+'/'+q0.unit or p1.unit!=p0.unit:
        raise ValueError('单价单位须明确为金额单位/数量单位，不能推断或自动换算')
    if (q0.period,p0.period)!=(value.baseline_period,value.baseline_period) or (q1.period,p1.period)!=(value.current_period,value.current_period):
        raise ValueError('四个因素字段的期间须分别与指定的基准和当前月份一致')
    for name in value.group_by:
        if name not in fields or fields[name].data_type not in ('text','date','boolean'):
            raise ValueError('分组仅支持已声明的文本、日期或布尔字段')
    _checked_grain(rows,schema)
    input_rows=len(rows)
    for f in value.filters:
        col=fields.get(f.column)
        if not col or col.data_type not in ('text','date','boolean'):
            raise ValueError('筛选仅支持已声明的文本、日期或布尔字段')
        if f.value is not None and ((col.data_type=='boolean' and type(f.value) is not bool) or (col.data_type!='boolean' and not isinstance(f.value,str))):
            raise ValueError('筛选值与字段类型不一致')
        if not _missing(f.value) and any(_missing(row.get(f.column)) for row in rows):
            raise ValueError('筛选字段存在空值，无法判断是否属于所选范围；请先核对空值')
    rows=[row for row in rows if all(_as_dimension(row.get(f.column),fields[f.column])==f.value for f in value.filters)]
    if not rows:raise ValueError('筛选后没有数据，未将缺失结果当作零')
    groups={}
    with localcontext() as ctx:
        ctx.prec=120
        for row in rows:
            if any(row.get(name) is None for name in names):raise ValueError('因素存在空值，不能把缺失值当作零')
            nums=[decimal_value(row[name]) for name in names]
            if any(c.data_type=='integer' and n!=n.to_integral_value() for c,n in zip((q0,q1,p0,p1),nums)):
                raise ValueError('整数因素字段存在非整数值')
            before_qty,after_qty,before_price,after_price=nums
            key=tuple(_as_dimension(row.get(name),fields[name]) for name in value.group_by)
            if key not in groups:
                if len(groups)>=MAX_GROUPS:raise ValueError('分组超过500项，未截断计算')
                groups[key]={'count':0,'before':Decimal(0),'after':Decimal(0),'quantity_effect':Decimal(0),'price_effect':Decimal(0)}
            group=groups[key];group['count']+=1
            group['before']+=before_qty*before_price
            group['after']+=after_qty*after_price
            group['quantity_effect']+=(after_qty-before_qty)*before_price
            group['price_effect']+=after_qty*(after_price-before_price)
        result=[]
        for key in sorted(groups,key=lambda x:tuple('' if v is None else ('1' if v is True else '0' if v is False else '2'+v) for v in x)):
            item=groups[key]; delta=item['after']-item['before']
            if item['quantity_effect']+item['price_effect']!=delta:raise ValueError('因素分解未平衡，不能交付')
            result.append({'values':list(key),'count':item['count'],**{k:format(item[k],'f') for k in ('before','after','quantity_effect','price_effect')},'delta':format(delta,'f')})
    return {'group_by':value.group_by,'filters':[f.model_dump() for f in value.filters], 'groups':result,'total_rows':len(rows),'input_rows':input_rows,'unit':value.amount_unit,
            'baseline_period':value.baseline_period,'current_period':value.current_period,
            'columns':names,'formula':{'quantity_effect':'(本期数量-基准数量)×基准单价',
            'price_effect':'本期数量×(本期单价-基准单价)','delta':'本期数量×本期单价-基准数量×基准单价'},
            'notice':'逐条计算后汇总；采用数量优先分解，分解顺序改变会改变因素贡献。结果是数学归因，不证明业务因果、原始资料完整或财务审批。'}


def analyze(pid,rid,value):
    from . import workspace, project_data as data
    from .workspace_lifecycle import _project_lock, assert_project_live
    from .jobs import get_job
    value=FactorRequest.model_validate(value)
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
            raise ValueError('当前因素分析上限为20 MiB，请缩小范围；没有截断计算')
        table=read_all_handoff(root,expected_manifest_sha256=meta['handoff_sha256'],max_rows=MAX_ROWS)
        if table['total']!=meta['records']:raise ValueError('结果记录数不一致')
        data.require_safe_numeric_lineage(pid,rid,root_meta=meta,root_page=table)
        result=evaluate(table,value)
        assert_project_live(pid)
        if data.owned(pid,'data_workflow',run['target']).get('published_run')!=rid:raise ValueError('分析期间正式结果已变化')
        return dict(result,run_id=rid,handoff_sha256=meta['handoff_sha256'],plan_sha256=meta['plan_sha256'])
