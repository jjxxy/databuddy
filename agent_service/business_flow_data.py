"""Version-bound file parsing and typed table processing for business flows.

These handlers perform actual reads/calculations. A successful run proves only
that declared technical checks ran; source interpretation and finance approval
remain separate obligations carried in the output.
"""
from __future__ import annotations

import json
from pydantic import Field, model_validator

from .models import StrictModel
from .business_flow_engine import FlowStepError
from .project_data import InputBinding, ReadOptions
from .data_workflows import WorkflowPlan, canonical


class SourceConfig(StrictModel):
    source_id: str | None = Field(default=None,min_length=1,max_length=64)
    revision: int | None = Field(default=None,ge=1,strict=True)
    content_hash: str | None = Field(default=None,pattern=r'^[a-f0-9]{64}$')
    options: ReadOptions = Field(default_factory=ReadOptions)
    column_map: dict[str,str] = Field(default_factory=dict,max_length=200)

    @model_validator(mode='after')
    def complete_pin(self):
        fields=(self.source_id,self.revision,self.content_hash)
        if any(v is not None for v in fields) and any(v is None for v in fields):
            raise ValueError('固定来源必须同时包含资料、版本和内容摘要')
        return self


class SourcePin(StrictModel):
    source_id: str = Field(min_length=1,max_length=64)
    revision: int = Field(ge=1,strict=True)
    content_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


class TableConfig(StrictModel):
    plan: WorkflowPlan


def validate_source(config):SourceConfig.model_validate(config)
def validate_table(config):TableConfig.model_validate(config)


def _authorized_turn(context):
    # Reuse the model/Skill adapter's actual project, actor, task and gateway
    # authorization checks, including revoked/cancelled turn boundaries.
    from .workflow_model_skill import _turn
    return _turn(context)


def source_table(context, config, inputs):
    turn=_authorized_turn(context);cfg=SourceConfig.model_validate(config)
    if cfg.source_id is None:
        if not isinstance(inputs,dict) or set(inputs)!={'source'}:raise ValueError('运行时需要明确固定的资料版本')
        pin=SourcePin.model_validate(inputs['source'])
    else:
        if inputs:raise ValueError('固定资料不能再指定其他来源')
        pin=SourcePin(source_id=cfg.source_id,revision=cfg.revision,content_hash=cfg.content_hash)
    from . import agent_context,agent_gateway,agent_team
    agent_gateway.assert_turn_allowed(turn)
    agent_team.assert_turn_allowed(turn,'read_source_table',{'source_id':pin.source_id})
    if 'read_source_table' not in agent_context.allowed(turn):raise ValueError('当前任务无权读取来源资料')
    from .project_data import owned,read_input
    source=owned(turn['project_id'],'source',pin.source_id)
    if (source.get('kind')!='file' or source.get('status')!='ready' or source.get('archived')
            or source.get('revision')!=pin.revision or source.get('content_hash')!=pin.content_hash):
        raise FlowStepError('SOURCE_VERSION_CHANGED','资料版本已变化或不可用，请重新确认并运行')
    binding=InputBinding(source_id=pin.source_id,options=cfg.options,column_map=cfg.column_map)
    rows,fields,evidence,provenance=read_input(turn['project_id'],binding,frozen_source=source)
    current=owned(turn['project_id'],'source',pin.source_id)
    if (current.get('revision')!=pin.revision or current.get('content_hash')!=pin.content_hash
            or current.get('archived') or current.get('status')!='ready'):
        raise FlowStepError('SOURCE_VERSION_CHANGED','读取期间资料版本已变化，请重新确认并运行')
    # Full file reads preserve duplicate transactions. A snapshot-scoped row
    # coordinate gives the typed engine a unique grain without silently
    # identifying equal-valued transactions as duplicates. It is not a
    # business key and is not stable if a later source snapshot is reordered.
    if any('atlas_row' in row for row in rows) or any(f.get('name')=='atlas_row' for f in fields):
        raise ValueError('来源字段与内部行标识冲突，请调整字段映射')
    rows=[dict(row,atlas_row=str(index)) for index,row in enumerate(rows,1)]
    fields=list(fields)+[{'name':'atlas_row','label':'本次资料行号','numeric':False}]
    # canonical encodes Decimal values exactly as strings and rejects binary
    # floats for numeric execution through the existing typed workflow engine.
    return json.loads(canonical({'kind':'source_table_v1','source_id':pin.source_id,
        'revision':pin.revision,'content_hash':pin.content_hash,'rows':rows,'fields':fields,
        'row_identity':'snapshot_scoped_ordinal_not_business_key',
        'complete':True,'requires_source_review':bool(evidence.get('requires_source_review')),
        'requires_semantic_review':bool(evidence.get('requires_semantic_review')),
        'evidence':evidence,'provenance':provenance}))


def table_workflow(context, config, inputs):
    turn=_authorized_turn(context);cfg=TableConfig.model_validate(config)
    from . import agent_context,agent_gateway,agent_team
    agent_gateway.assert_turn_allowed(turn)
    agent_team.assert_turn_allowed(turn,'run_data_workflow',{})
    if 'run_data_workflow' not in agent_context.allowed(turn):raise ValueError('当前任务无权运行数据加工')
    if not isinstance(inputs,dict) or set(inputs)!=set(cfg.plan.inputs):
        raise ValueError('加工输入与已确认的数据步骤不一致')
    tables={};source_review=False;semantic_review=False
    for alias,value in inputs.items():
        if (not isinstance(value,dict) or value.get('kind') not in ('source_table_v1','table_workflow_v1')
                or value.get('complete') is not True or not isinstance(value.get('rows'),list)):
            raise ValueError('加工输入不是完整数据来源或已完成的数据步骤')
        tables[alias]=value['rows']
        source_review |= value.get('requires_source_review') is True
        semantic_review |= value.get('requires_semantic_review') is True
    from .data_workflows import execute
    result=execute(cfg.plan,tables)
    return {'kind':'table_workflow_v1','complete':result['complete'],'rows':result['output']['rows'],
        'schema':result['output']['schema'],'row_count':result['output']['row_count'],
        'output_sha256':result['output']['sha256'],'plan_sha256':result['plan_sha256'],
        'input_sha256':result['input_sha256'],'stage_receipts':result['stages'],
        'requires_source_review':source_review,
        'requires_semantic_review':semantic_review or result.get('requires_semantic_review') is True,
        'financial_evidence_status':'requires_business_review'}
