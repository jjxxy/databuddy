from typing import Literal
from pydantic import BaseModel,Field,model_validator,ConfigDict

class StrictModel(BaseModel):
    model_config=ConfigDict(extra='forbid')

class SourceInput(StrictModel):
    name:str=Field(min_length=1,max_length=120)
    kind:Literal['api','database']
    config:dict
    goal:str=Field(default='',max_length=2000)

class JoinSpec(StrictModel):
    source_id:str
    left_field:str
    right_field:str
    label:str=Field(default='关联来源',min_length=1,max_length=60)
    required_match:bool=False

class FilterSpec(StrictModel):
    field:str
    operator:Literal['eq','neq','contains','gt','gte','lt','lte','in','not_in','is_null','not_null']='eq'
    data_type:Literal['text','number','date']='text'
    value:str|int|float|list[str|int|float]|None=None
    @model_validator(mode='after')
    def validate_value(self):
        if self.operator in ('is_null','not_null'):return self
        if self.value is None:raise ValueError('筛选需要填写条件值')
        if self.operator in ('in','not_in'):
            if not isinstance(self.value,list) or not 1<=len(self.value)<=50:raise ValueError('集合筛选需要1～50个值')
        elif isinstance(self.value,list):raise ValueError('该筛选只能使用一个值')
        if len(str(self.value))>2000:raise ValueError('筛选值过长')
        from .rules import filter_sql
        filter_sql(self.model_dump())
        return self

class ComputeSpec(StrictModel):
    name:str=Field(pattern=r'^m_[a-z][a-z0-9_]{0,31}$')
    label:str=Field(min_length=1,max_length=100)
    operation:Literal['add','subtract','multiply','divide']='subtract'
    left_field:str
    right_field:str

class TransformSpec(StrictModel):
    object_label:str=Field(default='业务记录',min_length=1,max_length=50)
    primary_key:str|None=None
    group_by:str|None=None
    amount_field:str|None=None
    amount_label:str=Field(default='金额合计（待确认口径）',max_length=100)
    filters:list[FilterSpec]=Field(default_factory=list,max_length=20)
    computations:list[ComputeSpec]=Field(default_factory=list,max_length=10)
    allow_empty_source:bool=False
    joins:list[JoinSpec]=Field(default_factory=list,max_length=4)
    required_fields:list[str]=Field(default_factory=list,max_length=40)
    nonnegative_fields:list[str]=Field(default_factory=list,max_length=40)
    min_rows:int=Field(default=1,ge=0,le=10000000)
    trim_fields:list[str]=Field(default_factory=list,max_length=40)
    field_labels:dict[str,str]=Field(default_factory=dict)
    metric_status:Literal['draft','confirmed']='draft'
    metric_definition:str=Field(default='',max_length=2000)
    currency:str=Field(default='',max_length=20)
    @model_validator(mode='after')
    def check(self):
        if bool(self.group_by)!=bool(self.amount_field):raise ValueError('分组字段与数值字段必须同时填写')
        if self.metric_status=='confirmed' and self.amount_field and (not self.metric_definition.strip() or not self.currency.strip()):raise ValueError('确认指标必须填写业务口径和单位/币种')
        if self.allow_empty_source and self.min_rows!=0:raise ValueError('允许空来源时，最少结果行数必须设为0')
        if len(self.field_labels)>80 or any(len(v)>100 for v in self.field_labels.values()):raise ValueError('字段说明过多或过长')
        return self

class RuntimePolicy(StrictModel):
    timeout_seconds:int=Field(default=240,ge=10,le=3600)
    max_attempts:int=Field(default=3,ge=1,le=5)
    freshness_minutes:int=Field(default=1440,ge=1,le=43200)

class ApplicationSpec(StrictModel):
    title:str=Field(default='',max_length=120)
    description:str=Field(default='',max_length=1500)
    layout:Literal['table','summary']='summary'
    columns:list[str]=Field(default_factory=list,max_length=30)
    chart:Literal['none','bar']='none'
    page_size:int=Field(default=25,ge=10,le=100)
    default_sort:str='object_id'
    descending:bool=False


class PipelineInput(StrictModel):
    knowledge_evidence_ids:list[str]=Field(default_factory=list,max_length=10)
    application:ApplicationSpec|None=None
    source_id:str
    name:str=Field(min_length=1,max_length=120)
    spec:TransformSpec=Field(default_factory=TransformSpec)
    release_mode:Literal['manual','auto']='manual'
    runtime:RuntimePolicy=Field(default_factory=RuntimePolicy)

class ScheduleInput(StrictModel):
    cron:str|None=None

class NoteInput(StrictModel):
    note:str=Field(max_length=4000)
    tags:list[str]=Field(default_factory=list,max_length=20)

class QueryInput(StrictModel):
    sql:str=Field(min_length=1,max_length=6000)

class AskInput(StrictModel):
    question:str=Field(min_length=1,max_length=2000)

class ViewInput(StrictModel):
    name:str=Field(min_length=1,max_length=120)
    pipeline_id:str
    sql:str=Field(min_length=1,max_length=6000)
    chart:Literal['table','bar','line']='table'

class SourceUpdate(StrictModel):
    name:str|None=Field(default=None,min_length=1,max_length=120)
    config:dict|None=None
    archived:bool|None=None

class PublishInput(StrictModel):
    run_id:str

class ReleaseInput(StrictModel):
    mode:Literal['manual','auto']
