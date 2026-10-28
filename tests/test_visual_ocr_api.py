"""Authenticated OCR endpoint: verified image candidates only, never approval."""
import importlib.metadata
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_service import visual_fact_service as visual
from agent_service.api import app
from agent_service.config import DATA
from agent_service.locks import FileLock
from agent_service.store import get, patch, put, uid
from test_fact_service import document
from test_offline_ocr import make_image


def input_value(sid):
    source=get('source',sid)
    return {'expected_revision':source['revision'],'expected_sha256':source['content_hash'],'page':1}


def api():return TestClient(app),{'Authorization':'Bearer '+os.environ['APP_TOKEN']}


def test_ocr_endpoint_auth_project_format_and_source_version(tmp_path):
    image,_=make_image(tmp_path);pid,sid,_=document(image.read_bytes(),'.png')
    client,headers=api();root=f'/api/projects/{pid}/sources/{sid}/ocr';payload=input_value(sid)
    assert client.post(root,json=payload).status_code in (401,403)
    other=put('project',{'name':'other'},'p_'+uid())['id']
    assert client.post(f'/api/projects/{other}/sources/{sid}/ocr',json=payload,headers=headers).status_code==400
    assert client.post(root,json=dict(payload,expected_revision=2),headers=headers).status_code==400
    assert client.post(root,json=dict(payload,expected_sha256='0'*64),headers=headers).status_code==400
    assert client.post(root,json=dict(payload,page=2),headers=headers).status_code==400
    assert client.post(root,json=dict(payload,page=True),headers=headers).status_code==422
    pdfpid,pdfsid,_=document(b'%PDF-not-a-real-file','.pdf')
    response=client.post(f'/api/projects/{pdfpid}/sources/{pdfsid}/ocr',json=input_value(pdfsid),headers=headers)
    assert response.status_code==400
    assert visual.list_proposals(pid)['total']==0


def test_busy_and_timeout_are_user_readable_and_never_create_candidates(tmp_path,monkeypatch):
    image,_=make_image(tmp_path);pid,sid,_=document(image.read_bytes(),'.png');client,headers=api()
    root=f'/api/projects/{pid}/sources/{sid}/ocr';payload=input_value(sid)
    with FileLock(str(DATA/'visual-ocr-execution.lock'),timeout=0):
        response=client.post(root,json=payload,headers=headers)
        assert response.status_code==400 and '稍后重试' in response.json()['detail']
    def timeout(*args,**kwargs):raise subprocess.TimeoutExpired('worker',60)
    monkeypatch.setattr(visual,'_bounded_worker',timeout)
    response=client.post(root,json=payload,headers=headers)
    assert response.status_code==400 and '超时' in response.json()['detail']
    assert visual.list_proposals(pid)['total']==0


def test_source_changed_during_worker_cannot_return_stale_candidate(tmp_path,monkeypatch):
    image,_=make_image(tmp_path);pid,sid,_=document(image.read_bytes(),'.png');client,headers=api()
    payload=input_value(sid)
    def worker(*args,**kwargs):
        patch('source',sid,revision=2)
        result={'ok':True,'result':{'source_file_sha256':payload['expected_sha256'],'page':1,
              'human_review_required':True,'financial_facts_verified':False,'complete':False}}
        return SimpleNamespace(returncode=0,stdout=json.dumps(result).encode())
    monkeypatch.setattr(visual,'_bounded_worker',worker)
    response=client.post(f'/api/projects/{pid}/sources/{sid}/ocr',json=payload,headers=headers)
    assert response.status_code==400 and '变化' in response.json()['detail']
    assert visual.list_proposals(pid)['total']==0


def test_missing_runtime_message(tmp_path):
    try:importlib.metadata.version('rapidocr')
    except importlib.metadata.PackageNotFoundError:pass
    else:pytest.skip('This case runs when optional OCR is missing')
    image,_=make_image(tmp_path);pid,sid,_=document(image.read_bytes(),'.png');client,headers=api()
    response=client.post(f'/api/projects/{pid}/sources/{sid}/ocr',json=input_value(sid),headers=headers)
    assert response.status_code==400 and 'OCR' in response.json()['detail']
    assert visual.list_proposals(pid)['total']==0


def test_real_chinese_image_ocr_over_api(tmp_path):
    try:version=importlib.metadata.version('rapidocr')
    except importlib.metadata.PackageNotFoundError:pytest.skip('Optional OCR runtime is unavailable')
    if version!='3.10.0':pytest.skip('Unverified OCR version')
    image,_=make_image(tmp_path,text=True);pid,sid,_=document(image.read_bytes(),'.png');client,headers=api()
    payload=input_value(sid)
    response=client.post(f'/api/projects/{pid}/sources/{sid}/ocr',json=payload,headers=headers)
    assert response.status_code==200,response.text
    result=response.json();combined=''.join(line['text'] for line in result['lines'])
    assert '合肥办事处' in combined and '123.45' in combined
    assert result['source_id']==sid and result['source_revision']==1 and result['source_file_sha256']==payload['expected_sha256']
    assert result['image_width']==1100 and result['image_height']==300 and result['page']==1
    assert result['human_review_required'] and result['ocr_executed'] and not result['financial_facts_verified'] and not result['complete']
    assert all(line['human_review_required'] and len(line['box'])==4 for line in result['lines'])
    assert visual.list_proposals(pid)['total']==0
