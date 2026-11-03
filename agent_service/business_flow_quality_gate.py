"""Explicit, deterministic checks over an already authenticated table."""
from decimal import Decimal, InvalidOperation, localcontext
from typing import Annotated, Literal
from pydantic import Field, model_validator
from .models import StrictModel
from .business_flow_engine import FlowStepError

NAME=r'^[A-Za-z][A-Za-z0-9_]{0,63}$'

class Required(StrictModel):
    kind:Literal['required']
    column:str=Field(pattern=NAME)

class Unique(StrictModel):
    kind:Literal['unique']
    columns:list[str]=Field(min_length=1,max_length=20,description='组合键；空值视为一个具体值，重复空键会失败。')
    @model_validator(mode='after')
    def valid_columns(self):
        import re
        if len(set(self.columns))!=len(self.columns) or any(not re.fullmatch(NAME,x) for x in self.columns):raise ValueError('唯一键字段无效')
        return self

class Range(StrictModel):
    kind:Literal['range']
    column:str=Field(pattern=NAME)
    minimum:str|None=Field(default=None,description='含边界的十进制文本下限；空值不参与范围检查，若必填需另设 required 规则。')
    maximum:str|None=Field(default=None,description='含边界的十进制文本上限；空值不参与范围检查。')
    @model_validator(mode='after')
    def bounds(self):
        import re
        def parse(x):
            if not isinstance(x,str) or len(x)>80 or not re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?',x):raise ValueError('范围边界必须是明确的十进制文本')
            return Decimal(x)
        if self.minimum is None and self.maximum is None:raise ValueError('范围至少需要一个边界')
        lower=parse(self.minimum) if self.minimum is not None else None
        upper=parse(self.maximum) if self.maximum is not None else None
        if lower is not None and upper is not None and lower>upper:raise ValueError('范围下限不能大于上限')
        return self

class Balance(StrictModel):
    """Explicit, same-unit grouped equality; no inferred accounting policy."""
    kind:Literal['balance']
    left_column:str=Field(pattern=NAME)
    right_column:str=Field(pattern=NAME)
    group_by:list[str]=Field(min_length=1,max_length=20)

    @model_validator(mode='after')
    def distinct_fields(self):
        import re
        if (self.left_column==self.right_column or len(set(self.group_by))!=len(self.group_by)
                or any(not re.fullmatch(NAME,x) for x in self.group_by)
                or self.left_column in self.group_by or self.right_column in self.group_by):
            raise ValueError('平衡检查需要不同的金额字段和明确分组字段')
        return self


Rule=Annotated[Required|Unique|Range|Balance,Field(discriminator='kind')]
class Config(StrictModel):
    rules:list[Rule]=Field(min_length=1,max_length=50)

def validate(config):Config.model_validate(config)

def run(context,config,inputs):
    cfg=Config.model_validate(config)
    if set(inputs)!={'table'}:raise ValueError('检查节点必须明确指定数据表')
    value=inputs['table']
    if not isinstance(value,dict) or value.get('kind')!='table_workflow_v1' or value.get('complete') is not True:raise ValueError('检查节点需要完整加工结果')
    from .business_flow_data import materialize_table
    from .data_workflows import TableSchema,digest,decimal_value
    schema=value.get('schema');parsed=TableSchema.model_validate(schema);table=materialize_table(context,value);rows=table.get('rows')
    if not isinstance(rows,list) or type(table.get('row_count')) is not int or table['row_count']!=len(rows) or table.get('output_sha256')!=digest({'schema':schema,'rows':rows}):
        raise ValueError('检查节点的数据快照不完整')
    fields={x.name:x for x in parsed.columns}
    for rule in cfg.rules:
        columns=rule.columns if isinstance(rule,Unique) else [*rule.group_by,rule.left_column,rule.right_column] if isinstance(rule,Balance) else [rule.column]
        if not set(columns)<=set(fields):raise ValueError('检查规则引用的字段不存在')
        if isinstance(rule,Balance):
            left=fields[rule.left_column];right=fields[rule.right_column]
            if (left.data_type not in ('decimal','integer') or right.data_type not in ('decimal','integer')
                    or left.unit!=right.unit or left.period!=right.period or left.value_format!=right.value_format
                    or left.aggregation!='additive' or right.aggregation!='additive'):
                raise ValueError('平衡检查需要单位、期间和格式一致的可加数值字段')
            if not rows:raise FlowStepError('CHECK_BALANCE_FAILED','没有记录可供平衡检查')
            groups={}
            for row in rows:
                if not isinstance(row,dict):raise ValueError('检查节点的数据行无效')
                keys=[]
                for column in rule.group_by:
                    v=row.get(column)
                    if v is None or isinstance(v,str) and not v.strip():
                        raise FlowStepError('CHECK_BALANCE_FAILED','平衡检查的分组字段缺失：'+column)
                    if fields[column].data_type in ('decimal','integer'):
                        try:
                            v=decimal_value(v)
                            if fields[column].data_type=='integer' and v!=v.to_integral_value():raise ValueError()
                        except ValueError:raise FlowStepError('CHECK_BALANCE_FAILED','平衡检查的分组数值无效：'+column) from None
                    keys.append(v)
                try:key=tuple(keys);hash(key)
                except TypeError:raise ValueError('平衡检查的分组字段不能包含复杂值') from None
                try:
                    l=decimal_value(row.get(rule.left_column));r=decimal_value(row.get(rule.right_column))
                    if (left.data_type=='integer' and l!=l.to_integral_value()) or (right.data_type=='integer' and r!=r.to_integral_value()):raise ValueError()
                except (ValueError,TypeError):raise FlowStepError('CHECK_BALANCE_FAILED','平衡检查的金额缺失或无效，请核对') from None
                with localcontext() as ctx:
                    ctx.prec=100
                    totals=groups.setdefault(key,[Decimal(0),Decimal(0)])
                    totals[0]+=l;totals[1]+=r
            if any(left!=right for left,right in groups.values()):
                raise FlowStepError('CHECK_BALANCE_FAILED','分组平衡检查未通过，请核对两侧金额和业务口径')
            continue
        if isinstance(rule,Range) and fields[rule.column].data_type not in ('decimal','integer'):raise ValueError('范围检查仅支持明确声明的数值字段')
        seen=set()
        for row in rows:
            if not isinstance(row,dict):raise ValueError('检查节点的数据行无效')
            if isinstance(rule,Required):
                v=row.get(rule.column)
                if v is None or isinstance(v,str) and not v.strip():raise FlowStepError('CHECK_REQUIRED_FAILED','必填检查未通过，请核对字段 '+rule.column)
            elif isinstance(rule,Unique):
                def canonical(column):
                    value=row.get(column)
                    if value is None:return None
                    if fields[column].data_type in ('decimal','integer'):
                        # The same numeric key may be serialized with different
                        # trailing zeroes. Compare values, not their spelling.
                        try:number=decimal_value(value)
                        except ValueError:raise FlowStepError('CHECK_UNIQUE_FAILED','唯一键字段数值无效，请核对字段 '+column) from None
                        if fields[column].data_type=='integer' and number!=number.to_integral_value():
                            raise FlowStepError('CHECK_UNIQUE_FAILED','唯一键字段数值无效，请核对字段 '+column)
                        return number
                    return value
                key=tuple(canonical(c) for c in columns)
                try:
                    if key in seen:raise FlowStepError('CHECK_UNIQUE_FAILED','唯一键检查未通过，请核对字段 '+', '.join(columns))
                    seen.add(key)
                except TypeError:raise ValueError('唯一键字段不能包含复杂值') from None
            else:
                value_at=row.get(rule.column)
                if value_at is None:continue  # nulls require a separate explicit required rule
                try:
                    if isinstance(value_at,bool) or isinstance(value_at,float):raise ValueError()
                    number=Decimal(str(value_at))
                    if not number.is_finite():raise ValueError()
                except (InvalidOperation,ValueError):raise FlowStepError('CHECK_RANGE_FAILED','数值范围检查无法读取字段 '+rule.column) from None
                if (rule.minimum is not None and number<Decimal(rule.minimum)) or (rule.maximum is not None and number>Decimal(rule.maximum)):
                    raise FlowStepError('CHECK_RANGE_FAILED','数值范围检查未通过，请核对字段 '+rule.column)
    return value
