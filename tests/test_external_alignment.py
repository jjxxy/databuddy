"""An authenticated independent source permits only a narrowly reviewed result."""
import pytest
from agent_service import project_data as data
from agent_service.store import patch, get, uid
from agent_service.actor_context import actor_scope
from agent_service.jobs import get_job
from agent_service.execution import process_once
from agent_service.external_alignment import local_candidate
from test_handoff_anchor import pair
from test_multi_file_conversation import project, H
from test_project_data import registered, run, column


def candidate(project):
    pid,client,_=project
    sid,anchor,grant=pair(project)
    columns=[column('id'),column('amount','decimal','CNY','additive') | {'period':'2026-09'}]
    doc={'version':1,'plan':{'name':'原表','inputs':{'detail':{'columns':columns,'grain':['id'],'meaning':'逐行原始明细'}},
         'stages':[{'id':'keep','kind':'select','description':'保留完整原表','input':'detail','columns':['id','amount']}],
         'output':'keep'},'bindings':{'detail':{'source_id':sid,'column_map':{'c_1':'id','c_2':'amount'}}}}
    wid=registered(pid,doc,path='workflows/external-test.json');job=run(pid,wid)
    return pid,client,sid,anchor,grant,job


def test_review_binds_whole_result_and_revokes_on_gateway_change(project):
    pid,client,sid,anchor,grant,job=candidate(project);rid=job['id']
    path=f'/api/projects/{pid}/data-results/{rid}/external-alignment'
    with pytest.raises(ValueError):data.ensure_candidate_bulk_allowed(pid,rid)
    response=client.get(path,params={'anchor_source_id':anchor},headers=H)
    assert response.status_code==200,response.text
    value=response.json()
    assert value['anchor_revision']==1 and value['anchor_binding']['remote_lineage_verified'] is False
    payload={'anchor_source_id':anchor,'expected_preview_sha256':value['preview_sha256'],
             'confirmed_fields':['id','amount'],'note':'逐列核对来源与业务口径',
             'confirm_original':True,'confirm_plan':True}
    incomplete=dict(payload,confirmed_fields=['id'])
    assert client.post(path+'/confirm',json=incomplete,headers=H).status_code==400
    assert not data.read_result(pid,rid)['external_alignment_confirmed']
    response=client.post(path+'/confirm',json=payload,headers=H)
    assert response.status_code==200,response.text
    shown=data.read_result(pid,rid)
    assert shown['external_alignment_recorded'] and not shown['external_alignment_confirmed']
    with actor_scope('u_'+('2'*64)):
        assert data.read_result(pid,rid)['external_alignment_recorded_details'] is None
    with pytest.raises(ValueError):
        local_candidate(pid,rid,anchor,value['preview_sha256'],'u_'+('2'*64))
    with pytest.raises(ValueError):data.ensure_candidate_bulk_allowed(pid,rid)
    with pytest.raises(ValueError):data.publish_result(pid,rid,job['commit'])
    body={'anchor_source_id':anchor,'expected_preview_sha256':value['preview_sha256']}
    c1=client.post(path+'/local-candidate',json=body,headers=H)
    assert c1.status_code==200,c1.text
    c2=client.post(path+'/local-candidate',json=body,headers=H)
    assert c2.status_code==200 and c2.json()['run_id']==c1.json()['run_id']
    local=c1.json()
    for _ in range(100):
        if get_job(local['run_id'])['status'] not in ('queued','running','retrying'):break
        assert process_once()
    local_job=get_job(local['run_id'])
    assert local_job['status']=='validated',local_job
    assert local_job['source_snapshot'][anchor]['id']==anchor
    assert local_job['data_result']['requires_source_review'] and local_job['data_result']['requires_semantic_review']
    with pytest.raises(ValueError):data.publish_result(pid,local['run_id'],local_job['commit'])
    data.publish_result(pid,local['run_id'],local_job['commit'],review_actor='owner',semantic_review_actor='owner',
                        review_handoff_sha256=local_job['data_result']['handoff_sha256'])
    assert len(data.read_input(pid,{'result_id':local['run_id']})[0])==1
    patch('agent_client',grant['id'],enabled=False)
    assert not data.read_result(pid,rid)['external_alignment_recorded']
    with pytest.raises(ValueError):data.ensure_candidate_bulk_allowed(pid,rid)
    with pytest.raises(ValueError):data.read_input(pid,{'result_id':rid})
    assert client.post(path+'/local-candidate',json=body,headers=H).status_code==400
    assert len(data.read_input(pid,{'result_id':local['run_id']})[0])==1


def test_published_history_missing_source_snapshot_is_not_bulk_readable(monkeypatch):
    from test_fact_semantic_gate import fact_source,workflow
    from agent_service.jobs import save
    pid,sid=fact_source();wid,_=workflow(pid,sid)
    with monkeypatch.context() as old:
        old.setattr(data,'source_requires_fact_alignment',lambda source,evidence:False)
        old.setattr(data,'source_interpretation_needs_review',lambda source,evidence:False)
        job=run(pid,wid);data.publish_result(pid,job['id'],job['commit'])
    for snapshot in (None,{}):
        changed=get_job(job['id']);changed['source_snapshot']=snapshot;save(changed,job['id'])
        with pytest.raises(ValueError,match='证据不完整'):data.ensure_candidate_bulk_allowed(pid,job['id'])
