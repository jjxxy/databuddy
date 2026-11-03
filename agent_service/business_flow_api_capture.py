"""Authorized API collection into a run-bound, encrypted immutable snapshot."""
import hashlib
import json
from pydantic import Field
from .models import StrictModel
from .store import get, put, seal, unseal, now
from .business_flow_engine import FlowStepError


class ApiCaptureConfig(StrictModel):
    source_id: str = Field(min_length=1,max_length=64)
    revision: int = Field(ge=1,strict=True)
    config_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


def validate(config):ApiCaptureConfig.model_validate(config)


def _bytes(value):
    try:return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    except (ValueError,TypeError,UnicodeError,RecursionError):raise ValueError('采集快照不是有效的精确数据') from None


def check_source(pid,cfg):
    from .project_data import owned
    source=owned(pid,'source',cfg.source_id)
    sealed=source.get('secret_config')
    if (source.get('kind')!='api' or source.get('status')!='ready' or source.get('archived')
            or source.get('revision')!=cfg.revision or not isinstance(sealed,str)
            or hashlib.sha256(sealed.encode()).hexdigest()!=cfg.config_sha256):
        raise FlowStepError('SOURCE_VERSION_CHANGED','API 连接版本已变化或不可用，请重新确认')
    return source


def _scope(context):
    keys=('project_id','actor_id','turn_id','run_id','node_id')
    if any(not isinstance(context.get(k),str) or not context[k] for k in keys):raise ValueError('采集缺少运行身份')
    return {k:context[k] for k in keys}


def _snapshot_digest(payload):
    keys=('scope','source_id','revision','config_sha256','content_hash','rows','fields','evidence','collected_at')
    if any(key not in payload for key in keys):raise ValueError('采集快照证据不完整')
    return hashlib.sha256(_bytes({key:payload[key] for key in keys})).hexdigest()


def verify(context,ref):
    """Read a frozen capture under current project, actor and source grants.

    This is an immutable historical snapshot, not a new API request. A changed
    connection revision or temporary probe failure must not rewrite its past;
    source deletion/archival and revoked task permissions still deny access.
    New capture and resumed execution remain subject to check_source's exact
    connection pin in run().
    """
    from .workflow_model_skill import _turn
    from .project_data import owned
    from . import agent_context, agent_gateway, agent_team
    turn=_turn(context)
    scope=_scope({**context,'node_id':ref.get('capture_node_id')});ident=ref.get('snapshot_id')
    if not isinstance(ident,str):raise ValueError('采集快照标识无效')
    record=get('flow_api_snapshot',ident)
    payload=unseal(record['secret_payload'])
    if (record.get('status')!='complete' or record.get('project_id')!=scope['project_id'] or record.get('actor_id')!=scope['actor_id']
            or payload.get('scope')!=scope or payload.get('source_id')!=ref.get('source_id')
            or payload.get('revision')!=ref.get('revision') or payload.get('content_hash')!=ref.get('content_hash')
            or payload.get('snapshot_sha256')!=ref.get('snapshot_sha256')
            or ('config_sha256' in ref and payload.get('config_sha256')!=ref['config_sha256'])):
        raise ValueError('采集快照不属于当前运行')
    # Historical access is distinct from permission to execute the live API.
    # Preserve source revocation, project ownership and the actual calling
    # agent's grants; changing credentials or connection version alone does
    # not revoke access to a previously captured, authenticated record.
    try:source=owned(scope['project_id'],'source',payload['source_id'])
    except KeyError:raise ValueError('API 来源已不可用，不能读取历史采集') from None
    if source.get('kind')!='api' or source.get('archived') or source.get('deleted_at'):
        raise ValueError('API 来源已停用，不能读取历史采集')
    agent_gateway.assert_turn_allowed(turn)
    agent_team.assert_turn_allowed(turn,'read_source_table',{'source_id':payload['source_id']})
    if 'read_source_table' not in agent_context.allowed(turn):
        raise ValueError('当前任务无权查看采集数据')
    actual=hashlib.sha256(_bytes({'rows':payload['rows'],'fields':payload['fields']})).hexdigest()
    if actual!=payload['content_hash']:raise ValueError('采集快照内容摘要不一致')
    if _snapshot_digest(payload)!=payload.get('snapshot_sha256'):raise ValueError('采集快照及来源证据摘要不一致')
    return payload


def run(context,config,inputs):
    if inputs:raise ValueError('API 采集节点不接受未声明输入')
    from .business_flow_data import _authorized_turn,_compact_table
    from . import agent_context,agent_gateway,agent_team
    from .connectors import read_source
    from .config import DATA
    from .locks import FileLock
    cfg=ApiCaptureConfig.model_validate(config);turn=_authorized_turn(context);scope=_scope(context)
    agent_gateway.assert_turn_allowed(turn)
    agent_team.assert_turn_allowed(turn,'read_source_table',{'source_id':cfg.source_id})
    if 'read_source_table' not in agent_context.allowed(turn):raise ValueError('当前任务无权读取来源资料')
    source=check_source(scope['project_id'],cfg)
    ident='fas_'+hashlib.sha256(_bytes(scope)).hexdigest()[:40]
    with FileLock(str(DATA/(ident+'.lock')),timeout=30):
        try:record=get('flow_api_snapshot',ident)
        except KeyError:record=None
        if record is not None:
            if record.get('status')!='complete':
                raise FlowStepError('CAPTURE_OUTCOME_UNKNOWN','上次采集未留下完整快照；请发起新运行，不会自动重新采集')
            payload=unseal(record['secret_payload'])
            ref={'snapshot_id':ident,'capture_node_id':scope['node_id'],'source_id':cfg.source_id,'revision':cfg.revision,'content_hash':payload.get('content_hash'),'snapshot_sha256':payload.get('snapshot_sha256')}
            payload=verify(context,ref)
            if payload.get('config_sha256')!=cfg.config_sha256:raise ValueError('采集配置与快照不一致')
        else:
            # Persist a fence before the first external request. If the process
            # dies before committing the snapshot, replay must not silently
            # read a potentially different API response under the same run.
            put('flow_api_snapshot',{'project_id':scope['project_id'],'actor_id':scope['actor_id'],
                'status':'collecting','secret_payload':seal({'scope':scope})},ident)
            rows,fields,evidence,truncated=read_source(source)
            if truncated or evidence.get('complete') is not True:raise ValueError('API 采集未确认完整，不能加工')
            evidence.update(collection_scope_verified=False,source_consistency_verified=False,
                            notice='分页结束和来源总数只表示接口的声明，不能证明业务范围或跨页一致性；仍需核对。')
            if any(any(isinstance(v,float) for v in row.values()) for row in rows):raise ValueError('API 包含不精确数值，不能作为财务输入')
            check_source(scope['project_id'],cfg)
            rows=[{**{k:v for k,v in row.items() if k not in ('record_hash','_source_index')},'nexus_row':str(i)} for i,row in enumerate(rows,1)]
            fields=list(fields)+[{'name':'nexus_row','label':'本次资料行号'}]
            content_hash=hashlib.sha256(_bytes({'rows':rows,'fields':fields})).hexdigest()
            payload={'scope':scope,'source_id':cfg.source_id,'revision':cfg.revision,'config_sha256':cfg.config_sha256,
                     'content_hash':content_hash,'rows':rows,'fields':fields,'evidence':evidence,'collected_at':now()}
            payload['snapshot_sha256']=_snapshot_digest(payload)
            if len(_bytes(payload))>2*1024*1024:raise FlowStepError('API_SNAPSHOT_CAPACITY','API 采集数据超过当前单次快照的 2 MiB 安全容量，未生成或发布截断结果；请调整资料范围或使用经核对的分批方案。')
            put('flow_api_snapshot',{'project_id':scope['project_id'],'actor_id':scope['actor_id'],'status':'complete','secret_payload':seal(payload)},ident)
    return _compact_table(context,{'kind':'api_capture_v1','snapshot_id':ident,'capture_node_id':scope['node_id'],'source_id':cfg.source_id,'revision':cfg.revision,'config_sha256':cfg.config_sha256,
        'content_hash':payload['content_hash'],'snapshot_sha256':payload['snapshot_sha256'],'rows':payload['rows'],'fields':payload['fields'],'complete':True,
        'requires_source_review':True,'requires_semantic_review':True,'evidence':payload['evidence'],
        'provenance':{'collected_at':payload['collected_at'],'connection_revision':cfg.revision}})
