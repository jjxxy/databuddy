"""Portable OCR selects only its own interpreter; missing runtime fails closed."""
from pathlib import Path
import sys
import pytest
from agent_service import visual_fact_service as service
from agent_service.offline_ocr import OCRError


def test_development_worker_uses_its_current_interpreter(monkeypatch):
    monkeypatch.delenv('ATLAS_DESKTOP_MODE',raising=False)
    assert service._ocr_worker_python()==sys.executable


def test_desktop_worker_is_absolute_packaged_and_never_falls_back(tmp_path,monkeypatch):
    monkeypatch.setenv('ATLAS_DESKTOP_MODE','1')
    monkeypatch.setenv('PATH',str(tmp_path/'untrusted'))
    fake_module=tmp_path/'app'/'agent_service'/'visual_fact_service.py'
    fake_module.parent.mkdir(parents=True);fake_module.write_text('',encoding='utf-8')
    monkeypatch.setattr(service,'__file__',str(fake_module))
    with pytest.raises(OCRError) as error:service._ocr_worker_python()
    assert error.value.code=='OCR_RUNTIME_UNAVAILABLE'
    exe=tmp_path/'app'/'runtime'/'ocr'/'python.exe';exe.parent.mkdir(parents=True);exe.write_bytes(b'fixture')
    assert service._ocr_worker_python()==str(exe.resolve()) and Path(service._ocr_worker_python()).is_absolute()


def test_desktop_refuses_external_symlink(tmp_path,monkeypatch):
    monkeypatch.setenv('ATLAS_DESKTOP_MODE','1')
    fake_module=tmp_path/'app'/'agent_service'/'visual_fact_service.py';fake_module.parent.mkdir(parents=True)
    fake_module.write_text('',encoding='utf-8');monkeypatch.setattr(service,'__file__',str(fake_module))
    outside=tmp_path/'external.exe';outside.write_bytes(b'fixture')
    exe=tmp_path/'app'/'runtime'/'ocr'/'python.exe';exe.parent.mkdir(parents=True)
    try:exe.symlink_to(outside)
    except OSError:pytest.skip('Symlink creation not permitted on this Windows host')
    with pytest.raises(OCRError) as error:service._ocr_worker_python()
    assert error.value.code=='OCR_RUNTIME_UNAVAILABLE'
