import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

import run_debug
from agent_service import process_control


def test_debug_detects_git_and_desktop_can_execute_it(monkeypatch,tmp_path):
    installed=shutil.which('git')
    if not installed:pytest.skip('Real Git is required for source launcher acceptance')
    monkeypatch.delenv('ATLAS_GIT_BINARY',raising=False)
    monkeypatch.setenv('ATLAS_DESKTOP_MODE','1')
    selected=run_debug.configure_git()
    assert Path(selected).is_absolute() and selected==os.environ['ATLAS_GIT_BINARY']
    result=subprocess.run([process_control.executable('git'),'init',str(tmp_path/'actual-repository')],
                          capture_output=True,text=True,timeout=15)
    assert result.returncode==0 and (tmp_path/'actual-repository'/'.git').is_dir()


def test_explicit_git_is_preserved_and_invalid_override_does_not_fall_back(monkeypatch,tmp_path):
    binary=tmp_path/'Git with spaces.exe';binary.write_bytes(b'fixture-only')
    monkeypatch.setattr(run_debug.shutil,'which',lambda *a,**k:pytest.fail('Explicit binary must not search PATH'))
    env={'ATLAS_GIT_BINARY':str(binary)}
    assert run_debug.configure_git(env)==str(binary)
    with pytest.raises(ValueError,match='ATLAS_GIT_BINARY'):run_debug.configure_git({'ATLAS_GIT_BINARY':'relative/git'})


def test_missing_git_fails_before_any_application_start(monkeypatch):
    monkeypatch.setattr(run_debug.shutil,'which',lambda *a,**k:None)
    with pytest.raises(ValueError,match='安装 Git'):run_debug.configure_git({})


def test_debug_discovers_bundled_opencode_when_not_on_path(monkeypatch,tmp_path):
    monkeypatch.setattr(run_debug,'ROOT',tmp_path)
    monkeypatch.setattr(run_debug.shutil,'which',lambda *a,**k:None)
    binary=tmp_path/'runtime'/'opencode'/('opencode.exe' if os.name=='nt' else 'opencode')
    binary.parent.mkdir(parents=True);binary.write_bytes(b'test executable');binary.chmod(0o755)
    env={'PATH':''}
    assert run_debug.configure_opencode(env)
    assert env['ATLAS_OPENCODE_BINARY']==str(binary.resolve())
    assert env['ATLAS_OPENCODE_RUNTIME']=='1'


def test_debug_honors_explicit_opencode_and_legacy_mode(monkeypatch):
    monkeypatch.setattr(run_debug.shutil,'which',lambda *a,**k:pytest.fail('explicit binary must not search PATH'))
    env={'ATLAS_OPENCODE_BINARY':'/explicit/opencode','ATLAS_OPENCODE_RUNTIME':'0'}
    assert not run_debug.configure_opencode(env)
    assert env['ATLAS_OPENCODE_BINARY']=='/explicit/opencode'


def test_debug_reports_compatibility_mode_when_opencode_not_found(monkeypatch,tmp_path):
    monkeypatch.setattr(run_debug,'ROOT',tmp_path)
    monkeypatch.setattr(run_debug.shutil,'which',lambda *a,**k:None)
    env={'PATH':''}
    assert not run_debug.configure_opencode(env)
    assert env['ATLAS_OPENCODE_RUNTIME']=='0' and 'ATLAS_OPENCODE_BINARY' not in env


@pytest.mark.parametrize('configured,expected',[('1','opencode'),('0','legacy')])
def test_running_agent_heartbeat_reports_selected_planning_mode(monkeypatch,configured,expected):
    from agent_service import production_release,runtime
    saved=[]
    monkeypatch.setattr(production_release,'process_build_fingerprint',lambda:'fixture-build')
    monkeypatch.setattr(runtime,'put',lambda kind,value,identity:saved.append((kind,value,identity)))
    monkeypatch.setenv('ATLAS_OPENCODE_RUNTIME',configured)
    runtime.heartbeat('agent')
    assert len(saved)==2 and all(value['planner_mode']==expected for _,value,_ in saved)


def test_packaged_desktop_keeps_its_strict_binary_requirement(monkeypatch):
    monkeypatch.setenv('ATLAS_DESKTOP_MODE','1');monkeypatch.delenv('ATLAS_GIT_BINARY',raising=False)
    with pytest.raises(ValueError,match='桌面包未提供'):process_control.executable('git')


def test_debug_main_scopes_new_state_and_preserves_credentials_on_restart(monkeypatch,tmp_path,capsys):
    from agent_service import cloud
    installed=shutil.which('git')
    if not installed:pytest.skip('Git required')
    environment={k:v for k,v in os.environ.items() if not k.startswith(('ATLAS_','APP_TOKEN','DATA_DIR','DATABASE_URL'))}
    monkeypatch.setattr(run_debug.os,'environ',environment)
    calls=[]
    monkeypatch.setattr(cloud,'main',lambda:calls.append(dict(os.environ)) or 0)
    state=tmp_path/'debug-state'
    for _ in range(2):assert run_debug.main(['--state-dir',str(state),'--port','8129'])==0
    assert calls[0]['ATLAS_DESKTOP_MODE']=='1' and Path(calls[0]['ATLAS_GIT_BINARY']).is_absolute()
    assert calls[0]['ATLAS_API_HOST']=='127.0.0.1'
    assert calls[0]['APP_TOKEN']==calls[1]['APP_TOKEN']
    assert calls[0]['DATABASE_URL']=='sqlite:///'+str(state/'data'/'metadata.sqlite')
    assert json.loads((state/'credentials.json').read_text())['APP_TOKEN']==calls[0]['APP_TOKEN']
    assert calls[0]['APP_TOKEN'] not in capsys.readouterr().out
    environment['APP_TOKEN']='explicit-debug-owner-token-123456'
    assert run_debug.main(['--state-dir',str(state),'--port','8129'])==0
    assert calls[-1]['APP_TOKEN']=='explicit-debug-owner-token-123456'
    assert 'APP_TOKEN 环境变量' in capsys.readouterr().out
    assert json.loads((state/'credentials.json').read_text())['APP_TOKEN']==calls[0]['APP_TOKEN']
