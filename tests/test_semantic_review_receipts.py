"""A successful job or model confirmation is not authenticated semantic review."""
import copy
import os

import pytest
from fastapi.testclient import TestClient

from agent_service import business_reports, project_data
from agent_service.api import app
from agent_service.jobs import get_job, save
from test_business_reports import real_result


def test_semantic_adoption_needs_authenticated_bound_confirmation():
    pid,rid=real_result(sql=True);job=get_job(rid)
    assert job['status']=='validated' and job['data_result']['requires_semantic_review'] is True
    assert job['data_result']['requires_source_review'] is False
    # The model's tool and background scheduler do not possess a review actor.
    with pytest.raises(ValueError,match='工作台'):
        project_data.publish_result(pid,rid,job['commit'])
    with pytest.raises(ValueError,match='工作台'):
        project_data.publish_result(pid,rid,job['commit'],lease_token='not-a-review')
    client=TestClient(app);headers={'Authorization':'Bearer '+os.environ['APP_TOKEN']}
    url=f'/api/projects/{pid}/data-results/{rid}/publish'
    body={'run_id':rid,'expected_plan_sha256':job['commit']}
    assert client.post(url,json=body).status_code in (401,403)
    assert client.post(url,headers=headers,json=body).status_code==400
    assert client.post(url,headers=headers,json={**body,'confirm_semantic_review':True,
        'expected_handoff_sha256':'0'*64}).status_code==400
    # Identity cannot be supplied in the JSON body; the route derives it from
    # the authenticated request, and binds the exact result preview.
    correct={**body,'confirm_semantic_review':True,'expected_handoff_sha256':job['data_result']['handoff_sha256']}
    assert client.post(url,headers=headers,json={**correct,'semantic_review_actor':'owner'}).status_code==422
    response=client.post(url,headers=headers,json=correct)
    assert response.status_code==200,response.text
    reviewed=get_job(rid);receipt=reviewed['semantic_review_receipt']
    assert reviewed['status']=='succeeded' and receipt['reviewer']=='owner'
    assert receipt['handoff_sha256']==job['data_result']['handoff_sha256']
    assert receipt['plan_sha256']==job['commit'] and receipt['run_id']==rid
    assert business_reports.inspect_result(pid,rid)['server_adopted_result'] is True
    assert project_data.read_result(pid,rid)['semantic_review_confirmed'] is True

    # Previously successful results without a real receipt remain previewable
    # but cannot masquerade as confirmed financial report input.
    historical=copy.deepcopy(reviewed);historical.pop('semantic_review_receipt');save(historical,rid)
    historical_page=project_data.read_result(pid,rid)
    assert historical_page['total']==2 and historical_page['semantic_review_confirmed'] is False
    with pytest.raises(ValueError,match='人工复核'):
        business_reports.inspect_result(pid,rid)
    with pytest.raises(ValueError,match='人工复核'):
        project_data.read_input(pid,{'result_id':rid})


def test_changed_review_receipt_does_not_authorize_report(monkeypatch):
    pid,rid=real_result(sql=True);job=get_job(rid)
    project_data.publish_result(pid,rid,job['commit'],semantic_review_actor='owner',
                                review_handoff_sha256=job['data_result']['handoff_sha256'])
    import agent_service.jobs as jobs
    real=jobs.get_job
    def altered(ident):
        value=real(ident)
        if ident==rid:
            value=copy.deepcopy(value);value['semantic_review_receipt']['output_semantics_sha256']='0'*64
        return value
    monkeypatch.setattr(jobs,'get_job',altered)
    with pytest.raises(ValueError,match='人工复核'):
        business_reports.inspect_result(pid,rid)


def test_scheduled_semantic_recalculation_stays_candidate_until_review():
    from agent_service.execution import process_once
    pid,rid=real_result(sql=True);initial=get_job(rid)
    project_data.publish_result(pid,rid,initial['commit'],semantic_review_actor='owner',
                                review_handoff_sha256=initial['data_result']['handoff_sha256'])
    workflow=initial['target']
    next_run=project_data.enqueue_workflow(pid,workflow,use_published=True,scheduled_key='semantic-refresh-'+rid)
    assert process_once()
    refreshed=get_job(next_run['id'])
    assert refreshed['status']=='validated' and refreshed['data_result']['requires_semantic_review'] is True
    current=project_data.owned(pid,'data_workflow',workflow)
    assert current['published_run']==rid and current['candidate_run']==next_run['id']
    with pytest.raises(ValueError,match='工作台'):
        project_data.publish_result(pid,next_run['id'],refreshed['commit'])


def test_background_refresh_preserves_existing_candidate_and_old_formal_result():
    from agent_service.execution import process_once
    pid,rid=real_result(sql=True);first=get_job(rid);workflow=first['target']
    project_data.publish_result(pid,rid,first['commit'],semantic_review_actor='owner',
                                review_handoff_sha256=first['data_result']['handoff_sha256'])
    user_run=project_data.enqueue_workflow(pid,workflow)
    assert process_once();candidate=get_job(user_run['id'])
    assert candidate['status']=='validated'
    assert project_data.owned(pid,'data_workflow',workflow)['candidate_run']==candidate['id']
    refresh=project_data.enqueue_workflow(pid,workflow,use_published=True,scheduled_key='refresh-preserve-'+rid)
    assert process_once();refreshed=get_job(refresh['id'])
    state=project_data.owned(pid,'data_workflow',workflow)
    assert refreshed['status']=='validated' and state['candidate_run']==candidate['id'] and state['published_run']==rid
    assert project_data.read_result(pid,refreshed['id'])['total']==2
    # Repeating the already published result is idempotent and cannot remove
    # the pending newer candidate.
    project_data.publish_result(pid,rid,first['commit'],semantic_review_actor='owner',
                                review_handoff_sha256=first['data_result']['handoff_sha256'])
    assert project_data.owned(pid,'data_workflow',workflow)['candidate_run']==candidate['id']


def test_old_review_page_cannot_adopt_replaced_candidate():
    from agent_service.execution import process_once
    pid,rid=real_result(sql=True);old=get_job(rid);workflow=old['target']
    newer=project_data.enqueue_workflow(pid,workflow)
    assert process_once();new_job=get_job(newer['id'])
    assert new_job['status']=='validated' and project_data.owned(pid,'data_workflow',workflow)['candidate_run']==new_job['id']
    with pytest.raises(ValueError,match='候选结果已变化'):
        project_data.publish_result(pid,rid,old['commit'],semantic_review_actor='owner',
                                    review_handoff_sha256=old['data_result']['handoff_sha256'])
    assert project_data.owned(pid,'data_workflow',workflow)['candidate_run']==new_job['id']


def test_background_refresh_cannot_overwrite_a_candidate_added_after_enqueue():
    from agent_service.execution import process_once
    from agent_service.store import patch
    pid,rid=real_result();first=get_job(rid);workflow=first['target']
    project_data.publish_result(pid,rid,first['commit'])
    refresh=project_data.enqueue_workflow(pid,workflow,use_published=True,scheduled_key='refresh-race-'+rid)
    patch('data_workflow',workflow,candidate_run='new-candidate')
    try:
        assert process_once();refreshed=get_job(refresh['id'])
        state=project_data.owned(pid,'data_workflow',workflow)
        assert refreshed['status']=='validated' and state['candidate_run']=='new-candidate' and state['published_run']==rid
        assert project_data.read_result(pid,refreshed['id'])['total']==2
    finally:
        # The synthetic pointer must not pollute global retention tests.
        patch('data_workflow',workflow,candidate_run=None)


def test_active_job_for_old_source_is_not_reported_as_new_source_run():
    from agent_service.execution import process_once
    from test_project_data import project,source,document,registered,replace_source,csv_bytes
    pid=project();src=source(pid);workflow=registered(pid,document(src['id']))
    old=project_data.enqueue_workflow(pid,workflow)
    replace_source(src['id'],csv_bytes(amount_offset=1))
    with pytest.raises(ValueError,match='不同资料版本'):
        project_data.enqueue_workflow(pid,workflow)
    assert get_job(old['id'])['status']=='queued'
    # Do not leave an unrelated active job in the shared integration queue.
    assert process_once()


@pytest.mark.parametrize('marker',[False,None])
def test_historical_sql_cannot_hide_review_by_clearing_both_flags(monkeypatch,marker):
    from agent_service.result_delivery import read_handoff
    import agent_service.jobs as jobs
    pid,rid=real_result(sql=True);job=get_job(rid);folder,meta=project_data.result_folder(pid,rid)
    page=read_handoff(folder/'handoff',limit=1,expected_manifest_sha256=meta['handoff_sha256'])
    meta=copy.deepcopy(meta);page=copy.deepcopy(page)
    if marker is None:
        meta.pop('requires_semantic_review');page['context'].pop('requires_semantic_review')
    else:
        meta['requires_semantic_review']=False;page['context']['requires_semantic_review']=False
    original=jobs.get_job
    def altered(ident):
        value=original(ident)
        if ident==rid:
            value=copy.deepcopy(value);value['data_result']=meta;value['status']='succeeded'
        return value
    monkeypatch.setattr(jobs,'get_job',altered)
    with pytest.raises(ValueError,match='语义复核依据不完整'):
        project_data.require_safe_numeric_lineage(pid,rid,root_meta=meta,root_page=page)
