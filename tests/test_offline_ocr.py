"""Actual offline OCR integration; no financial assertion is auto-approved."""
import hashlib
import importlib.metadata
import importlib.util
import io
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from agent_service import offline_ocr


def make_image(tmp_path, text=False):
    image=Image.new('RGB',(1100,300),'white')
    if text:
        font_path=Path('C:/Windows/Fonts/simhei.ttf')
        if not font_path.is_file():pytest.skip('Synthetic Chinese font is unavailable')
        draw=ImageDraw.Draw(image);font=ImageFont.truetype(str(font_path),52)
        draw.text((20,20),'2026年10月 合肥办事处 净收入',fill='black',font=font)
        draw.text((20,115),'金额 123.45 元',fill='black',font=font)
    path=tmp_path/'source.png';image.save(path)
    return path,hashlib.sha256(path.read_bytes()).hexdigest()


def test_digest_and_image_budgets_reject_without_runtime(tmp_path):
    path,digest=make_image(tmp_path)
    with pytest.raises(offline_ocr.OCRError) as error:offline_ocr.recognize_image(path,'0'*64)
    assert error.value.code=='OCR_SOURCE_CHANGED'
    with pytest.raises(offline_ocr.OCRError) as error:offline_ocr.recognize_image(path,digest,page=True)
    assert error.value.code=='OCR_PAGE_UNSUPPORTED'
    path.write_bytes(b'not a real image');bad_hash=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(offline_ocr.OCRError) as error:offline_ocr.recognize_image(path,bad_hash)
    assert error.value.code=='OCR_IMAGE_INVALID'


def test_missing_model_never_fetches_anything(tmp_path,monkeypatch):
    path,digest=make_image(tmp_path)
    def network_denied(*args,**kwargs):raise AssertionError('network access forbidden')
    monkeypatch.setattr(socket.socket,'connect',network_denied)
    monkeypatch.setattr(socket,'getaddrinfo',network_denied)
    monkeypatch.setattr(offline_ocr.importlib.metadata,'version',lambda package:'3.10.0')
    root=tmp_path/'isolated';root.mkdir();origin=root/'__init__.py';origin.write_text('')
    monkeypatch.setattr(offline_ocr.importlib.util,'find_spec',lambda name:SimpleNamespace(origin=str(origin)))
    with pytest.raises(offline_ocr.OCRError) as error:offline_ocr.recognize_image(path,digest)
    assert error.value.code=='OCR_MODEL_UNAVAILABLE'


def test_tampered_local_models_refused_without_fallback(tmp_path,monkeypatch):
    path,digest=make_image(tmp_path)
    monkeypatch.setattr(offline_ocr.importlib.metadata,'version',lambda package:'3.10.0')
    root=tmp_path/'isolated';root.mkdir();origin=root/'__init__.py';origin.write_text('')
    model_dir=root/'models';model_dir.mkdir()
    for filename,_ in offline_ocr.MODEL_FILES.values():(model_dir/filename).write_bytes(b'tampered')
    monkeypatch.setattr(offline_ocr.importlib.util,'find_spec',lambda name:SimpleNamespace(origin=str(origin)))
    def network_denied(*args,**kwargs):raise AssertionError('network access forbidden')
    monkeypatch.setattr(socket.socket,'connect',network_denied)
    with pytest.raises(offline_ocr.OCRError) as error:offline_ocr.recognize_image(path,digest)
    assert error.value.code=='OCR_MODEL_MISMATCH'


def test_actual_chinese_ocr_uses_local_models_only(tmp_path,monkeypatch):
    try:version=importlib.metadata.version('rapidocr')
    except importlib.metadata.PackageNotFoundError:pytest.skip('Optional OCR runtime not installed')
    if version!='3.10.0':pytest.skip('Unverified OCR runtime version')
    path,digest=make_image(tmp_path,text=True)
    calls=[]
    def network_denied(*args,**kwargs):calls.append(True);raise AssertionError('network access forbidden')
    monkeypatch.setattr(socket.socket,'connect',network_denied)
    monkeypatch.setattr(socket,'getaddrinfo',network_denied)
    result=offline_ocr.recognize_image(path,digest)
    assert result['source_file_sha256']==digest and result['page']==1
    assert result['ocr_executed'] and result['human_review_required'] and not result['financial_facts_verified']
    assert not result['complete'] and result['coordinate_space']=='original_image_pixels_without_exif_rotation'
    combined=''.join(item['text'] for item in result['lines'])
    assert '合肥办事处' in combined and '净收入' in combined and '123.45' in combined, combined
    assert all(item['human_review_required'] and len(item['box'])==4 for item in result['lines'])
    assert not calls
