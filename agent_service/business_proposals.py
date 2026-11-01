"""Confirm a concrete Agent data-application proposal before code generation.

The proposal is an immutable digest-bound projection of validated rules, not
an execution receipt and not a confirmation of a later generated result.
"""
import hashlib
import json
import re
import time
from fastapi import Depends
from pydantic import Field
from sqlalchemy import select, update

from .actor_context import current_actor
from .models import StrictModel
from .store import get, put, seal, unseal, now, transaction, items, _decode, _encode
from .config import DATA
from .locks import FileLock


class DecisionInput(StrictModel):
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    session_id: str = Field(min_length=1, max_length=100)
    approve: bool = Field(strict=True)


def _digest(value):
    data=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str)
    return hashlib.sha256(data.encode()).hexdigest()


def _source_marker(source):
    # Do not expose encrypted connection details, but a changed configuration or
    # field profile must invalidate an approval for the previously shown plan.
    keys=('id','project_id','kind','revision','revision_id','content_hash','secret_config','profile','status','archived')
    return _digest({key:source.get(key) for key in keys})


def _owned(kind, identity, pid):
    from .conversation import owned
    return owned(kind,identity,pid)


def propose(turn, source, existing, args, operation_id):
    from .codegen import merged_fields, _safe_user_request
    from .applications import configuration
    from .business_plan import build
    from .conversation import stable, default_session
    from .models import RuntimePolicy, TransformSpec
    pid=turn['project_id']
    spec=TransformSpec.model_validate(args['spec']).model_dump()
    for join in spec['joins']:
        _owned('source',join['source_id'],pid)
    source_ids=list(dict.fromkeys([source['id']]+[j['source_id'] for j in spec['joins']]))
    source_snapshots={sid:_owned('source',sid,pid) for sid in source_ids}
    if _source_marker(source)!=_source_marker(source_snapshots[source['id']]):
        raise ValueError('资料正在变化，请重新生成方案')
    fields,joins=merged_fields(source,spec,source_lookup=lambda sid:source_snapshots[sid])
    name=args.get('name') or (existing['name'] if existing else spec['object_label']+'数据应用')
    app_value=args.get('application') if args.get('application') is not None else (existing.get('application_spec') if existing and existing.get('application_customized') else None)
    app=configuration(name,spec,fields,app_value)
    runtime=RuntimePolicy.model_validate((existing or {}).get('runtime',{})).model_dump()
    request_text,request_turn_id,continuation_request=_safe_user_request(pid,turn['id'])
    draft={'user_request':request_text,'request_turn_id':request_turn_id,
           'continuation_request':continuation_request,
           'source_id':source['id'],'source_name':source.get('name'),'source_revision':source.get('revision',1),
           'spec':spec,'fields':fields,'joins':joins,'application':app,'runtime':runtime,
           'identity_policy':'business_primary_key_sha256_v1' if spec['primary_key'] else 'preserve_all_rows_occurrence_v1'}
    description=build(draft,(existing or {}).get('id'),pid,name,source_name=source.get('name'),origin='proposed')
    markers={sid:_source_marker(source_snapshots[sid]) for sid in source_ids}
    if any(_source_marker(_owned('source',sid,pid))!=marker for sid,marker in markers.items()):
        raise ValueError('资料正在变化，请重新生成方案')
    session_id=turn.get('session_id') or default_session(pid)
    payload={'project_id':pid,'session_id':session_id,'turn_id':turn['id'],'actor_id':turn.get('actor_id','owner'),
             'source_id':source['id'],'source_markers':markers,'source_snapshots':source_snapshots,'target_id':(existing or {}).get('id'),
             'target_commit':(existing or {}).get('commit'),'draft':draft,
             'knowledge_basis':get('turn',turn['id']).get('knowledge_basis',[]),
             'decision':{'action':'analyze','spec':spec,
             'name':name,'application':args.get('application')},'operation_id':operation_id,'description':description}
    digest=_digest(payload)
    identity=stable('bp_',turn['id'],operation_id)
    try:existing_proposal=get('business_proposal',identity)
    except KeyError:existing_proposal=None
    if existing_proposal is not None:
        if existing_proposal['digest']!=digest:raise ValueError('同一项工作方案已变化，请重新发起任务')
        return public(existing_proposal)
    value={'project_id':pid,'session_id':session_id,'turn_id':turn['id'],'actor_id':turn.get('actor_id','owner'),
           'digest':digest,'state':'pending','created_at':now(),'expires':time.time()+86400,'secret_payload':seal(payload)}
    return public(put('business_proposal',value,identity))


def public(value):
    payload=unseal(value['secret_payload'])
    desc=payload['description']
    return {'id':value['id'],'digest':value['digest'],'state':value['state'],'created_at':value['created_at'],
            'title':desc['title'],'user_request':desc.get('user_request'),
            'continuation_request':desc.get('continuation_request'),
            'steps':desc['steps'],'warnings':desc['warnings'],'sources':desc['sources']}


def _row(connection,kind,identity):
    row=connection.execute(select(items.c.body).where(items.c.kind==kind,items.c.id==identity).with_for_update()).first()
    if not row:raise KeyError(identity)
    return _decode(row[0])


def _save(connection,kind,value):
    connection.execute(update(items).where(items.c.kind==kind,items.c.id==value['id']).values(body=_encode(kind,value,connection)))


def _check_payload(pid, turn, payload, source_lookup, target_lookup):
    from .workspace_lifecycle import assert_turn_live
    from .conversation import default_session
    assert_turn_live(turn)
    if turn.get('project_id')!=pid or (turn.get('session_id') or default_session(pid))!=payload['session_id']:
        raise ValueError('方案所属项目或对话已变化')
    for sid,marker in payload['source_markers'].items():
        source=source_lookup(sid)
        if source.get('project_id')!=pid or source.get('archived') or source.get('status')!='ready' or _source_marker(source)!=marker:
            raise ValueError('方案涉及的资料或字段已变化，请重新生成并确认方案')
    target=payload.get('target_id')
    if target:
        pipeline=target_lookup(target)
        own_generated=(pipeline.get('origin_turn')==payload['operation_id'] and
                       pipeline.get('origin_commit') and pipeline.get('origin_commit')==pipeline.get('commit'))
        if (pipeline.get('project_id')!=pid or pipeline.get('archived') or
                pipeline.get('commit')!=payload['target_commit'] and not own_generated):
            raise ValueError('应用版本已变化，请重新生成并确认方案')


def decide(pid, identity, value):
    if not re.fullmatch(r'bp_[a-f0-9]{40}',identity):raise ValueError('方案标识无效')
    from .workspace_lifecycle import assert_project_live
    assert_project_live(pid)
    # The same project lock is held by the orchestrator; acceptance and task
    # reactivation are one DB transaction, never two competing submissions.
    # The scheduler acquires chat before lifecycle when it enqueues work;
    # follow that ordering and share the lifecycle lock with deletion/restore.
    lifecycle='lifecycle-'+hashlib.sha256(pid.encode()).hexdigest()[:32]+'.lock'
    with FileLock(str(DATA/('chat-'+pid+'.lock')),timeout=5),FileLock(str(DATA/lifecycle),timeout=5):
        with transaction() as connection:
            proposal=_row(connection,'business_proposal',identity)
            if (proposal.get('project_id')!=pid or proposal.get('session_id')!=value.session_id
                    or proposal.get('actor_id')!=current_actor() or proposal.get('digest')!=value.digest):
                raise ValueError('方案、对话、用户或确认内容不匹配')
            payload=unseal(proposal['secret_payload'])
            if _digest(payload)!=proposal['digest']:raise ValueError('方案内容校验失败')
            project=_row(connection,'project',pid)
            if project.get('deleted_at'):raise ValueError('项目已移至回收站，不能确认方案')
            from .conversation import default_session
            try:session=_row(connection,'conversation',value.session_id)
            except KeyError:
                if value.session_id!=default_session(pid):raise ValueError('对话不存在，不能确认方案') from None
                session=None
            if session and (session.get('project_id')!=pid or session.get('deleted_at')):
                raise ValueError('对话已变化或已删除，不能确认方案')
            if proposal['state']!='pending':
                if (proposal['state']=='approved')!=value.approve:raise ValueError('该方案已经处理，不能更改确认结果')
                return public(proposal)
            if proposal.get('expires',0)<time.time():raise ValueError('方案已过期，请重新生成并确认')
            turn=_row(connection,'turn',proposal['turn_id'])
            if (turn.get('state')!='done' or turn.get('outcome')!='needs_input'
                    or turn.get('business_proposal_id')!=identity):
                raise ValueError('任务状态已变化，请重新查看方案')
            _check_payload(pid,turn,payload,lambda sid:_row(connection,'source',sid),lambda aid:_row(connection,'pipeline',aid))
            proposal['state']='approved' if value.approve else 'rejected'
            proposal['decided_at']=now()
            if value.approve:
                project.update(source_id=payload['source_id'],pipeline_id=payload.get('target_id'))
                prior_version=turn.get('work_version')
                turn.update(state='queued',outcome=None,answer='',finished_at=None,progress='方案已确认，正在生成并验证',
                            source_id=payload['source_id'],agent_pending=True,forced_decision=payload['decision'],
                            operation_id=payload['operation_id'],resume_available=False,
                            proposal_previous_work_version=prior_version,
                            before_version=None,work_version=None,restored_version=None)
                _save(connection,'project',project);_save(connection,'turn',turn)
            _save(connection,'business_proposal',proposal)
    return public(proposal)


def assert_approved(turn):
    """Check the frozen approval again immediately before generation."""
    identity=turn.get('business_proposal_id')
    if not identity:raise ValueError('此处理方案尚未确认，不能生成代码')
    proposal=get('business_proposal',identity)
    payload=unseal(proposal['secret_payload'])
    if (proposal.get('state')!='approved' or proposal.get('turn_id')!=turn['id']
            or _digest(payload)!=proposal.get('digest') or payload['decision']!=turn.get('forced_decision')):
        raise ValueError('已确认方案与当前处理内容不一致，不能生成代码')
    _check_payload(turn['project_id'],turn,payload,lambda sid:get('source',sid),lambda aid:get('pipeline',aid))
    snapshots=payload.get('source_snapshots')
    if not isinstance(snapshots,dict) or set(snapshots)!=set(payload['source_markers']):
        raise ValueError('已确认方案缺少完整来源快照，不能生成代码')
    for sid,source in snapshots.items():
        if not isinstance(source,dict) or source.get('id')!=sid or _source_marker(source)!=payload['source_markers'][sid]:
            raise ValueError('已确认方案来源快照不一致，不能生成代码')
    return payload


def assert_generated(pipeline,payload):
    """Do not replay a mutable pipeline which only retained an old origin ID."""
    from .codegen import git
    if (pipeline.get('origin_turn')!=payload['operation_id'] or
            not pipeline.get('origin_commit') or pipeline.get('origin_commit')!=pipeline.get('commit')):
        raise ValueError('批准后的代码版本无法确认或已被修改，未运行；请重新核对方案')
    try:plan=json.loads(git(pipeline['repository'],'show',pipeline['commit']+':plan.json'))
    except (ValueError,TypeError):raise ValueError('批准后的代码版本不可读取，未运行') from None
    draft=payload['draft']
    for key in ('user_request','request_turn_id','continuation_request','source_id','source_name','source_revision','spec','fields','joins','application','runtime','identity_policy','knowledge_basis'):
        expected=payload.get('knowledge_basis') if key=='knowledge_basis' else draft.get(key)
        if plan.get(key)!=expected:
            raise ValueError('生成代码与已确认的业务方案不一致，未运行；请重新核对')


def install(app,auth):
    @app.post('/api/projects/{pid}/business-proposals/{identity}/decision',dependencies=[Depends(auth)])
    def decision(pid:str,identity:str,value:DecisionInput):
        return decide(pid,identity,value)
