"""Version-bound, explicitly authorized continuation of a paused run."""
import json,time
from fastapi import Depends
from pydantic import Field,field_validator
from sqlalchemy import select,update
from .models import StrictModel
from .actor_context import current_actor
from .config import DATA
from .locks import FileLock
from .store import transaction,items,seal,unseal,now
from .jobs import jobs,render,get_job
from .business_flows import _owned,_turn,_read_commit,_lock_path,_runtime_lock,_checkpoint_sha,_verify_partial_table,get_run
from . import workspace

class Approval(StrictModel):
    expected_checkpoint_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    note:str=Field(min_length=1,max_length=500)
    @field_validator('note')
    @classmethod
    def nonempty(cls,value):
        if not value.strip():raise ValueError('请说明核对依据')
        return value.strip()

def approve(pid,fid,rid,actor,value):
    with FileLock(str(DATA/('business-flow-run-'+fid+'.lock')),timeout=10):
        flow=_owned(pid,fid,actor);job=get_job(rid)
        if job.get('kind')!='business_flow' or job.get('target')!=fid or job.get('project_id')!=pid or job.get('actor_id')!=actor or job.get('status')!='awaiting_review':raise ValueError('此运行不处于可复核状态')
        _turn(pid,actor,job['flow_turn_id']);payload=unseal(job['secret_flow_payload'])
        role='published' if payload.get('use_published') else 'candidate'
        if flow.get(role+'_digest')!=job['commit'] or flow.get(role+'_commit')!=job['project_commit']:raise ValueError('流程版本已变化，请重新核对')
        document=_read_commit(pid,fid,job['project_commit'],job['commit'])
        if document!=payload.get('document'):raise ValueError('冻结流程不一致')
        try:locked=json.loads(workspace.git_content(workspace.folder(pid),job['project_commit'],'files/'+_lock_path(fid)))
        except Exception:raise ValueError('流程执行环境锁不可用') from None
        if locked!=_runtime_lock(document):raise ValueError('流程执行环境已变化，不能继续旧运行')
        state=unseal(job['secret_flow_checkpoint'])
        if state.get('status')!='awaiting_review' or _checkpoint_sha(state)!=value.expected_checkpoint_sha256:raise ValueError('复核记录已变化，请重新查看')
        pending=[s for s in state.get('steps',[]) if s.get('status')=='awaiting_review']
        if len(pending)!=1 or pending[0] is not state['steps'][-1]:raise ValueError('人工核对状态不完整')
        node=next((n for n in document['nodes'] if n['id']==pending[0]['node_id'] and n['kind']=='human_review'),None)
        if not node:raise ValueError('人工核对节点与流程不一致')
        _verify_partial_table(pid,job,state,node['inputs']['table']['name'],0,1)
        with transaction() as connection:
            flow_row=connection.execute(select(items.c.body).where(items.c.id==fid,items.c.kind=='business_flow').with_for_update()).first()
            current_flow=json.loads(flow_row[0]) if flow_row else {}
            if current_flow.get(role+'_digest')!=job['commit'] or current_flow.get(role+'_commit')!=job['project_commit']:raise ValueError('流程版本已变化，请重新核对')
            row=connection.execute(select(jobs).where(jobs.c.id==rid).with_for_update()).mappings().first()
            if not row or row['status']!='awaiting_review':raise ValueError('此运行已变化，请重新查看')
            current=render(row)
            if current.get('actor_id')!=actor or _checkpoint_sha(unseal(current['secret_flow_checkpoint']))!=value.expected_checkpoint_sha256:raise ValueError('复核记录已变化，请重新查看')
            pending[0]['approval']={'approved':True,'actor_id':actor,'note':value.note,'approved_at':now(),'checkpoint_sha256':value.expected_checkpoint_sha256}
            state['status']='running';current['secret_flow_checkpoint']=seal(state);current['stage']='人工核对通过，等待继续';current['status']='retrying'
            connection.execute(update(jobs).where(jobs.c.id==rid).values(status='retrying',available_at=time.time(),lease_token=None,lease_until=None,body=json.dumps(current,ensure_ascii=False),updated=time.time()))
    return get_run(pid,fid,rid,actor)

def install(app,auth):
    @app.post('/api/projects/{pid}/business-flows/{fid}/runs/{rid}/approve-checkpoint',dependencies=[Depends(auth)])
    def approve_route(pid:str,fid:str,rid:str,value:Approval):return approve(pid,fid,rid,current_actor(),value)
