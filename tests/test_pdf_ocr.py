"""Real single-page PDF OCR through the isolated worker, never a finance approval."""
import io,os
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader,PdfWriter
from agent_service import visual_fact_service as visual
from agent_service.api import app
from agent_service.store import get
from test_fact_service import document
from test_offline_ocr import make_image


@pytest.fixture
def worker(monkeypatch):
    executable=os.environ.get('ATLAS_TEST_PDF_OCR_PYTHON')
    if not executable or not Path(executable).is_file():pytest.skip('Optional pinned PDF OCR test runtime unavailable')
    monkeypatch.setattr(visual,'_ocr_worker_python',lambda:executable)
    # No parent-process PDF parser may be invoked on the OCR path.
    monkeypatch.setattr(visual,'_original',lambda *a,**kw:pytest.fail('OCR used the parent PDF parser'))


def request(pid,sid,page=1):
    source=get('source',sid);client=TestClient(app)
    return client.post(f'/api/projects/{pid}/sources/{sid}/ocr',headers={'Authorization':'Bearer '+os.environ['APP_TOKEN']},
        json={'expected_revision':source['revision'],'expected_sha256':source['content_hash'],'page':page})


def multi_pdf(tmp_path):
    image,_=make_image(tmp_path,text=True)
    data=io.BytesIO()
    with Image.open(image) as im:im.convert('RGB').save(data,'PDF',resolution=144)
    reader=PdfReader(io.BytesIO(data.getvalue()));writer=PdfWriter()
    writer.add_page(reader.pages[0]);writer.add_page(reader.pages[0])
    writer.pages[1].rotate(90)
    writer.pages[1].cropbox.lower_left=(20,20)
    writer.pages[1].cropbox.upper_right=(300,130)
    out=io.BytesIO();writer.write(out);return out.getvalue()


def test_real_pdf_ocr_pages_rotation_crop_and_source_binding(worker,tmp_path):
    raw=multi_pdf(tmp_path);pid,sid,_=document(raw,'.pdf')
    one=request(pid,sid,1);assert one.status_code==200,one.text
    result=one.json();combined=''.join(x['text'] for x in result['lines'])
    assert '合肥办事处' in combined and '123.45' in combined
    assert result['source_file_sha256']==get('source',sid)['content_hash']
    assert result['page']==1 and result['pdf_page_count']==2 and result['render_scale']==2.0
    assert result['coordinate_space']=='rendered_pdf_page_pixels_not_pdf_coordinates'
    assert result['human_review_required'] and not result['financial_facts_verified'] and not result['complete']
    assert len(result['rendered_image_sha256'])==64
    rotated=request(pid,sid,2);assert rotated.status_code==200,rotated.text
    rotated=rotated.json();assert rotated['page']==2 and rotated['pdf_rotation_degrees']==90
    assert rotated['image_width']<=6000 and rotated['image_height']<=6000
    assert all(0<=x<=rotated['image_width'] and 0<=y<=rotated['image_height'] for line in rotated['lines'] for x,y in line['box'])
    assert request(pid,sid,3).status_code==400
    assert visual.list_proposals(pid)['total']==0


def test_pdf_malformed_encrypted_oversized_and_page_count_are_rejected(worker):
    pid,sid,_=document(b'%PDF broken','.pdf');assert request(pid,sid).status_code==400
    writer=PdfWriter();writer.add_blank_page(width=100,height=100);writer.encrypt('secret')
    out=io.BytesIO();writer.write(out);pid,sid,_=document(out.getvalue(),'.pdf')
    response=request(pid,sid);assert response.status_code==400 and '加密' in response.text
    # Empty user password can be opened by PDFium, but is still encrypted.
    writer=PdfWriter();writer.add_blank_page(width=100,height=100);writer.encrypt(user_password='',owner_password='owner-secret')
    out=io.BytesIO();writer.write(out);pid,sid,_=document(out.getvalue(),'.pdf')
    response=request(pid,sid);assert response.status_code==400 and '加密' in response.text
    writer=PdfWriter();writer.add_blank_page(width=4000,height=100)
    out=io.BytesIO();writer.write(out);pid,sid,_=document(out.getvalue(),'.pdf')
    response=request(pid,sid);assert response.status_code==400 and '过大' in response.text
    writer=PdfWriter();writer.add_blank_page(width=2500,height=2000)
    out=io.BytesIO();writer.write(out);pid,sid,_=document(out.getvalue(),'.pdf')
    response=request(pid,sid);assert response.status_code==400 and '过大' in response.text
    writer=PdfWriter()
    for _ in range(501):writer.add_blank_page(width=20,height=20)
    out=io.BytesIO();writer.write(out);pid,sid,_=document(out.getvalue(),'.pdf')
    response=request(pid,sid);assert response.status_code==400 and '500' in response.text


def test_pdf_changed_during_worker_never_returns_ocr_candidates(monkeypatch,tmp_path):
    import json
    from types import SimpleNamespace
    raw=multi_pdf(tmp_path);pid,sid,path=document(raw,'.pdf')
    source=get('source',sid)
    def worker(_):
        path.write_bytes(raw+b'changed')
        return SimpleNamespace(returncode=0,stdout=json.dumps({'ok':True,'result':{
            'source_file_sha256':source['content_hash'],'page':1,'human_review_required':True,
            'financial_facts_verified':False,'complete':False,'coordinate_space':'rendered_pdf_page_pixels_not_pdf_coordinates',
            'render_scale':2.0,'pdf_rotation_degrees':0,'pdf_page_count':2,'rendered_image_sha256':'0'*64,
            'image_width':100,'image_height':100}}).encode())
    monkeypatch.setattr(visual,'_run_ocr_worker',worker)
    assert request(pid,sid).status_code==400
    assert visual.list_proposals(pid)['total']==0
