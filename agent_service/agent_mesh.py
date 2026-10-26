"""Durable, actor-scoped Agent delegation with no blind external replay.

Transport success is not financial approval. Only explicit, complete handoff
bytes can earn an integrity receipt; all peer prose remains untrusted.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
import base64,hashlib,json,re,threading,time
from typing import Literal

from filelock import Timeout
from pydantic import Field,SecretStr,model_validator

from .actor_context import current_actor,actor_scope
from .config import DATA
from .locks import FileLock
from .models import StrictModel
from .store import get,put,patch,all_items,seal,unseal,uid,now,transaction,items
from . import agent_mesh_transport as transport

TERMINAL={'succeeded','failed','cancelled','unknown','rejected','input_required','auth_required'}
ACTIVE={'queued','submitting','submitted','working','cancel_requested'}
_pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix='atlas-agent-peer')
_futures={};_pool_lock=threading.Lock()


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def instance_id():
    with FileLock(str(DATA/'agent-mesh-instance.lock'),timeout=5):
        try:return get('system','agent-mesh-instance')['instance_id']
        except KeyError:return put('system',{'instance_id':'atlas_'+uid(),'created_at':now()},'agent-mesh-instance')['instance_id']


class PeerInput(StrictModel):
    name:str=Field(min_length=1,max_length=80)
    transport:Literal['a2a','mcp']='a2a'
    protocol_version:Literal['1.0','0.3.0']='1.0'
    url:str=Field(min_length=8,max_length=1000)
    credential:SecretStr=Field(default=SecretStr(''))
    remote_project_id:str=Field(min_length=1,max_length=64)
    remote_model_id:str=Field(default='',max_length=64)
    data_sharing_approved:bool=False
    submit_approved:bool=False
    model_approved:bool=False
    approved_model_ids:list[str]=Field(default_factory=list,max_length=32)
    private_data_approved:bool=False
    submit_tool:str=Field(default='atlas_submit_task',min_length=1,max_length=120)
    get_tool:str=Field(default='atlas_get_task',min_length=1,max_length=120)
    cancel_tool:str=Field(default='atlas_cancel_task',min_length=1,max_length=120)
    @model_validator(mode='after')
    def bounded(self):
        self.name=self.name.strip()
        if not self.name or any(ord(v)<32 for v in self.name):raise ValueError('Agent名称无效')
        if len(self.credential.get_secret_value())>8000:raise ValueError('Agent凭据过长')
        if len(set(self.approved_model_ids))!=len(self.approved_model_ids):raise ValueError('模型批准列表重复')
        if self.transport=='mcp' and len({self.submit_tool,self.get_tool,self.cancel_tool})!=3:raise ValueError('提交、查询和取消须为不同MCP工具')
        return self


class Assignment(StrictModel):
    peer_id:str=Field(min_length=1,max_length=64)
    goal:str=Field(min_length=1,max_length=6000)
    source_ids:list[str]=Field(default_factory=list,max_length=20)
    result_ids:list[str]=Field(default_factory=list,max_length=20)
    expected_delivery:Literal['advisory','artifacts']='advisory'
    @model_validator(mode='after')
    def unique(self):
        if any(not isinstance(v,str) or not 1<=len(v)<=64 for v in [*self.source_ids,*self.result_ids]):raise ValueError('协作输入标识无效')
        if len(set(self.source_ids))!=len(self.source_ids) or len(set(self.result_ids))!=len(self.result_ids):raise ValueError('协作输入重复')
        return self


class Delegation(StrictModel):assignments:list[Assignment]=Field(min_length=1,max_length=4)
class TaskTarget(StrictModel):task_id:str=Field(min_length=1,max_length=64)
class ArtifactTarget(TaskTarget):
    artifact_id:str=Field(min_length=1,max_length=100)
    offset:int=Field(default=0,ge=0)
    limit:int=Field(default=65536,ge=1,le=65536)
class Empty(StrictModel):pass


def _owned(kind,identity,pid=None):
    value=get(kind,identity)
    if pid and value.get('project_id')!=pid:raise ValueError('协作资源不属于当前项目')
    if value.get('actor_id',value.get('owner_id')) not in (None,current_actor()):raise ValueError('协作资源不属于当前用户')
    return value


def public_peer(value):
    fields=('id','project_id','name','transport','protocol_version','revision','enabled','origin','remote_project_id','remote_model_id',
        'data_sharing_approved','submit_approved','model_approved','approved_model_ids','private_data_approved','protocol_profile','updated_at',
        'url','submit_tool','get_tool','cancel_tool')
    return {k:value[k] for k in fields if k in value}|{'can_edit':value.get('origin')=='personal','configured':True}


def public_task(value):
    fields=('id','project_id','actor_id','parent_turn_id','peer_id','peer_name','peer_revision','transport','goal','state','created_at','updated_at',
        'deadline_at','remote_task_id','child_turn_id','cancel_requested','error','status_note','evidence','input_manifest_sha256',
        'execution_status','verification_status','expected_delivery','received_result_ids','received_artifact_ids','output_artifacts')
    result={k:value[k] for k in fields if k in value}
    summary=value.get('result_summary','');local_result=unseal(value['secret_result']) if value.get('secret_result') else {}
    if not summary:summary=local_result.get('answer','')
    result['result_summary']=summary[:6000]
    result['untrusted_peer_content']=True
    result['financial_verified']=False
    for field in ('evidence_count','result_sha256','input_read_coverage'):
        if field in value:result[field]=value[field]
    coverage=value.get('input_read_coverage') or local_result.get('input_read_coverage')
    if coverage:result['input_read_coverage']={'all_assigned_inputs_read':bool(coverage.get('all_assigned_inputs_read')),
        'missing_sources':len(coverage.get('missing_source_ids',[])),'missing_results':len(coverage.get('missing_result_ids',[]))}
    result.setdefault('execution_status',value.get('state'))
    result.setdefault('verification_status','advisory_unverified' if value.get('state')=='succeeded' else 'not_completed')
    return result


def _peer_config(value,old=None):
    credential=value.credential.get_secret_value()
    if not credential and old and old['url']==value.url:credential=unseal(old['secret_config']).get('credential','')
    from .config import TOKEN
    from .provider import settings
    import os,secrets
    raw=credential.removeprefix('Bearer ')
    protected=[TOKEN,os.getenv('ATLAS_OPERATOR_TOKEN',''),settings().get('api_key','')]
    from .model_catalog import owner_configuration
    try:protected.extend(entry.get('api_key','') for entry in owner_configuration().get('models',[]))
    except KeyError:pass
    if any(secret and secrets.compare_digest(raw,str(secret)) for secret in protected):raise ValueError('Agent连接须使用远端专用凭据，不能转发本工作台或模型密钥')
    return {k:v for k,v in value.model_dump(exclude={'credential'}).items()}|{'credential':credential,'auto_protocol':'protocol_version' not in value.model_fields_set}


def probe_peer(value):
    config=_peer_config(value)
    result=transport.probe(config)
    return {k:v for k,v in result.items() if k!='catalog'}|{'configured':False}


def _policy(peer):return {k:peer.get(k) for k in ('model_approved','model_fingerprint','approved_model_ids','approved_models')}


def _apply_policy(pid,peer):
    with FileLock(str(DATA/('agent-policy-'+pid+'.lock')),timeout=5):
        p=get('project',pid);policies=dict(p.get('mesh_model_policies',{}));policies[peer['id']]=_policy(peer)
        patch('project',pid,mesh_model_policies=policies)


def configure_peer(pid,value,identity=None,expected_revision=None,*,operator=False):
    from .company_search import model_fingerprint,capture_model_approvals
    get('project',pid)
    if not value.data_sharing_approved or not value.submit_approved or not value.model_approved:
        raise ValueError('需明确批准发送指定资料、提交远端任务及所选模型处理返回内容')
    if value.private_data_approved and not operator:raise ValueError('内部资料发送目的地须由管理员在后台批准')
    old=_owned('collab_peer',identity,pid) if identity else None
    if old and old.get('origin')=='preset' and not operator:raise ValueError('管理员预置Agent由管理员维护，可查看但不能改写')
    if old and old['revision']!=expected_revision:raise ValueError('Agent连接已变化，请重新核对后修改')
    config=_peer_config(value,old)
    discovered=transport.probe(config)
    config['protocol_version']=discovered.get('protocol_version',config['protocol_version'])
    config.pop('auto_protocol',None)
    if config['transport']=='mcp':config['catalog']=discovered['catalog']
    approved=capture_model_approvals(value.approved_model_ids)
    record={k:v for k,v in config.items() if k not in ('credential','catalog')}
    record.update(project_id=pid,owner_id=None if operator else current_actor(),origin='preset' if operator else 'personal',
        enabled=True,revision=uid(),secret_config=seal(config),updated_at=now(),approved_models=approved,
        model_fingerprint=model_fingerprint(),protocol_profile=discovered['protocol_profile'],card_sha256=discovered.get('card_sha256'))
    selected_id=identity or 'peer_'+uid()
    with FileLock(str(DATA/('agent-peer-'+selected_id+'.lock')),timeout=5):
        if old and get('collab_peer',selected_id)['revision']!=expected_revision:raise ValueError('Agent连接在验证期间已变化，请重新读取后修改')
        saved=put('collab_peer',record,selected_id)
        if old and old['id'] in get('project',pid).get('mesh_model_policies',{}):_apply_policy(pid,saved)
    return public_peer(saved)


def disable_peer(pid,identity,expected_revision,*,operator=False):
    with FileLock(str(DATA/('agent-peer-'+identity+'.lock')),timeout=5):
        peer=_owned('collab_peer',identity,pid)
        if peer.get('origin')=='preset' and not operator:raise ValueError('管理员预置Agent不能由用户停用')
        if peer['revision']!=expected_revision:raise ValueError('Agent连接已变化，请刷新后停用')
        patch('collab_peer',identity,enabled=False,secret_config=seal({}),revision=uid(),updated_at=now())
    return {'disabled':True,'id':identity}


def list_peers(pid):
    get('project',pid)
    from .agent_team import builtin_peers
    local=builtin_peers(pid)
    if isinstance(local,dict):local=local.get('items',[])
    remote=[public_peer(p) for p in all_items('collab_peer') if p.get('project_id')==pid and p.get('owner_id') in (None,current_actor())]
    return {'items':list(local)+remote,'scope':'current_actor_and_project'}


def list_tasks(pid,parent_turn_id=None):
    get('project',pid)
    values=[t for t in all_items('collab_task') if t.get('project_id')==pid and t.get('actor_id')==current_actor() and (not parent_turn_id or t.get('parent_turn_id')==parent_turn_id)]
    return {'items':[public_task(v) for v in sorted(values,key=lambda t:(t['created_at'],t['id']),reverse=True)[:200]],'limit':200}


def assert_project_model_allowed(pid):
    from .company_search import model_approval_allowed
    for policy in get('project',pid).get('mesh_model_policies',{}).values():
        if not model_approval_allowed(policy):raise ValueError('该项目包含协作Agent返回内容，当前模型尚未获得目的地许可')


def _check_peer(parent,peer):
    from .provider import settings
    from .company_search import model_approval_allowed
    if not peer.get('enabled'):raise ValueError('协作Agent已停用')
    if not settings().get('share_data') or not model_approval_allowed(peer):raise ValueError('当前模型未获准读取此协作Agent的输入和返回内容')
    project=get('project',parent['project_id'])
    if (project.get('company_private') or project.get('work_private')) and not peer.get('private_data_approved'):
        raise ValueError('项目包含内部资料，远端Agent目的地需管理员单独批准')


def _provenance(parent):
    # Service-origin tasks cannot recursively export a company's work through a
    # second agent. Local specialists are also depth-one, read-only tasks.
    if parent.get('gateway_client_id') or parent.get('inbound_client_id') or parent.get('collaboration_task_id') or parent.get('collaboration_depth',0):
        raise ValueError('被委派任务不能再次委派，避免循环调用与权限扩大')
    identity=instance_id()
    return {'chain_id':parent.get('collaboration_chain_id') or 'chain_'+digest(parent['id'])[:32],
        'hops':1,'origin_instance':identity,'visited_instance_ids':[identity]}


def delegate(parent,value):
    if parent.get('actor_id','owner')!=current_actor():raise ValueError('协作任务发起用户不匹配')
    if parent.get('interaction_mode','work')!='work':raise ValueError('聊天模式不能创建协作任务')
    provenance=_provenance(parent)
    current=get('turn',parent['id'])
    operation=next((step.get('operation_id') or step.get('id') for step in reversed(current.get('agent_trace',[])) if step.get('tool')=='delegate_agents'),None)
    operation=operation or 'direct-'+digest(value.model_dump())[:24]
    origin=parent.get('agent_origin',parent['id'])
    batch_id='batch_'+digest([origin,operation])[:40]
    request_hash=digest(value.model_dump())
    with FileLock(str(DATA/('agent-delegate-'+origin+'.lock')),timeout=10):
        try:
            prior=_owned('collab_batch',batch_id,parent['project_id'])
            if prior['request_sha256']!=request_hash:raise ValueError('同一协作请求编号的内容已变化，未重复提交')
            prior_tasks=[get('collab_task',identity) for identity in prior['task_ids']]
            pending=any(task['state'] not in TERMINAL for task in prior_tasks)
            if pending:patch('turn',parent['id'],state='waiting_agent_tasks',progress='正在恢复已有协作任务，未重新提交')
            return {'tasks':[public_task(task) for task in prior_tasks],'pending':pending,'batch_id':batch_id}
        except KeyError:pass
        existing=[t for t in all_items('collab_task') if t.get('parent_turn_id') in (parent['id'],origin)]
        if len(existing)+len(value.assignments)>4:raise ValueError('单项工作最多委派4个子任务，请复用已有结果')
        from .agent_team import builtin_peers
        local=builtin_peers(parent)
        if isinstance(local,dict):local=local.get('items',[])
        locals_by_id={item['id']:item for item in local}
        prepared=[]
        from .agent_handoff import build_input
        # Validate every input before admitting any submission in this batch.
        for index,assignment in enumerate(value.assignments):
            from .memory_service import sensitive
            if sensitive(assignment.goal):raise ValueError('协作目标不能包含模型密钥、密码或连接凭据')
            local_peer=locals_by_id.get(assignment.peer_id)
            peer=local_peer or _owned('collab_peer',assignment.peer_id,parent['project_id'])
            if local_peer and assignment.expected_delivery=='artifacts':raise ValueError('内置复核Agent只读并返回复核意见；完整文件由主Agent或获准的远端执行Agent交付')
            if not local_peer:_check_peer(parent,peer)
            bundle=build_input(parent,source_ids=assignment.source_ids,result_ids=assignment.result_ids)
            if len(json.dumps(bundle,ensure_ascii=False).encode())+len(assignment.goal.encode())*2+65536>transport.MAX_RESPONSE:
                raise ValueError('完整协作输入超过传输预算，未创建或提交任务；请缩小明确输入范围')
            prepared.append((index,assignment,peer,local_peer is not None,bundle))
        from .collaboration_budget import initialize
        budget=initialize(parent)
        seconds=600 if budget['deadline_at'] is None else min(600,budget['deadline_at']-time.time())
        if seconds<=0:raise ValueError('协作时间预算已耗尽，未延长截止时间或提交新任务')
        deadline=(datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()
        identities=[];records=[]
        for index,assignment,peer,is_local,bundle in prepared:
            identity='ct_'+digest([batch_id,index])[:40]
            task={'project_id':parent['project_id'],'actor_id':current_actor(),'parent_turn_id':parent['id'],'root_turn_id':origin,
                'peer_id':peer['id'],'peer_name':peer.get('name',peer['id']),'peer_revision':peer.get('revision','builtin-v1'),
                'transport':'local' if is_local else peer['transport'],'request_id':identity,'goal':assignment.goal,
                'expected_delivery':assignment.expected_delivery,
                'assignment':assignment.model_dump(),'state':'queued','created_at':now(),'updated_at':now(),
                'deadline_at':deadline,'provenance':provenance,'cancel_requested':False,'input_manifest_sha256':bundle['manifest_sha256'],
                'secret_input_bundle':seal(bundle),'evidence':{'integrity_verified':False,'financial_verified':False},
                'secret_peer_snapshot':seal({} if is_local else unseal(peer['secret_config']))}
            records.append(dict(task,id=identity));identities.append(identity)
            if not is_local:_apply_policy(parent['project_id'],peer)
        batch={'id':batch_id,'project_id':parent['project_id'],'actor_id':current_actor(),'parent_turn_id':parent['id'],
            'request_sha256':request_hash,'task_ids':identities,'created_at':now()}
        # Batch admission is atomic. A crash cannot leave a dispatched task
        # without its dedup receipt and later overwrite it as a fresh request.
        with transaction() as connection:
            from .store import _encode
            connection.execute(items.insert(),[{'id':batch_id,'kind':'collab_batch','body':_encode('collab_batch',batch,connection)}]+
                [{'id':record['id'],'kind':'collab_task','body':_encode('collab_task',record,connection)} for record in records])
        from .agent_team import create_local
        for identity,(_,assignment,_,is_local,_) in zip(identities,prepared):
            if is_local:
                try:create_local(parent,get('collab_task',identity),assignment.model_dump())
                except Exception:
                    patch('collab_task',identity,state='failed',error='本地协作任务未能启动，已有输入和状态保留',updated_at=now())
        patch('turn',parent['id'],state='waiting_agent_tasks',progress='协作Agent正在处理，主任务保留已有步骤')
    return {'tasks':[public_task(get('collab_task',identity)) for identity in identities],'pending':True,'batch_id':batch_id}


def cancel_task(pid,identity):
    task=_owned('collab_task',identity,pid)
    if task['state'] in TERMINAL and not (task['state']=='unknown' and task.get('remote_task_id') and not task.get('lifecycle_cancelled_at')):return public_task(task)
    try:
        with FileLock(str(DATA/('agent-task-'+identity+'.lock')),timeout=0):
            task=get('collab_task',identity)
            if task['state']=='queued' and not task.get('child_turn_id'):
                return public_task(patch('collab_task',identity,state='cancelled',cancel_requested=True,status_note='提交前取消，未发送远端请求',updated_at=now()))
            return public_task(patch('collab_task',identity,cancel_requested=True,state='cancel_requested' if task.get('remote_task_id') or task.get('child_turn_id') else task['state'],poll_failures=0,updated_at=now()))
    except Timeout:
        # A submission already in flight cannot be honestly called cancelled.
        return public_task(patch('collab_task',identity,cancel_requested=True,status_note='取消请求已记录，等待实际执行回执；尚未确认远端取消',updated_at=now()))


def _remote_summary(response):
    atlas=None;texts=[]
    if 'outcome' in response or 'output_bundle' in response:atlas=response
    status=response.get('status')
    message=status.get('message') if isinstance(status,dict) else None
    if isinstance(message,dict):
        texts.extend(p['text'] for p in message.get('parts',[]) if isinstance(p,dict) and isinstance(p.get('text'),str))
    for artifact in response.get('artifacts',[]):
        if not isinstance(artifact,dict):continue
        for part in artifact.get('parts',[]):
            if not isinstance(part,dict):continue
            if isinstance(part.get('text'),str):texts.append(part['text'])
            data=part.get('data')
            if isinstance(data,dict) and isinstance(data.get('atlas'),dict):atlas=data['atlas']
    if atlas is None:
        candidate=response.get('metadata',{}).get('atlas') if isinstance(response.get('metadata'),dict) else None
        if isinstance(candidate,dict):atlas=candidate
    atlas=atlas or {}
    if isinstance(atlas.get('output'),dict):atlas=atlas|atlas['output']
    answer=atlas.get('answer') or atlas.get('result_summary') or response.get('answer') or '\n'.join(texts)
    return atlas,str(answer)[:6000]


def _receive(task,response):
    remote_id=response.get('id') or response.get('task_id')
    if not isinstance(remote_id,str) or not 1<=len(remote_id)<=160 or any(ord(c)<32 or ord(c)==127 for c in remote_id):raise transport.RemoteAmbiguous('协作Agent没有返回有效的任务编号')
    if task.get('remote_task_id') and task['remote_task_id']!=remote_id:raise transport.RemoteAmbiguous('协作Agent返回了其他任务的回执')
    # An accepted opaque task identifier is useful even if a later field of the
    # receipt is malformed. Keep it for read-only reconciliation, never resend.
    patch('collab_task',task['id'],remote_task_id=remote_id,state='submitted' if task['state']=='submitting' else task['state'])
    if get('collab_task',task['id']).get('lifecycle_cancelled_at'):
        patch('collab_task',task['id'],state='unknown',cancel_requested=True,status_note='本地工作已停止，刚取得远端任务编号，将请求取消并核对实际回执',updated_at=now())
        return
    status=response.get('status')
    raw_state=status.get('state') if isinstance(status,dict) else status if isinstance(status,str) else response.get('state')
    if raw_state is None:raw_state=response.get('state')
    state=str(raw_state or '').removeprefix('TASK_STATE_').lower().replace('_','-')
    mapped={'submitted':'submitted','working':'working','input-required':'input_required','auth-required':'auth_required',
        'completed':'succeeded','succeeded':'succeeded','failed':'failed','rejected':'rejected','canceled':'cancelled','cancelled':'cancelled'}
    if state not in mapped:raise transport.RemoteAmbiguous('协作Agent任务状态不在已验证协议中')
    target=mapped[state];atlas,summary=_remote_summary(response)
    outcome=atlas.get('outcome') or response.get('outcome')
    if target=='succeeded' and outcome in ('needs_input','blocked','paused','partial','cancelled','failed'):
        target='input_required' if outcome in ('needs_input','blocked','paused','partial') else 'failed'
    evidence={'response_sha256':digest(response),'integrity_verified':False,'financial_verified':False}
    error=None;verification_status='not_completed';execution_status=target
    if target=='succeeded':
        output=atlas.get('output') if isinstance(atlas.get('output'),dict) else {}
        bundle=atlas.get('output_bundle') or output.get('output_bundle') or response.get('output_bundle')
        if bundle is None:
            if task.get('expected_delivery','advisory')=='advisory':verification_status='advisory_unverified'
            else:
                target='input_required';verification_status='missing_artifacts';error='远端称已完成，但未提供可校验的完整成果字节；需回查完整产物，未作为完成证据'
        else:
            from .agent_handoff import validate_bundle
            try:
                validated=validate_bundle(bundle)
                evidence.update(integrity_verified=True,manifest_sha256=bundle['manifest_sha256'],
                    files=len(bundle.get('items',[])),bytes=sum(item.get('bytes',0) for item in bundle.get('items',[])))
                deliverables=[item for item in bundle.get('items',[]) if item.get('kind') in ('artifact','result')]
                if task.get('expected_delivery')=='artifacts' and not deliverables:
                    target='input_required';verification_status='missing_artifacts';error='完整输入原件不等于已交付成果；远端未提供结果或产物字节'
                elif not bundle.get('complete') or not bundle.get('items'):
                    if task.get('expected_delivery','advisory')=='advisory' and bundle.get('complete'):verification_status='advisory_unverified'
                    else:target='input_required';error='远端没有完整非空成果，不能只凭完成状态通过验收'
                else:
                    verification_status='integrity_verified'
                    artifacts=[{k:v for k,v in item.items() if k in ('id','kind','name','media_type','sha256','bytes')} for item in bundle['items']]
                    patch('collab_task',task['id'],secret_output_bundle=seal(bundle),output_artifacts=artifacts)
                    if any(item.get('kind')=='artifact' for item in bundle['items']):
                        try:
                            from .agent_handoff import import_artifacts
                            imported=import_artifacts(get('turn',task['parent_turn_id']),bundle,scope_id='mesh:'+task['id'],request_id=remote_id)
                            patch('collab_task',task['id'],received_artifact_ids=imported['artifact_ids'])
                        except Exception:
                            target='input_required';error='完整文件字节已核对，但接入统一成果目录尚未完成；保留原包供核查与分页下载'
                    if any(item.get('kind')=='result' for item in bundle['items']):
                        try:
                            from .agent_handoff import import_results
                            imported=import_results(task['project_id'],bundle,scope_id='mesh:'+task['id'],request_id=remote_id)
                            patch('collab_task',task['id'],received_result_ids=imported['result_ids'])
                        except Exception:
                            target='input_required';error='完整成果字节已核对，但数值结果接入本项目尚未完成；保留原包供核查，未宣称后续分析已完成'
            except (ValueError,KeyError,TypeError):
                target='rejected';verification_status='rejected';error='协作Agent成果结构、完整性或摘要未通过核对'
    current=get('collab_task',task['id'])
    if current.get('cancel_requested') and target not in TERMINAL:target='cancel_requested'
    patch('collab_task',task['id'],remote_task_id=remote_id,state=target,result_summary=summary,evidence=evidence,
        remote_outcome=outcome,error=error,secret_remote_response=seal(response),updated_at=now(),next_poll_at=time.time()+2,poll_failures=0)
    patch('collab_task',task['id'],execution_status=execution_status,verification_status=verification_status)


def _cleanup_pending(task):
    return bool(task.get('lifecycle_cancelled_at') and task.get('cancel_requested') and task.get('remote_task_id')
        and task.get('transport') in ('a2a','mcp') and not task.get('lifecycle_cleanup_finished_at'))


def _cleanup_remote(task):
    """Deletion may stop local work, but cannot assert that a peer stopped.

    Cleanup sends only an already bound identifier. It never reads new content,
    uses a model, resubmits, or restores the deleted coordinator.
    """
    if time.time()<task.get('lifecycle_cleanup_next_at',0):return
    attempts=task.get('lifecycle_cleanup_attempts',0)
    if attempts>=5:
        patch('collab_task',task['id'],lifecycle_cleanup_finished_at=now(),status_note='本地工作已停止；远端取消仍未确认，需人工核对',updated_at=now());return
    try:peer=_owned('collab_peer',task['peer_id'],task['project_id'])
    except (KeyError,ValueError):
        patch('collab_task',task['id'],lifecycle_cleanup_finished_at=now(),status_note='远端连接记录不可用，不能确认取消；请在远端人工核对',updated_at=now());return
    if not peer.get('enabled') or peer.get('revision')!=task.get('peer_revision'):
        patch('collab_task',task['id'],lifecycle_cleanup_finished_at=now(),status_note='远端授权已变化，未使用旧凭据取消；请在远端核对',updated_at=now());return
    patch('collab_task',task['id'],lifecycle_cleanup_attempts=attempts+1,lifecycle_cleanup_next_at=time.time()+5,updated_at=now())
    try:
        config=unseal(task['secret_peer_snapshot'])
        config.update(mesh_task_id=task['id'],mesh_cleanup=True)
        if task.get('cancel_dispatched_at'):response=transport.poll(config,task['remote_task_id'])
        else:
            # Persist before send so a crash never causes a second cancellation.
            patch('collab_task',task['id'],cancel_dispatched_at=now(),updated_at=now())
            response=transport.cancel(config,task['remote_task_id'])
        if not isinstance(response,dict) or (response.get('id') or response.get('task_id'))!=task['remote_task_id']:
            raise transport.RemoteAmbiguous('远端取消回执编号不匹配')
        status=response.get('status');raw=status.get('state') if isinstance(status,dict) else status or response.get('state')
        state=str(raw or '').removeprefix('TASK_STATE_').lower().replace('_','-')
        receipt={'remote_cleanup_state':state,'remote_cleanup_response_sha256':digest(response),'updated_at':now()}
        if state in ('canceled','cancelled'):
            receipt.update(state='cancelled',lifecycle_cleanup_finished_at=now(),status_note='远端已实际确认取消，项目与对话仍保留在回收站')
        elif state in ('completed','succeeded','failed','rejected','input-required','auth-required'):
            receipt.update(lifecycle_cleanup_finished_at=now(),status_note='远端已返回终态，删除前任务的实际结果需人工核对；未恢复本地任务')
        patch('collab_task',task['id'],**receipt)
    except transport.RemoteRejected:
        patch('collab_task',task['id'],lifecycle_cleanup_finished_at=now(),status_note='远端拒绝取消或查询，不能确认取消；需人工核对',updated_at=now())
    except Exception:
        patch('collab_task',task['id'],status_note='已停止本地工作；取消回执暂未知，仅继续查询已有远端任务',updated_at=now())


def _advance(identity):
    try:
        with FileLock(str(DATA/('agent-task-'+identity+'.lock')),timeout=0):
            task=get('collab_task',identity)
            if _cleanup_pending(task):
                patch('collab_task',identity,last_mesh_check_at=now())
                with actor_scope(task['actor_id']):_cleanup_remote(task)
                return
            if task['state'] in TERMINAL:return
            patch('collab_task',identity,last_mesh_check_at=now())
            with actor_scope(task['actor_id']):
                from .provider import task_scope
                with task_scope(task['parent_turn_id']):
                    parent=get('turn',task['parent_turn_id'])
                    if task['transport']=='local':
                        from .agent_team import reconcile_local,create_local
                        if task['state']=='queued':
                            create_local(parent,task,task['assignment'])
                        else:reconcile_local(task)
                        return
                    peer=_owned('collab_peer',task['peer_id'],task['project_id'])
                    if not peer.get('enabled') or peer['revision']!=task['peer_revision']:
                        patch('collab_task',identity,state='auth_required',error='Agent连接已停用或授权版本变化，未继续使用旧凭据',updated_at=now());return
                    _check_peer(parent,peer)
                    config=unseal(task['secret_peer_snapshot'])
                    config['mesh_task_id']=identity
                    expired=datetime.fromisoformat(task['deadline_at'])<=datetime.now(timezone.utc)
                    if expired and task.get('cancel_dispatched_at') and (datetime.now(timezone.utc)-datetime.fromisoformat(task['deadline_at'])).total_seconds()>30:
                        patch('collab_task',identity,state='unknown',error='协作截止后仍未确认远端终止；需人工核对，未重复提交或伪称取消',updated_at=now());return
                    if expired and not task.get('remote_task_id') and task['state']=='queued':
                        patch('collab_task',identity,state='failed',error='协作时间预算已到，尚未发送远端任务',updated_at=now());return
                    if task['state']=='submitting':
                        # The previous process disappeared after durable admission.
                        # Even a request that probably never left this host is not replayed.
                        patch('collab_task',identity,state='unknown',error='上次提交中断，无法确认远端是否已创建任务；需人工核对，系统不会自动重发',updated_at=now());return
                    try:
                        if task['state']=='queued':
                            if task.get('cancel_requested'):
                                patch('collab_task',identity,state='cancelled',updated_at=now());return
                            patch('collab_task',identity,state='submitting',dispatch_started_at=now(),dispatch_token=uid(),updated_at=now())
                            # This is the only external submit path. Its durable state
                            # is written before opening a connection.
                            _receive(get('collab_task',identity),transport.submit(config,task,unseal(task['secret_input_bundle'])))
                        elif task.get('remote_task_id'):
                            if task.get('cancel_requested') or expired:
                                if task.get('cancel_dispatched_at'):
                                    _receive(task,transport.poll(config,task['remote_task_id']))
                                else:
                                    patch('collab_task',identity,state='cancel_requested',cancel_requested=True,cancel_dispatched_at=now(),updated_at=now())
                                    _receive(get('collab_task',identity),transport.cancel(config,task['remote_task_id']))
                            elif time.time()>=task.get('next_poll_at',0):_receive(task,transport.poll(config,task['remote_task_id']))
                    except transport.RemoteRejected as exc:
                        patch('collab_task',identity,state='auth_required' if exc.auth else 'rejected',error=str(exc),updated_at=now())
                    except Exception:
                        current=get('collab_task',identity)
                        failures=current.get('poll_failures',0)+1
                        if current.get('remote_task_id') and failures<5 and not expired:
                            patch('collab_task',identity,poll_failures=failures,status_note='暂未取得新状态；保留原任务编号，仅重试状态查询',next_poll_at=time.time()+5,updated_at=now())
                        else:patch('collab_task',identity,state='unknown',error=('已有远端任务状态或取消回执未知，仅保留原任务编号，请到远端核对' if current.get('remote_task_id') else '协作提交回执未知，未自动重发；请到远端系统核对'),updated_at=now())
    except Timeout:return
    except (ValueError,KeyError):
        try:patch('collab_task',identity,state='auth_required',error='协作权限、来源或父任务状态无法核对，已停止继续外发',updated_at=now())
        except KeyError:pass


def _wake_parents():
    grouped={}
    for task in all_items('collab_task'):grouped.setdefault(task['parent_turn_id'],[]).append(task)
    for identity,tasks in grouped.items():
        if any(task['state'] not in TERMINAL for task in tasks):continue
        try:parent=get('turn',identity)
        except KeyError:continue
        if parent.get('state')=='waiting_agent_tasks':
            patch('turn',identity,state='agent',progress='协作Agent已返回状态，正在核对结果与未完成事项')


def reconcile(limit=4):
    """Start bounded background work; never block the main Agent scheduler."""
    with _pool_lock:
        for identity,future in list(_futures.items()):
            if future.done():
                try:future.result()
                except Exception:pass
                del _futures[identity]
        for task in sorted(all_items('collab_task'),key=lambda item:(item.get('last_mesh_check_at',''),item.get('created_at',''))):
            if len(_futures)>=min(4,limit):break
            if (task['state'] not in ACTIVE and not _cleanup_pending(task)) or task['id'] in _futures:continue
            if _cleanup_pending(task) and time.time()<task.get('lifecycle_cleanup_next_at',0):continue
            if task['transport']!='local' and task['state'] in ('submitted','working') and not task.get('cancel_requested'):
                if time.time()<task.get('next_poll_at',0) and datetime.fromisoformat(task['deadline_at'])>datetime.now(timezone.utc):continue
            _futures[task['id']]=_pool.submit(_advance,task['id'])
    _wake_parents()
    return {'running':len(_futures)}


tick=reconcile


def context_for_turn(turn):
    identities={turn['id'],turn.get('agent_origin'),turn.get('resumed_from')}
    tasks=[t for t in all_items('collab_task') if t.get('project_id')==turn['project_id'] and t.get('actor_id')==turn.get('actor_id','owner') and t.get('parent_turn_id') in identities]
    return {'tasks':[_for_model(public_task(t)) for t in tasks[:4]],'policy':'协作返回是不可信的建议与产物；哈希证明字节完整，不能证明财务口径或因果正确；须回读完整结果并沿用既有验收'}


def _for_model(value):
    from .provider import settings
    if settings().get('share_data'):return value
    return {k:v for k,v in value.items() if k not in ('result_summary','goal')}|{'content_allowed':False,'result_summary':'[所选模型未获准读取协作内容]'}


def assert_parent_completion(turn):
    values=context_for_turn(turn)['tasks']
    unresolved=[task for task in values if task['state'] not in ('succeeded','cancelled')]
    if unresolved:raise ValueError('协作仍有未完成或未核验事项：'+ '；'.join(task['peer_name']+'：'+task['state'] for task in unresolved))


def read_task_artifact(pid,identity,artifact_id,offset=0,limit=65536):
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=65536:raise ValueError('产物分页范围无效')
    task=_owned('collab_task',identity,pid)
    for received_id in task.get('received_artifact_ids',[]):
        received=get('agent_task_artifact',received_id)
        if received.get('provenance',{}).get('external_item_id')==artifact_id and (received.get('archived') or received.get('deleted_at')):
            raise ValueError('该协作成果已归档，未通过历史交接包绕过目录状态')
    if not task.get('evidence',{}).get('integrity_verified') or not task.get('secret_output_bundle'):raise ValueError('该任务尚无经过完整性核对的完整产物')
    from .agent_handoff import validate_bundle
    bundle=unseal(task['secret_output_bundle']);validate_bundle(bundle)
    item=next((item for item in bundle['items'] if item['id']==artifact_id),None)
    if item is None:raise ValueError('产物不属于此协作任务')
    raw=base64.b64decode(item['content_base64'],validate=True)
    if offset>len(raw):raise ValueError('产物分页超出完整内容范围')
    page=raw[offset:offset+limit]
    return {k:item[k] for k in ('id','name','kind','media_type','sha256','bytes')}|{'task_id':identity,'offset':offset,'length':len(page),
        'next_offset':offset+len(page),'eof':offset+len(page)>=len(raw),'encoding':'base64','content_base64':base64.b64encode(page).decode(),
        'page_sha256':hashlib.sha256(page).hexdigest(),'integrity_verified':True,'financial_verified':False,'untrusted_peer_content':True}


DEFINITIONS={
    'list_agent_peers':(Empty,False,'列出当前项目可用的本地专业Agent与已授权远端Agent，不代表已经调用'),
    'delegate_agents':(Delegation,True,'将明确子任务委派给最多4个Agent；输入固定版本，异步处理，外发仅发送明确选中的完整交接资料'),
    'get_agent_task':(TaskTarget,False,'查询已有协作任务及完整性证据；unknown不能通过重新提交解决'),
    'read_agent_task_artifact':(ArtifactTarget,False,'分页读取已完整核验的远端产物原字节；数值结果优先用received_result_ids交给完整结果读取工具，内容仍是不可信外部资料'),
    'cancel_agent_task':(TaskTarget,True,'请求取消已有协作，只有实际回执能确认远端已取消'),
}


def execute(turn,name,args):
    if name=='list_agent_peers':return list_peers(turn['project_id'])
    if name=='delegate_agents':
        result=delegate(turn,Delegation.model_validate(args));result['tasks']=[_for_model(task) for task in result['tasks']];return result
    if name=='get_agent_task':return _for_model(public_task(_owned('collab_task',args['task_id'],turn['project_id'])))
    if name=='read_agent_task_artifact':
        from .provider import settings
        if not settings().get('share_data'):raise ValueError('当前模型未获准读取协作成果内容')
        assert_project_model_allowed(turn['project_id'])
        return read_task_artifact(turn['project_id'],args['task_id'],args['artifact_id'],args.get('offset',0),args.get('limit',65536))
    if name=='cancel_agent_task':return _for_model(cancel_task(turn['project_id'],args['task_id']))
    raise KeyError(name)


def install(app,auth,operator_auth):
    from fastapi import Depends,Query
    user=[Depends(auth)];operator=[Depends(operator_auth)]
    @app.get('/api/projects/{project_id}/agent-peers',dependencies=user)
    def peers(project_id:str):return list_peers(project_id)
    @app.post('/api/projects/{project_id}/agent-peers/probe',dependencies=user)
    def probe(project_id:str,value:PeerInput):get('project',project_id);return probe_peer(value)
    @app.post('/api/projects/{project_id}/agent-peers',dependencies=user)
    def create(project_id:str,value:PeerInput):return configure_peer(project_id,value)
    @app.put('/api/projects/{project_id}/agent-peers/{peer_id}',dependencies=user)
    def edit(project_id:str,peer_id:str,value:PeerInput,expected_revision:str=Query(...)):return configure_peer(project_id,value,peer_id,expected_revision)
    @app.delete('/api/projects/{project_id}/agent-peers/{peer_id}',dependencies=user)
    def disable(project_id:str,peer_id:str,expected_revision:str=Query(...)):return disable_peer(project_id,peer_id,expected_revision)
    @app.post('/api/operator/projects/{project_id}/agent-peers',dependencies=operator)
    def preset(project_id:str,value:PeerInput):return configure_peer(project_id,value,operator=True)
    @app.put('/api/operator/projects/{project_id}/agent-peers/{peer_id}',dependencies=operator)
    def edit_preset(project_id:str,peer_id:str,value:PeerInput,expected_revision:str=Query(...)):return configure_peer(project_id,value,peer_id,expected_revision,operator=True)
    @app.delete('/api/operator/projects/{project_id}/agent-peers/{peer_id}',dependencies=operator)
    def disable_preset(project_id:str,peer_id:str,expected_revision:str=Query(...)):return disable_peer(project_id,peer_id,expected_revision,operator=True)
    @app.get('/api/projects/{project_id}/agent-tasks',dependencies=user)
    def tasks(project_id:str,parent_turn_id:str|None=None):return list_tasks(project_id,parent_turn_id)
    @app.get('/api/projects/{project_id}/agent-tasks/{task_id}',dependencies=user)
    def task(project_id:str,task_id:str):return public_task(_owned('collab_task',task_id,project_id))
    @app.post('/api/projects/{project_id}/agent-tasks/{task_id}/cancel',dependencies=user)
    def cancel(project_id:str,task_id:str):return cancel_task(project_id,task_id)
    @app.get('/api/projects/{project_id}/agent-tasks/{task_id}/artifacts/{artifact_id}',dependencies=user)
    def artifact(project_id:str,task_id:str,artifact_id:str,offset:int=Query(0,ge=0),limit:int=Query(65536,ge=1,le=65536)):
        return read_task_artifact(project_id,task_id,artifact_id,offset,limit)
