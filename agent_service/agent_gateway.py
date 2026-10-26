"""Project-scoped machine access to Atlas's real durable Agent execution.

This is an explicit A2A 1.0 async / 0.3 JSON-RPC polling profile and a stateless
MCP tools profile. Accepted HTTP requests and model prose are not execution proof.
"""
from __future__ import annotations
import hashlib, hmac, json, os, re, secrets, time
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from pydantic import Field, model_validator, ValidationError
from .models import StrictModel
from .config import DATA
from .store import get, put, patch, all_items, seal, now, uid
from .locks import FileLock
from .actor_context import actor_scope, current_actor

MAX_BODY=3*1024*1024
MCP_VERSIONS=('2025-11-25','2025-06-18','2025-03-26')
READ_TOOLS={
 'project_context','load_workflow','business_rules','search_project_history','discover_tools',
 'read_tool_result','list_data_applications','list_data_workflows','discover_skills','load_skill',
 'read_skill_file','recall_memories','read_context_original','read_context_checkpoint','compact_work_context',
 'semantic_catalog_guide','list_semantic_catalogs','inspect_semantic_catalog','inspect_semantic_proposal',
 'list_business_reports','business_report_guide','inspect_business_report_input','data_workflow_guide',
 'read_document_data_evidence','inspect_extracted_data_facts','list_report_sources','read_report_acquisition',
 'inspect_source_tables','read_source_table','inspect_data_workflow','read_data_workflow_result',
 'data_asset_catalog','inspect_data_quality','inspect_column_lineage','inspect_metric','source_schema',
 'source_preview','list_files','read_file','search_files','versions','application_info','view_result',
 'query_sql','ask_data_application','project_health','local_workspace_status',
}
WORK_TOOLS={'set_task_plan','write_file','edit_file','register_data_workflow','run_data_workflow',
            'render_business_report','render_office_artifact','export_data_workflow_result'}

class GatewayError(ValueError):
    def __init__(self,status,code,message):self.status=status;self.code=code;super().__init__(message)

class ClientInput(StrictModel):
    name:str=Field(min_length=1,max_length=80)
    project_ids:list[str]=Field(min_length=1,max_length=20)
    approved_model_ids:list[str]=Field(min_length=1,max_length=20)
    interaction_mode:Literal['chat','work']='chat'
    max_active_tasks:int=Field(default=4,ge=1,le=8)
    max_total_tasks:int=Field(default=1000,ge=1,le=10000)
    max_model_calls_per_task:int=Field(default=12,ge=1,le=40)
    max_total_model_calls:int=Field(default=10000,ge=1,le=1000000)
    max_input_characters:int=Field(default=360000,ge=1000,le=1000000)
    max_output_tokens:int=Field(default=24000,ge=128,le=128000)
    task_timeout_seconds:int=Field(default=300,ge=10,le=3600)
    ttl_seconds:int=Field(default=2592000,ge=60,le=7776000)
    @model_validator(mode='after')
    def distinct(self):
        if len(set(self.project_ids))!=len(self.project_ids) or len(set(self.approved_model_ids))!=len(self.approved_model_ids):raise ValueError('项目与模型授权不能重复')
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',x) for x in self.project_ids+self.approved_model_ids):raise ValueError('授权资源标识无效')
        self.name=self.name.strip()
        if not self.name or any(ord(c)<32 for c in self.name):raise ValueError('调用方名称无效')
        return self

class ClientUpdate(ClientInput):expected_revision:str=Field(min_length=1,max_length=64)
class ClientRevision(StrictModel):expected_revision:str=Field(min_length=1,max_length=64)
class Provenance(StrictModel):
    chain_id:str=Field(default='',max_length=128)
    hops:int=Field(default=0,ge=0,le=2)
    origin_instance:str=Field(default='',max_length=128)
    visited_instance_ids:list[str]=Field(default_factory=list,max_length=3)
class TaskInput(StrictModel):
    project_id:str=Field(min_length=1,max_length=64)
    request_id:str=Field(min_length=8,max_length=100,pattern=r'^[A-Za-z0-9_-]+$')
    goal:str=Field(min_length=1,max_length=12000)
    source_ids:list[str]=Field(default_factory=list,max_length=20)
    model_id:str|None=Field(default=None,min_length=1,max_length=64)
    interaction_mode:Literal['chat','work']|None=None
    input_bundle:dict|None=None
    provenance:Provenance=Field(default_factory=Provenance)
    @model_validator(mode='after')
    def valid(self):
        if not self.goal.strip() or len(set(self.source_ids))!=len(self.source_ids):raise ValueError('任务目标不能为空，来源不能重复')
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',x) for x in self.source_ids):raise ValueError('来源标识无效')
        if len(json.dumps(self.input_bundle,ensure_ascii=False,default=str).encode())>MAX_BODY:raise ValueError('任务输入超过大小限制')
        return self

def _hash(value):return hashlib.sha256(value.encode()).hexdigest()
def _actor(identity):return 'u_'+_hash('atlas-agent-client:'+identity)
def _lock(identity):return FileLock(str(DATA/('gateway-client-'+identity+'.lock')),timeout=10)
def _public(c):return {k:v for k,v in c.items() if k not in ('token_hash','key')}
def _project_live(pid):
    project=get('project',pid)
    if project.get('deleted_at'):raise GatewayError(404,'project_unavailable','项目已删除或不可用')
    return project
def _fresh(identity):
    c=get('agent_client',identity)
    if not c.get('enabled') or c.get('expires_at','')<=now():raise GatewayError(403,'client_inactive','调用方已停用或到期')
    return c

def clients():return [_public(c) for c in all_items('agent_client')]
def _validate_grants(value):
    from .model_catalog import public_catalog
    for pid in value.project_ids:_project_live(pid)
    available={entry['id'] for entry in public_catalog()['models']}
    if not set(value.approved_model_ids)<=available:raise GatewayError(400,'unknown_model','只能授权当前已配置的模型')

def issue_client(value):
    value=ClientInput.model_validate(value);_validate_grants(value)
    identity='agc_'+uid();token='atlas_ag_'+secrets.token_urlsafe(40)
    expires=(datetime.now(timezone.utc)+timedelta(seconds=value.ttl_seconds)).isoformat()
    record=put('agent_client',dict(value.model_dump(exclude={'ttl_seconds'}),token_hash=_hash(token),enabled=True,
        revision=uid(),actor_id=_actor(identity),owner_id=current_actor(),created_at=now(),updated_at=now(),
        expires_at=expires,model_requests=0,input_characters=0,output_tokens_budgeted=0),identity)
    return {'client':_public(record),'token':token,'token_returned_once':True}

def update_client(identity,value):
    value=ClientUpdate.model_validate(value);_validate_grants(value)
    with _lock(identity):
        old=get('agent_client',identity)
        if old['revision']!=value.expected_revision:raise GatewayError(409,'revision_conflict','调用方已变化，请重新读取后修改')
        fields=value.model_dump(exclude={'expected_revision','ttl_seconds'})
        fields.update(revision=uid(),updated_at=now(),expires_at=(datetime.now(timezone.utc)+timedelta(seconds=value.ttl_seconds)).isoformat())
        result=patch('agent_client',identity,**fields)
    _cancel_client_tasks(identity,revision=old['revision'])
    return _public(result)

def rotate_client(identity,expected_revision):
    with _lock(identity):
        old=_fresh(identity)
        if old['revision']!=expected_revision:raise GatewayError(409,'revision_conflict','调用方已变化，请重新读取后轮换')
        token='atlas_ag_'+secrets.token_urlsafe(40)
        result=patch('agent_client',identity,token_hash=_hash(token),revision=uid(),updated_at=now())
    _cancel_client_tasks(identity,revision=old['revision'])
    return {'client':_public(result),'token':token,'token_returned_once':True}

def revoke_client(identity,expected_revision):
    with _lock(identity):
        old=get('agent_client',identity)
        if old['revision']!=expected_revision:raise GatewayError(409,'revision_conflict','调用方已变化，请重新读取后停用')
        result=patch('agent_client',identity,enabled=False,token_hash='',revision=uid(),updated_at=now())
    _cancel_client_tasks(identity)
    return _public(result)

def authenticate(token):
    digest=_hash(token);matched=None
    for c in all_items('agent_client'):
        if hmac.compare_digest(c.get('token_hash') or '0'*64,digest):matched=c
    if not token.startswith('atlas_ag_') or not matched:raise GatewayError(401,'invalid_client','需要有效的 Agent 调用凭据')
    return _fresh(matched['id'])

def assert_turn_allowed(turn):
    identity=turn.get('gateway_client_id')
    if not identity:return
    if turn.get('id'):
        current=get('turn',turn['id'])
        if current.get('gateway_client_id')!=identity or current.get('gateway_cancelled') or current.get('outcome')=='cancelled':
            raise GatewayError(403,'task_cancelled','任务已取消，未继续执行')
    c=_fresh(identity)
    _project_live(turn['project_id'])
    if (turn.get('gateway_client_revision')!=c['revision'] or turn.get('gateway_actor_id')!=c['actor_id']
        or turn.get('actor_id')!=c['actor_id'] or turn.get('project_id') not in c['project_ids']
        or turn.get('model_id') not in c['approved_model_ids']):raise GatewayError(403,'grant_changed','任务授权已变化，未继续执行')
    if time.time()>turn.get('gateway_deadline',0):raise GatewayError(403,'task_expired','Agent 任务时间预算已到，已有成果保留')
    if turn.get('interaction_mode')=='work' and c['interaction_mode']!='work':raise GatewayError(403,'read_only','调用方仅获准只读分析')

def allowed_tools(turn,names):
    if not turn.get('gateway_client_id'):return set(names)
    allowed=READ_TOOLS|(WORK_TOOLS if turn.get('interaction_mode')=='work' else set())
    return set(names)&allowed

def admit_model_request(turn,input_characters,max_tokens):
    if not turn.get('gateway_client_id'):return
    with _lock(turn['gateway_client_id']):
        assert_turn_allowed(turn);c=_fresh(turn['gateway_client_id'])
        if c.get('model_requests',0)>=c['max_total_model_calls']:raise GatewayError(429,'client_budget','调用方模型调用总预算已用完')
        patch('agent_client',c['id'],model_requests=c.get('model_requests',0)+1,
              input_characters=c.get('input_characters',0)+int(input_characters),
              output_tokens_budgeted=c.get('output_tokens_budgeted',0)+int(max_tokens))

def _owned_task(client,identity):
    current=_fresh(client['id']);task=get('gateway_task',identity)
    _project_live(task['project_id'])
    if task.get('client_id')!=current['id'] or task.get('project_id') not in current['project_ids']:
        raise GatewayError(404,'task_not_found','任务不存在')
    if task.get('client_revision')!=current['revision']:raise GatewayError(403,'grant_changed','此任务原授权已失效')
    return current,task

def _task_state(turn):
    if turn.get('gateway_cancelled') or turn.get('outcome')=='cancelled':return 'cancelled'
    if turn.get('state')=='failed':return 'failed'
    if turn.get('state')!='done':return 'working' if turn.get('state') not in ('queued','waiting_probe') else 'submitted'
    outcome=turn.get('outcome')
    if outcome in ('needs_input','paused'):return 'input-required'
    if outcome in ('blocked','partial'):return 'failed'
    if outcome in ('answered','complete','verified_complete'):
        verification=turn.get('verification')
        if verification and not verification.get('verified'):return 'failed'
        return 'completed'
    return 'failed'

def submit_task(client,value,*,protocol='rest'):
    from . import conversation
    value=TaskInput.model_validate(value)
    digest=_hash(json.dumps(value.model_dump(),sort_keys=True,ensure_ascii=False))
    identity='agt_'+_hash(client['id']+'\0'+value.project_id+'\0'+value.request_id)[:48]
    with _lock(client['id']):
        c=_fresh(client['id'])
        if c['revision']!=client['revision']:raise GatewayError(403,'grant_changed','调用方授权已变化')
        if value.project_id not in c['project_ids']:raise GatewayError(403,'project_denied','目标项目未授权')
        _project_live(value.project_id)
        chosen=value.model_id or c['approved_model_ids'][0]
        if chosen not in c['approved_model_ids']:raise GatewayError(403,'model_denied','模型未授权给此调用方')
        mode=value.interaction_mode or c['interaction_mode']
        if mode=='work' and c['interaction_mode']!='work':raise GatewayError(403,'read_only','调用方仅获准只读分析')
        if value.provenance.hops>=2:raise GatewayError(400,'hop_limit','跨 Agent 调用已达到跳数限制')
        if value.provenance.visited_instance_ids:
            from .agent_mesh import instance_id
            if instance_id() in value.provenance.visited_instance_ids:raise GatewayError(400,'agent_loop','调用链已包含本实例，未递归提交')
        try:previous=get('gateway_task',identity)
        except KeyError:previous=None
        if previous:
            if previous.get('client_revision')!=c['revision']:raise GatewayError(403,'grant_changed','原请求授权已变化，请使用新请求编号明确发起')
            if previous['request_sha256']!=digest:raise GatewayError(409,'idempotency_conflict','同一请求编号不能更换目标、输入或模型')
            if previous.get('state')=='cancelled':return task_status(c,identity)
            if previous.get('turn_id'):return task_status(c,identity)
        records=[t for t in all_items('gateway_task') if t.get('client_id')==c['id']]
        if not previous:
            if len(records)>=c['max_total_tasks']:raise GatewayError(429,'task_budget','调用方任务总额度已用完')
        active=0
        for task in records:
            if task['id']==identity:continue
            if not task.get('turn_id'):
                active+=task.get('state')=='preparing'
                continue
            try:active+=get('turn',task['turn_id'])['state'] not in ('done','failed')
            except KeyError:active+=1
        if active>=c['max_active_tasks']:raise GatewayError(429,'active_limit','调用方仍有任务处理中')
        if not previous:
            put('gateway_task',{'client_id':c['id'],'client_revision':c['revision'],'project_id':value.project_id,
                'request_id':value.request_id,'request_sha256':digest,'created_at':now(),'state':'preparing',
                'secret_request':seal(value.model_dump())},identity)
        patch('gateway_task',identity,state='preparing',updated_at=now())
        try:
            with actor_scope(c['actor_id']):
                source_ids=list(value.source_ids);result_ids=[];manifest=None
                if value.input_bundle is not None:
                    from .agent_handoff import import_bundle
                    imported=import_bundle(value.project_id,value.input_bundle,scope_id='gateway:'+c['id'],request_id=identity)
                    source_ids=list(dict.fromkeys(source_ids+imported.get('source_ids',[])))
                    result_ids=imported.get('result_ids',[]);manifest=imported.get('manifest_sha256')
                if len(source_ids)>20:raise GatewayError(400,'source_limit','附件总数超过20份')
                for sid in source_ids:conversation.owned('source',sid,value.project_id)
                session=conversation.create_session(value.project_id,conversation.SessionInput(name='外部 Agent 任务',request_id=identity),internal=True)
                limits={'model_calls':c['max_model_calls_per_task'],'input_characters':c['max_input_characters'],
                        'output_tokens':c['max_output_tokens'],'duration_ms':c['task_timeout_seconds']*1000}
                trusted={'gateway_client_id':c['id'],'gateway_client_revision':c['revision'],'gateway_actor_id':c['actor_id'],
                         'gateway_limits':limits,'gateway_deadline':time.time()+c['task_timeout_seconds'],
                         'gateway_protocol':protocol,'gateway_input_result_ids':result_ids,
                         'gateway_input_manifest_sha256':manifest,'gateway_provenance':value.provenance.model_dump(),
                         'gateway_request_id':identity}
                submitted=conversation.submit(value.project_id,conversation.TurnInput(text=value.goal,source_ids=source_ids,
                    session_id=session['id'],model_id=chosen,interaction_mode=mode,request_id=identity),trusted_fields=trusted)
                patch('gateway_task',identity,turn_id=submitted['id'],state='submitted',model_id=chosen,
                      input_manifest_sha256=manifest,updated_at=now())
        except Exception:
            patch('gateway_task',identity,state='failed',error_code='PREPARATION_FAILED',updated_at=now())
            raise
    return task_status(c,identity)

def task_status(client,identity):
    c,task=_owned_task(client,identity)
    if not task.get('turn_id'):
        return {'id':task['id'],'project_id':task['project_id'],'status':task.get('state') if task.get('state') in ('cancelled','failed') else 'submitted','preparing':task.get('state')=='preparing','accepted':True}
    turn=get('turn',task['turn_id'])
    if turn.get('gateway_client_id')!=c['id'] or turn.get('project_id')!=task['project_id']:raise GatewayError(404,'task_not_found','任务不存在')
    from .company_search import visible_answer
    artifact_error=False
    with actor_scope(c['actor_id']):
        from .agent_handoff import build_output
        try:output=build_output(turn) if turn.get('state') in ('done','failed') else {'artifacts':[],'output_bundle':None,'integrity_verified':False,'financial_verified':False}
        except (ValueError,KeyError,OSError):
            artifact_error=True
            output={'artifacts':[],'output_bundle':None,'integrity_verified':False,'financial_verified':False,
                    'artifact_error':'本任务成果未通过完整性回读，不作为已核验交付；已有记录保留'}
        answer=visible_answer(turn)
    return {'id':task['id'],'project_id':task['project_id'],'context_id':turn.get('session_id'),'turn_id':turn['id'],
            'status':'failed' if artifact_error else _task_state(turn),'accepted':True,'outcome':turn.get('outcome'),'answer':answer,
            'progress':turn.get('progress',''),'created_at':task['created_at'],'updated_at':turn.get('finished_at') or task.get('updated_at'),
            'model_id':turn.get('model_id'),'verification':turn.get('verification'),'usage':turn.get('model_usage',{}),
            'input_manifest_sha256':task.get('input_manifest_sha256'),**output,
            'scope':'this client task within its explicitly authorized project; narrative is not numerical evidence'}

def _cancel_turn(turn):
    from .job_authority import client_guard
    with client_guard(turn['gateway_client_id']):
        return _cancel_turn_locked(get('turn',turn['id']))

def _cancel_turn_locked(turn):
    from .jobs import request_cancel,list_jobs
    # Jobs are bound at admission; this also covers cancellation before the
    # agent has written data_workflow_job onto its turn. Never cancel borrowed work.
    for job in list_jobs(limit=100000):
        authority=job.get('gateway_authority') or {}
        if authority.get('turn_id')==turn['id'] and authority.get('client_id')==turn['gateway_client_id']:
            if job['status'] in ('queued','running','retrying'):request_cancel(job['id'])
    if turn.get('run_id'):
        try:request_cancel(turn['run_id'])
        except KeyError:pass
    if _task_state(turn) in ('submitted','working','input-required','auth-required'):
        patch('turn',turn['id'],state='done',outcome='cancelled',gateway_cancelled=True,answer='外部调用任务已取消；已有成果保留，已完成的外部操作不会回滚。',
              resume_available=False,secret_text=None,finished_at=now(),progress='')

def _cancel_client_tasks(identity,*,revision=None):
    for task in all_items('gateway_task'):
        if task.get('client_id')==identity and task.get('turn_id') and (revision is None or task.get('client_revision')==revision):
            try:_cancel_turn(get('turn',task['turn_id']))
            except KeyError:pass

def cancel_task(client,identity,*,strict=False):
    c,task=_owned_task(client,identity)
    if task.get('turn_id'):
        turn=get('turn',task['turn_id'])
        if strict and _task_state(turn) in ('completed','failed'):raise GatewayError(409,'task_not_cancelable','此任务已经结束，不能取消')
        _cancel_turn(turn)
    else:patch('gateway_task',task['id'],state='cancelled',updated_at=now())
    return task_status(c,identity)

def read_task_artifact(client,identity,artifact_id,offset=0,limit=65536):
    c,task=_owned_task(client,identity)
    if not task.get('turn_id'):raise GatewayError(409,'not_ready','任务尚未产生可读成果')
    turn=get('turn',task['turn_id'])
    if turn.get('gateway_client_id')!=c['id']:raise GatewayError(404,'task_not_found','任务不存在')
    from .agent_handoff import read_artifact
    try:
        with actor_scope(c['actor_id']):return read_artifact(turn,artifact_id,offset=offset,limit=limit)
    except (ValueError,OSError):raise GatewayError(409,'artifact_unavailable','产物页参数无效或文件未通过完整性校验') from None

def _base_url():
    value=os.getenv('ATLAS_AGENT_PUBLIC_URL') or 'http://127.0.0.1:'+os.getenv('PORT','8000')
    p=urlsplit(value)
    if p.scheme not in ('https','http') or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ('','/'):
        raise GatewayError(503,'public_url','管理员需配置有效的 Agent 对外站点地址')
    if p.scheme=='http' and p.hostname not in ('127.0.0.1','localhost','::1'):
        raise GatewayError(503,'public_tls','对外 Agent 站点需使用 HTTPS')
    return value.rstrip('/')

def agent_card(version='1.0'):
    legacy={'protocolVersion':'0.3.0','name':'Atlas DataBuddy','description':'Project-scoped financial data work with durable task status and verified artifacts. Async polling profile only.',
        'url':_base_url()+'/a2a','preferredTransport':'JSONRPC','version':'0.20.0',
        'capabilities':{'streaming':False,'pushNotifications':False,'stateTransitionHistory':False},
        'defaultInputModes':['text/plain','application/json'],'defaultOutputModes':['text/plain','application/json'],
        'securitySchemes':{'atlasBearer':{'type':'http','scheme':'bearer'}},'security':[{'atlasBearer':[]}],
        'skills':[{'id':'financial-data-work','name':'财经数据处理与核对','description':'明确项目授权内的分析、受控加工和可核对成果；使用 Atlas 数据交接包传递完整输入。',
                   'tags':['finance','data','verified-results'],'inputModes':['text/plain','application/json'],'outputModes':['text/plain','application/json']}],
        'supportsAuthenticatedExtendedCard':False}
    if version in ('0.3','0.3.0'):return legacy
    card={key:value for key,value in legacy.items() if key not in ('protocolVersion','url','preferredTransport','security','supportsAuthenticatedExtendedCard')}
    card['supportedInterfaces']=[{'url':_base_url()+'/a2a','protocolBinding':'JSONRPC','protocolVersion':'1.0'},
                                 {'url':_base_url()+'/a2a','protocolBinding':'JSONRPC','protocolVersion':'0.3'}]
    card['capabilities']={'streaming':False,'pushNotifications':False,'extendedAgentCard':False}
    card['securitySchemes']={'atlasBearer':{'httpAuthSecurityScheme':{'scheme':'Bearer'}}}
    card['securityRequirements']=[{'schemes':{'atlasBearer':{'list':[]}}}]
    card['description']+=' A2A 1.0 SendMessage requires configuration.returnImmediately=true; no streaming, push or task continuation.'
    return card

def _a2a_task(value,version='0.3'):
    state=value['status']
    text=value.get('answer') or value.get('progress') or '任务已接收，尚未完成'
    message={'kind':'message','role':'agent','messageId':'status_'+value['id'],
             'parts':[{'kind':'text','text':text}], 'taskId':value['id'],'contextId':value.get('context_id') or value['id']}
    data={k:v for k,v in value.items() if k not in ('answer','progress')}
    task={'kind':'task','id':value['id'],'contextId':value.get('context_id') or value['id'],
            'status':{'state':state,'message':message,'timestamp':now()},
            'artifacts':[{'artifactId':'atlas-task-receipt','name':'Atlas task receipt','parts':[{'kind':'data','data':{'atlas':data}}]}],
            'metadata':{'atlasProtocolProfile':'0.3.0-jsonrpc-polling','businessOutcome':value.get('outcome')}}
    if version=='1.0':
        task.pop('kind');message.pop('kind');message['role']='ROLE_AGENT'
        message['parts']=[{'text':text}]
        task['status']['state']={'submitted':'TASK_STATE_SUBMITTED','working':'TASK_STATE_WORKING','completed':'TASK_STATE_COMPLETED',
            'failed':'TASK_STATE_FAILED','cancelled':'TASK_STATE_CANCELED','input-required':'TASK_STATE_INPUT_REQUIRED','auth-required':'TASK_STATE_AUTH_REQUIRED'}[state]
        task['artifacts'][0]['parts']=[{'data':{'atlas':data}}]
        task['metadata']['atlasProtocolProfile']='1.0-jsonrpc-async-polling'
    return task

def _a2a_send(params,version='0.3'):
    if not isinstance(params,dict) or set(params)-{'message','configuration','metadata'}:raise GatewayError(400,'invalid_params','A2A 参数无效')
    configuration=params.get('configuration',{})
    if not isinstance(configuration,dict):raise GatewayError(400,'invalid_params','A2A 配置无效')
    if configuration.get('pushNotificationConfig') or configuration.get('taskPushNotificationConfig'):raise GatewayError(400,'unsupported','此部署未启用推送通知')
    if version=='1.0' and configuration.get('returnImmediately') is not True:
        raise GatewayError(400,'unsupported_blocking','此 A2A 1.0 配置仅支持 returnImmediately=true 的异步轮询')
    if version!='1.0' and configuration.get('blocking'):raise GatewayError(400,'unsupported_blocking','此 A2A 配置仅支持异步轮询')
    message=params.get('message')
    if not isinstance(message,dict) or message.get('role')!=('ROLE_USER' if version=='1.0' else 'user') or not isinstance(message.get('messageId'),str):raise GatewayError(400,'invalid_message','需要有效的 A2A 用户消息')
    if version=='1.0' and 'kind' in message:raise GatewayError(400,'invalid_message','A2A 1.0 消息不使用旧版 kind 字段')
    if message.get('taskId') or message.get('contextId'):raise GatewayError(400,'unsupported_continuation','本配置通过独立请求提交任务，不接受隐式任务续写')
    parts=message.get('parts');texts=[];data=None
    if not isinstance(parts,list) or not 1<=len(parts)<=20:raise GatewayError(400,'invalid_parts','A2A 消息片段无效')
    for part in parts:
        if not isinstance(part,dict):raise GatewayError(400,'invalid_parts','A2A 消息片段无效')
        if version=='1.0':
            if 'kind' in part or len(set(part)&{'text','data','raw','url'})!=1:raise GatewayError(400,'invalid_parts','A2A 1.0 片段结构无效')
            kind='text' if 'text' in part else 'data' if 'data' in part else None
        else:kind=part.get('kind')
        if kind=='text' and isinstance(part.get('text'),str):texts.append(part['text'])
        elif kind=='data' and isinstance(part.get('data'),dict) and set(part['data'])=={'atlas'} and data is None:data=part['data']['atlas']
        else:raise GatewayError(400,'unsupported_part','仅接收文本和明确的 Atlas 交接数据，不下载外部文件 URL')
    if not isinstance(data,dict):raise GatewayError(400,'project_required','A2A 数据片段需明确 Atlas 项目及任务参数')
    data=dict(data);data.setdefault('request_id',message['messageId']);data.setdefault('goal','\n'.join(texts))
    return TaskInput.model_validate(data)

TOOL_SCHEMAS=[
 {'name':'atlas_submit_task','description':'Submit a project-scoped durable analysis or controlled work task; returns accepted state, not completion.','inputSchema':TaskInput.model_json_schema(),'annotations':{'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True}},
 {'name':'atlas_get_task','description':'Read task status and verified artifact metadata for this machine client.','inputSchema':{'type':'object','properties':{'task_id':{'type':'string'}},'required':['task_id'],'additionalProperties':False},'annotations':{'readOnlyHint':True}},
 {'name':'atlas_cancel_task','description':'Cancel this client task and request cancellation of its active data run.','inputSchema':{'type':'object','properties':{'task_id':{'type':'string'}},'required':['task_id'],'additionalProperties':False},'annotations':{'readOnlyHint':False,'idempotentHint':True}},
 {'name':'atlas_read_artifact','description':'Read a hash-bound binary artifact page; does not fetch arbitrary URLs.','inputSchema':{'type':'object','properties':{'task_id':{'type':'string'},'artifact_id':{'type':'string'},'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':65536}},'required':['task_id','artifact_id'],'additionalProperties':False},'annotations':{'readOnlyHint':True}},
]

async def _body(request):
    try:
        length=int(request.headers.get('content-length','0'))
    except ValueError:raise GatewayError(400,'content_length','请求长度无效') from None
    if length<0:raise GatewayError(400,'content_length','请求长度无效')
    if length>MAX_BODY:raise GatewayError(413,'body_limit','Agent 请求超过大小限制')
    chunks=bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks)>MAX_BODY:raise GatewayError(413,'body_limit','Agent 请求超过大小限制')
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise ValueError('duplicate JSON key')
            value[key]=item
        return value
    def constant(value):raise ValueError('invalid JSON constant')
    try:return json.loads(chunks,object_pairs_hook=unique,parse_constant=constant)
    except (ValueError,UnicodeError):raise GatewayError(400,'invalid_json','请求不是有效 JSON') from None

def _check_origin(request):
    origin=request.headers.get('origin')
    if not origin:return
    allowed={v.strip().rstrip('/') for v in os.getenv('ATLAS_AGENT_ALLOWED_ORIGINS','').split(',') if v.strip()}
    public=os.getenv('ATLAS_AGENT_PUBLIC_URL')
    if public:allowed.add(public.rstrip('/'))
    p=urlsplit(origin)
    local=p.scheme in ('http','https') and p.hostname in ('localhost','127.0.0.1','::1') and p.netloc==request.headers.get('host') and p.path in ('','/') and not p.query and not p.fragment
    if origin.rstrip('/') not in allowed and not local:raise GatewayError(403,'origin_denied','此请求来源未获批准')

def install(app,auth,operator_auth):
    @app.exception_handler(GatewayError)
    async def error(request,exc):return JSONResponse(status_code=exc.status,content={'detail':str(exc),'code':exc.code})
    async def machine(request:Request):
        _check_origin(request)
        header=request.headers.get('authorization','')
        if not header.startswith('Bearer ') or len(header)>512:raise GatewayError(401,'invalid_client','需要有效的 Agent 调用凭据')
        client=authenticate(header[7:])
        request.state.identity={'authenticated':True,'mode':'agent','subject':client['id']}
        from .workspace_lifecycle import request_scope
        with request_scope():yield client
    @app.get('/api/operator/agent-clients',dependencies=[Depends(operator_auth)])
    def list_clients():return {'clients':clients()}
    @app.post('/api/operator/agent-clients',dependencies=[Depends(operator_auth)])
    def create_client(value:ClientInput):return issue_client(value)
    @app.put('/api/operator/agent-clients/{identity}',dependencies=[Depends(operator_auth)])
    def change_client(identity:str,value:ClientUpdate):return update_client(identity,value)
    @app.post('/api/operator/agent-clients/{identity}/rotate',dependencies=[Depends(operator_auth)])
    def rotate(identity:str,value:ClientRevision):return rotate_client(identity,value.expected_revision)
    @app.delete('/api/operator/agent-clients/{identity}',dependencies=[Depends(operator_auth)])
    def revoke(identity:str,expected_revision:str):return revoke_client(identity,expected_revision)
    @app.post('/api/agent/v1/tasks')
    async def submit(request:Request,client=Depends(machine)):
        try:value=TaskInput.model_validate(await _body(request))
        except ValidationError:raise GatewayError(400,'invalid_task','任务参数无效') from None
        try:result=await run_in_threadpool(submit_task,client,value)
        except GatewayError:raise
        except (ValueError,KeyError):raise GatewayError(400,'invalid_task','项目、资料或交接未通过校验') from None
        return JSONResponse(status_code=202,content=result)
    @app.get('/api/agent/v1/tasks/{identity}')
    def status(identity:str,client=Depends(machine)):return task_status(client,identity)
    @app.post('/api/agent/v1/tasks/{identity}/cancel')
    def cancel(identity:str,client=Depends(machine)):return cancel_task(client,identity)
    @app.get('/api/agent/v1/tasks/{identity}/artifacts/{artifact_id}')
    def artifact(identity:str,artifact_id:str,offset:int=0,limit:int=65536,client=Depends(machine)):
        return read_task_artifact(client,identity,artifact_id,offset,limit)
    @app.get('/.well-known/agent-card.json')
    def card(request:Request,client=Depends(machine)):
        version=request.headers.get('A2A-Version','1.0')
        if version not in ('1.0','0.3','0.3.0'):raise GatewayError(400,'protocol_version','A2A 协议版本不受支持')
        return agent_card(version)

    @app.post('/a2a')
    async def a2a(request:Request,client=Depends(machine)):
        version=request.headers.get('A2A-Version') or '0.3'
        if version not in ('1.0','0.3','0.3.0'):raise GatewayError(400,'protocol_version','此端点支持明确声明的 A2A 1.0 或 0.3 轮询配置')
        body=await _body(request);identity=body.get('id') if isinstance(body,dict) else None
        if not isinstance(body,dict) or body.get('jsonrpc')!='2.0' or not isinstance(identity,(str,int)) or isinstance(identity,bool):
            return JSONResponse(status_code=400,content={'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Invalid Request'}})
        try:
            method=body.get('method');params=body.get('params',{})
            send_method='SendMessage' if version=='1.0' else 'message/send'
            get_method='GetTask' if version=='1.0' else 'tasks/get'
            cancel_method='CancelTask' if version=='1.0' else 'tasks/cancel'
            if method==send_method:value=await run_in_threadpool(submit_task,client,_a2a_send(params,version),protocol='a2a-'+version)
            elif method in (get_method,cancel_method):
                if not isinstance(params,dict) or set(params)-{'id','historyLength','metadata'} or not isinstance(params.get('id'),str):raise GatewayError(400,'invalid_params','任务参数无效')
                value=await run_in_threadpool(task_status,client,params['id']) if method==get_method else await run_in_threadpool(cancel_task,client,params['id'],strict=True)
            else:return {'jsonrpc':'2.0','id':identity,'error':{'code':-32601,'message':'Method not found; polling profile only'}}
            task=_a2a_task(value,version)
            return {'jsonrpc':'2.0','id':identity,'result':{'task':task} if version=='1.0' and method==send_method else task}
        except (GatewayError,KeyError,ValueError) as exc:
            code=-32001 if isinstance(exc,KeyError) else -32602 if isinstance(exc,ValidationError) else -32000
            return {'jsonrpc':'2.0','id':identity,'error':{'code':code,'message':str(exc) if isinstance(exc,GatewayError) else 'Task or parameters unavailable'}}

    @app.post('/mcp/atlas')
    async def mcp(request:Request,client=Depends(machine)):
        header=request.headers.get('MCP-Protocol-Version')
        if header and header not in MCP_VERSIONS:raise GatewayError(400,'protocol_version','MCP 协议版本不受支持')
        body=await _body(request)
        if not isinstance(body,dict) or body.get('jsonrpc')!='2.0':raise GatewayError(400,'invalid_rpc','需要单个 JSON-RPC 2.0 请求')
        identity=body.get('id');method=body.get('method');params=body.get('params',{})
        if not isinstance(params,dict):raise GatewayError(400,'invalid_params','MCP 参数须为对象')
        if identity is None:
            if method=='notifications/initialized':return Response(status_code=202)
            raise GatewayError(400,'rpc_id','需要请求编号')
        if not isinstance(identity,(str,int)) or isinstance(identity,bool):raise GatewayError(400,'rpc_id','请求编号无效')
        try:
            if method=='initialize':
                if not isinstance(params,dict):raise GatewayError(400,'invalid_params','初始化参数无效')
                requested=params.get('protocolVersion')
                version=requested if requested in MCP_VERSIONS else MCP_VERSIONS[0]
                result={'protocolVersion':version,'capabilities':{'tools':{'listChanged':False}},'serverInfo':{'name':'atlas-agent-gateway','version':'0.20.0'}}
            elif method=='ping':result={}
            elif method=='tools/list':
                if params.get('cursor'):raise GatewayError(400,'cursor','此目录无下一页')
                result={'tools':TOOL_SCHEMAS}
            elif method=='tools/call':
                if not isinstance(params,dict) or set(params)-{'name','arguments','_meta'}:raise GatewayError(400,'invalid_params','工具参数无效')
                name=params.get('name');args=params.get('arguments',{})
                schema=next((tool['inputSchema'] for tool in TOOL_SCHEMAS if tool['name']==name),None)
                if schema is None:return {'jsonrpc':'2.0','id':identity,'error':{'code':-32602,'message':'Unknown tool'}}
                import jsonschema
                try:jsonschema.validate(args,schema)
                except jsonschema.ValidationError:raise GatewayError(400,'invalid_params','工具参数未通过结构校验') from None
                if name=='atlas_submit_task':value=await run_in_threadpool(submit_task,client,TaskInput.model_validate(args),protocol='mcp')
                elif name=='atlas_get_task':value=await run_in_threadpool(task_status,client,args['task_id'])
                elif name=='atlas_cancel_task':value=await run_in_threadpool(cancel_task,client,args['task_id'])
                else:value=await run_in_threadpool(read_task_artifact,client,args['task_id'],args['artifact_id'],args.get('offset',0),args.get('limit',65536))
                summary={k:value[k] for k in ('id','status','accepted','artifact_id','offset','next_offset') if k in value}
                result={'content':[{'type':'text','text':json.dumps(summary or {'receipt_in_structured_content':True},ensure_ascii=False)}], 'structuredContent':value,'isError':False}
            else:return {'jsonrpc':'2.0','id':identity,'error':{'code':-32601,'message':'Method not found'}}
            return {'jsonrpc':'2.0','id':identity,'result':result}
        except (GatewayError,KeyError,ValueError) as exc:
            message=str(exc) if isinstance(exc,GatewayError) else 'Task or parameters unavailable'
            if method=='tools/call':return {'jsonrpc':'2.0','id':identity,'result':{'content':[{'type':'text','text':message}],'isError':True}}
            return {'jsonrpc':'2.0','id':identity,'error':{'code':-32602,'message':message}}
