import copy
import hashlib
import json
from pathlib import Path

import pytest

from agent_service import fact_service as service, workspace
from agent_service.config import DATA
from agent_service.connectors import read_source
from agent_service.store import get, put, seal, uid


TEXT='2026-10 合肥办事处净收入123.4500元。\n2026-10 南京办事处净收入200元。'


def document(text=TEXT,suffix='.txt'):
    pid=put('project',{'name':'事实证据'},'p_'+uid())['id']
    (DATA/'uploads').mkdir(parents=True,exist_ok=True)
    name=uid()+suffix;raw=text.encode() if isinstance(text,str) else text
    path=DATA/'uploads'/name;path.write_bytes(raw)
    sid=put('source',{'project_id':pid,'kind':'file','name':'报告'+suffix,'revision':1,'status':'ready',
         'secret_config':seal({'stored_name':name}),'content_hash':hashlib.sha256(raw).hexdigest()},'s_'+uid())['id']
    return pid,sid,path


def fact(text=TEXT):
    quote=text.split('\n')[0];start=text.index(quote)
    return {'value_as_printed':'123.4500','measure':'净收入','unit':'元','period':'2026-10',
            'dimensions':{'office':'合肥办事处'},'start':start,'end':start+len(quote),'quote':quote}


def test_document_read_offsets_hashes_and_project_scope():
    pid,sid,path=document()
    first=service.read_document(pid,sid,offset=0,limit=10)
    assert first['text']==TEXT[:10] and first['next_offset']==10
    assert first['source_text_sha256']==hashlib.sha256(TEXT.encode()).hexdigest()
    second=service.read_document(pid,sid,offset=10,limit=64000)
    assert first['text']+second['text']==TEXT
    assert not first['graphics_verified'] and not first['ocr_executed']
    other=put('project',{'name':'其他'},'p_'+uid())['id']
    with pytest.raises(ValueError,match='当前项目'):service.read_document(other,sid)
    with pytest.raises(ValueError):service.read_document(pid,sid,limit=True)


def test_proposed_facts_are_pending_and_encrypted_without_creating_source():
    pid,sid,_=document();record=service.propose_facts(pid,sid,[fact()])
    assert record['status']=='pending' and record['output_source_id'] is None
    assert record['approval_blockers']==[]
    stored=get(service.KIND,record['id'])
    assert '123.4500' not in json.dumps(stored) and 'secret_payload' in stored
    assert service.propose_facts(pid,sid,[fact()])['id']==record['id']
    assert service.list_facts(pid)['total']==1
    assert 'facts' not in service.list_facts(pid)['items'][0]
    with pytest.raises(ValueError):service.align_facts(pid,record['id'],[])


def test_fake_numeric_quote_or_offsets_never_become_proposals():
    pid,sid,_=document()
    wrong=fact();wrong['value_as_printed']='999'
    with pytest.raises(ValueError,match='未逐字'):service.propose_facts(pid,sid,[wrong])
    wrong=fact();wrong['start']=1
    with pytest.raises(ValueError,match='位置'):service.propose_facts(pid,sid,[wrong])


def test_a_numeric_substring_is_not_a_verified_amount():
    text=TEXT.replace('123.4500','1123.4500');pid,sid,_=document(text)
    candidate=fact(text);candidate['value_as_printed']='123.4500'
    with pytest.raises(ValueError,match='完整匹配'):service.propose_facts(pid,sid,[candidate])


def test_review_creates_ready_precise_source_and_retains_provenance():
    pid,sid,_=document();record=service.propose_facts(pid,sid,[fact()])
    approved=service.review_facts(pid,record['id'],record['content_sha256'],'owner')
    assert approved['status']=='approved'
    source=get('source',approved['output_source_id'])
    assert source['status']=='ready' and source['origin_source_id']==sid
    assert source['fact_review_id']==record['id'] and source['revision']==1
    assert get('source_version',source['revision_id'])['source_id']==source['id']
    rows,fields,evidence,truncated=read_source(source)
    by_label={f['label']:f['name'] for f in fields}
    assert rows[0][by_label['value']]=='123.4500'
    assert rows[0][by_label['source_text_sha256']]==record['source_text_sha256']
    assert rows[0][by_label['dim:office']]=='合肥办事处' and not truncated
    assert service.review_facts(pid,record['id'],record['content_sha256'],'another-owner')['review']==approved['review']
    assert not any('fact' in f['path'] for f in workspace.list_files(pid))
    with pytest.raises(ValueError,match='不可变'):
        service.review_facts(pid,record['id'],record['content_sha256'],'owner','rejected')


def test_bare_numbers_do_not_authorize_invented_meaning_or_scope():
    pid,sid,_=document('123.4500')
    candidate=fact();candidate.update(start=0,end=8,quote='123.4500')
    record=service.propose_facts(pid,sid,[candidate])
    assert record['approval_blockers']
    with pytest.raises(ValueError,match='原文支持'):
        service.review_facts(pid,record['id'],record['content_sha256'],'owner')
    assert service.get_facts(pid,record['id'])['output_source_id'] is None


def test_unscoped_and_relative_periods_cannot_be_approved():
    pid,sid,_=document('本月净收入123.4500元。')
    candidate={'value_as_printed':'123.4500','measure':'净收入','unit':'元','period':'本月',
               'dimensions':{},'start':0,'end':len('本月净收入123.4500元。'),'quote':'本月净收入123.4500元。'}
    record=service.propose_facts(pid,sid,[candidate])
    blockers=record['approval_blockers'][0]['missing_or_unsupported']
    assert 'dimensions_or_scope_missing' in blockers
    assert 'period_not_concrete_or_semantic_context_invalid' in blockers
    with pytest.raises(ValueError):service.review_facts(pid,record['id'],record['content_sha256'],'owner')


def test_source_changes_or_stale_hash_block_approval():
    pid,sid,path=document();record=service.propose_facts(pid,sid,[fact()])
    with pytest.raises(ValueError,match='版本已变化'):
        service.review_facts(pid,record['id'],'0'*64,'owner')
    path.write_text(TEXT+'\nnew material')
    with pytest.raises(ValueError,match='来源或文字层版本已变化'):
        service.review_facts(pid,record['id'],record['content_sha256'],'owner')


def test_html_uses_visible_static_text_and_retains_unsupported_graphics_status():
    pid,sid,_=document('<html><script>fake 999</script><p>'+TEXT.split('\n')[0]+'</p><canvas>chart 777</canvas></html>',suffix='.html')
    doc=service.read_document(pid,sid)
    assert 'fake 999' not in doc['text'] and '123.4500' in doc['text']
    assert 'chart 777' not in doc['text']
    assert not doc['graphics_verified'] and 'no_iframe_or_chart_rendering' in doc['representation']
    candidate=fact(doc['text']);candidate['quote']=TEXT.split('\n')[0]
    candidate['start']=doc['text'].index(candidate['quote']);candidate['end']=candidate['start']+len(candidate['quote'])
    assert service.propose_facts(pid,sid,[candidate])['status']=='pending'


def test_html_explicit_hidden_content_is_not_fact_evidence():
    html='<div hidden>'+TEXT+'</div><p style="display:none">hidden 777</p><p>visible</p>'
    pid,sid,_=document(html,suffix='.html');doc=service.read_document(pid,sid)
    assert '123.4500' not in doc['text'] and '777' not in doc['text'] and 'visible' in doc['text']


def test_image_sources_do_not_fake_ocr_or_numeric_verification():
    pid,sid,_=document(b'not-a-real-png',suffix='.png')
    doc=service.read_document(pid,sid)
    assert not doc['supported'] and doc['status']=='unsupported_needs_ocr_or_visual_review'
    with pytest.raises(ValueError,match='OCR'):service.propose_facts(pid,sid,[fact()])


def test_facts_require_separate_semantic_review_and_never_infer_sum():
    pid,sid,_=document();record=service.propose_facts(pid,sid,[fact()])
    service.review_facts(pid,record['id'],record['content_sha256'],'owner')
    target={'id':'net_revenue','name':'净收入','meaning':'已付款金额扣除退款','kind':'measure',
            'grain':['office'],'unit':'元','value_format':'decimal','temporal_kind':'interval','period':'2026-10','aggregation':'sum'}
    uncertain=service.align_facts(pid,record['id'],[target])
    assert uncertain['items'][0]['status']=='needs_semantic_metadata'
    assert uncertain['items'][0]['missing']==['aggregation_behavior']
    proposals=service.align_facts(pid,record['id'],[target],aggregation_by_measure={'净收入':'sum'})
    proposal=proposals['items'][0]
    assert proposal['status']=='semantic_review_required' and not proposal['ready_for_automatic_consumption']
    assert proposal['fact']['provenance'][0]['quote']==fact()['quote']
    assert proposal['proposal']['candidates'][0]['state']=='review_required'


def test_rejection_and_ownership_boundaries_are_persistent():
    pid,sid,_=document();record=service.propose_facts(pid,sid,[fact()])
    other=put('project',{'name':'other'},'p_'+uid())['id']
    with pytest.raises(ValueError,match='当前项目'):service.get_facts(other,record['id'])
    with pytest.raises(ValueError,match='当前项目'):
        service.review_facts(other,record['id'],record['content_sha256'],'owner')
    rejected=service.review_facts(pid,record['id'],record['content_sha256'],'owner','rejected')
    assert rejected['status']=='rejected' and rejected['output_source_id'] is None
    assert service.list_facts(other)['items']==[]
