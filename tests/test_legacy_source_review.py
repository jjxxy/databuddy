"""Owner confirmation for interpreted legacy pipeline inputs."""
import hashlib
import json
import pytest
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.codegen import generate
from agent_service.config import DATA,TOKEN
from agent_service.execution import enqueue,process_once
from agent_service.jobs import get_job
from agent_service.project_data import extract_indented_source
from agent_service.releases import publish,manifest_for
from agent_service.sources import revision
from agent_service.store import get,now,put,seal,uid


def candidate():
    client=TestClient(app)
    response=client.post('/api/projects',headers={'Authorization':'Bearer '+TOKEN},
                         json={'name':'复核流程','request_id':uid()})
    assert response.status_code==200,response.text
    pid=response.json()['id']
    raw='华东\n  上海  12  15%\n  安徽  8  10%'.encode();name=uid()+'.txt';folder=DATA/'uploads'
    folder.mkdir(parents=True,exist_ok=True);(folder/name).write_bytes(raw)
    original=revision(put('source',{'project_id':pid,'name':'sample.txt','kind':'file',
        'secret_config':seal({'stored_name':name}),'content_hash':hashlib.sha256(raw).hexdigest(),
        'bytes':len(raw),'status':'ready','created_at':now()},'s_'+uid()))
    derived=extract_indented_source(pid,original['id'],['办事处','收入','增长率']);assert process_once()
    source=get('source',derived['source_id']);pipeline=generate(source,'解释待核对',source['proposed_spec'],project_id=pid,release_mode='auto')
    queued=enqueue('pipeline',pipeline['id'],commit=pipeline['commit']);assert process_once()
    run=get_job(queued['id']);_,manifest=manifest_for(run)
    assert run['status']=='validated' and manifest['requires_source_review'] is True
    from agent_service.store import patch
    patch('project',pid,pipeline_id=pipeline['id'],source_id=source['id'])
    return pipeline,run,manifest


def test_risk_candidate_requires_explicit_bound_owner_confirmation_and_atomic_receipt():
    pipeline,run,manifest=candidate();pid=pipeline['id'];rid=run['id']
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN};url=f'/api/pipelines/{pid}/publish'
    # No high-level route or direct service call may mark this reviewed silently.
    with pytest.raises(ValueError,match='对照原始资料'):publish(pid,rid)
    assert client.post(url,json={'run_id':rid}).status_code==401
    assert client.post(url,headers=headers,json={'run_id':rid}).status_code==400
    # An ordinary conversational confirmation must not fabricate source review.
    from agent_service.conversation import tick
    message=client.post(f"/api/projects/{pipeline['project_id']}/messages",headers=headers,
        json={'text':'确认发布','candidate_run':rid,'request_id':'legacy-confirm-'+uid()})
    assert message.status_code==200,message.text
    tick();turn=get('turn',message.json()['id'])
    assert '对照原始资料' in turn.get('answer','') and get('pipeline',pid).get('published_run') is None
    baseline={'run_id':rid,'confirm_source_review':True,'expected_commit':run['commit'],
              'expected_parquet_sha256':manifest['parquet_hash']}
    for changes in ({'confirm_source_review':False},{'expected_commit':'wrong'},
                    {'expected_parquet_sha256':'0'*64},{'confirm_source_review':'true'}):
        response=client.post(url,headers=headers,json={**baseline,**changes})
        assert response.status_code in (400,422),response.text
        assert get('pipeline',pid).get('published_run') is None
    # A derived JSON is not a substitute for the original source being reviewed.
    from agent_service.store import unseal,patch
    derived=get('source',manifest['inputs'][0]['source_id']);original=get('source',derived['origin_source_id'])
    original_path=DATA/'uploads'/unseal(original['secret_config'])['stored_name'];original_bytes=original_path.read_bytes()
    original_path.write_bytes(b'changed')
    assert client.post(url,headers=headers,json=baseline).status_code==400
    original_path.write_bytes(original_bytes)
    patch('source',original['id'],revision=original['revision']+1)
    assert client.post(url,headers=headers,json=baseline).status_code==400
    patch('source',original['id'],revision=original['revision'])
    response=client.post(url,headers=headers,json=baseline);assert response.status_code==200,response.text
    assert get('pipeline',pid)['published_run']==rid
    receipt=get_job(rid)['source_review_receipt']
    assert receipt['confirmed_source_review'] and receipt['pipeline_id']==pid and receipt['run_id']==rid
    assert receipt['commit']==run['commit'] and receipt['parquet_sha256']==manifest['parquet_hash']
    assert receipt['actor_hash']==hashlib.sha256(b'owner').hexdigest()
    assert receipt['sources'][0]['source_id']==manifest['inputs'][0]['source_id']
    assert receipt['sources'][0]['content_sha256']==manifest['inputs'][0]['evidence']['content_sha256']
    assert receipt['original_sources']==[{'source_id':original['id'],'revision':original['revision'],
                                         'content_sha256':original['content_hash']}]
    rollback_url=f'/api/pipelines/{pid}/rollback'
    assert client.post(rollback_url,headers=headers,json={'run_id':rid}).status_code==400
    original_path.unlink()
    # Existing authenticated receipt remains idempotent when originals go offline.
    assert client.post(rollback_url,headers=headers,json=baseline).status_code==200
    assert get_job(rid)['source_review_receipt']==receipt
    original_path.write_bytes(original_bytes)
    # A fresh refresh is not automatically published, and needs its own confirmation.
    later=enqueue('pipeline',pid,commit=run['commit']);assert process_once();next_run=get_job(later['id'])
    assert next_run['status']=='validated' and get('pipeline',pid)['published_run']==rid
    assert client.post(url,headers=headers,json={'run_id':later['id']}).status_code==400
    assert get('pipeline',pid)['published_run']==rid
    # An old preview cannot be re-published over the newer candidate using an old confirmation.
    stale=client.post(url,headers=headers,json=baseline);assert stale.status_code==400
    assert get('pipeline',pid)['published_run']==rid


@pytest.mark.parametrize('kind',['text_facts','visual_facts'])
def test_reviewed_facts_cannot_publish_without_per_fact_semantics(kind):
    from decimal import Decimal
    from agent_service import fact_service,visual_fact_service
    from test_fact_service import document
    text='2026-10 合肥办事处净收入123.4500元。\n2026-10 合肥办事处预算200元。'
    vals=[('123.4500','净收入'),('200','预算')]
    if kind=='text_facts':
        pid,sid,_=document(text)
        facts=[]
        for line,(amount,measure) in zip(text.splitlines(),vals):
            start=text.index(line);facts.append({'value_as_printed':amount,'measure':measure,'unit':'元',
                'period':'2026-10','dimensions':{'office':'合肥办事处'},'start':start,'end':start+len(line),'quote':line})
        proposal=fact_service.propose_facts(pid,sid,facts)
        approved=fact_service.review_facts(pid,proposal['id'],proposal['content_sha256'],'owner')
    else:
        from test_visual_fact_service import image_bytes
        pid,sid,_=document(image_bytes(),'.png')
        facts=[{'value_as_printed':amount,'measure':measure,'unit':'元','period':'2026-10',
                'dimensions':{'office':'合肥办事处'}} for amount,measure in vals]
        proposal=visual_fact_service.propose(pid,{'source_id':sid,'page':1,'quote':text,'facts':facts})
        approved=visual_fact_service.review(pid,proposal['id'],{'expected_sha256':proposal['content_sha256'],
                     'decision':'approved','confirmed_visual_review':True},'owner')
        enqueue('probe',approved['output_source_id']);assert process_once()
    source=get('source',approved['output_source_id'])
    labels={field['label']:field['name'] for field in source['profile']['fields']}
    spec=dict(source['proposed_spec'],primary_key=None,group_by=labels['unit'],amount_field=labels['value'],
              amount_label='错误混加示例')
    pipeline=generate(source,'事实语义验收',spec,project_id=pid,release_mode='auto')
    queued=enqueue('pipeline',pipeline['id'],commit=pipeline['commit']);assert process_once()
    run=get_job(queued['id']);_,manifest=manifest_for(run)
    assert run['status']=='validated' and get('pipeline',pipeline['id']).get('published_run') is None
    assert Decimal(str(manifest['summary'][0]['amount_total']))==Decimal('323.45')
    assert manifest['fact_semantic_alignment_required'] is True
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN};url=f'/api/pipelines/{pipeline["id"]}'
    preview=client.get(url+'/preview',headers=headers);assert preview.status_code==200,preview.text
    assert preview.json()['fact_semantic_alignment_required'] is True
    explicit={'run_id':run['id'],'confirm_source_review':True,'expected_commit':run['commit'],
              'expected_parquet_sha256':manifest['parquet_hash']}
    for endpoint in ('/publish','/rollback'):
        blocked=client.post(url+endpoint,headers=headers,json=explicit)
        assert blocked.status_code==400 and '业务语义' in blocked.text,blocked.text
    assert get('pipeline',pipeline['id']).get('published_run') is None
