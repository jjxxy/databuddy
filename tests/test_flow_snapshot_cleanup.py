import json
import time
import uuid

import pytest
from sqlalchemy import update

from agent_service import business_flow_data as tables
from agent_service import flow_snapshot_cleanup as cleanup
from agent_service.business_flow_engine import digest
from agent_service.config import DATA
from agent_service.jobs import jobs, save
from agent_service.store import put, seal, transaction


def fixture():
    tag=uuid.uuid4().hex
    ctx={'project_id':'p_'+tag,'actor_id':'owner','turn_id':'t_'+tag,'run_id':'r_'+tag}
    flow_id='f_'+tag; sha='1'*64
    put('business_flow',{'project_id':ctx['project_id'],'actor_id':ctx['actor_id']},flow_id)
    def snapshot(label):
        rows=[{'value':label+str(i)} for i in range(4)]
        return tables._save_rows_paged(ctx,rows,tables._serialized_table_rows(rows))
    keep=snapshot('keep');orphan=snapshot('orphan')
    output={'rows_ref':keep,'nested':{'future_ref':keep['sha256']}}
    state={'version':1,'status':'failed','flow_sha256':sha,'steps':[
        {'status':'succeeded','output':output,'output_sha256':digest(output)},
        {'status':'failed','error_code':'STEP_FAILED'}]}
    job={'id':ctx['run_id'],'kind':'business_flow','target':flow_id,'commit':sha,'status':'failed',
        'project_id':ctx['project_id'],'actor_id':ctx['actor_id'],'flow_turn_id':ctx['turn_id'],
        'secret_flow_checkpoint':seal(state)}
    save(job,ctx['run_id'])
    with transaction() as c:c.execute(update(jobs).where(jobs.c.id==ctx['run_id']).values(updated=time.time()-90000))
    folder=DATA/'business-flow-tables'/keep['scope']
    return ctx,job,state,flow_id,folder,keep,orphan


def stale(job):
    with transaction() as c:c.execute(update(jobs).where(jobs.c.id==job['id']).values(updated=time.time()-90000))


def test_preview_then_delete_only_unreferenced_snapshot():
    ctx,job,state,flow_id,folder,keep,orphan=fixture()
    keep_path=folder/(keep['sha256']+'.enc');orphan_path=folder/(orphan['sha256']+'.enc')
    preview=cleanup.cleanup_failed_run(ctx['run_id'])
    assert preview['files']==1 and preview['bytes']==orphan_path.stat().st_size and preview['applied'] is False
    assert keep_path.exists() and orphan_path.exists()
    result=cleanup.cleanup_failed_run(ctx['run_id'],apply=True)
    assert result['files']==1 and result['applied'] is True
    assert keep_path.exists() and not orphan_path.exists()
    assert tables._read_rows(ctx,keep)[0]['value']=='keep0'
    assert cleanup.cleanup_failed_run(ctx['run_id'],apply=True)['files']==0


@pytest.mark.parametrize('change', ['active','recent','bad_state','unknown','bad_receipt','no_checkpoint','published','bad_owner'])
def test_uncertain_state_never_deletes(change):
    ctx,job,state,flow_id,folder,keep,orphan=fixture()
    if change=='active':job['status']='running'
    if change=='bad_state':state['status']='succeeded'
    if change=='unknown':state['steps'][-1]['status']='unknown'
    if change=='bad_receipt':state['steps'][0]['output_sha256']='f'*64
    if change=='no_checkpoint':job.pop('secret_flow_checkpoint')
    if change not in ('active','no_checkpoint'):job['secret_flow_checkpoint']=seal(state)
    save(job,ctx['run_id'])
    if change!='recent':stale(job)
    if change=='published':put('business_flow',{'project_id':ctx['project_id'],'actor_id':ctx['actor_id'],'published_run':ctx['run_id']},flow_id)
    if change=='bad_owner':
        marker=folder/'.owner.json';original=marker.read_text();marker.write_text(json.dumps({'bad':True}))
    try:
        with pytest.raises(ValueError):cleanup.cleanup_failed_run(ctx['run_id'],apply=True)
        assert (folder/(keep['sha256']+'.enc')).exists()
        assert (folder/(orphan['sha256']+'.enc')).exists()
    finally:
        if change=='bad_owner':marker.write_text(original)


def test_future_nested_hashes_are_also_preserved():
    ctx,job,state,flow_id,folder,keep,orphan=fixture()
    state['future_extension']={'additional_artifact':orphan['sha256']}
    job['secret_flow_checkpoint']=seal(state);save(job,ctx['run_id']);stale(job)
    assert cleanup.cleanup_failed_run(ctx['run_id'],apply=True)['files']==0
    assert (folder/(orphan['sha256']+'.enc')).exists()


def test_cleanup_locks_target_before_job(monkeypatch):
    from contextlib import contextmanager
    ctx,job,state,flow_id,folder,keep,orphan=fixture()
    observed=[];actual=cleanup.transaction
    @contextmanager
    def recording():
        with actual() as conn:
            class Proxy:
                def execute(self,query,*args,**kwargs):
                    observed.append(str(query))
                    return conn.execute(query,*args,**kwargs)
            yield Proxy()
    monkeypatch.setattr(cleanup,'transaction',recording)
    assert cleanup.cleanup_failed_run(ctx['run_id'])['files']==1
    assert len(observed)==2 and 'FROM items' in observed[0] and 'FROM jobs' in observed[1]
    assert 'FOR UPDATE' in observed[0] and 'FOR UPDATE' in observed[1]


def test_changed_target_after_discovery_aborts_without_deletion(monkeypatch):
    from contextlib import contextmanager
    ctx,job,state,flow_id,folder,keep,orphan=fixture()
    actual=cleanup.transaction
    @contextmanager
    def moved():
        with actual() as c:
            c.execute(update(jobs).where(jobs.c.id==ctx['run_id']).values(target='different_flow'))
        with actual() as c:
            yield c
    monkeypatch.setattr(cleanup,'transaction',moved)
    with pytest.raises(ValueError,match='运行记录已变化'):
        cleanup.cleanup_failed_run(ctx['run_id'],apply=True)
    assert (folder/(orphan['sha256']+'.enc')).exists()
    assert (folder/(keep['sha256']+'.enc')).exists()
