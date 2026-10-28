import os
import pytest
from fastapi.testclient import TestClient
from agent_service import project_data as data,single_fact_value
from agent_service.api import app
from agent_service.store import get,put,uid
from test_fact_semantic_gate import fact_source
from test_project_data import registered,run


def single(pid,sid,measure='净收入',mutate=None):
    _,fields,_,_=data.read_input(pid,{'source_id':sid})
    names=[f['name'] for f in fields]
    columns=[]
    for f in fields:
        value={'name':f['name'],'label':f['label'],'meaning':f['label'],'data_type':'text','aggregation':'unknown'}
        if f['label']=='value':value.update(meaning=measure,unit='元',period='2026-10',aggregation='non_additive')
        columns.append(value)
    ident=next(f['name'] for f in fields if f['label']=='fact_id')
    doc={'version':1,'plan':{'name':'单事实原值','inputs':{'facts':{'columns':columns,'grain':[ident],'meaning':'原文事实'}},
         'stages':[{'id':'keep','kind':'select','input':'facts','description':'原样保留','columns':names}],'output':'keep'},
         'bindings':{'facts':{'source_id':sid}}}
    if mutate:mutate(doc)
    wid=registered(pid,doc,path='workflows/single-fact.json');result=run(pid,wid)
    return wid,result


def test_single_text_value_requires_explicit_bound_confirmation_and_cannot_be_reused():
    pid,sid=fact_source(single=True);wid,result=single(pid,sid);rid=result['id']
    c=TestClient(app);headers={'Authorization':'Bearer '+os.environ['APP_TOKEN']}
    base=f'/api/projects/{pid}/data-results/{rid}/single-fact-review'
    resp=c.get(base,headers=headers);assert resp.status_code==200,resp.text
    preview=resp.json();assert preview['fact']['value_as_printed']=='123.4500'
    assert c.get(base+'/download',headers=headers).status_code==400
    assert c.post(base+'/confirm',headers=headers,json={'expected_preview_sha256':preview['preview_sha256'],'confirm_original_value':False}).status_code==400
    assert c.post(base+'/confirm',headers=headers,json={'expected_preview_sha256':preview['preview_sha256'],'confirm_original_value':True,'reviewer':'owner'}).status_code==422
    res=c.post(base+'/confirm',headers=headers,json={'expected_preview_sha256':preview['preview_sha256'],'confirm_original_value':True})
    assert res.status_code==200,res.text
    download=c.get(base+'/download',headers=headers);assert download.status_code==200,download.text
    assert download.json()['fact']['normalized_value']=='123.4500'
    assert data.owned(pid,'data_workflow',wid).get('published_run') is None
    with pytest.raises(ValueError):data.read_input(pid,{'result_id':rid})
    assert c.get(f'/api/projects/{pid}/data-results/{rid}/download',headers=headers).status_code==400
    other=put('project',{'name':'other'},'p_'+uid())['id']
    assert c.get(f'/api/projects/{other}/data-results/{rid}/single-fact-review',headers=headers).status_code in (400,404)
    original=get('source',preview['original']['id']);data.source_path(original).write_bytes(b'replaced')
    assert c.get(base+'/download',headers=headers).status_code==400


def test_visual_single_value_keeps_page_and_does_not_claim_machine_verification():
    from agent_service import visual_fact_service as visual
    from test_visual_fact_service import image_bytes,input_value,approval
    from test_fact_service import document
    pid,original,_=document(image_bytes(),'.png')
    proposal=visual.propose(pid,input_value(original));reviewed=visual.review(pid,proposal['id'],approval(proposal),'owner')
    wid,result=single(pid,reviewed['output_source_id']);rid=result['id']
    value=single_fact_value.preview(pid,rid)
    assert value['original']['page']==1 and value['fact_review']['kind']=='visual'
    single_fact_value.confirm(pid,rid,value['preview_sha256'],'owner')
    output=single_fact_value.export(pid,rid)
    assert output['fact']['value_as_printed']=='123.4500' and '不代表财务批准' in output['warning']
    assert data.owned(pid,'data_workflow',wid).get('published_run') is None


def test_multiple_facts_not_approved_and_changes_invalidate_confirmation():
    from agent_service.store import patch
    pid,sid=fact_source();_,result=single(pid,sid)
    with pytest.raises(ValueError,match='恰好一条'):single_fact_value.preview(pid,result['id'])
    pid,sid=fact_source(single=True);_,result=single(pid,sid);rid=result['id']
    value=single_fact_value.preview(pid,rid);single_fact_value.confirm(pid,rid,value['preview_sha256'],'owner')
    source=get('source',value['original']['id']);patch('source',source['id'],revision=2)
    with pytest.raises(ValueError):single_fact_value.export(pid,rid)


def test_duplicate_concurrent_confirmations_are_idempotent():
    from concurrent.futures import ThreadPoolExecutor
    pid,sid=fact_source(single=True);_,result=single(pid,sid);rid=result['id']
    value=single_fact_value.preview(pid,rid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        confirmations=list(pool.map(lambda _:single_fact_value.confirm(pid,rid,value['preview_sha256'],'owner'),range(2)))
    assert confirmations[0]['receipt_id']==confirmations[1]['receipt_id']
    assert single_fact_value.export(pid,rid)['fact']['value_as_printed']=='123.4500'


def test_json_preserves_negative_precision_and_formula_like_text_without_spreadsheet_export():
    from agent_service import fact_service
    from test_fact_service import document
    text='2026-10 合肥办事处=净收入-123.4500元。'
    pid,sid,_=document(text)
    fact={'value_as_printed':'-123.4500','measure':'=净收入','unit':'元','period':'2026-10',
          'dimensions':{'office':'合肥办事处'},'start':0,'end':len(text),'quote':text}
    proposal=fact_service.propose_facts(pid,sid,[fact])
    reviewed=fact_service.review_facts(pid,proposal['id'],proposal['content_sha256'],'owner')
    _,result=single(pid,reviewed['output_source_id'],measure='=净收入');rid=result['id']
    c=TestClient(app);h={'Authorization':'Bearer '+os.environ['APP_TOKEN']}
    base=f'/api/projects/{pid}/data-results/{rid}/single-fact-review'
    value=c.get(base,headers=h).json()
    assert c.post(base+'/confirm',headers=h,json={'expected_preview_sha256':value['preview_sha256'],'confirm_original_value':True}).status_code==200
    response=c.get(base+'/download',headers=h)
    assert response.status_code==200 and response.headers['content-type'].startswith('application/json')
    assert response.json()['fact']['value_as_printed']=='-123.4500'
    assert response.json()['fact']['normalized_value']=='-123.4500' and response.json()['fact']['measure']=='=净收入'
    for fmt in ('xlsx','csv','handoff'):
        assert c.get(f'/api/projects/{pid}/data-results/{rid}/download',params={'format':fmt},headers=h).status_code==400


def test_tampered_handoff_and_changed_plan_invalidate_confirmation():
    from agent_service import workspace
    pid,sid=fact_source(single=True);wid,result=single(pid,sid);rid=result['id']
    before=single_fact_value.preview(pid,rid)
    single_fact_value.confirm(pid,rid,before['preview_sha256'],'owner')
    workflow=data.owned(pid,'data_workflow',wid)
    workspace.write_file(pid,workflow['path'], '{"version":1}')
    with pytest.raises(ValueError):single_fact_value.export(pid,rid)
    workspace.write_file(pid,workflow['path'],__import__('json').dumps(result['workflow_document'],ensure_ascii=False))
    folder,_=data.result_folder(pid,rid);rows=folder/'handoff'/'rows.jsonl'
    rows.write_bytes(rows.read_bytes().replace(b'123.4500',b'999.4500'))
    with pytest.raises(ValueError):single_fact_value.export(pid,rid)


@pytest.mark.parametrize('mutation',[
    lambda doc:doc['plan']['stages'][0].update(kind='filter',conditions=[{'column':'c_1','operator':'not_null'}]) or doc['plan']['stages'][0].pop('columns'),
    lambda doc:doc['plan']['inputs']['facts']['columns'][2].update(aggregation='additive'),
    lambda doc:doc['plan']['inputs']['facts']['columns'][2].update(unit='万元'),
    lambda doc:doc['plan']['inputs']['facts']['columns'][2].update(period='2026-09'),
])
def test_single_value_rejects_actual_filter_or_unverified_semantics(mutation):
    pid,sid=fact_source(single=True);_,result=single(pid,sid,mutate=mutation)
    with pytest.raises(ValueError):single_fact_value.preview(pid,result['id'])


def test_confirm_rejects_stale_preview_unauthenticated_or_spoofed_actor():
    pid,sid=fact_source(single=True);_,result=single(pid,sid);rid=result['id']
    value=single_fact_value.preview(pid,rid)
    c=TestClient(app);h={'Authorization':'Bearer '+os.environ['APP_TOKEN']}
    url=f'/api/projects/{pid}/data-results/{rid}/single-fact-review/confirm'
    payload={'expected_preview_sha256':value['preview_sha256'],'confirm_original_value':True}
    assert c.post(url,json=payload).status_code in (401,403)
    assert c.post(url,headers=h,json={**payload,'expected_preview_sha256':'0'*64}).status_code==400
    assert c.post(url,headers=h,json={**payload,'reviewer':'owner'}).status_code==422
    with pytest.raises(ValueError):single_fact_value.confirm(pid,rid,value['preview_sha256'],'agent')


def test_malformed_or_tampered_receipt_cannot_authorize_export():
    from agent_service.store import patch
    pid,sid=fact_source(single=True);_,result=single(pid,sid);rid=result['id']
    value=single_fact_value.preview(pid,rid)
    confirmed=single_fact_value.confirm(pid,rid,value['preview_sha256'],'owner')
    patch(single_fact_value.KIND,confirmed['receipt_id'],reviewer='agent')
    with pytest.raises(ValueError,match='回执'):single_fact_value.export(pid,rid)


@pytest.mark.parametrize('kind',['text','visual'])
def test_real_api_generates_single_candidate_from_approved_record_idempotently(kind):
    from concurrent.futures import ThreadPoolExecutor
    from agent_service import fact_service,visual_fact_service
    from agent_service.execution import process_once
    if kind=='text':
        pid,sid=fact_source(single=True);source=get('source',sid)
        record,payload=fact_service._owned(pid,source['fact_review_id'])
    else:
        from test_visual_fact_service import image_bytes,input_value,approval
        from test_fact_service import document
        pid,original,_=document(image_bytes(),'.png')
        proposal=visual_fact_service.propose(pid,input_value(original))
        reviewed=visual_fact_service.review(pid,proposal['id'],approval(proposal),'owner')
        record,payload=visual_fact_service._owned(pid,reviewed['id'])
    c=TestClient(app);h={'Authorization':'Bearer '+os.environ['APP_TOKEN']}
    url=f'/api/projects/{pid}/single-fact-candidates'
    body={'kind':kind,'fact_review_id':record['id'],'expected_fact_sha256':record['content_sha256'],
          'expected_original_revision':payload['source_revision'],'expected_original_sha256':payload['source_file_sha256']}
    assert c.post(url,json=body).status_code in (401,403)
    assert c.post(url,headers=h,json={**body,'reviewer':'owner'}).status_code==422
    assert c.post(url,headers=h,json={**body,'expected_fact_sha256':'0'*64}).status_code==400
    other=put('project',{'name':'other'},'p_'+uid())['id']
    assert c.post(f'/api/projects/{other}/single-fact-candidates',headers=h,json=body).status_code in (400,404)
    with ThreadPoolExecutor(max_workers=2) as pool:
        created=list(pool.map(lambda _:c.post(url,headers=h,json=body),range(2)))
    assert all(r.status_code==200 for r in created),[r.text for r in created]
    first=created[0].json();assert created[1].json()['run_id']==first['run_id']
    assert first['status']=='queued' and process_once()
    assert c.post(url,headers=h,json=body).json()['run_id']==first['run_id']
    assert data.owned(pid,'data_workflow',first['workflow_id']).get('published_run') is None
    base=f'/api/projects/{pid}/data-results/{first["run_id"]}/single-fact-review'
    preview=c.get(base,headers=h);assert preview.status_code==200,preview.text
    confirm=c.post(base+'/confirm',headers=h,json={'expected_preview_sha256':preview.json()['preview_sha256'],'confirm_original_value':True})
    assert confirm.status_code==200,confirm.text
    assert c.get(base+'/download',headers=h).json()['fact']['value_as_printed']=='123.4500'
    assert data.owned(pid,'data_workflow',first['workflow_id']).get('published_run') is None


def test_generate_rejects_pending_multiple_and_changed_original():
    from agent_service import fact_service
    from test_fact_service import document,fact
    from agent_service.store import patch
    pid,sid,path=document();proposal=fact_service.propose_facts(pid,sid,[fact()])
    body={'kind':'text','fact_review_id':proposal['id'],'expected_fact_sha256':proposal['content_sha256'],
          'expected_original_revision':proposal['source_revision'],'expected_original_sha256':proposal['source_file_sha256']}
    c=TestClient(app);h={'Authorization':'Bearer '+os.environ['APP_TOKEN']};url=f'/api/projects/{pid}/single-fact-candidates'
    assert c.post(url,headers=h,json=body).status_code==400
    fact_service.review_facts(pid,proposal['id'],proposal['content_sha256'],'owner')
    path.write_bytes(b'changed')
    assert c.post(url,headers=h,json=body).status_code==400
    multi_pid,multi_sid=fact_source();multi_source=get('source',multi_sid)
    multi_record,multi_payload=fact_service._owned(multi_pid,multi_source['fact_review_id'])
    multi={'kind':'text','fact_review_id':multi_record['id'],'expected_fact_sha256':multi_record['content_sha256'],
           'expected_original_revision':multi_payload['source_revision'],'expected_original_sha256':multi_payload['source_file_sha256']}
    assert c.post(f'/api/projects/{multi_pid}/single-fact-candidates',headers=h,json=multi).status_code==400


def test_candidate_explicit_retry_and_path_conflict_are_safe():
    from agent_service.jobs import get_job,save
    from agent_service.execution import process_once
    from agent_service import workspace
    pid,sid=fact_source(single=True);source=get('source',sid)
    from agent_service.fact_service import _owned
    record,payload=_owned(pid,source['fact_review_id'])
    body={'kind':'text','fact_review_id':record['id'],'expected_fact_sha256':record['content_sha256'],
          'expected_original_revision':payload['source_revision'],'expected_original_sha256':payload['source_file_sha256']}
    c=TestClient(app);h={'Authorization':'Bearer '+os.environ['APP_TOKEN']};url=f'/api/projects/{pid}/single-fact-candidates'
    first=c.post(url,headers=h,json=body).json();rid=first['run_id']
    # A retry must refer to a real failed/cancelled run of this exact workflow.
    assert c.post(url,headers=h,json={**body,'retry_run_id':rid}).status_code==400
    failed=get_job(rid);failed['status']='failed';save(failed,rid)
    retry=c.post(url,headers=h,json={**body,'retry_run_id':rid});assert retry.status_code==200,retry.text
    assert retry.json()['run_id']!=rid
    assert c.post(url,headers=h,json={**body,'retry_run_id':rid}).json()['run_id']==retry.json()['run_id']
    # Complete this test's actual retry rather than leaving a queued job for
    # the next test's worker to claim.
    assert process_once()
    assert get_job(retry.json()['run_id'])['status']=='validated'
    workflow=data.owned(pid,'data_workflow',first['workflow_id'])
    original=workspace.read_file(pid,workflow['path'])
    try:
        workspace.write_file(pid,workflow['path'],'{"user_content":true}')
        assert c.post(url,headers=h,json=body).status_code==400
        assert workspace.read_file(pid,workflow['path'])=='{"user_content":true}'
    finally:
        workspace.write_file(pid,workflow['path'],original)
