"""Strict, checkpointed execution of a versioned business-flow document.

This module never imports tools by name from a user document. The host supplies
the permitted handlers, authorized execution context and encrypted checkpoint
storage. JSON values are bounded; large files/tables should travel as immutable
versioned references, not be copied into a model prompt or a checkpoint.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal

from pydantic import Field, model_validator, model_serializer

from .models import StrictModel

_NAME=r'^[A-Za-z][A-Za-z0-9_]{0,63}$'
_MAX_INPUT=2*1024*1024
_MAX_OUTPUT=2*1024*1024
_MAX_CHECKPOINT=16*1024*1024


class FlowStepError(ValueError):
    """A server-authored error with safe, user-facing text and no side effect."""
    def __init__(self,code,message):
        super().__init__(message)
        self.code=code


def _json_bytes(value, limit):
    try:
        raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    except (TypeError,ValueError,RecursionError,UnicodeError):
        raise ValueError('流程内容必须是有效的 JSON 数据') from None
    if len(raw)>limit:raise ValueError('流程数据超过可安全保存的大小，请使用固定版本的文件引用')
    return raw


def _copy(value, limit):
    return json.loads(_json_bytes(value,limit))


def digest(value, limit=_MAX_CHECKPOINT):
    return hashlib.sha256(_json_bytes(value,limit)).hexdigest()


class Reference(StrictModel):
    source: Literal['input','node']
    name: str = Field(pattern=_NAME)


class Node(StrictModel):
    id: str = Field(pattern=_NAME)
    kind: str = Field(pattern=_NAME)
    title: str = Field(min_length=1,max_length=200)
    depends_on: list[str] = Field(default_factory=list,max_length=50)
    inputs: dict[str,Reference] = Field(default_factory=dict,max_length=50)
    config: dict = Field(default_factory=dict)

    @model_validator(mode='after')
    def input_names(self):
        import re
        if any(not re.fullmatch(_NAME,key) for key in self.inputs):raise ValueError('节点输入名称无效')
        _json_bytes(self.config,_MAX_INPUT)
        return self


class InputSpec(StrictModel):
    """Declared user input; decimal values are text to preserve exactness."""
    type: Literal['text','integer','decimal','date','boolean','choice']
    label: str = Field(min_length=1,max_length=100)
    options: list[str] = Field(default_factory=list,max_length=50)

    @model_validator(mode='after')
    def valid_options(self):
        if self.type=='choice':
            if not self.options or len(set(self.options))!=len(self.options) or any(not x.strip() or len(x)>200 for x in self.options):
                raise ValueError('选项参数需要明确且不重复的选项')
        elif self.options:raise ValueError('只有选项参数可以配置选项')
        return self

    def validate_value(self,value):
        import re
        from datetime import date
        if self.type=='boolean':valid=type(value) is bool
        elif self.type=='integer':valid=type(value) is int and -(2**53-1)<=value<=2**53-1
        elif self.type=='decimal':valid=isinstance(value,str) and bool(re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?',value)) and len(value)<=80
        elif self.type=='choice':valid=isinstance(value,str) and value in self.options
        elif self.type=='date':
            valid=isinstance(value,str) and bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}',value))
            if valid:
                try:valid=date.fromisoformat(value).isoformat()==value
                except ValueError:valid=False
        else:valid=isinstance(value,str) and bool(value.strip()) and len(value)<=4000
        if not valid:raise ValueError('流程参数“'+self.label+'”的值不符合已确认的类型或选项')


class BusinessFlow(StrictModel):
    version: Literal[1] = 1
    name: str = Field(min_length=1,max_length=200)
    inputs: list[str] = Field(default_factory=list,max_length=50)
    input_specs: dict[str,InputSpec] | None = None
    nodes: list[Node] = Field(min_length=1,max_length=100)
    outputs: list[str] = Field(min_length=1,max_length=50)

    @model_serializer(mode='wrap')
    def legacy_compatible(self,handler):
        value=handler(self)
        # Existing Git versions were hashed without this optional field.
        if self.input_specs is None:value.pop('input_specs',None)
        return value

    @model_validator(mode='after')
    def graph(self):
        import re
        if any(not re.fullmatch(_NAME,name) for name in self.inputs) or len(set(self.inputs))!=len(self.inputs):
            raise ValueError('流程输入名称无效或重复')
        if self.input_specs is not None and not set(self.input_specs)<=set(self.inputs):
            raise ValueError('流程参数声明必须引用已声明的输入')
        seen=set();kinds={};used_parameters=set()
        for node in self.nodes:
            if node.id in seen or node.id in self.inputs:raise ValueError('流程节点名称重复')
            if len(set(node.depends_on))!=len(node.depends_on) or not set(node.depends_on)<=seen:
                raise ValueError('节点依赖必须指向已声明的前序步骤')
            if node.kind=='source_table' and node.inputs and (set(node.inputs)!={'source'} or node.inputs['source'].source!='input'):
                raise ValueError('数据来源只能使用明确声明的运行输入')
            if node.kind=='source_table':
                pinned=all(node.config.get(key) is not None for key in ('source_id','revision','content_hash'))
                if pinned==bool(node.inputs):raise ValueError('数据来源须选择固定版本或运行输入其中一种')
            if node.kind=='table_report_spec':
                if (not {'table'}<=set(node.inputs) or set(node.inputs)-{'table','commentary'} or node.inputs['table'].source!='node'
                        or kinds.get(node.inputs['table'].name) not in ('table_workflow','quality_gate','human_review')):
                    raise ValueError('报告数据必须来自已执行的数据加工步骤')
                if 'commentary' in node.inputs:
                    ref=node.inputs['commentary']
                    if ref.source!='node' or kinds.get(ref.name) not in ('model','skill'):
                        raise ValueError('报告说明只能来自已执行的模型或技能步骤')
            for key,reference in node.inputs.items():
                if reference.source=='input' and reference.name not in self.inputs:raise ValueError('节点引用了不存在的流程输入')
                if reference.source=='node' and reference.name not in node.depends_on:raise ValueError('节点输入必须引用声明过的前序依赖')
                table_parameter=(node.kind=='table_workflow' and reference.source=='input'
                    and reference.name in (self.input_specs or {}) and key in (node.config.get('parameter_bindings') or {}))
                if node.kind=='table_workflow' and not table_parameter and (reference.source!='node' or kinds.get(reference.name) not in ('source_table','table_workflow','quality_gate','human_review')):
                    raise ValueError('数据加工只能使用已执行的数据来源或加工结果，不能将模型内容作为已核验财务数据')
            if node.kind in ('quality_gate','human_review') and (set(node.inputs)!={'table'} or node.inputs['table'].source!='node' or kinds.get(node.inputs['table'].name) not in ('table_workflow','quality_gate','human_review')):
                raise ValueError('检查节点必须引用已执行的数据加工结果')
            for key,reference in node.inputs.items():
                table_parameter=(node.kind=='table_workflow' and key in (node.config.get('parameter_bindings') or {}))
                if reference.source=='input' and reference.name in (self.input_specs or {}) and not ((node.kind in ('model','skill') and key=='content') or table_parameter):
                    raise ValueError('流程参数只能用于指定的模型内容或已声明的数据筛选条件')
                if reference.source=='input' and reference.name in (self.input_specs or {}):used_parameters.add(reference.name)
            if node.kind=='table_workflow':
                bindings=node.config.get('parameter_bindings') or {}
                if not isinstance(bindings,dict) or set(bindings)!={key for key,ref in node.inputs.items() if ref.source=='input'}:
                    raise ValueError('数据筛选参数必须逐项绑定已声明的运行输入')
                plan=node.config.get('plan') or {}
                stages=plan.get('stages') if isinstance(plan,dict) else None
                schemas=plan.get('inputs') if isinstance(plan,dict) else None
                for key,binding in bindings.items():
                    if not isinstance(binding,dict) or set(binding)!={'stage_id','condition_index'} or type(binding['condition_index']) is not int:
                        raise ValueError('数据筛选参数绑定无效')
                    stage=next((x for x in stages or [] if isinstance(x,dict) and x.get('id')==binding['stage_id']),None)
                    if not stage or stage.get('kind')!='filter' or not isinstance(schemas,dict) or stage.get('input') not in schemas:
                        raise ValueError('数据筛选参数只能绑定原始输入表的筛选条件')
                    conditions=stage.get('conditions') or [];index=binding['condition_index']
                    if not isinstance(conditions,list) or index<0 or index>=len(conditions) or not isinstance(conditions[index],dict):
                        raise ValueError('数据筛选条件不存在')
                    condition=conditions[index]
                    if condition.get('operator') in ('in','not_in','is_null','not_null'):
                        raise ValueError('此筛选运算不支持运行参数')
                    schema=schemas[stage['input']];columns=schema.get('columns') if isinstance(schema,dict) else None
                    column=next((c for c in columns or [] if isinstance(c,dict) and c.get('name')==condition.get('column')),None)
                    dtype=column.get('data_type') if column else None
                    allowed={'text':{'text','choice'},'decimal':{'decimal'},'integer':{'integer'},'boolean':{'boolean'},'date':{'date'}}
                    spec=(self.input_specs or {}).get(node.inputs[key].name)
                    if spec is None or spec.type not in allowed.get(dtype,set()):
                        raise ValueError('数据筛选参数类型与输入字段语义不一致')
            seen.add(node.id)
            kinds[node.id]=node.kind
        if used_parameters!=set(self.input_specs or {}):raise ValueError('流程声明了未使用的参数')
        if len(set(self.outputs))!=len(self.outputs) or not set(self.outputs)<=seen:
            raise ValueError('流程输出必须引用已声明节点')
        return self


@dataclass(frozen=True)
class Handler:
    """Server-owned capabilities, never properties asserted by the model."""
    run: Callable
    deterministic: bool
    replay_safe: bool
    validate_config: Callable | None = None
    version: str = '1'

    def __post_init__(self):
        if not callable(self.run) or type(self.deterministic) is not bool or type(self.replay_safe) is not bool:
            raise ValueError('流程处理器配置无效')
        if self.validate_config is not None and not callable(self.validate_config):raise ValueError('流程处理器校验器无效')
        if not isinstance(self.version,str) or not self.version or len(self.version)>100:raise ValueError('流程处理器版本无效')


def _now():return datetime.now(timezone.utc).isoformat()


def _prepare(document,inputs,handlers):
    flow=BusinessFlow.model_validate(document)
    config=flow.model_dump(mode='json')
    frozen=_copy(inputs,_MAX_INPUT)
    if not isinstance(frozen,dict) or set(frozen)!=set(flow.inputs):raise ValueError('流程输入与已确认的定义不一致')
    for name,spec in (flow.input_specs or {}).items():spec.validate_value(frozen[name])
    if not isinstance(handlers,dict):raise ValueError('流程处理器不可用')
    for node in flow.nodes:
        handler=handlers.get(node.kind)
        if not isinstance(handler,Handler):raise ValueError('流程包含当前环境未提供的步骤：'+node.kind)
        if handler.validate_config is not None:handler.validate_config(_copy(node.config,_MAX_INPUT))
    return flow,config,frozen


def run(document, frozen_inputs, handlers, context, *, checkpoint=None, persist=None):
    """Execute or resume a frozen flow; persist each state before side effects.

    The caller must check project, actor and permissions before invoking this
    function, and persist checkpoint contents encrypted. A non-replay-safe node
    left running across a crash is reported as unknown, never rerun blindly.
    """
    if not callable(persist):raise ValueError('缺少流程检查点持久化服务')
    flow,configuration,inputs=_prepare(document,frozen_inputs,handlers)
    plan_hash=digest(configuration);input_hash=digest(inputs,_MAX_INPUT)
    if checkpoint is None:
        state={'version':1,'flow_sha256':plan_hash,'inputs_sha256':input_hash,'status':'running','steps':[]}
        persist(_copy(state,_MAX_CHECKPOINT))
    else:
        state=_copy(checkpoint,_MAX_CHECKPOINT)
        if (not isinstance(state,dict) or state.get('version')!=1 or state.get('flow_sha256')!=plan_hash
                or state.get('inputs_sha256')!=input_hash or not isinstance(state.get('steps'),list)):
            raise ValueError('恢复记录与冻结的流程或输入不一致')
        if len(state['steps'])>len(flow.nodes):raise ValueError('流程检查点步骤无效')
        if state.get('status')=='succeeded':
            # Check every receipt below before accepting a saved completion.
            if len(state['steps'])!=len(flow.nodes):raise ValueError('已完成的流程缺少步骤记录')
            if any(not isinstance(step,dict) or step.get('status')!='succeeded' for step in state['steps']):
                raise ValueError('已完成的流程存在未完成步骤')
        elif state.get('status') in ('failed','unknown','awaiting_review'):
            return state
        elif state.get('status')!='running':raise ValueError('流程检查点状态无效')
    values={}
    for index,node in enumerate(flow.nodes):
        handler=handlers[node.kind]
        arguments={key:(inputs[ref.name] if ref.source=='input' else values[ref.name]) for key,ref in node.inputs.items()}
        argument_hash=digest({'config':node.config,'inputs':arguments},_MAX_INPUT)
        receipt=state['steps'][index] if index<len(state['steps']) else None
        if receipt is not None:
            if (not isinstance(receipt,dict) or receipt.get('node_id')!=node.id or receipt.get('kind')!=node.kind
                    or receipt.get('input_sha256')!=argument_hash or receipt.get('deterministic') is not handler.deterministic
                    or receipt.get('replay_safe') is not handler.replay_safe or receipt.get('handler_version')!=handler.version):
                raise ValueError('流程步骤记录与冻结版本不一致')
            if receipt.get('status')=='succeeded':
                output=receipt.get('output')
                if digest(output,_MAX_OUTPUT)!=receipt.get('output_sha256'):raise ValueError('流程步骤结果摘要不一致')
                values[node.id]=output
                continue
            if node.kind=='human_review' and receipt.get('status')=='awaiting_review' and receipt.get('approval',{}).get('approved') is True:
                output=_copy(arguments['table'],_MAX_OUTPUT)
                receipt.update(status='succeeded',finished_at=_now(),output_sha256=digest(output,_MAX_OUTPUT),output=output)
                values[node.id]=output;persist(_copy(state,_MAX_CHECKPOINT));continue
            if receipt.get('status')=='running' and not handler.replay_safe:
                if index!=len(state['steps'])-1:raise ValueError('未完成的步骤之后存在异常记录')
                receipt.update(status='unknown',finished_at=_now(),error_code='EXECUTION_OUTCOME_UNKNOWN')
                state['status']='unknown';persist(_copy(state,_MAX_CHECKPOINT));return state
            if receipt.get('status')!='running' or not handler.replay_safe:
                raise ValueError('流程步骤状态无法安全恢复')
            if index!=len(state['steps'])-1:raise ValueError('未完成的步骤之后存在异常记录')
        else:
            if index!=len(state['steps']):raise ValueError('流程步骤记录缺失')
            receipt={'node_id':node.id,'kind':node.kind,'title':node.title,'status':'running',
                     'input_sha256':argument_hash,'deterministic':handler.deterministic,
                     'replay_safe':handler.replay_safe,'handler_version':handler.version,'started_at':_now()}
            state['steps'].append(receipt)
            persist(_copy(state,_MAX_CHECKPOINT))
        if node.kind=='human_review':
            receipt['status']='awaiting_review';state['status']='awaiting_review'
            persist(_copy(state,_MAX_CHECKPOINT));return state
        try:
            output=_copy(handler.run(context,_copy(node.config,_MAX_INPUT),_copy(arguments,_MAX_INPUT)),_MAX_OUTPUT)
            output_hash=digest(output,_MAX_OUTPUT)
        except Exception as exc:
            # A handler may already have committed an external operation. Do
            # not expose raw exception data or assume that operation failed.
            if isinstance(exc,FlowStepError) and handler.replay_safe:
                receipt.update(status='failed',finished_at=_now(),error_code=exc.code,error_message=str(exc))
            elif handler.replay_safe:
                from .data_workflows import WorkflowError
                messages={'GRAIN_DUPLICATE':'数据的业务粒度不唯一，请检查关联键或明细行号',
                          'SCHEMA_MISMATCH':'数据字段与已确认的加工方案不一致',
                          'LOOKUP_CARDINALITY':'关联来源的键不唯一或为空，不能安全关联',
                          'UNMAPPED_VALUE':'存在未匹配的数据，请检查关联或分类规则及有效日期',
                          'INVALID_EFFECTIVE_INTERVAL':'规则的业务键或生效区间无效，请核对起止日期和关联键',
                          'OVERLAPPING_EFFECTIVE_INTERVALS':'同一业务键的规则有效期重叠，无法确定应采用的版本',
                          'INVALID_BUSINESS_DATE':'业务日期缺失，无法选择对应的有效规则',
                          'MISSING_LINEAGE':'缺少规则版本标识或生效起止日期，无法追溯实际匹配依据',
                          'TYPE_MISMATCH':'字段类型或日期格式不符合已确认的处理方案，请核对原始资料',
                          'NULL_VALUE':'必要字段有缺失，无法完成计算',
                          'UNIT_MISMATCH':'计算字段的单位不一致',
                          'FX_CURRENCY_MISMATCH':'金额或汇率币种与确认的换算方向不一致，未计算',
                          'FX_RATE_TYPE_MISMATCH':'匹配到的汇率类型与确认口径不一致，未计算',
                          'FX_RATE_INVALID':'汇率无效或不为正数，未计算',
                          'DECIMAL_PRECISION':'换算结果超出精度或需要明确舍入规则，未计算',
                          'ROW_BUDGET':'数据量超过本次执行范围，未生成截断结果',
                          'BYTE_BUDGET':'数据大小超过本次执行范围，未生成截断结果'}
                code=exc.code if isinstance(exc,WorkflowError) and exc.code in messages else 'STEP_FAILED'
                receipt.update(status='failed',finished_at=_now(),error_code=code,
                               error_message=messages.get(code,'该步骤未完成，请检查来源和流程配置'))
            else:
                receipt.update(status='unknown',finished_at=_now(),error_code='EXECUTION_OUTCOME_UNKNOWN')
            state['status']=receipt['status'];persist(_copy(state,_MAX_CHECKPOINT));return state
        receipt.update(status='succeeded',finished_at=_now(),output_sha256=output_hash,output=output)
        values[node.id]=output
        # If this durable save fails, a later run will see the persisted running
        # receipt and will not replay a non-idempotent operation automatically.
        persist(_copy(state,_MAX_CHECKPOINT))
    if len(state['steps'])!=len(flow.nodes):raise ValueError('流程检查点步骤不完整')
    outputs={name:values[name] for name in flow.outputs}
    if state['status']=='succeeded' and state.get('outputs')!=outputs:raise ValueError('流程最终结果与步骤回执不一致')
    state['status']='succeeded';state['outputs']=outputs
    persist(_copy(state,_MAX_CHECKPOINT));return state
