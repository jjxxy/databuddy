"""A generic source review cannot approve facts hidden behind derived sources."""
import pytest
from fastapi.testclient import TestClient

from agent_service import project_data as data
from agent_service.api import app
from agent_service.codegen import generate
from agent_service.config import TOKEN
from agent_service.execution import enqueue,process_once
from agent_service.jobs import get_job
from agent_service.releases import manifest_for,publish,semantic_warning
from agent_service.sources import revision
from agent_service.store import get,patch,put,uid
from test_fact_semantic_gate import fact_source
from test_project_data import column,registered,run


def derived_facts():
    pid,parent=fact_source()
    text='2026-10\n  净收入  合肥  123.4500\n  预算  合肥  200'.encode()
    response=TestClient(app).post(f'/api/sources/{parent}/replace-file',
        headers={'Authorization':'Bearer '+TOKEN},files={'file':('changed.txt',text,'text/plain')})
    assert response.status_code==200,response.text
    child=data.extract_indented_source(pid,parent,['项目','办事处','金额'])['source_id']
    assert process_once() and process_once()
    assert get('source',child)['semantic_alignment_required'] is True
    # Simulate a source frozen before propagation was introduced: its parent
    # version remains the only evidence that this came from reviewed facts.
    patch('source',child,semantic_alignment_required=False)
    return pid,parent,child


def test_real_legacy_derived_fact_cannot_be_published_by_generic_review():
    pid,parent,sid=derived_facts();source=get('source',sid)
    fields={f['label']:f['name'] for f in source['profile']['fields']}
    spec=dict(source['proposed_spec'],group_by=fields['办事处'],amount_field=fields['金额'])
    pipeline=generate(source,'来源祖先核查',spec,project_id=pid,release_mode='manual')
    rid=enqueue('pipeline',pipeline['id'],commit=pipeline['commit'])['id'];assert process_once()
    job=get_job(rid);_,manifest=manifest_for(job)
    assert job['status']=='validated' and manifest['summary'][0]['amount_total']=='323.450000000000'
    assert manifest['fact_semantic_alignment_required'] is True
    assert '业务语义' in semantic_warning(job,manifest)
    # Even if a stored ancestor is later corrupted, the original candidate's
    # frozen warning cannot be silently downgraded by generic confirmation.
    version=get('source_version',get('source',parent)['revision_id'])
    for key in ('fact_review_id','fact_sha256','origin_source_id','origin_source_revision'):
        version.pop(key,None)
    version['profile']={};put('source_version',version,version['id'])
    with pytest.raises(ValueError,match='业务语义'):
        publish(pipeline['id'],rid,confirm_source_review=True,expected_commit=job['commit'],
                expected_parquet_sha256=manifest['parquet_hash'],review_actor='owner')
    assert get('pipeline',pipeline['id']).get('published_run') is None


def test_real_new_workflow_also_blocks_derived_fact_even_after_review():
    pid,parent,sid=derived_facts();_,fields,_,_=data.read_input(pid,{'source_id':sid})
    names=[f['name'] for f in fields];columns=[column(name) for name in names]
    doc={'version':1,'plan':{'name':'历史派生来源','inputs':{'facts':{'columns':columns,'grain':[names[0]],
         'meaning':'保留来源'}},'stages':[{'id':'keep','kind':'select','input':'facts',
         'description':'保留来源字段','columns':names}],'output':'keep'},'bindings':{'facts':{'source_id':sid}}}
    wid=registered(pid,doc,path='workflows/derived-facts.json');result=run(pid,wid)
    assert result['status']=='validated' and data.read_result(pid,result['id'])['total']==2
    with pytest.raises(ValueError,match='语义对齐'):
        data.publish_result(pid,result['id'],result['commit'],review_actor='owner',
                            review_handoff_sha256=result['data_result']['handoff_sha256'])
    assert data.owned(pid,'data_workflow',wid).get('published_run') is None


def test_frozen_ancestors_fail_closed_and_safe_sources_stay_usable():
    pid='p_'+uid();other='p_'+uid()
    clean=revision(put('source',{'project_id':pid,'kind':'file','name':'safe.csv'},'s_'+uid()))
    assert not data.source_requires_fact_alignment(clean,{})
    assert not data.source_requires_fact_alignment({'id':'child','project_id':pid,'revision':1,
        'origin_source_id':clean['id'],'origin_source_revision':clean['revision']},{})
    fact=revision(put('source',{'project_id':pid,'kind':'file','name':'facts.json',
                                'fact_review_id':'reviewed-fact'},'s_'+uid()))
    mid=revision(put('source',{'project_id':pid,'kind':'file','name':'derived.json',
        'origin_source_id':fact['id'],'origin_source_revision':fact['revision']},'s_'+uid()))
    leaf={'id':'leaf','project_id':pid,'revision':1,'origin_source_id':mid['id'],'origin_source_revision':mid['revision']}
    assert data.source_requires_fact_alignment(leaf,{})
    # Later changes to current records do not erase a frozen ancestor version.
    patch('source',fact['id'],fact_review_id=None,revision=2)
    assert data.source_requires_fact_alignment(leaf,{})
    assert data.source_requires_fact_alignment(dict(leaf,origin_source_id='missing'),{})
    assert data.source_requires_fact_alignment(dict(leaf,origin_source_revision=None),{})
    assert data.source_requires_fact_alignment(dict(leaf,project_id=other),{})
    cycle=revision(put('source',{'project_id':pid,'kind':'file','name':'cycle',
        'origin_source_id':'cycle','origin_source_revision':1},'cycle'))
    assert data.source_requires_fact_alignment(cycle,{})
    # Ambiguous records for the same frozen revision are not evidence of safety.
    put('source_version',dict(get('source_version',clean['revision_id']),id='sv_'+uid()),'sv_'+uid())
    assert data.source_requires_fact_alignment({'id':'child','project_id':pid,'revision':1,
        'origin_source_id':clean['id'],'origin_source_revision':clean['revision']},{})
    assert data.source_requires_fact_alignment({'id':'old','origin_agent_handoff':'historical'}, {})
