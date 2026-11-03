"""Project-scoped, Git-versioned business flows and durable execution.

Raw inputs and step outputs are stored encrypted in job records, never in the
project Git. A draft can be trialled; only an explicitly adopted successful
run becomes the formal version. Financial interpretation is not certified.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import sys
import threading
from pathlib import Path
from typing import Literal
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from croniter import croniter

from fastapi import Depends
from pydantic import Field,field_validator

from .models import StrictModel
from .store import get,put,patch,all_items,seal,unseal,now,uid
from .business_flow_engine import BusinessFlow,digest,run as run_engine
from .actor_context import current_actor,actor_scope
from .workspace_lifecycle import assert_project_live,assert_turn_live
from . import workspace
from .jobs import enqueue,get_job,fenced_update,heartbeat,list_jobs


def _id(pid,request_id):
    return 'bf_'+hashlib.sha256((pid+'\0'+request_id).encode()).hexdigest()[:40]


def _turn(pid,actor,tid):
    assert_project_live(pid)
    if not isinstance(tid,str) or not tid:raise ValueError('需要真实项目对话的授权')
    turn=get('turn',tid)
    if turn.get('project_id')!=pid or turn.get('actor_id','owner')!=actor or turn.get('gateway_client_id') or turn.get('outcome')=='cancelled':
        raise ValueError('当前用户无权使用该对话的流程授权')
    assert_turn_live(turn)
    from .intents import require
    require(turn,'write')
    return turn


def _owned(pid,fid,actor=None):
    assert_project_live(pid)
    flow=get('business_flow',fid)
    if flow.get('project_id')!=pid or flow.get('archived') or (actor is not None and flow.get('actor_id')!=actor):
        raise ValueError('业务流程不属于当前项目或使用者')
    return flow


def _handlers():
    from . import business_flow_data,business_deliverables,workflow_model_skill,business_flow_quality_gate,business_flow_human_review
    from .business_flow_engine import Handler
    result={'source_table':Handler(business_flow_data.source_table,False,True,business_flow_data.validate_source),
            'table_workflow':Handler(business_flow_data.table_workflow,False,True,business_flow_data.validate_table),
            'quality_gate':Handler(business_flow_quality_gate.run,True,True,business_flow_quality_gate.validate),
            'human_review':Handler(business_flow_human_review.never_execute,True,True,business_flow_human_review.validate)}
    result.update(business_deliverables.handlers());result.update(workflow_model_skill.handlers())
    extra=getattr(business_deliverables,'data_handlers',lambda:{})()
    result.update(extra)
    return result


def _source_pin(pid,source_id,revision,content_hash):
    s=get('source',source_id)
    if (s.get('project_id')!=pid or s.get('kind')!='file' or s.get('status')!='ready' or s.get('archived')
            or s.get('revision')!=revision or s.get('content_hash')!=content_hash):
        raise ValueError('流程资料版本不属于当前项目或已变化')


def _validate(pid,actor,tid,document):
    from . import workflow_model_skill
    turn=_turn(pid,actor,tid);flow=BusinessFlow.model_validate(document)
    value=flow.model_dump(mode='json');handlers=_handlers()
    context={'project_id':pid,'actor_id':actor,'turn_id':tid,'originating_turn_id':tid}
    with actor_scope(actor):
        for node in flow.nodes:
            handler=handlers.get(node.kind)
            if handler is None:raise ValueError('当前环境未提供所需步骤：'+node.kind)
            if handler.validate_config:handler.validate_config(node.config)
            if node.kind in ('model','skill'):
                pin=workflow_model_skill.freeze_model(context)
                if node.config.get('model_id')!=pin['model_id'] or node.config.get('catalog_revision')!=pin['catalog_revision']:
                    raise ValueError('模型配置与已授权对话不一致')
                if node.kind=='skill':workflow_model_skill._skill_text(context,node.config)
            if node.kind=='source_table' and node.config.get('source_id'):
                _source_pin(pid,node.config['source_id'],node.config.get('revision'),node.config.get('content_hash'))
    return value


def _path(fid):return 'business-flows/'+fid+'.json'


def _lock_path(fid):return 'business-flows/'+fid+'.lock.json'


def _entry_path(fid):return 'business-flows/'+fid+'.py'


def _runtime_lock(document):
    """Conservative runtime pin; a changed handler cannot silently rerun."""
    kinds={n['kind'] for n in document['nodes']}
    names={'business_flow_engine.py','business_flows.py','workflow_model_skill.py',
           'agent_context.py','agent_gateway.py','agent_team.py','workspace.py'}
    if kinds & {'source_table','table_workflow','table_report_spec','quality_gate'}:
        names|={'business_flow_data.py','project_data.py','data_workflows.py','workbook_reader.py','connectors.py'}
    if 'quality_gate' in kinds:names.add('business_flow_quality_gate.py')
    if 'human_review' in kinds:names|={'business_flow_human_review.py','business_flow_checkpoint.py'}
    if kinds & {'office_document','html_report','table_report_spec'}:
        names|={'business_deliverables.py','office.py','workspace_artifacts.py'}
    if 'office_document' in kinds:names.add('office_presentation.py')
    if kinds & {'model','skill'}:names|={'provider.py','provider_compatibility.py','extensions.py'}
    root=Path(__file__).parent
    modules={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in sorted(names)}
    packages={}
    for package in ('duckdb','dlt','dbt-core','dbt-duckdb','openpyxl','python-pptx','python-docx'):
        try:packages[package]=importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:packages[package]=None
    return {'version':1,'python':'.'.join(map(str,sys.version_info[:3])),
            'modules':modules,'packages':packages,
            'handlers':{key:{'version':_handlers()[key].version,'deterministic':_handlers()[key].deterministic,
                             'replay_safe':_handlers()[key].replay_safe} for key in sorted(kinds)}}


def _entry(pid,fid,sha):
    # This project entry submits to the authenticated service; it never loads
    # user-supplied Python or bypasses the server's authorization/receipts.
    return ("#!/usr/bin/env python3\n"
            "# 使用已授权 Nexus WorkStudio 服务执行固定的业务流程版本。数据和凭证不保存在代码中。\n"
            "import json, os, sys, urllib.request\n"
            "base=os.environ['NEXUS_URL'].rstrip('/')\n"
            "token=os.environ['NEXUS_TOKEN']\n"
            "inputs=json.load(open(sys.argv[1],encoding='utf-8')) if len(sys.argv)>1 else {}\n"
            "body=json.dumps({'request_id':os.urandom(16).hex(),'expected_digest':"+repr(sha)+",'inputs':inputs}).encode()\n"
            "request=urllib.request.Request(base+"+repr('/api/projects/'+pid+'/business-flows/'+fid+'/runs')+",data=body,headers={'Content-Type':'application/json'},method='POST')\n"
            "request.add_header('Author'+'ization','Bearer '+token)\n"
            "print(urllib.request.urlopen(request,timeout=30).read().decode())\n")


def _write_version_files(pid,fid,document):
    sha=digest(document)
    workspace.write_file(pid,_path(fid),json.dumps(document,ensure_ascii=False,sort_keys=True,indent=2))
    workspace.write_file(pid,_lock_path(fid),json.dumps(_runtime_lock(document),ensure_ascii=False,sort_keys=True,indent=2))
    workspace.write_file(pid,_entry_path(fid),_entry(pid,fid,sha))


def _read_commit(pid,fid,commit,expected):
    if not isinstance(commit,str) or not re.fullmatch(r'[a-f0-9]{40}',commit):raise ValueError('流程项目版本不可用')
    try:raw=workspace.git_content(workspace.folder(pid),commit,'files/'+_path(fid))
    except Exception:raise ValueError('该项目版本缺少业务流程定义') from None
    try:doc=BusinessFlow.model_validate(json.loads(raw)).model_dump(mode='json')
    except Exception:raise ValueError('该项目版本的流程定义无效') from None
    if digest(doc)!=expected:raise ValueError('流程定义与版本摘要不一致')
    return doc


def create(pid,actor_id,turn_id,document,request_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',request_id or ''):raise ValueError('请求标识无效')
    value=_validate(pid,actor_id,turn_id,document);fid=_id(pid,request_id);sha=digest(value)
    with workspace.lock(pid):
        try:
            old=_owned(pid,fid,actor_id)
            if old['candidate_digest']!=sha or old['turn_id']!=turn_id:raise ValueError('请求标识已用于另一方案')
            if not old.get('candidate_commit'):
                # Recover a crash between the durable file/row and commit.
                if digest(BusinessFlow.model_validate_json(workspace.read_file(pid,_path(fid))).model_dump(mode='json'))!=sha:
                    raise ValueError('待恢复流程与原始请求不一致')
                version=workspace._checkpoint(pid,turn_id,'恢复业务流程方案')
                patch('business_flow',fid,candidate_commit=version['commit'])
            return get_view(pid,fid,actor_id)
        except KeyError:pass
        record={'project_id':pid,'actor_id':actor_id,'turn_id':turn_id,'name':value['name'],'path':_path(fid),
                'candidate_digest':sha,'candidate_run':None,'published_digest':None,'published_run':None,
                'published_commit':None,'created_at':now(),'updated_at':now()}
        _write_version_files(pid,fid,value)
        put('business_flow',record,fid)
        version=workspace._checkpoint(pid,turn_id,'建立业务流程方案')
        patch('business_flow',fid,candidate_commit=version['commit'])
    return get_view(pid,fid,actor_id)


def update(pid,flow_id,actor_id,turn_id,document,request_id,expected_digest):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',request_id or ''):raise ValueError('请求标识无效')
    value=_validate(pid,actor_id,turn_id,document);sha=digest(value)
    with workspace.lock(pid):
        flow=_owned(pid,flow_id,actor_id)
        if flow.get('last_update_request')==request_id:
            if flow.get('candidate_digest')!=sha:raise ValueError('请求标识已用于另一方案')
            if not flow.get('candidate_commit'):
                if digest(BusinessFlow.model_validate_json(workspace.read_file(pid,_path(flow_id))).model_dump(mode='json'))!=sha:
                    raise ValueError('待恢复流程与原始请求不一致')
                version=workspace._checkpoint(pid,turn_id,'恢复业务流程候选方案')
                patch('business_flow',flow_id,candidate_commit=version['commit'])
            return get_view(pid,flow_id,actor_id)
        if flow.get('candidate_digest')!=expected_digest:raise ValueError('方案已变化，请重新查看')
        _write_version_files(pid,flow_id,value)
        patch('business_flow',flow_id,name=value['name'],turn_id=turn_id,candidate_digest=sha,candidate_run=None,
              candidate_commit=None,last_update_request=request_id,updated_at=now())
        version=workspace._checkpoint(pid,turn_id,'修改业务流程候选方案')
        patch('business_flow',flow_id,candidate_commit=version['commit'])
    return get_view(pid,flow_id,actor_id)


def get_view(pid,flow_id,actor_id=None):
    from .config import TIMEZONE
    actor=actor_id or current_actor();flow=_owned(pid,flow_id,actor)
    candidate=_read_commit(pid,flow_id,flow.get('candidate_commit'),flow['candidate_digest'])
    published=(_read_commit(pid,flow_id,flow.get('published_commit'),flow['published_digest'])
               if flow.get('published_digest') else None)
    jobs=list_jobs(limit=1,target=flow_id)
    latest=get_run(pid,flow_id,jobs[0]['id'],actor) if jobs and jobs[0].get('project_id')==pid and jobs[0].get('actor_id')==actor else None
    return {k:flow.get(k) for k in ('id','name','candidate_digest','candidate_commit','candidate_run',
             'published_digest','published_commit','published_run','created_at','updated_at','cron','next_due','refresh_error',
             'schedule_uncertain_run','schedule_resume_required')}|{
             'candidate_document':candidate,'published_document':published,'latest_run':latest,
             'timezone':TIMEZONE,'schedule_source_bindings':flow.get('schedule_source_bindings') or {},
             'notice':'流程说明是声明式定义，运行成功不等于财经结果已核验。'}


def list_views(pid,actor_id=None):
    actor=actor_id or current_actor();assert_project_live(pid)
    return {'items':[{k:f.get(k) for k in ('id','name','candidate_digest','candidate_commit','candidate_run',
                         'published_digest','published_commit','published_run','updated_at')}
                     for f in all_items('business_flow') if f.get('project_id')==pid and f.get('actor_id')==actor and not f.get('archived')]}


def input_sources(pid,flow_id,actor_id=None,use_published=False):
    actor=actor_id or current_actor();flow=_owned(pid,flow_id,actor)
    tid=flow.get('published_turn_id') if use_published else flow['turn_id']
    turn=_turn(pid,actor,tid)
    from . import agent_context,agent_gateway,agent_team
    agent_gateway.assert_turn_allowed(turn)
    if 'read_source_table' not in agent_context.allowed(turn):raise ValueError('当前任务无权读取项目资料')
    result=[]
    for source in all_items('source'):
        if (source.get('project_id')!=pid or source.get('kind')!='file' or source.get('status')!='ready'
                or source.get('archived') or type(source.get('revision')) is not int
                or not re.fullmatch(r'[a-f0-9]{64}',source.get('content_hash') or '')):continue
        try:agent_team.assert_turn_allowed(turn,'read_source_table',{'source_id':source['id']})
        except ValueError:continue
        result.append({k:source.get(k) for k in ('id','name','revision','content_hash')})
    return {'items':result}


def _uncertain(job,state):
    if state.get('status')=='unknown':return True
    # Cancellation or lost worker can leave a non-replay-safe action in
    # progress without a durable result; a terminal job is not proof it failed.
    return job.get('status') in ('failed','cancelled') and any(
        isinstance(step,dict) and step.get('status')=='running' and step.get('replay_safe') is not True
        for step in state.get('steps',[]))



def _checkpoint_sha(state):
    return digest(state)


def _record_id(prefix, job, state):
    return prefix+hashlib.sha256((job['id']+'\0'+job['actor_id']+'\0'+_checkpoint_sha(state)).encode()).hexdigest()


def _saved_review(job,state):
    try:
        record=get('business_flow_review',_record_id('bfr_',job,state))
        if record.get('project_id')!=job['project_id'] or record.get('actor_id')!=job['actor_id'] or record.get('run_id')!=job['id']:return None
        return record
    except KeyError:return None


def _review_items(pid,state):
    """Expose only evidence metadata, never source rows or raw cell content."""
    from .project_data import source_requires_fact_alignment
    result=[];blocked=None
    for step in state.get('steps',[]):
        if not isinstance(step,dict):continue
        output=step.get('output')
        if not isinstance(output,dict):continue
        sid=output.get('source_id')
        if isinstance(sid,str):
            try:
                source=get('source',sid)
                if (source.get('project_id')!=pid or source.get('revision')!=output.get('revision')
                    or source.get('content_hash')!=output.get('content_hash') or source.get('archived')):
                    blocked='资料版本已变化，请重新运行并核对';continue
                if source_requires_fact_alignment(source,output.get('evidence')):
                    blocked='资料存在待核对的事实语义，当前不能通过通用复核发布'
            except KeyError:blocked='资料已不可用，请重新运行并核对'
        kinds=[name for name,field in [('source','requires_source_review'),('semantic','requires_semantic_review')]
               if output.get(field) is True]
        if not kinds:continue
        proof=output.get('evidence') if isinstance(output.get('evidence'),dict) else {}
        reasons=proof.get('semantic_review_reasons') or []
        reasons=[x for x in reasons if isinstance(x,str) and re.fullmatch(r'[A-Z0-9_]{1,100}',x)][:30] if isinstance(reasons,list) else []
        result.append({'node_id':step.get('node_id'),'title':step.get('title'),'kinds':kinds,
                       'input_sha256':step.get('input_sha256'),'output_sha256':step.get('output_sha256'),
                       'source_id':sid,'revision':output.get('revision'),'content_hash':output.get('content_hash'),
                       'reasons':reasons})
    return result,blocked



def _verify_completed_state(pid,job,state,*,verify_evidence=True,page_request=None):
    """Review the actual immutable receipts, never just a success flag."""
    if state.get('status')!='succeeded' or state.get('flow_sha256')!=job.get('commit'):
        raise ValueError('流程执行回执不完整')
    doc=BusinessFlow.model_validate(_read_commit(pid,job['target'],job['project_commit'],job['commit']))
    payload=unseal(job['secret_flow_payload'])
    if state.get('inputs_sha256')!=digest(payload.get('inputs')):raise ValueError('运行输入回执不一致')
    steps=state.get('steps')
    if not isinstance(steps,list) or len(steps)!=len(doc.nodes):raise ValueError('流程步骤回执不完整')
    outputs={};requested_page=None
    for node,step in zip(doc.nodes,steps):
        if (not isinstance(step,dict) or step.get('node_id')!=node.id or step.get('kind')!=node.kind
            or step.get('status')!='succeeded' or digest(step.get('output'))!=step.get('output_sha256')):
            raise ValueError('流程步骤回执不一致')
        output=step['output']
        if isinstance(output,dict) and 'rows_ref' in output:
            # The JSON reference alone is not evidence the encrypted table is
            # still present or belongs to this run. Verify the complete data.
            from .business_flow_data import verify_table_snapshot,get_table_page
            context={'project_id':pid,'actor_id':job['actor_id'],'turn_id':job['flow_turn_id'],
                     'originating_turn_id':job['flow_turn_id'],'run_id':job['id']}
            if page_request is not None and node.id==page_request[0]:
                ref=output.get('rows_ref')
                if not isinstance(ref,dict) or ref.get('version')!=2:
                    raise ValueError('所请求的数据快照不支持分页校验')
                # Page reading itself authenticates the full ciphertext, index,
                # and requested blocks, so do not rescan the same file. All
                # other tables still receive full-snapshot verification.
                requested_page=get_table_page(context,output,page_request[1],page_request[2])
            else:verify_table_snapshot(context,output)
        if verify_evidence and node.kind not in ('quality_gate','human_review') and isinstance(output,dict) and 'evidence_tables' in output:
            entries=output['evidence_tables']
            if not isinstance(entries,list) or not entries or len(entries)>30:
                raise ValueError('换算明细回执不完整')
            _fx_evidence_plan(job,node.id,output)
            from .business_flow_data import verify_table_snapshot
            context={'project_id':pid,'actor_id':job['actor_id'],'turn_id':job['flow_turn_id'],
                     'originating_turn_id':job['flow_turn_id'],'run_id':job['id']}
            for entry in entries:
                if not isinstance(entry,dict):raise ValueError('换算明细回执无效')
                verified=_fx_evidence(output,entry.get('stage_id'))
                verify_table_snapshot(context,{**verified,'kind':'table_workflow_v1'})
        if verify_evidence and node.kind not in ('quality_gate','human_review') and isinstance(output,dict) and 'contribution_evidence' in output:
            entry,_=_aggregate_contribution(job,node.id,output)
            from .business_flow_data import verify_table_snapshot
            context={'project_id':pid,'actor_id':job['actor_id'],'turn_id':job['flow_turn_id'],
                     'originating_turn_id':job['flow_turn_id'],'run_id':job['id']}
            verify_table_snapshot(context,{**entry,'kind':'table_workflow_v1'})
        outputs[node.id]=output
    if state.get('outputs')!={name:outputs[name] for name in doc.outputs}:
        raise ValueError('流程最终结果回执不一致')
    if page_request is not None and requested_page is None:
        raise ValueError('所请求的数据快照不存在')
    return requested_page


def _verify_partial_table(pid,job,state,node_id,offset,limit):
    """Verify a durable completed prefix of an unsuccessful, terminal run.

    This authorizes inspection only, never publication or replay. Inputs,
    execution handler versions, each prior receipt and its referenced snapshot
    are bound to the frozen document and the originating run.
    """
    if job.get('status') not in ('failed','cancelled','awaiting_review') or state.get('status') not in ('failed','unknown','running','awaiting_review'):
        raise ValueError('该运行状态不支持查看部分结果')
    if state.get('version')!=1 or state.get('flow_sha256')!=job.get('commit'):
        raise ValueError('流程执行回执与版本不一致')
    doc=BusinessFlow.model_validate(_read_commit(pid,job['target'],job['project_commit'],job['commit']))
    payload=unseal(job['secret_flow_payload']);inputs=payload.get('inputs')
    if not isinstance(inputs,dict) or state.get('inputs_sha256')!=digest(inputs,2*1024*1024):
        raise ValueError('运行输入回执不一致')
    steps=state.get('steps')
    if not isinstance(steps,list) or len(steps)>len(doc.nodes):raise ValueError('流程步骤回执不完整')
    from .business_flow_data import verify_table_snapshot,get_table_page
    context={'project_id':pid,'actor_id':job['actor_id'],'turn_id':job['flow_turn_id'],
             'originating_turn_id':job['flow_turn_id'],'run_id':job['id']}
    handlers=_handlers();values={}
    for node,step in zip(doc.nodes,steps):
        handler=handlers[node.kind]
        try:arguments={key:(inputs[ref.name] if ref.source=='input' else values[ref.name]) for key,ref in node.inputs.items()}
        except KeyError:raise ValueError('步骤输入依赖不完整') from None
        if (not isinstance(step,dict) or step.get('node_id')!=node.id or step.get('kind')!=node.kind
            or step.get('status')!='succeeded' or step.get('deterministic') is not handler.deterministic
            or step.get('replay_safe') is not handler.replay_safe or step.get('handler_version')!=handler.version
            or step.get('input_sha256')!=digest({'config':node.config,'inputs':arguments},2*1024*1024)
            or step.get('output_sha256')!=digest(step.get('output'))):
            raise ValueError('步骤回执与冻结版本不一致')
        output=step['output'];values[node.id]=output
        if isinstance(output,dict) and 'rows_ref' in output:
            ref=output['rows_ref']
            if node.id==node_id and isinstance(ref,dict) and ref.get('version')==2:
                return get_table_page(context,output,offset,limit)
            verify_table_snapshot(context,output)
        if node.id==node_id:return None
    raise ValueError('该步骤没有已完成的结果')

def _review_view(pid,job,state):
    if job.get('status')!='succeeded' or state.get('status')!='succeeded':return None
    items,blocked=_review_items(pid,state)
    record=_saved_review(job,state)
    return {'required':bool(items),'checkpoint_sha256':_checkpoint_sha(state),'items':items,
            'approved':bool(record),'blocked_reason':blocked}


def review_run(pid,flow_id,run_id,actor_id,expected_checkpoint_sha256,items):
    with workspace.lock(pid):
        flow=_owned(pid,flow_id,actor_id);job=get_job(run_id)
        if (job.get('kind')!='business_flow' or job.get('project_id')!=pid or job.get('target')!=flow_id
            or job.get('actor_id')!=actor_id or job.get('status')!='succeeded'):
            raise ValueError('只能复核当前用户已完成的流程运行')
        if (job.get('commit')!=flow.get('candidate_digest') or job.get('project_commit')!=flow.get('candidate_commit')
            or job.get('flow_turn_id')!=flow.get('turn_id')):raise ValueError('候选版本已变化，请重新运行并核对')
        state=unseal(job['secret_flow_checkpoint'])
        if _checkpoint_sha(state)!=expected_checkpoint_sha256:raise ValueError('运行记录已变化，请重新核对')
        _verify_completed_state(pid,job,state)
        required,blocked=_review_items(pid,state)
        if blocked:raise ValueError(blocked)
        if not required:raise ValueError('本次运行没有待核对项')
        expected={(x['node_id'],tuple(sorted(x['kinds']))) for x in required}
        supplied={(x.node_id,tuple(sorted(x.kinds))) for x in items}
        if len(items)!=len(expected) or supplied!=expected:raise ValueError('请逐项核对所有待确认的资料和业务语义')
        value=[x.model_dump(mode='json') for x in items]
        ident=_record_id('bfr_',job,state)
        try:
            old=get('business_flow_review',ident)
            if unseal(old['secret_items'])!=value:raise ValueError('该运行已有不同的复核记录，请重新运行后复核')
        except KeyError:
            put('business_flow_review',{'project_id':pid,'flow_id':flow_id,'run_id':run_id,'actor_id':actor_id,
                'checkpoint_sha256':expected_checkpoint_sha256,'secret_items':seal(value),'reviewed_at':now()},ident)
        return get_run(pid,flow_id,run_id,actor_id)


def _resolution_view(job,state):
    if not _uncertain(job,state):return None
    running=[x for x in state.get('steps',[]) if isinstance(x,dict) and x.get('status') in ('running','unknown') and x.get('replay_safe') is not True]
    prior=[x for x in state.get('steps',[]) if isinstance(x,dict) and x.get('status')=='succeeded' and x.get('replay_safe') is not True]
    allowed=job.get('status') in ('failed','cancelled') and len(running)==1 and not prior
    try:
        saved=get('business_flow_resolution',_record_id('bfu_',job,state))
        confirmed=(saved.get('project_id')==job.get('project_id') and saved.get('actor_id')==job.get('actor_id')
                   and saved.get('run_id')==job.get('id') and saved.get('outcome')=='not_executed')
    except KeyError:confirmed=False
    return {'checkpoint_sha256':_checkpoint_sha(state),'can_confirm_not_executed':allowed,
            'confirmed_not_executed':bool(confirmed and allowed),
            'reason':None if allowed else '无法安全确认完整重跑不会重复其他外部操作，请先由管理员处理原运行'}


def resolve_unknown(pid,flow_id,run_id,actor_id,expected_checkpoint_sha256,outcome,note):
    from .config import DATA
    from .locks import FileLock
    with FileLock(str(DATA/('business-flow-run-'+flow_id+'.lock')),timeout=10):
        _owned(pid,flow_id,actor_id);job=get_job(run_id)
        if (job.get('kind')!='business_flow' or job.get('project_id')!=pid or job.get('target')!=flow_id or job.get('actor_id')!=actor_id):
            raise ValueError('运行记录不属于当前用户')
        state=unseal(job['secret_flow_checkpoint']) if job.get('secret_flow_checkpoint') else {}
        view=_resolution_view(job,state)
        if not view or view['checkpoint_sha256']!=expected_checkpoint_sha256:raise ValueError('运行记录已变化，请重新核对')
        if outcome!='not_executed' or not view['can_confirm_not_executed']:raise ValueError('该运行不能安全重新发起，请先由管理员核对')
        ident=_record_id('bfu_',job,state)
        try:
            old=get('business_flow_resolution',ident)
            if unseal(old['secret_note'])!=note:raise ValueError('该运行已有不同的核对记录')
        except KeyError:
            put('business_flow_resolution',{'project_id':pid,'flow_id':flow_id,'run_id':run_id,'actor_id':actor_id,
                'checkpoint_sha256':expected_checkpoint_sha256,'outcome':outcome,'secret_note':seal(note),'reviewed_at':now()},ident)
    return get_run(pid,flow_id,run_id,actor_id)

def submit_run(pid,flow_id,actor_id,request_id,expected_digest,inputs,use_published=False,schedule_snapshot=None):
    from .business_flow_engine import _prepare
    flow=_owned(pid,flow_id,actor_id)
    if flow.get('schedule_uncertain_run') or flow.get('schedule_uncertain_runs'):
        raise ValueError('上次自动更新的外部操作状态待核对，请先记录核对结论')
    tid=flow.get('published_turn_id') if use_published else flow['turn_id']
    turn=_turn(pid,actor_id,tid)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',request_id or ''):raise ValueError('请求标识无效')
    prefix='published' if use_published else 'candidate'
    sha=flow.get(prefix+'_digest');commit=flow.get(prefix+'_commit')
    if not sha or sha!=expected_digest:raise ValueError('业务流程版本已变化，请重新查看')
    doc=_read_commit(pid,flow_id,commit,sha)
    try:locked=json.loads(workspace.git_content(workspace.folder(pid),commit,'files/'+_lock_path(flow_id)))
    except Exception:raise ValueError('该流程版本缺少执行环境锁，不能运行') from None
    if locked!=_runtime_lock(doc):raise ValueError('流程执行器或依赖已变化，请重新验证并生成候选版本')
    with actor_scope(actor_id):
        _validate(pid,actor_id,turn['id'],doc);_prepare(doc,inputs,_handlers())
        from .business_flow_data import SourcePin
        for node in BusinessFlow.model_validate(doc).nodes:
            if node.kind=='source_table' and not node.config.get('source_id'):
                ref=node.inputs.get('source')
                if ref is None or ref.source!='input':raise ValueError('流程资料参数未正确绑定')
                pin=SourcePin.model_validate(inputs.get(ref.name))
                _source_pin(pid,pin.source_id,pin.revision,pin.content_hash)
    payload={'project_id':pid,'actor_id':actor_id,'flow_turn_id':turn['id'],'document':doc,'inputs':inputs,'commit':commit,'digest':sha,'use_published':bool(use_published)}
    key='bf-run:'+hashlib.sha256((pid+'\0'+actor_id+'\0'+request_id).encode()).hexdigest()
    from .config import DATA
    from .locks import FileLock
    with FileLock(str(DATA/('business-flow-run-'+flow_id+'.lock')),timeout=10):
        previous=list_jobs(limit=1,target=flow_id)
        if previous and previous[0].get('scheduled_key')==key:
            previous_payload=unseal(previous[0].get('secret_flow_payload',''))
            if previous_payload!=payload:raise ValueError('同一请求标识已用于其他流程或输入')
            return {'run_id':previous[0]['id'],'status':previous[0]['status'],'plan_digest':sha,'commit':commit}
        if previous and previous[0].get('status') in ('queued','running','retrying','awaiting_review'):
            raise ValueError('该流程已有运行正在处理，请等待完成后再发起')
        if previous and previous[0].get('secret_flow_checkpoint'):
            state=unseal(previous[0]['secret_flow_checkpoint'])
            if _uncertain(previous[0],state) and not (_resolution_view(previous[0],state) or {}).get('confirmed_not_executed'):
                raise ValueError('上次流程操作结果未知，请先核对外部状态；不能自动重复')
        job=enqueue('business_flow',flow_id,commit=sha,scheduled_key=key,max_attempts=2,
                    extra={'project_id':pid,'actor_id':actor_id,'flow_turn_id':turn['id'],'project_commit':commit,'secret_flow_payload':seal(payload),'schedule_snapshot':schedule_snapshot})
    if (job.get('target')!=flow_id or job.get('commit')!=sha or job.get('actor_id')!=actor_id or
            unseal(job.get('secret_flow_payload',''))!=payload):
        raise ValueError('同一请求标识已用于其他流程或输入')
    return {'run_id':job['id'],'status':job['status'],'plan_digest':sha,'commit':commit}


def get_run(pid,flow_id,run_id,actor_id=None):
    actor=actor_id or current_actor();_owned(pid,flow_id,actor);job=get_job(run_id)
    if job.get('project_id')!=pid or job.get('target')!=flow_id or job.get('kind')!='business_flow' or job.get('actor_id')!=actor:
        raise ValueError('运行记录不属于当前项目或使用者')
    state=unseal(job['secret_flow_checkpoint']) if job.get('secret_flow_checkpoint') else {}
    steps=[];artifacts=[]
    for item in state.get('steps',[]):
        steps.append({k:item.get(k) for k in ('node_id','title','status','started_at','finished_at','input_sha256','output_sha256','error_code','error_message')})
        if item.get('kind')=='human_review' and item.get('status')=='succeeded' and isinstance(item.get('approval'),dict) and item['approval'].get('approved') is True:
            steps[-1]['human_review']={k:item['approval'].get(k) for k in ('note','approved_at','checkpoint_sha256')}
        output=item.get('output')
        table_status=(job.get('status')=='succeeded' or (job.get('status') in ('failed','cancelled','awaiting_review') and state.get('status') in ('failed','unknown','running','awaiting_review')))
        steps[-1]['table_available']=bool(table_status and item.get('status')=='succeeded' and
                                         isinstance(output,dict) and output.get('kind')=='table_workflow_v1' and output.get('complete') is True)
        if steps[-1]['table_available'] and job.get('status')=='succeeded':
            try:
                entries=output.get('evidence_tables',[])
                if not isinstance(entries,list) or len(entries)>30 or any(not isinstance(entry,dict) for entry in entries):raise ValueError('明细记录无效')
                steps[-1]['evidence_stages']=[{'stage_id':entry['stage_id'],'row_count':entry['row_count']}
                    for entry in entries if _fx_evidence(output,entry.get('stage_id')) is entry]
            except (KeyError,TypeError,ValueError):steps[-1]['evidence_stages']=[]
            try:
                _aggregate_contribution(job,item['node_id'],output)
                steps[-1]['contribution_available']=True
            except (KeyError,TypeError,ValueError):steps[-1]['contribution_available']=False
        if isinstance(output,dict) and isinstance(output.get('artifact_id'),str):
            from .workspace_artifacts import _record,_public
            # An artifact link is returned only if separately authorized and present.
            try:
                artifact=_public(_record(pid,output['artifact_id']))
                artifacts.append({k:artifact.get(k) for k in ('id','name','preview_url','download_url')})
            except Exception:pass
    status='unknown' if _uncertain(job,state) else job['status']
    messages={'BUSINESS_RUNTIME_CHANGED':'流程执行器或依赖已变化，请重新验证并生成候选版本',
              'BUSINESS_RUNTIME_LOCK_MISSING':'该流程版本缺少执行环境锁，不能运行',
              'WORKER_LOST':'执行服务中断，已有回执已保存'}
    waiting=next((s for s in steps if s.get('status')=='awaiting_review'),None)
    checkpoint_review={'node_id':waiting['node_id'],'checkpoint_sha256':_checkpoint_sha(state)} if status=='awaiting_review' and waiting else None
    if checkpoint_review:
        document=_read_commit(pid,flow_id,job['project_commit'],job['commit'])
        definition=next((n for n in document['nodes'] if n['id']==waiting['node_id'] and n['kind']=='human_review'),None)
        if not definition:raise ValueError('人工核对节点与流程版本不一致')
        checkpoint_review.update(instructions=definition['config']['instructions'],input_node=definition['inputs']['table']['name'])
    return {'id':job['id'],'flow_id':flow_id,'status':status,'plan_digest':job['commit'],
            'commit':job.get('project_commit'),'steps':steps,'artifacts':artifacts,'created_at':job.get('created_at'),
            'finished_at':job.get('finished_at'),'message':job.get('public_error') or messages.get(job.get('error_code')),'financial_verified':False,
            'review':_review_view(pid,job,state),'unknown_resolution':_resolution_view(job,state),'human_checkpoint':checkpoint_review}


def get_run_table(pid,flow_id,run_id,node_id,offset=0,limit=50):
    """Page an actual completed table, with run identity and integrity checks.

    Access is limited to the flow owner. Intermediate AI content and raw source
    tables are not exposed through this endpoint. Snapshot decoding currently
    materializes the table; this is not a streaming or large-concurrency claim.
    """
    actor=current_actor();_owned(pid,flow_id,actor);job=get_job(run_id)
    if (job.get('project_id')!=pid or job.get('target')!=flow_id or job.get('kind')!='business_flow'
            or job.get('actor_id')!=actor or job.get('status') not in ('succeeded','failed','cancelled','awaiting_review')):
        raise ValueError('运行记录不属于当前项目或尚未完成')
    turn=_turn(pid,actor,job.get('flow_turn_id'))
    from . import agent_gateway,agent_team
    agent_gateway.assert_turn_allowed(turn)
    agent_team.assert_turn_allowed(turn,'run_data_workflow',{})
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',node_id or ''):
        raise ValueError('步骤标识无效')
    if type(offset) is not int or type(limit) is not int or offset<0 or not 1<=limit<=100:
        raise ValueError('分页参数无效')
    state=unseal(job['secret_flow_checkpoint'])
    if not isinstance(state,dict) or not isinstance(state.get('steps'),list):
        raise ValueError('流程步骤回执不完整')
    match=[item for item in state['steps'] if isinstance(item,dict) and item.get('node_id')==node_id]
    output=match[0].get('output') if len(match)==1 else None
    if (len(match)!=1 or match[0].get('status')!='succeeded' or not isinstance(output,dict) or output.get('kind')!='table_workflow_v1' or output.get('complete') is not True):
        raise ValueError('该步骤没有完整可查看的数据结果')
    from .business_flow_data import get_table_page,materialize_table
    from .data_workflows import TableSchema, digest as table_digest
    context={'project_id':pid,'actor_id':actor,'turn_id':job['flow_turn_id'],
             'originating_turn_id':job['flow_turn_id'],'run_id':job['id']}
    schema=output.get('schema');parsed=TableSchema.model_validate(schema)
    ref=output.get('rows_ref')
    if isinstance(ref,dict) and ref.get('version')==2:
        # v2 binds an authenticated index and full ciphertext hash to the
        # receipt. Only pages intersecting this request are decrypted.
        total=output.get('row_count')
        if type(total) is not int or total<0:raise ValueError('数据结果行数无效')
        page=(_verify_completed_state(pid,job,state,verify_evidence=False,page_request=(node_id,offset,limit))
              if job['status']=='succeeded' else _verify_partial_table(pid,job,state,node_id,offset,limit))
    else:
        # Inline and legacy v1 records still need full data verification.
        if job['status']=='succeeded':_verify_completed_state(pid,job,state,verify_evidence=False)
        else:_verify_partial_table(pid,job,state,node_id,offset,limit)
        table=materialize_table(context,output);rows=table.get('rows')
        if (not isinstance(rows,list) or type(table.get('row_count')) is not int or table['row_count']!=len(rows)
                or table.get('output_sha256')!=table_digest({'schema':schema,'rows':rows})):
            raise ValueError('完整数据结果与回执不一致')
        total=len(rows);page=rows[offset:offset+limit]
    if len(json.dumps(page,ensure_ascii=False).encode('utf-8'))>1024*1024:
        raise ValueError('本页数据过大，请减少每页行数')
    return {'run_id':run_id,'node_id':node_id,'columns':[{'name':c.name,'label':c.label or c.name,'unit':c.unit} for c in parsed.columns],
            'rows':page,'total':total,'offset':offset,'next_offset':offset+len(page) if offset+len(page)<total else None,
            'output_sha256':output['output_sha256'],'financial_verified':False,
            'notice':('展示已完成步骤的实际结果；整体流程未完成，财务口径和数据仍需业务复核。' if job['status']!='succeeded'
                      else '展示实际完整结果的分页；财务口径和数据仍需业务复核。')}


def _fx_evidence(output,stage_id):
    """Validate that this detail is an ancestor of this particular output."""
    if not isinstance(stage_id,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',stage_id):
        raise ValueError('明细步骤标识无效')
    entries=output.get('evidence_tables');receipts=output.get('stage_receipts')
    if not isinstance(entries,list) or len(entries)>30 or not isinstance(receipts,list) or len(receipts)>30:
        raise ValueError('换算明细记录无效')
    matched=[entry for entry in entries if isinstance(entry,dict) and entry.get('stage_id')==stage_id]
    if len(matched)!=1:raise ValueError('该步骤没有可查看的换算明细')
    item=matched[0]
    if set(item)!={'stage_id','kind','schema','row_count','output_sha256','rows_ref'} or item.get('kind')!='fx_convert':
        raise ValueError('换算明细记录无效')
    from .data_workflows import TableSchema,digest as table_digest
    parsed=TableSchema.model_validate(item['schema'])
    if (type(item.get('row_count')) is not int or item['row_count']<0
            or not re.fullmatch(r'[a-f0-9]{64}',str(item.get('output_sha256','')))):
        raise ValueError('换算明细记录无效')
    by_id={}
    for receipt in receipts:
        if not isinstance(receipt,dict) or not isinstance(receipt.get('stage_id'),str) or receipt['stage_id'] in by_id:
            raise ValueError('数据步骤回执无效')
        lineage=receipt.get('lineage')
        if (not isinstance(lineage,dict) or lineage.get('id')!=receipt['stage_id'] or lineage.get('kind')!=receipt.get('kind')
                or table_digest(lineage)!=receipt.get('stage_sha256')):
            raise ValueError('数据步骤内容与回执不一致')
        by_id[receipt['stage_id']]=receipt
    final_id=output.get('workflow_output_stage');final=by_id.get(final_id)
    if not final or final.get('output_sha256')!=output.get('output_sha256'):
        raise ValueError('明细与最终结果不属于同一加工链')
    seen=set();pending=[final_id]
    while pending:
        current=pending.pop()
        if current in seen:continue
        seen.add(current);receipt=by_id.get(current)
        if receipt:
            dependencies=receipt.get('inputs')
            if not isinstance(dependencies,list):raise ValueError('数据步骤依赖无效')
            for dependency in dependencies:
                if not isinstance(dependency,dict) or not isinstance(dependency.get('table'),str):
                    raise ValueError('数据步骤依赖无效')
                name=dependency['table'];previous=by_id.get(name)
                expected=previous.get('output_sha256') if previous else output.get('input_sha256',{}).get(name)
                if (not isinstance(expected,str) or dependency.get('sha256')!=expected
                        or type(dependency.get('rows')) is not int or dependency['rows']<0
                        or previous and dependency['rows']!=previous.get('output_rows')):
                    raise ValueError('数据步骤依赖摘要或行数不一致')
                pending.append(name)
    receipt=by_id.get(stage_id)
    if (stage_id not in seen or not receipt or receipt.get('kind')!='fx_convert'
            or receipt.get('output_sha256')!=item['output_sha256'] or receipt.get('output_rows')!=item['row_count']
            or receipt.get('schema_sha256')!=table_digest(parsed.model_dump(mode='json'))):
        raise ValueError('换算明细与加工回执不一致')
    return item


def _fx_evidence_plan(job,node_id,output):
    """Rebuild the server-executed plan with the immutable run parameters."""
    from .business_flow_data import TableConfig
    from .data_workflows import WorkflowPlan,_coerce,dependencies_for,digest as table_digest
    document=BusinessFlow.model_validate(_read_commit(job['project_id'],job['target'],job['project_commit'],job['commit']))
    nodes=[node for node in document.nodes if node.id==node_id and node.kind=='table_workflow']
    if len(nodes)!=1:raise ValueError('明细不属于已确认的加工步骤')
    node=nodes[0];cfg=TableConfig.model_validate(node.config)
    plan=cfg.plan.model_dump(mode='json');stages={stage['id']:stage for stage in plan['stages']}
    payload=unseal(job['secret_flow_payload']);inputs=payload.get('inputs')
    if not isinstance(inputs,dict):raise ValueError('冻结运行参数不可用')
    for alias,binding in cfg.parameter_bindings.items():
        ref=node.inputs.get(alias)
        if ref is None or ref.source!='input' or ref.name not in inputs:
            raise ValueError('冻结加工参数不可用')
        stage=stages[binding.stage_id];condition=stage['conditions'][binding.condition_index]
        columns={column.name:column for column in cfg.plan.inputs[stage['input']].columns}
        _coerce(inputs[ref.name],columns[condition['column']]);condition['value']=inputs[ref.name]
    actual=WorkflowPlan.model_validate(plan)
    if output.get('plan_sha256')!=table_digest(actual.model_dump()) or output.get('workflow_output_stage')!=actual.output:
        raise ValueError('换算明细与冻结加工方案不一致')
    receipts=output.get('stage_receipts')
    if not isinstance(receipts,list) or len(receipts)!=len(actual.stages):
        raise ValueError('换算明细缺少完整的加工步骤')
    for stage,receipt in zip(actual.stages,receipts):
        if (not isinstance(receipt,dict) or receipt.get('stage_id')!=stage.id
                or receipt.get('kind')!=stage.kind or receipt.get('lineage')!=stage.model_dump(mode='json')
                or not isinstance(receipt.get('inputs'),list)
                or [item.get('table') if isinstance(item,dict) else None for item in receipt['inputs']]!=dependencies_for(stage)):
            raise ValueError('加工步骤与冻结方案不一致')
    return actual


def _aggregate_contribution(job,node_id,output):
    """Bind an eligible final aggregate's complete input to the frozen plan."""
    from .data_workflows import Aggregate,TableSchema,digest as table_digest
    item=output.get('contribution_evidence')
    if not isinstance(item,dict) or set(item)!={'stage_id','input_id','schema','row_count','output_sha256','rows_ref'}:
        raise ValueError('该结果没有可用的逐笔汇总依据')
    plan=_fx_evidence_plan(job,node_id,output)
    stages={stage.id:stage for stage in plan.stages};stage=stages.get(plan.output)
    if (not isinstance(stage,Aggregate) or item['stage_id']!=stage.id or item['input_id']!=stage.input
            or any(a.function!='sum' and not (a.function=='count' and a.column is None)
                   for a in stage.aggregations)):
        raise ValueError('该汇总规则不支持逐笔贡献查看')
    receipts=output.get('stage_receipts');receipt=next((r for r in receipts if r.get('stage_id')==stage.id),None)
    if (not isinstance(receipt,dict) or receipt.get('output_sha256')!=output.get('output_sha256')
            or receipt.get('output_rows')!=output.get('row_count') or len(receipt.get('inputs',[]))!=1):
        raise ValueError('汇总结果与执行回执不一致')
    dep=receipt['inputs'][0]
    previous=next((r for r in receipts if r.get('stage_id')==stage.input),None)
    expected_hash=previous.get('output_sha256') if previous else output.get('input_sha256',{}).get(stage.input)
    expected_count=previous.get('output_rows') if previous else dep.get('rows')
    expected_schema=previous.get('schema_sha256') if previous else table_digest(plan.inputs[stage.input].model_dump(mode='json'))
    schema=TableSchema.model_validate(item['schema'])
    if (type(item.get('row_count')) is not int or item['row_count']<0 or dep.get('table')!=stage.input
            or dep.get('sha256')!=expected_hash or item.get('output_sha256')!=expected_hash
            or dep.get('rows')!=expected_count or item['row_count']!=expected_count
            or table_digest(schema.model_dump(mode='json'))!=expected_schema):
        raise ValueError('汇总输入与冻结步骤回执不一致')
    return item,stage


def _matching_aggregate_rows(rows,group,schema,stage):
    from .data_workflows import _coerce
    fields={c.name:c for c in schema.columns}
    wanted=tuple(_coerce(group[name],fields[name]) for name in stage.by)
    return [row for row in rows if tuple(_coerce(row[name],fields[name]) for name in stage.by)==wanted]


def get_run_contributions(pid,flow_id,run_id,node_id,row_index,offset=0,limit=50):
    """Return rows actually consumed by this final sum/COUNT(*) aggregate group."""
    if type(row_index) is not int or row_index<0 or type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
        raise ValueError('逐笔依据分页参数无效')
    # This also validates project/user/turn scope and the completed result.
    output_page=get_run_table(pid,flow_id,run_id,node_id,offset=row_index,limit=1)
    if row_index>=output_page['total'] or len(output_page['rows'])!=1:raise ValueError('汇总结果行不存在')
    actor=current_actor();job=get_job(run_id);state=unseal(job['secret_flow_checkpoint'])
    steps=[step for step in state['steps'] if step.get('node_id')==node_id]
    output=steps[0]['output'] if len(steps)==1 else None
    if not isinstance(output,dict):raise ValueError('汇总结果不存在')
    entry,stage=_aggregate_contribution(job,node_id,output)
    from .business_flow_data import materialize_table
    from .data_workflows import TableSchema,digest as table_digest
    context={'project_id':pid,'actor_id':actor,'turn_id':job['flow_turn_id'],
             'originating_turn_id':job['flow_turn_id'],'run_id':job['id']}
    table=materialize_table(context,{**entry,'kind':'table_workflow_v1'});rows=table.get('rows')
    if (not isinstance(rows,list) or len(rows)!=entry['row_count']
            or table_digest({'schema':entry['schema'],'rows':rows})!=entry['output_sha256']):
        raise ValueError('汇总输入快照与执行回执不一致')
    schema=TableSchema.model_validate(entry['schema'])
    result_row=output_page['rows'][0]
    group={name:result_row[name] for name in stage.by}
    matched=_matching_aggregate_rows(rows,group,schema,stage)
    page=matched[offset:offset+limit]
    if len(json.dumps(page,ensure_ascii=False).encode('utf-8'))>1024*1024:
        raise ValueError('本页数据过大，请减少每页行数')
    return {'run_id':run_id,'node_id':node_id,'row_index':row_index,
            'columns':[{'name':col.name,'label':col.label or col.name,'unit':col.unit} for col in schema.columns],
            'rows':page,'total':len(matched),'offset':offset,
            'next_offset':offset+len(page) if offset+len(page)<len(matched) else None,
            'group':group,'aggregations':[a.model_dump(mode='json') for a in stage.aggregations],
            'input_sha256':entry['output_sha256'],'output_sha256':output['output_sha256'],
            'financial_verified':False,
            'notice':'这些行是本次汇总步骤的实际同组输入；仅支持求和及逐行计数。业务口径和原始资料仍需复核，后续其他流程步骤不在此范围。'}


def get_run_fx_evidence(pid,flow_id,run_id,node_id,stage_id,offset=0,limit=50):
    """Read actual pre-aggregation FX rows for the exact authenticated run."""
    actor=current_actor();_owned(pid,flow_id,actor);job=get_job(run_id)
    if (job.get('project_id')!=pid or job.get('target')!=flow_id or job.get('kind')!='business_flow'
            or job.get('actor_id')!=actor or job.get('status')!='succeeded'):
        raise ValueError('运行记录不属于当前项目或尚未完成')
    turn=_turn(pid,actor,job.get('flow_turn_id'))
    from . import agent_gateway,agent_team
    agent_gateway.assert_turn_allowed(turn);agent_team.assert_turn_allowed(turn,'run_data_workflow',{})
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',node_id or ''):
        raise ValueError('步骤标识无效')
    if type(offset) is not int or type(limit) is not int or offset<0 or not 1<=limit<=100:
        raise ValueError('分页参数无效')
    state=unseal(job['secret_flow_checkpoint']);_verify_completed_state(pid,job,state,verify_evidence=False)
    steps=[step for step in state['steps'] if step.get('node_id')==node_id]
    output=steps[0].get('output') if len(steps)==1 else None
    if not isinstance(output,dict) or output.get('kind')!='table_workflow_v1' or output.get('complete') is not True:
        raise ValueError('该步骤没有完整数据结果')
    _fx_evidence_plan(job,node_id,output)
    entry=_fx_evidence(output,stage_id)
    from .business_flow_data import get_table_page,materialize_table
    from .data_workflows import TableSchema,digest as table_digest
    context={'project_id':pid,'actor_id':actor,'turn_id':job['flow_turn_id'],
             'originating_turn_id':job['flow_turn_id'],'run_id':job['id']}
    table={**entry,'kind':'table_workflow_v1'}
    ref=entry.get('rows_ref')
    if isinstance(ref,dict) and ref.get('version')==2:
        page=get_table_page(context,table,offset,limit)
    else:
        full=materialize_table(context,table);rows=full.get('rows')
        if (not isinstance(rows,list) or len(rows)!=entry['row_count']
                or table_digest({'schema':entry['schema'],'rows':rows})!=entry['output_sha256']):
            raise ValueError('换算明细与回执不一致')
        page=rows[offset:offset+limit]
    if len(json.dumps(page,ensure_ascii=False).encode('utf-8'))>1024*1024:
        raise ValueError('本页数据过大，请减少每页行数')
    parsed=TableSchema.model_validate(entry['schema']);total=entry['row_count']
    return {'run_id':run_id,'node_id':node_id,'stage_id':stage_id,
            'columns':[{'name':col.name,'label':col.label or col.name,'unit':col.unit} for col in parsed.columns],
            'rows':page,'total':total,'offset':offset,'next_offset':offset+len(page) if offset+len(page)<total else None,
            'output_sha256':entry['output_sha256'],'final_output_sha256':output['output_sha256'],
            'financial_verified':False,'notice':'本次运行中该步骤的完整逐笔换算明细；后续筛选、计算或汇总可能改变结果，不是对某一汇总行的逐笔归因，业务口径仍需核对。'}


def get_run_reconciliation(pid,flow_id,run_id):
    """Read technical checks from the exact authorized completed run."""
    actor=current_actor();_owned(pid,flow_id,actor);job=get_job(run_id)
    if (job.get('project_id')!=pid or job.get('target')!=flow_id or job.get('kind')!='business_flow'
            or job.get('actor_id')!=actor or job.get('status')!='succeeded'):
        raise ValueError('运行记录不属于当前项目或尚未完成')
    turn=_turn(pid,actor,job.get('flow_turn_id'))
    from . import agent_gateway,agent_team
    agent_gateway.assert_turn_allowed(turn);agent_team.assert_turn_allowed(turn,'run_data_workflow',{})
    state=unseal(job['secret_flow_checkpoint']);_verify_completed_state(pid,job,state)
    nodes=[];sources=[]
    for step in state['steps']:
        output=step.get('output')
        if not isinstance(output,dict):continue
        if output.get('kind')=='source_table_v1':
            source={key:output.get(key) for key in ('source_id','revision','content_hash')}
            try:
                current=get('source',source['source_id'])
                if current.get('project_id')==pid:source['name']=current.get('name')
            except KeyError:pass
            sources.append(source)
        if output.get('kind')!='table_workflow_v1' or step.get('kind') in ('quality_gate','human_review'):continue
        stages=[]
        for receipt in output.get('stage_receipts',[]):
            check=receipt.get('reconciliation') if isinstance(receipt,dict) else None
            if isinstance(check,dict):
                stages.append({'stage_id':receipt.get('stage_id'),'kind':receipt.get('kind'),
                    'inputs':receipt.get('inputs'),'output_sha256':receipt.get('output_sha256'),**check})
        if stages:nodes.append({'node_id':step.get('node_id'),'title':step.get('title'),
                                'plan_sha256':output.get('plan_sha256'),'stages':stages})
    return {'run_id':run_id,'flow_id':flow_id,'plan_digest':job['commit'],'sources':sources,'nodes':nodes,
            'financial_verified':False,'notice':'仅显示实际运行中已记录的筛选技术对账；来源、口径和业务结论仍需人工核对。'}


def request_status(pid,flow_id,request_id,actor_id=None):
    actor=actor_id or current_actor();_owned(pid,flow_id,actor)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',request_id or ''):raise ValueError('请求标识无效')
    key='bf-run:'+hashlib.sha256((pid+'\0'+actor+'\0'+request_id).encode()).hexdigest()
    from sqlalchemy import select
    from .jobs import jobs,render
    from .store import engine
    with engine.connect() as connection:
        row=connection.execute(select(jobs).where(jobs.c.dedup_key==key)).mappings().first()
    # Absence is not proof of rejection: the original HTTP request may still
    # be entering admission. The client must keep the same ID and inputs.
    if not row:return {'accepted':None}
    job=render(row)
    if job.get('kind')!='business_flow' or job.get('target')!=flow_id or job.get('project_id')!=pid or job.get('actor_id')!=actor:
        raise ValueError('请求标识不属于该流程')
    return {'accepted':True,'run_id':job['id'],'status':job['status']}


def cancel_run(pid,flow_id,run_id,actor_id=None):
    actor=actor_id or current_actor();_owned(pid,flow_id,actor);job=get_job(run_id)
    if job.get('kind')!='business_flow' or job.get('target')!=flow_id or job.get('project_id')!=pid or job.get('actor_id')!=actor:
        raise ValueError('运行记录不属于当前项目或使用者')
    from .jobs import request_cancel
    if job.get('status') in ('queued','running','retrying','awaiting_review'):request_cancel(run_id)
    return get_run(pid,flow_id,run_id,actor)


def publish(pid,flow_id,actor_id,run_id,expected_digest):
    with workspace.lock(pid):
        flow=_owned(pid,flow_id,actor_id);job=get_job(run_id)
        if job.get('project_id')!=pid or job.get('target')!=flow_id or job.get('actor_id')!=actor_id or job.get('status')!='succeeded':
            raise ValueError('只能采用当前项目已成功完成的试运行')
        if (job.get('commit')!=expected_digest or flow.get('candidate_digest')!=expected_digest
                or job.get('project_commit')!=flow.get('candidate_commit') or job.get('flow_turn_id')!=flow.get('turn_id')):
            raise ValueError('候选方案已变化，请重新检查')
        state=unseal(job['secret_flow_checkpoint'])
        _verify_completed_state(pid,job,state)
        pending,blocked=_review_items(pid,state)
        if blocked:raise ValueError(blocked)
        if pending and not _saved_review(job,state):
            raise ValueError('资料解析或财经语义仍需逐项核对，不能直接发布')
        if flow.get('published_run')==run_id:
            # Recover a lost response or crash after updating the formal
            # pointer but before the project checkpoint became durable.
            workspace._checkpoint(pid,flow['turn_id'],'确认业务流程正式版本')
            return get_view(pid,flow_id,actor_id)
        changed=bool(flow.get('published_digest') and flow['published_digest']!=expected_digest)
        schedule_changes=({'cron':None,'next_due':None,'schedule_inputs':None,'schedule_source_bindings':{},
                           'refresh_error':'正式方案已改变，定时已暂停，请核对新参数后重新启用'}
                          if changed and flow.get('cron') else {})
        patch('business_flow',flow_id,published_digest=expected_digest,published_commit=job['project_commit'],published_run=run_id,
              published_turn_id=job['flow_turn_id'],updated_at=now(),**schedule_changes)
        workspace._checkpoint(pid,flow['turn_id'],'确认业务流程正式版本')
    return get_view(pid,flow_id,actor_id)


def set_schedule(pid,flow_id,actor_id,cron,source_bindings,expected_published_digest,parameter_values=None):
    from .config import TIMEZONE,DATA
    from .locks import FileLock
    if cron is not None and (not isinstance(cron,str) or len(cron.split())!=5 or not croniter.is_valid(cron)):
        raise ValueError('需要有效的五段定时表达式')
    with workspace.lock(pid),FileLock(str(DATA/'scheduler.lock'),timeout=10):
        flow=_owned(pid,flow_id,actor_id)
        if flow.get('published_digest')!=expected_published_digest:raise ValueError('正式版本已变化，请重新查看再设置定时')
        if parameter_values is None and flow.get('cron')==cron and (flow.get('schedule_source_bindings') or {})==(source_bindings or {}) and not flow.get('schedule_gap_pending') and not flow.get('refresh_error') and not flow.get('schedule_resume_required'):
            return get_view(pid,flow_id,actor_id)
        if cron and not flow.get('published_run'):raise ValueError('请先确认正式版本，再设置定时运行')
        if cron:
            run=get_job(flow['published_run'])
            if run.get('target')!=flow_id or run.get('project_id')!=pid or run.get('status')!='succeeded':raise ValueError('正式运行记录不可用')
            payload=unseal(run['secret_flow_payload']);doc=_read_commit(pid,flow_id,flow['published_commit'],flow['published_digest'])
            definition=BusinessFlow.model_validate(doc)
            dynamic={node.inputs['source'].name for node in definition.nodes
                     if node.kind=='source_table' and not node.config.get('source_id') and node.inputs.get('source')}
            if not isinstance(source_bindings,dict) or not set(source_bindings)<=dynamic:raise ValueError('定时资料绑定不属于动态资料参数')
            for key,sid in source_bindings.items():
                if not isinstance(sid,str):raise ValueError('定时资料标识无效')
                source=get('source',sid);_source_pin(pid,sid,source.get('revision'),source.get('content_hash'))
            inputs=dict(payload['inputs'])
            if parameter_values is not None:
                if not isinstance(parameter_values,dict) or set(parameter_values)!=set(definition.input_specs or {}):
                    raise ValueError('请明确填写全部定时业务参数')
                inputs.update(parameter_values)
            # Validate against the frozen published definition before saving;
            # decimals remain exact text and inputs never enter a public view.
            from .business_flow_engine import _prepare
            _prepare(doc,inputs,_handlers())
            for key,sid in source_bindings.items():
                source=get('source',sid)
                inputs[key]={'source_id':sid,'revision':source['revision'],'content_hash':source['content_hash']}
            schedule_inputs=seal(inputs)
            next_due=croniter(cron,datetime.now(ZoneInfo(TIMEZONE))).get_next(datetime).astimezone(timezone.utc).isoformat()
        else:schedule_inputs=None;next_due=None;source_bindings={}
        patch('business_flow',flow_id,cron=cron,next_due=next_due,schedule_inputs=schedule_inputs,
              schedule_source_bindings=source_bindings,refresh_error=('上次自动更新结果未知，请核对外部回执并记录结论' if flow.get('schedule_uncertain_run') else None),
              refresh_retry_after=None,schedule_gap_pending=False,schedule_generation=uid(),
              schedule_resume_required=bool(flow.get('schedule_uncertain_run')),updated_at=now())
        workspace._checkpoint(pid,flow['turn_id'],'设置业务流程定时运行')
    return get_view(pid,flow_id,actor_id)


def _patch_schedule_if_current(flow, **updates):
    from sqlalchemy import select,update
    from .store import transaction,items
    fields=('project_id','actor_id','archived','cron','next_due','schedule_generation',
            'published_digest','published_commit','published_turn_id','published_run','refresh_error','schedule_uncertain_run','schedule_resume_required')
    with transaction() as connection:
        row=connection.execute(select(items.c.body).where(items.c.id==flow['id'],
            items.c.kind=='business_flow').with_for_update()).first()
        if not row:return False
        current=json.loads(row[0])
        if any(current.get(field)!=flow.get(field) for field in fields):return False
        current.update(updates)
        connection.execute(update(items).where(items.c.id==flow['id'],items.c.kind=='business_flow')
                           .values(body=json.dumps(current,ensure_ascii=False,default=str)))
        return True


def schedule_parameters(pid,flow_id,actor_id):
    """Explicit owner-only read; values are never included in general flow views."""
    flow=_owned(pid,flow_id,actor_id)
    if not flow.get('cron') or not flow.get('schedule_inputs') or not flow.get('published_digest'):
        raise ValueError('当前流程没有已设置的定时参数')
    doc=BusinessFlow.model_validate(_read_commit(pid,flow_id,flow['published_commit'],flow['published_digest']))
    inputs=unseal(flow['schedule_inputs']);values={}
    if not isinstance(inputs,dict):raise ValueError('定时参数记录无效')
    for name,spec in (doc.input_specs or {}).items():
        if name not in inputs:raise ValueError('定时参数记录不完整')
        spec.validate_value(inputs[name]);values[name]=inputs[name]
    return {'flow_id':flow_id,'published_digest':flow['published_digest'],'parameter_values':values,
            'source_bindings':flow.get('schedule_source_bindings') or {}}


def queue_due():
    from datetime import timedelta
    from .config import TIMEZONE,DATA
    from .locks import FileLock
    # The configuration writer acquires the workspace lock before the global
    # scheduler lock. Use the same order, and refresh each flow under both,
    # so a concurrent publish or schedule edit cannot submit a stale version.
    for snapshot in all_items('business_flow'):
        if not snapshot.get('cron') or snapshot.get('archived') or not snapshot.get('published_digest'):continue
        try:
            with workspace.lock(snapshot['project_id']),FileLock(str(DATA/'scheduler.lock'),timeout=5):
                flow=get('business_flow',snapshot['id'])
                if not flow.get('cron') or flow.get('archived') or not flow.get('published_digest'):continue
                if flow.get('schedule_uncertain_run') or flow.get('schedule_uncertain_runs') or flow.get('schedule_resume_required'):continue
                current=datetime.now(timezone.utc)
                try:
                    retry=flow.get('refresh_retry_after')
                    if retry and datetime.fromisoformat(retry)>current:continue
                    due=datetime.fromisoformat(flow['next_due'])
                    if due.tzinfo is None:raise ValueError('定时配置缺少时区')
                    if due>current:continue
                    actor=flow['actor_id'];inputs=unseal(flow['schedule_inputs'])
                    for key,sid in (flow.get('schedule_source_bindings') or {}).items():
                        source=get('source',sid);_source_pin(flow['project_id'],sid,source.get('revision'),source.get('content_hash'))
                        inputs[key]={'source_id':sid,'revision':source['revision'],'content_hash':source['content_hash']}
                    request_id='scheduled_'+hashlib.sha256((flow['id']+flow['next_due']).encode()).hexdigest()
                    schedule_snapshot={'kind':'business_flow','generation':flow.get('schedule_generation'),'project_id':flow['project_id'],
                                       'actor_id':actor,'published_run':flow.get('published_run'),'published_commit':flow.get('published_commit'),
                                       'published_turn_id':flow.get('published_turn_id')}
                    with actor_scope(actor):
                        admitted=submit_run(flow['project_id'],flow['id'],actor,request_id,flow['published_digest'],inputs,True,schedule_snapshot)
                    # A recovered submission may point to a failed/cancelled
                    # job. It is not an accepted occurrence, and repeating it
                    # blindly may duplicate uncertain external side effects.
                    if admitted.get('status') not in ('queued','running','retrying','succeeded'):
                        raise ValueError('本次计划的历史运行未成功，请先核对执行状态')
                    following=croniter(flow['cron'],due.astimezone(ZoneInfo(TIMEZONE))).get_next(datetime).astimezone(timezone.utc)
                    gap=bool(flow.get('schedule_gap_pending') or following<=current)
                    next_due=croniter(flow['cron'],current.astimezone(ZoneInfo(TIMEZONE))).get_next(datetime).astimezone(timezone.utc).isoformat()
                    _patch_schedule_if_current(flow,next_due=next_due,refresh_retry_after=None,schedule_gap_pending=gap,
                          refresh_error='存在多个未执行的计划时间；本次仅安排一次运行，请核对缺失期间后重新设置定时' if gap else None)
                except Exception:
                    # Do not advance a due occurrence when source validation,
                    # admission, or the time configuration fails.
                    notice=('历史计划缺口尚待核对；' if flow.get('schedule_gap_pending') else '')+'定时运行未启动或历史运行未成功，请核对资料、授权和执行状态；正式成果未变'
                    _patch_schedule_if_current(flow,refresh_error=notice,
                          refresh_retry_after=(current+timedelta(seconds=30)).isoformat())
        except (KeyError,TimeoutError):
            # A removed project or an occupied lock must not block other flows.
            continue


def execute_job(job,folder):
    from .execution import checkpoint,patch_run
    from .provider import task_scope
    from .errors import TaskError
    payload=unseal(job['secret_flow_payload']);pid=job['project_id'];actor=job['actor_id']
    if payload.get('project_id')!=pid or payload.get('actor_id')!=actor or payload.get('digest')!=job['commit'] or payload.get('commit')!=job['project_commit']:
        raise ValueError('冻结的流程运行记录不一致')
    document=_read_commit(pid,job['target'],job['project_commit'],job['commit'])
    if document!=payload['document']:raise ValueError('冻结流程与项目代码不一致')
    try:locked=json.loads(workspace.git_content(workspace.folder(pid),job['project_commit'],'files/'+_lock_path(job['target'])))
    except Exception:raise TaskError('该流程版本缺少执行环境锁，不能运行','BUSINESS_RUNTIME_LOCK_MISSING') from None
    if locked!=_runtime_lock(document):raise TaskError('流程执行器或依赖已变化，请重新验证并生成候选版本','BUSINESS_RUNTIME_CHANGED')
    context={'project_id':pid,'actor_id':actor,'turn_id':job['flow_turn_id'],'originating_turn_id':job['flow_turn_id'],'run_id':job['id']}
    stop=threading.Event()
    def beat():
        while not stop.wait(5):
            try:
                if not heartbeat(job['id'],job['lease_token']):return
            except Exception:return
    thread=threading.Thread(target=beat,daemon=True);thread.start()
    try:
        with actor_scope(actor),task_scope(job['flow_turn_id']):
            _owned(pid,job['target'],actor);_turn(pid,actor,job['flow_turn_id'])
            def persist(state):
                checkpoint(job);patch_run(job,secret_flow_checkpoint=seal(state),stage='正在执行业务流程')
            prior=unseal(job['secret_flow_checkpoint']) if job.get('secret_flow_checkpoint') else None
            result=run_engine(document,payload['inputs'],_handlers(),context,checkpoint=prior,persist=persist)
            checkpoint(job)
            if result.get('status')=='succeeded':
                needs_review=any(isinstance(step.get('output'),dict) and
                    (step['output'].get('requires_source_review') is True or step['output'].get('requires_semantic_review') is True)
                    for step in result.get('steps',[]) if isinstance(step,dict))
                nondeterministic=any(node['kind'] in ('model','skill') for node in document['nodes'])
                if payload['use_published']:
                    # Do not automatically adopt data requiring review or new
                    # non-deterministic model/Skill conclusions.
                    update={'pending_refresh_run':job['id']} if needs_review or nondeterministic else {'published_run':job['id'],'pending_refresh_run':None}
                    target={'kind':'business_flow','expected':{'published_digest':job['commit'],'published_commit':job['project_commit'],
                            'published_turn_id':job['flow_turn_id']},'values':update}
                else:target={'kind':'business_flow','expected':{'candidate_digest':job['commit'],'candidate_commit':job['project_commit'],
                             'turn_id':job['flow_turn_id']},'values':{'candidate_run':job['id']}}
                patch_run(job,status='succeeded',finished_at=now(),stage='完成，等待业务核对',
                          target_patch=target)
            elif result.get('status')=='awaiting_review':
                patch_run(job,status='awaiting_review',stage='等待人工核对')
            else:
                patch_run(job,status='failed',finished_at=now(),stage='流程未完成',public_error='流程步骤未完成或执行状态未知，已有回执已保留')
                if result.get('status')=='failed':
                    # Diagnosis must never change the recorded workflow outcome.
                    try:
                        from .business_flow_repair import enqueue_diagnostic
                        enqueue_diagnostic(job)
                    except Exception:
                        import logging
                        logging.getLogger(__name__).warning('自动诊断未启动；原失败回执已保留')
    finally:stop.set();thread.join(timeout=6)


class CreateInput(StrictModel):
    request_id:str=Field(min_length=1,max_length=100)
    turn_id:str=Field(min_length=1,max_length=100)
    document:BusinessFlow
class RunInput(StrictModel):
    request_id:str=Field(min_length=1,max_length=100)
    expected_digest:str=Field(pattern=r'^[a-f0-9]{64}$')
    inputs:dict=Field(default_factory=dict)
    use_published:bool=False
class UpdateInput(CreateInput):
    expected_digest:str=Field(pattern=r'^[a-f0-9]{64}$')
class PublishInput(StrictModel):
    run_id:str=Field(min_length=1,max_length=64)
    expected_digest:str=Field(pattern=r'^[a-f0-9]{64}$')
class ReviewItem(StrictModel):
    node_id:str=Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,63}$')
    kinds:list[Literal['source','semantic']]=Field(min_length=1,max_length=2)
    note:str=Field(min_length=4,max_length=500)
    @field_validator('note')
    @classmethod
    def substantive_note(cls,value):
        if len(value.strip())<4:raise ValueError('请说明核对依据')
        return value.strip()

class ReviewInput(StrictModel):
    expected_checkpoint_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    items:list[ReviewItem]=Field(min_length=1,max_length=100)

class ResolveInput(StrictModel):
    expected_checkpoint_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    outcome:Literal['not_executed']
    note:str=Field(min_length=4,max_length=500)
    @field_validator('note')
    @classmethod
    def substantive_note(cls,value):
        if len(value.strip())<4:raise ValueError('请说明核对依据')
        return value.strip()

class ScheduleInput(StrictModel):
    cron:str|None=None
    source_bindings:dict[str,str]=Field(default_factory=dict,max_length=50)
    parameter_values:dict|None=None
    expected_published_digest:str=Field(pattern=r'^[a-f0-9]{64}$')
class PinInput(StrictModel):
    turn_id:str=Field(min_length=1,max_length=100)
    kind:Literal['model','skill']
    skill_id:str|None=None


def install(app,auth):
    @app.get('/api/projects/{pid}/business-flows',dependencies=[Depends(auth)])
    def listing(pid:str):return list_views(pid)
    @app.post('/api/projects/{pid}/business-flows',dependencies=[Depends(auth)])
    def create_route(pid:str,value:CreateInput):return create(pid,current_actor(),value.turn_id,value.document.model_dump(mode='json'),value.request_id)
    @app.get('/api/projects/{pid}/business-flows/{fid}/download',dependencies=[Depends(auth)])
    def download_workflow(pid:str,fid:str,expected_digest:str):
        from fastapi.responses import Response
        from .workflow_export import business_bundle
        return Response(business_bundle(pid,fid,expected_digest,current_actor()),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="workflow.zip"','Cache-Control':'no-store'})

    @app.get('/api/projects/{pid}/business-flows/{fid}',dependencies=[Depends(auth)])
    def detail(pid:str,fid:str):return get_view(pid,fid)
    @app.get('/api/projects/{pid}/business-flows/{fid}/input-sources',dependencies=[Depends(auth)])
    def source_options(pid:str,fid:str,use_published:bool=False):return input_sources(pid,fid,use_published=use_published)
    @app.get('/api/projects/{pid}/business-flows/{fid}/storage',dependencies=[Depends(auth)])
    def storage_usage(pid:str,fid:str):
        actor=current_actor();_owned(pid,fid,actor)
        from .business_flow_data import snapshot_storage_usage
        return snapshot_storage_usage(pid,actor)
    @app.put('/api/projects/{pid}/business-flows/{fid}',dependencies=[Depends(auth)])
    def update_route(pid:str,fid:str,value:UpdateInput):return update(pid,fid,current_actor(),value.turn_id,value.document.model_dump(mode='json'),value.request_id,value.expected_digest)
    @app.post('/api/projects/{pid}/business-flows/{fid}/runs',dependencies=[Depends(auth)])
    def start(pid:str,fid:str,value:RunInput):return submit_run(pid,fid,current_actor(),value.request_id,value.expected_digest,value.inputs,value.use_published)
    @app.get('/api/projects/{pid}/business-flows/{fid}/runs/{rid}',dependencies=[Depends(auth)])
    def status(pid:str,fid:str,rid:str):return get_run(pid,fid,rid)
    @app.get('/api/projects/{pid}/business-flows/{fid}/runs/{rid}/tables/{node_id}',dependencies=[Depends(auth)])
    def table(pid:str,fid:str,rid:str,node_id:str,offset:int=0,limit:int=50):return get_run_table(pid,fid,rid,node_id,offset,limit)
    @app.get('/api/projects/{pid}/business-flows/{fid}/runs/{rid}/tables/{node_id}/contributions',dependencies=[Depends(auth)])
    def contributions(pid:str,fid:str,rid:str,node_id:str,row_index:int,offset:int=0,limit:int=50):
        return get_run_contributions(pid,fid,rid,node_id,row_index,offset,limit)
    @app.get('/api/projects/{pid}/business-flows/{fid}/runs/{rid}/tables/{node_id}/evidence/{stage_id}',dependencies=[Depends(auth)])
    def fx_evidence(pid:str,fid:str,rid:str,node_id:str,stage_id:str,offset:int=0,limit:int=50):
        return get_run_fx_evidence(pid,fid,rid,node_id,stage_id,offset,limit)
    @app.get('/api/projects/{pid}/business-flows/{fid}/runs/{rid}/reconciliation',dependencies=[Depends(auth)])
    def reconciliation(pid:str,fid:str,rid:str):return get_run_reconciliation(pid,fid,rid)
    @app.post('/api/projects/{pid}/business-flows/{fid}/runs/{rid}/review',dependencies=[Depends(auth)])
    def review(pid:str,fid:str,rid:str,value:ReviewInput):return review_run(pid,fid,rid,current_actor(),value.expected_checkpoint_sha256,value.items)
    @app.post('/api/projects/{pid}/business-flows/{fid}/runs/{rid}/resolve-unknown',dependencies=[Depends(auth)])
    def resolve(pid:str,fid:str,rid:str,value:ResolveInput):return resolve_unknown(pid,fid,rid,current_actor(),value.expected_checkpoint_sha256,value.outcome,value.note)
    @app.post('/api/projects/{pid}/business-flows/{fid}/runs/{rid}/cancel',dependencies=[Depends(auth)])
    def cancel(pid:str,fid:str,rid:str):return cancel_run(pid,fid,rid)
    @app.get('/api/projects/{pid}/business-flows/{fid}/requests/{request_id}',dependencies=[Depends(auth)])
    def request_status_route(pid:str,fid:str,request_id:str):return request_status(pid,fid,request_id)
    @app.post('/api/projects/{pid}/business-flows/{fid}/publish',dependencies=[Depends(auth)])
    def adopt(pid:str,fid:str,value:PublishInput):return publish(pid,fid,current_actor(),value.run_id,value.expected_digest)
    @app.put('/api/projects/{pid}/business-flows/{fid}/schedule',dependencies=[Depends(auth)])
    def schedule(pid:str,fid:str,value:ScheduleInput):return set_schedule(pid,fid,current_actor(),value.cron,value.source_bindings,value.expected_published_digest,value.parameter_values)
    @app.get('/api/projects/{pid}/business-flows/{fid}/schedule-parameters',dependencies=[Depends(auth)])
    def schedule_values(pid:str,fid:str):return schedule_parameters(pid,fid,current_actor())
    @app.post('/api/projects/{pid}/business-flow-pins',dependencies=[Depends(auth)])
    def pin(pid:str,value:PinInput):
        from .workflow_model_skill import freeze_model,freeze_skill
        context={'project_id':pid,'actor_id':current_actor(),'turn_id':value.turn_id}
        return freeze_model(context) if value.kind=='model' else freeze_skill(context,value.skill_id)
