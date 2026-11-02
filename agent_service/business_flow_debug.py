"""Isolated, non-publishing runs of pinned file and data-processing nodes."""
import hashlib
import json
import re
import threading

from fastapi import Depends
from pydantic import Field

from .actor_context import actor_scope, current_actor
from .business_flow_engine import BusinessFlow
from .business_flow_debug_engine import run_debug
from .business_flows import _handlers, _lock_path, _owned, _read_commit, _runtime_lock, _source_pin, _turn, _validate
from .jobs import enqueue, get_job, heartbeat
from .models import StrictModel
from .store import now, seal, unseal
from . import workspace

ALLOWED=frozenset({'source_table','table_workflow','quality_gate'})

class DebugInput(StrictModel):
    request_id:str=Field(min_length=1,max_length=100,pattern=r'^[A-Za-z0-9_-]+$')
    expected_digest:str=Field(pattern=r'^[a-f0-9]{64}$')
    selected_nodes:list[str]=Field(min_length=1,max_length=50)
    inputs:dict=Field(default_factory=dict)
    use_published:bool=False

def _target(pid,actor,fid):
    return 'bfd_'+hashlib.sha256((pid+'\0'+actor+'\0'+fid).encode()).hexdigest()

def _document(pid,fid,commit,sha):
    doc=_read_commit(pid,fid,commit,sha)
    try:locked=json.loads(workspace.git_content(workspace.folder(pid),commit,'files/'+_lock_path(fid)))
    except Exception:raise ValueError('流程版本缺少执行环境锁，不能调试') from None
    if locked!=_runtime_lock(doc):raise ValueError('流程执行环境已变化，请重新验证流程版本')
    return doc

def _check_scope(pid,doc,selected,inputs):
    flow=BusinessFlow.model_validate(doc);lookup={node.id:node for node in flow.nodes}
    if not isinstance(selected,list) or not selected or len(selected)>50 or any(not isinstance(n,str) for n in selected) or len(set(selected))!=len(selected) or not set(selected)<=set(lookup):
        raise ValueError('选择的节点无效')
    chosen=set(selected);nodes=[n for n in flow.nodes if n.id in chosen]
    missing=sorted({x for n in nodes for x in n.depends_on if x not in chosen})
    if missing:raise ValueError('所选节点缺少上游依赖：'+', '.join(missing))
    if any(n.kind not in ALLOWED for n in nodes):raise ValueError('当前仅支持调试固定文件读取、表格加工及数据检查节点')
    required={ref.name for n in nodes for ref in n.inputs.values() if ref.source=='input'}
    if not isinstance(inputs,dict) or set(inputs)!=required:raise ValueError('调试输入与所选节点不一致')
    from .business_flow_data import SourcePin
    for node in nodes:
        if node.kind!='source_table':continue
        pin=(SourcePin.model_validate({k:node.config.get(k) for k in ('source_id','revision','content_hash')}) if node.config.get('source_id') else SourcePin.model_validate(inputs[node.inputs['source'].name]))
        _source_pin(pid,pin.source_id,pin.revision,pin.content_hash)
    return [n.id for n in nodes]

def submit(pid,fid,actor,value,*,repair_of=None):
    flow=_owned(pid,fid,actor);role='published' if value.use_published else 'candidate'
    sha=flow.get(role+'_digest');commit=flow.get(role+'_commit')
    if not sha or sha!=value.expected_digest:raise ValueError('流程版本已变化，请重新查看')
    turn=_turn(pid,actor,flow.get('published_turn_id') if value.use_published else flow['turn_id'])
    document=_document(pid,fid,commit,sha)
    with actor_scope(actor):
        _validate(pid,actor,turn['id'],document)
        ordered=_check_scope(pid,document,value.selected_nodes,value.inputs)
    payload={'project_id':pid,'actor_id':actor,'flow_id':fid,'turn_id':turn['id'],'document':document,
             'inputs':value.inputs,'selected_nodes':ordered,'digest':sha,'commit':commit}
    key='bfd-run:'+hashlib.sha256((pid+'\0'+actor+'\0'+value.request_id).encode()).hexdigest()
    target=_target(pid,actor,fid)
    if repair_of is not None and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',repair_of):raise ValueError('修复来源无效')
    job=enqueue('business_flow_debug',target,commit=sha,scheduled_key=key,max_attempts=2,
                extra={'project_id':pid,'actor_id':actor,'flow_turn_id':turn['id'],'flow_id':fid,
                       'project_commit':commit,'secret_debug_payload':seal(payload),'repair_of':repair_of})
    if (job.get('kind')!='business_flow_debug' or job.get('target')!=target or job.get('actor_id')!=actor
            or job.get('repair_of')!=repair_of or unseal(job.get('secret_debug_payload',''))!=payload):
        raise ValueError('调试请求标识已使用或该流程已有调试运行')
    return {'run_id':job['id'],'status':job['status'],'plan_digest':sha,'commit':commit,'publishable':False}

def _read_job(pid,fid,rid,actor):
    _owned(pid,fid,actor);job=get_job(rid)
    if (job.get('kind')!='business_flow_debug' or job.get('target')!=_target(pid,actor,fid)
        or job.get('flow_id')!=fid or job.get('project_id')!=pid or job.get('actor_id')!=actor):
        raise ValueError('调试记录不属于当前项目或使用者')
    payload=unseal(job['secret_debug_payload'])
    if payload.get('project_id')!=pid or payload.get('actor_id')!=actor or payload.get('digest')!=job.get('commit') or payload.get('commit')!=job.get('project_commit') or payload.get('flow_id')!=fid or payload.get('turn_id')!=job.get('flow_turn_id'):
        raise ValueError('调试记录与流程不一致')
    _turn(pid,actor,job['flow_turn_id'])
    return job,payload

def status(pid,fid,rid,actor):
    job,payload=_read_job(pid,fid,rid,actor)
    envelope=unseal(job['secret_debug_checkpoint']) if job.get('secret_debug_checkpoint') else {}
    state=envelope.get('state') or {};steps=[]
    for item in state.get('steps',[]):
        steps.append({k:item.get(k) for k in ('node_id','title','status','started_at','finished_at','error_code','error_message','input_sha256','output_sha256','deterministic')})
        output=item.get('output');steps[-1]['table_available']=bool(job.get('status')=='succeeded' and item.get('status')=='succeeded' and isinstance(output,dict) and output.get('kind') in ('table_workflow_v1','source_table_v1') and output.get('complete') is True)
    result={'id':rid,'flow_id':fid,'status':job['status'],'plan_digest':job['commit'],'commit':job['project_commit'],
            'selected_nodes':payload['selected_nodes'],'steps':steps,'publishable':False,'financial_verified':False}
    # The original job remains failed. A separately authorized, non-published
    # retry is reported only if its own real job can still be read.
    from .store import get
    repair_id='bfr_'+hashlib.sha256((pid+'\0'+actor+'\0'+rid).encode()).hexdigest()
    try:
        record=get('business_flow_auto_repair',repair_id)
        if record.get('project_id')==pid and record.get('actor_id')==actor and record.get('original_run_id')==rid:
            repaired,repaired_payload=_read_job(pid,record['flow_id'],record['run_id'],actor)
            if repaired.get('repair_of')==rid:
                result['auto_repair']={'flow_id':record['flow_id'],'run_id':record['run_id'],
                    'status':repaired['status'],'plan_digest':repaired['commit'],'commit':repaired['project_commit'],
                    'selected_nodes':repaired_payload['selected_nodes'],
                    'publishable':False}
    except (KeyError,ValueError):pass
    return result

def cancel(pid,fid,rid,actor):
    job,_=_read_job(pid,fid,rid,actor)
    if job.get('status') not in ('queued','running','retrying'):raise ValueError('调试运行已经结束')
    from .jobs import request_cancel
    request_cancel(rid)
    return {'run_id':rid,'cancel_requested':True,'publishable':False}

def table(pid,fid,rid,node_id,actor,offset=0,limit=50):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',node_id or '') or type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:raise ValueError('节点或分页参数无效')
    job,payload=_read_job(pid,fid,rid,actor)
    if job.get('status')!='succeeded':raise ValueError('调试运行尚未成功完成')
    from . import agent_gateway,agent_team
    turn=_turn(pid,actor,job['flow_turn_id']);agent_gateway.assert_turn_allowed(turn);agent_team.assert_turn_allowed(turn,'run_data_workflow',{})
    document=_document(pid,fid,job['project_commit'],job['commit'])
    if document!=payload['document']:raise ValueError('冻结流程不一致')
    envelope=unseal(job['secret_debug_checkpoint'])
    context={'project_id':pid,'actor_id':actor,'turn_id':job['flow_turn_id'],'originating_turn_id':job['flow_turn_id'],'run_id':rid}
    with actor_scope(actor):
        checked=run_debug(document,payload['inputs'],_handlers(),context,payload['selected_nodes'],allowed_kinds=ALLOWED,checkpoint=envelope,persist=lambda _:None)
        if checked['state']['status']!='succeeded':raise ValueError('调试回执不完整')
        steps=checked['state']['steps'];matches=[s for s in steps if s['node_id']==node_id]
        if len(matches)!=1 or node_id not in payload['selected_nodes']:raise ValueError('节点不在调试结果中')
        from .business_flow_data import verify_table_snapshot,get_table_page,materialize_table
        for s in steps:
            output=s.get('output')
            if isinstance(output,dict) and 'rows_ref' in output:verify_table_snapshot(context,output)
        output=matches[0].get('output')
        if not isinstance(output,dict) or output.get('kind') not in ('table_workflow_v1','source_table_v1') or output.get('complete') is not True:raise ValueError('该节点没有可查看的表格')
        if output['kind']=='source_table_v1':
            from . import agent_context
            if 'read_source_table' not in agent_context.allowed(turn):raise ValueError('当前任务无权读取来源资料')
            agent_team.assert_turn_allowed(turn,'read_source_table',{'source_id':output.get('source_id')})
            _source_pin(pid,output.get('source_id'),output.get('revision'),output.get('content_hash'))
            fields=output.get('fields')
            if not isinstance(fields,list) or len(fields)>200 or any(not isinstance(f,dict) or not isinstance(f.get('name'),str) for f in fields):raise ValueError('来源字段无效')
            ref=output.get('rows_ref');all_rows=output.get('rows')
            total=ref.get('row_count') if isinstance(ref,dict) else len(all_rows) if isinstance(all_rows,list) else None
            if type(total) is not int or total<0:raise ValueError('来源数据行数无效')
            rows=get_table_page(context,output,offset,limit)
            if len(json.dumps(rows,ensure_ascii=False).encode('utf-8'))>1024*1024:raise ValueError('本页数据过大，请减少每页行数')
            return {'run_id':rid,'node_id':node_id,'columns':[{'name':f['name'],'label':f.get('label') or f['name']} for f in fields],
                    'rows':rows,'total':total,'offset':offset,'next_offset':offset+len(rows) if offset+len(rows)<total else None,
                    'publishable':False,'financial_verified':False}
        from .data_workflows import TableSchema, digest as table_digest
        schema=output.get('schema');parsed=TableSchema.model_validate(schema)
        ref=output.get('rows_ref')
        if isinstance(ref,dict) and ref.get('version')==2:
            rows=get_table_page(context,output,offset,limit);total=output.get('row_count')
            if type(total) is not int or total<0:raise ValueError('调试数据行数无效')
        else:
            material=materialize_table(context,output);all_rows=material.get('rows');total=material.get('row_count')
            if not isinstance(all_rows,list) or type(total) is not int or total!=len(all_rows) or material.get('output_sha256')!=table_digest({'schema':schema,'rows':all_rows}):raise ValueError('调试数据与回执不一致')
            rows=all_rows[offset:offset+limit]
    if len(json.dumps(rows,ensure_ascii=False).encode('utf-8'))>1024*1024:raise ValueError('本页数据过大，请减少每页行数')
    return {'run_id':rid,'node_id':node_id,'columns':[{'name':c.name,'label':c.label or c.name,'unit':c.unit} for c in parsed.columns],'rows':rows,'total':total,'offset':offset,
            'next_offset':offset+len(rows) if offset+len(rows)<total else None,'publishable':False,'financial_verified':False}

def execute_job(job,folder):
    from .execution import checkpoint,patch_run
    from .provider import task_scope
    pid=job['project_id'];actor=job['actor_id'];fid=job['flow_id']
    _,payload=_read_job(pid,fid,job['id'],actor)
    document=_document(pid,fid,job['project_commit'],job['commit'])
    if document!=payload['document']:raise ValueError('冻结流程不一致')
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
            _check_scope(pid,document,payload['selected_nodes'],payload['inputs'])
            def persist(state):checkpoint(job);patch_run(job,secret_debug_checkpoint=seal(state),stage='正在调试所选节点')
            prior=unseal(job['secret_debug_checkpoint']) if job.get('secret_debug_checkpoint') else None
            result=run_debug(document,payload['inputs'],_handlers(),context,payload['selected_nodes'],allowed_kinds=ALLOWED,checkpoint=prior,persist=persist)
            checkpoint(job)
            if result['state']['status']=='succeeded':patch_run(job,status='succeeded',finished_at=now(),stage='调试完成，结果未发布')
            else:
                patch_run(job,status='failed',finished_at=now(),stage='调试未完成',public_error='调试步骤未完成，请查看步骤记录')
                if result['state']['status']=='failed':
                    try:
                        from .business_flow_repair import rebind_identical_source_debug
                        rebind_identical_source_debug(job)
                    except Exception:
                        import logging
                        logging.getLogger(__name__).warning('自动修复未能安全启动，原失败回执已保留')
                    try:
                        from .business_flow_repair import enqueue_diagnostic
                        enqueue_diagnostic(job)
                    except Exception:
                        import logging
                        logging.getLogger(__name__).warning('调试失败后的自动诊断未能启动')
    finally:stop.set();thread.join(timeout=6)

def install(app,auth):
    root='/api/projects/{pid}/business-flows/{fid}/debug-runs'
    @app.post(root,dependencies=[Depends(auth)])
    def start(pid:str,fid:str,value:DebugInput):return submit(pid,fid,current_actor(),value)
    @app.get(root+'/{rid}',dependencies=[Depends(auth)])
    def view(pid:str,fid:str,rid:str):return status(pid,fid,rid,current_actor())
    @app.post(root+'/{rid}/cancel',dependencies=[Depends(auth)])
    def stop(pid:str,fid:str,rid:str):return cancel(pid,fid,rid,current_actor())
    @app.get(root+'/{rid}/tables/{node_id}',dependencies=[Depends(auth)])
    def view_table(pid:str,fid:str,rid:str,node_id:str,offset:int=0,limit:int=50):return table(pid,fid,rid,node_id,current_actor(),offset,limit)
