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


def test_worker_uses_isolated_environment_and_cleans_only_own_scratch(monkeypatch,tmp_path):
    import os,subprocess
    from types import SimpleNamespace
    seen={}
    monkeypatch.setenv('ANTHROPIC_API_KEY','sk-fixture-secret')
    monkeypatch.setenv('APP_TOKEN','fixture-token')
    monkeypatch.setenv('PYTHONPATH',str(tmp_path/'untrusted'))
    monkeypatch.setenv('PYTHONHOME',str(tmp_path/'untrusted-home'))
    monkeypatch.setenv('PATH',str(tmp_path/'untrusted-bin'))
    user_file=tmp_path/'user-data';user_file.write_text('untouched')
    def worker(args,request,**kwargs):
        seen.update(kwargs)
        assert '-E' in args and '-s' in args
        assert Path(kwargs['env']['TEMP']).is_dir()
        return SimpleNamespace(returncode=0,stdout=b'{}')
    monkeypatch.setattr(service,'_bounded_worker',worker)
    service._run_ocr_worker(b'{}')
    assert set(seen['env']) <= {'PATH','TEMP','TMP','SystemRoot','WINDIR'}
    assert seen['env']['PATH']=='' and seen['env']['TMP']==seen['env']['TEMP']
    assert not Path(seen['env']['TEMP']).exists()
    assert user_file.read_text()=='untouched'


@pytest.mark.parametrize('stream',['stdout','stderr'])
def test_bounded_worker_kills_excess_output_without_buffering(stream,tmp_path):
    import os
    program='import sys,time; sys.'+stream+'.buffer.write(b"x"*1000000); sys.'+stream+'.flush(); time.sleep(10)'
    with pytest.raises(OCRError) as error:
        service._bounded_worker([sys.executable,'-c',program],b'{}',cwd=tmp_path,env=dict(os.environ),
                                timeout=3,stdout_limit=4096,stderr_limit=4096)
    assert error.value.code=='OCR_OUTPUT_BUDGET_EXCEEDED'


def test_bounded_worker_timeout_when_worker_never_reads_input(tmp_path):
    import os,subprocess,time
    started=time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        service._bounded_worker([sys.executable,'-c','import time;time.sleep(10)'],b'x'*1000000,
            cwd=tmp_path,env=dict(os.environ),timeout=0.5)
    assert time.monotonic()-started<4


def test_bounded_worker_reads_both_streams_and_exact_limit(tmp_path):
    import os
    program='import sys;data=sys.stdin.buffer.read();sys.stdout.buffer.write(data);sys.stderr.write("warn")'
    result=service._bounded_worker([sys.executable,'-c',program],b'ok',cwd=tmp_path,env=dict(os.environ),
                                   timeout=3,stdout_limit=2,stderr_limit=4)
    assert result.returncode==0 and result.stdout==b'ok' and result.stderr==b'warn'
