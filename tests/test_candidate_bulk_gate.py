"""Unaligned candidates are pageable but cannot leave as bulk financial output."""
import os
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from agent_service import business_reports, data_tools, project_data as data, workspace_artifacts as assets
from agent_service.api import app
from agent_service.jobs import get_job
from agent_service.store import put, uid
from test_fact_semantic_gate import fact_source, workflow
from test_project_data import run


def client():return TestClient(app),{'Authorization':'Bearer '+os.environ['APP_TOKEN']}


def test_fact_candidate_direct_api_and_artifact_downloads_are_blocked_but_preview_works():
    pid,sid=fact_source();wid,value=workflow(pid,sid);result=run(pid,wid);rid=result['id']
    c,h=client();root=f'/api/projects/{pid}/data-results/{rid}'
    assert c.get(root).status_code in (401,403)
    page=c.get(root,headers=h);assert page.status_code==200 and page.json()['total']==2
    assert 'FACT_SEMANTIC_ALIGNMENT_REQUIRED' in page.json()['context']['semantic_review_reasons']
    for kind in ('xlsx','csv','handoff'):
        res=c.get(root+'/download',params={'format':kind},headers=h)
        assert res.status_code==400 and '语义' in res.text,(kind,res.text)
        with pytest.raises(ValueError,match='语义'):data.result_download(pid,rid,kind)
    with pytest.raises(ValueError,match='语义'):data.export_result(pid,rid)
    receipt=data_tools._workflow_receipt(pid,result,result['data_result'])
    assert 'download' not in receipt and '语义' in receipt['download_blocked_reason']
    item=next(x for x in assets.catalog(pid)['items'] if x['kind']=='result' and x['source_refs'])
    assert item['downloads']==[] and item['download_url'] is None and '语义' in item['download_blocked_reason']
    shown=assets.preview(pid,item['id']);assert shown['total']==2
    assert '语义' in shown['fidelity_note']
    with pytest.raises(ValueError,match='语义'):assets.file(pid,item['id'],'xlsx')
    other=put('project',{'name':'other'},'p_'+uid())['id']
    assert c.get(f'/api/projects/{other}/data-results/{rid}/download',headers=h).status_code in (400,404)
    assert c.get(f'/api/projects/{pid}/artifacts/{item["id"]}/download',params={'format':'xlsx'},headers=h).status_code==409
    assert c.get(f'/api/projects/{pid}/artifacts/{item["id"]}/preview',params={'chart_x':'c_1','chart_y':value},headers=h).status_code==409


def test_cached_candidate_export_cannot_bypass_guard(monkeypatch):
    pid,sid=fact_source();wid,_=workflow(pid,sid);result=run(pid,wid)
    with monkeypatch.context() as old:
        old.setattr(data,'ensure_candidate_bulk_allowed',lambda pid,rid:None)
        path=data.result_download(pid,result['id'],'csv');assert path.is_file()
    with pytest.raises(ValueError,match='语义'):data.result_download(pid,result['id'],'csv')


def test_historical_fact_download_and_safe_csv_candidate_remain_available(monkeypatch):
    pid,sid=fact_source();wid,value=workflow(pid,sid)
    with monkeypatch.context() as historical:
        historical.setattr(data,'source_requires_fact_alignment',lambda source,evidence:False)
        historical.setattr(data,'source_interpretation_needs_review',lambda source,evidence:False)
        first=run(pid,wid);data.publish_result(pid,first['id'],first['commit'])
    assert get_job(first['id'])['status']=='succeeded'
    assert data.result_download(pid,first['id'],'csv').is_file()
    item=next(x for x in assets.catalog(pid)['items'] if x['kind']=='result' and x['status']=='adopted')
    assert item['semantic_status']=='requires_review' and item['downloads']
    page=assets.preview(pid,item['id']);assert page['total']==2
    x=next(column['name'] for column in page['columns'] if column['name']!=value)
    with pytest.raises(ValueError,match='语义'):assets.preview(pid,item['id'],chart_x=x,chart_y=value)
    from test_business_reports import real_result
    safe_pid,safe_rid=real_result()
    assert get_job(safe_rid)['status']=='validated'
    assert data.result_download(safe_pid,safe_rid,'csv').is_file()


def test_unflagged_historical_ancestor_cannot_launder_candidate_export(monkeypatch):
    pid,sid=fact_source();wid,value=workflow(pid,sid)
    with monkeypatch.context() as historical:
        historical.setattr(data,'source_requires_fact_alignment',lambda source,evidence:False)
        historical.setattr(data,'source_interpretation_needs_review',lambda source,evidence:False)
        first=run(pid,wid);data.publish_result(pid,first['id'],first['commit'])
        doc=get_job(first['id'])['workflow_document'];doc['bindings']['facts']={'result_id':first['id'],'column_map':{}}
        # Build a child from the published selected output schema.
        page=data.read_result(pid,first['id']);doc['plan']['inputs']['facts']=page['context']['output_semantics']
        child_wid=__import__('test_project_data').registered(pid,doc,path='workflows/historical-child.json')
        child=run(pid,child_wid)
    assert data.read_result(pid,child['id'])['total']==2
    with pytest.raises(ValueError,match='语义'):data.result_download(pid,child['id'],'handoff')


def test_legacy_fact_candidate_bulk_and_chart_blocked_at_every_public_route():
    from agent_service import fact_service
    from agent_service.codegen import generate
    from agent_service.execution import enqueue,process_once
    from agent_service.releases import manifest_for
    from agent_service.store import get
    from test_fact_service import document
    text='2026-10 合肥办事处净收入123.4500元。\n2026-10 合肥办事处预算200元。'
    pid,sid,_=document(text);facts=[]
    for line,(amount,measure) in zip(text.splitlines(),[('123.4500','净收入'),('200','预算')]):
        start=text.index(line);facts.append({'value_as_printed':amount,'measure':measure,'unit':'元',
            'period':'2026-10','dimensions':{'office':'合肥办事处'},'start':start,'end':start+len(line),'quote':line})
    proposal=fact_service.propose_facts(pid,sid,facts)
    approved=fact_service.review_facts(pid,proposal['id'],proposal['content_sha256'],'owner')
    source=get('source',approved['output_source_id']);labels={f['label']:f['name'] for f in source['profile']['fields']}
    spec=dict(source['proposed_spec'],primary_key=None,group_by=labels['unit'],amount_field=labels['value'],amount_label='错误混加示例')
    pipeline=generate(source,'候选数据应用',spec,project_id=pid,release_mode='auto')
    queued=enqueue('pipeline',pipeline['id'],commit=pipeline['commit']);assert process_once()
    result=get_job(queued['id']);_,manifest=manifest_for(result)
    assert result['status']=='validated' and manifest['fact_semantic_alignment_required']
    assert Decimal(str(manifest['summary'][0]['amount_total']))==Decimal('323.45')
    c,h=client();base=f'/api/pipelines/{pipeline["id"]}'
    preview=c.get(base+'/preview',headers=h);assert preview.status_code==200
    csv=c.get(base+'/export',headers=h,params={'run_id':result['id']});assert csv.status_code==400 and '语义' in csv.text
    appbase=f'/api/projects/{pid}/applications/{pipeline["id"]}'
    info=c.get(appbase+'/data',headers=h,params={'preview':True});assert info.status_code==200,info.text
    assert info.json()['rows'] and info.json()['metric']['total'] is None and not info.json()['summary']
    assert '语义' in info.json()['bulk_blocked_reason']
    exported=c.get(appbase+'/export',headers=h,params={'preview':True});assert exported.status_code==400 and '语义' in exported.text
    items=assets.catalog(pid)['items'];apps=[item for item in items if item['kind']=='application']
    assert len(apps)==1 and apps[0]['downloads']==[] and apps[0]['download_url'] is None
    for kind in ('xlsx','parquet'):
        response=c.get(f'/api/projects/{pid}/artifacts/{apps[0]["id"]}/download',headers=h,params={'format':kind})
        assert response.status_code==409 and '语义' in response.text
    assert assets.preview(pid,apps[0]['id'])['total']>0
    chart=next((item for item in items if item['kind']=='chart'),None)
    if chart:
        assert c.get(f'/api/projects/{pid}/artifacts/{chart["id"]}/preview',headers=h).status_code==409
    # Simulate an old already-published version with a previously confirmed
    # metric: preserve access but never present it as financially confirmed.
    from agent_service import metric_registry
    from agent_service.store import patch,put
    confirmed=metric_registry.declaration(pipeline,result,manifest)
    put('metric',dict(confirmed,status='confirmed'),confirmed['id'])
    patch('pipeline',pipeline['id'],published_run=result['id'],candidate_run=None)
    history=c.get(appbase+'/data',headers=h)
    assert history.status_code==200,history.text
    assert history.json()['bulk_blocked_reason'] is None
    assert history.json()['rows'] and history.json()['metric']['total'] is None and history.json()['summary']==[]
    assert history.json()['metric']['status']=='requires_review' and '语义' in history.json()['semantic_warning']
    assert c.get(appbase+'/export',headers=h).status_code==200
    metric=c.get(appbase+'/metric',headers=h);assert metric.status_code==200,metric.text
    assert metric.json()['status']=='requires_review' and '语义' in metric.json()['semantic_warning']
    assert 'total' not in metric.json()
    assert metric_registry.manifest_status(manifest)=='requires_review'
    other=put('project',{'name':'other'},'p_'+uid())['id']
    assert c.get(f'/api/projects/{other}/applications/{pipeline["id"]}/metric',headers=h).status_code in (400,404)
    history_artifact=next(x for x in assets.catalog(pid)['items'] if x['kind']=='application')
    assert history_artifact['semantic_status']=='requires_review' and '语义' in history_artifact['semantic_warning']
    assert assets.preview(pid,history_artifact['id'])['total']>0
    historical_chart=next((x for x in assets.catalog(pid)['items'] if x['kind']=='chart'),None)
    if historical_chart:
        denied=c.get(f'/api/projects/{pid}/artifacts/{historical_chart["id"]}/preview',headers=h)
        assert denied.status_code==409 and '语义' in denied.text


def test_unknown_legacy_candidate_remains_viewable_but_cannot_be_bulk_exported():
    from agent_service.jobs import save
    from test_agent_handoff import project,result
    pid=project();rid,_,_=result(pid);record=get_job(rid)
    record.pop('workflow_document');record.pop('source_snapshot');save(record,rid)
    assert data.read_result(pid,rid)['total']==40
    c,h=client();response=c.get(f'/api/projects/{pid}/data-results/{rid}/download',headers=h)
    assert response.status_code==400 and '来源链无法核对' in response.text
