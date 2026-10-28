"""Manual visual data is never represented as machine-verified OCR."""
import io
import hashlib
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfWriter

from agent_service import visual_fact_service as service
from agent_service.connectors import read_source
from agent_service.store import get, patch, put, uid
from test_fact_service import document


def image_bytes():
    out=io.BytesIO();Image.new('RGB',(100,30),'white').save(out,format='PNG');return out.getvalue()


def input_value(sid, page=1):
    return {'source_id':sid,'page':page,'quote':'2026-10 合肥办事处净收入123.4500元。',
            'facts':[{'value_as_printed':'123.4500','measure':'净收入','unit':'元','period':'2026-10',
                      'dimensions':{'office':'合肥办事处'}}]}


def approval(record,confirmed=True,decision='approved'):
    return {'expected_sha256':record['content_sha256'],'decision':decision,'confirmed_visual_review':confirmed}


def test_manual_image_candidate_review_and_precise_output():
    pid,sid,path=document(image_bytes(),'.png');item=input_value(sid)
    rec=service.propose(pid,item)
    assert rec['status']=='pending' and rec['output_source_id'] is None
    assert not rec['ocr_executed'] and not rec['pixels_independently_verified']
    assert not rec['upstream_ocr_known'] and rec['ocr_scope']=='manual_transcription_submission_only; upstream_unknown'
    assert rec['source_file_sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    assert '123.4500' not in get(service.KIND,rec['id'])['secret_payload']
    with pytest.raises(ValueError,match='视觉核对'):service.review(pid,rec['id'],approval(rec,False),'reviewer')
    assert service.detail(pid,rec['id'])['status']=='pending'
    approved=service.review(pid,rec['id'],approval(rec),'reviewer')
    assert approved['status']=='approved' and approved['review']['visual_review_confirmed']
    result=get('source',approved['output_source_id']);assert result['origin_source_id']==sid
    assert result['provenance_scope']=='manual_transcription_visual_review; upstream_ocr_unknown'
    assert result['requires_semantic_review'] is True
    rows,fields,evidence,truncated=read_source(result);labels={f['label']:f['name'] for f in fields}
    assert rows[0][labels['value']]=='123.4500' and rows[0][labels['source_locator']]=='page:1'
    assert not truncated
    assert service.review(pid,rec['id'],approval(rec),'another-owner')['review']==approved['review']


def test_project_revision_file_digest_and_approval_digest_bound():
    pid,sid,path=document(image_bytes(),'.png');rec=service.propose(pid,input_value(sid))
    other=put('project',{'name':'other'},'p_'+uid())['id']
    with pytest.raises(ValueError,match='当前项目'):service.detail(other,rec['id'])
    with pytest.raises(ValueError,match='版本已变化'):service.review(pid,rec['id'],dict(approval(rec),expected_sha256='0'*64),'reviewer')
    path.write_bytes(image_bytes()+b'changed')
    with pytest.raises(ValueError,match='摘要不一致'):service.review(pid,rec['id'],approval(rec),'reviewer')
    path.write_bytes(image_bytes());patch('source',sid,revision=2)
    with pytest.raises(ValueError,match='来源版本'):service.review(pid,rec['id'],approval(rec),'reviewer')
    assert service.detail(pid,rec['id'])['output_source_id'] is None


def test_pdf_actual_page_validation_and_malformed_images():
    output=io.BytesIO();writer=PdfWriter();writer.add_blank_page(width=100,height=100);writer.add_blank_page(width=100,height=100);writer.write(output)
    pid,sid,_=document(output.getvalue(),'.pdf')
    rec=service.propose(pid,input_value(sid,2));assert rec['page']==2
    approved=service.review(pid,rec['id'],approval(rec),'reviewer')
    rows,fields,_,_=read_source(get('source',approved['output_source_id'])); labels={f['label']:f['name'] for f in fields}
    assert rows[0][labels['source_locator']]=='page:2'
    with pytest.raises(ValueError,match='页码'):service.propose(pid,input_value(sid,3))
    badpid,badsid,_=document(b'not an image','.png')
    with pytest.raises(ValueError,match='图片'):service.propose(badpid,input_value(badsid))
    with pytest.raises(ValueError,match='第 1 页'):service.propose(badpid,input_value(badsid,2))
    with pytest.raises(ValueError):service.propose(pid,input_value(sid,True))


def test_substring_and_unscoped_facts_never_approved():
    pid,sid,_=document(image_bytes(),'.png');invalid=input_value(sid)
    invalid['facts'][0]['value_as_printed']='23.4500'
    with pytest.raises(ValueError,match='完整匹配'):service.propose(pid,invalid)
    invalid=input_value(sid);invalid['facts'][0]['dimensions']={}
    rec=service.propose(pid,invalid);assert rec['approval_blockers']
    with pytest.raises(ValueError,match='口径'):service.review(pid,rec['id'],approval(rec),'reviewer')
    assert service.detail(pid,rec['id'])['output_source_id'] is None


def test_rejected_candidate_stays_unusable_and_immutable():
    pid,sid,_=document(image_bytes(),'.png');rec=service.propose(pid,input_value(sid))
    result=service.review(pid,rec['id'],approval(rec,False,'rejected'),'reviewer')
    assert result['status']=='rejected' and result['output_source_id'] is None
    with pytest.raises(ValueError,match='不可变'):service.review(pid,rec['id'],approval(rec),'reviewer')


def test_api_requires_auth_and_explicit_confirmation():
    from fastapi.testclient import TestClient
    from agent_service.api import app
    import os
    pid,sid,_=document(image_bytes(),'.png');client=TestClient(app)
    root=f'/api/projects/{pid}/visual-facts';headers={'Authorization':'Bearer '+os.environ['APP_TOKEN']}
    assert client.post(root,json=input_value(sid)).status_code in (401,403)
    response=client.post(root,json=input_value(sid),headers=headers)
    assert response.status_code==200,response.text
    rec=response.json();assert rec['status']=='pending' and rec['output_source_id'] is None
    listing=client.get(root,headers=headers); assert listing.status_code==200 and listing.json()['total']==1
    assert 'quote' not in listing.json()['items'][0] and 'secret_payload' not in listing.json()['items'][0]
    assert client.get(root+'/'+rec['id'],headers=headers).json()['quote']==input_value(sid)['quote']
    response=client.post(root+'/'+rec['id']+'/review',json={'expected_sha256':rec['content_sha256'],'decision':'approved'},headers=headers)
    assert response.status_code==422
    response=client.post(root+'/'+rec['id']+'/review',json=approval(rec),headers=headers)
    assert response.status_code==200,response.text
    assert response.json()['status']=='approved' and response.json()['output_source_id']


def test_approval_recovers_after_transaction_rollback(monkeypatch):
    from contextlib import contextmanager
    pid,sid,_=document(image_bytes(),'.png');rec=service.propose(pid,input_value(sid))
    original=service.transaction
    @contextmanager
    def fail_after_writes():
        with original() as connection:
            yield connection
            raise RuntimeError('synthetic crash before commit')
    with monkeypatch.context() as changed:
        changed.setattr(service,'transaction',fail_after_writes)
        with pytest.raises(RuntimeError,match='synthetic crash'):
            service.review(pid,rec['id'],approval(rec),'reviewer')
    assert service.detail(pid,rec['id'])['status']=='pending'
    done=service.review(pid,rec['id'],approval(rec),'reviewer')
    assert done['status']=='approved' and get('source',done['output_source_id'])['requires_semantic_review']
