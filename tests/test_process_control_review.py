"""Adversarial desktop lifecycle checks, using only synthetic local data."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_service.process_control import spawn


def test_explicit_empty_environment_does_not_inherit_parent_credentials(monkeypatch):
    monkeypatch.setenv('ATLAS_REVIEW_PRIVATE_SENTINEL', 'synthetic-only-private-value')
    script = ('import os,json;print(json.dumps({"inherited":'
              'os.getenv("ATLAS_REVIEW_PRIVATE_SENTINEL") is not None}))')
    process = spawn([sys.executable, '-c', script], env={},
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output, error = process.process.communicate(timeout=10)
    assert process.wait() == 0, error.decode('utf-8', errors='replace')
    assert json.loads(output)['inherited'] is False


@pytest.mark.skipif(os.name != 'nt', reason='Windows console process creation')
def test_actual_target_has_no_visible_console_not_just_the_wrapper():
    # GetConsoleProcessList returns a nonzero count even for this host's hidden
    # CREATE_NO_WINDOW processes. Check a real window handle and visibility.
    script = '''
import ctypes,json
from ctypes import wintypes
kernel=ctypes.WinDLL('kernel32')
kernel.GetConsoleWindow.restype=wintypes.HWND
user=ctypes.WinDLL('user32')
user.IsWindowVisible.argtypes=[wintypes.HWND]
user.IsWindowVisible.restype=wintypes.BOOL
handle=kernel.GetConsoleWindow()
print(json.dumps({'visible_console':bool(handle and user.IsWindowVisible(handle))}))
'''
    process = spawn([sys.executable, '-c', script],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output, error = process.process.communicate(timeout=10)
    assert process.wait() == 0, error.decode('utf-8', errors='replace')
    assert json.loads(output)['visible_console'] is False


@pytest.mark.skipif(os.name != 'nt', reason='Windows state-root junction')
def test_state_root_junction_is_rejected_before_acl_or_credential_changes(tmp_path, monkeypatch):
    import portable_runtime
    from agent_service import platform_files
    outside = tmp_path / 'untouched-business-folder'
    outside.mkdir()
    original = outside / 'original.txt'
    original.write_bytes(b'original business bytes')
    alias = tmp_path / 'state-junction'
    quote = lambda path: "'" + str(path).replace("'", "''") + "'"
    command = ('New-Item -ItemType Junction -Path ' + quote(alias)
               + ' -Target ' + quote(outside) + ' | Out-Null')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                             '-Command', command], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    called = []
    def forbidden_acl_change(path):
        called.append(path)
        raise AssertionError('State-root validation followed a junction before ACL changes')
    monkeypatch.setattr(platform_files, 'private_directory', forbidden_acl_change)
    args = SimpleNamespace(state_dir=str(alias), workspace_dir=None, port=0, model_config=None, offline=True, wait=1)
    with pytest.raises(OSError):
        portable_runtime.serve(args)
    assert called == []
    assert original.read_bytes() == b'original business bytes'
    assert not (outside / 'credentials.json').exists()


def make_junction(alias,target):
    quote=lambda path:"'"+str(path).replace("'","''")+"'"
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
        'New-Item -ItemType Junction -Path '+quote(alias)+' -Target '+quote(target)+' | Out-Null'],
        capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr.decode(errors='replace')


@pytest.mark.skipif(os.name!='nt',reason='Windows workspace-root junction')
def test_workspace_junction_is_rejected_before_creating_private_state(tmp_path,monkeypatch):
    import portable_runtime
    from agent_service import platform_files
    outside=tmp_path/'outside';outside.mkdir();(outside/'original.csv').write_bytes(b'original')
    alias=tmp_path/'selected-junction';make_junction(alias,outside)
    state=tmp_path/'not-created'
    def forbidden_acl_change(path):
        raise AssertionError('Selected folder junction must be refused before any ACL changes')
    monkeypatch.setattr(platform_files,'private_directory',forbidden_acl_change)
    args=SimpleNamespace(state_dir=str(state),workspace_dir=str(alias),port=0,model_config=None,offline=True,wait=1)
    with pytest.raises(OSError):portable_runtime.serve(args)
    assert not state.exists() and not (outside/'.atlas').exists()
    assert (outside/'original.csv').read_bytes()==b'original'


@pytest.mark.skipif(os.name!='nt',reason='Windows per-workspace state junction')
def test_scoped_state_junction_is_rejected_before_target_acl_or_credentials(tmp_path,monkeypatch):
    import portable_runtime
    from agent_service import platform_files
    selected=tmp_path/'business';selected.mkdir()
    outside=tmp_path/'outside';outside.mkdir();(outside/'original.txt').write_bytes(b'original')
    state=tmp_path/'state';(state/'workspaces').mkdir(parents=True)
    identity=hashlib.sha256(os.path.normcase(str(selected)).encode('utf-8')).hexdigest()[:24]
    make_junction(state/'workspaces'/identity,outside)
    calls=[]
    monkeypatch.setattr(platform_files,'private_directory',lambda path:calls.append(Path(path)))
    args=SimpleNamespace(state_dir=str(state),workspace_dir=str(selected),port=0,model_config=None,offline=True,wait=1)
    with pytest.raises(OSError):portable_runtime.serve(args)
    assert calls==[state]
    assert not (outside/'credentials.json').exists() and not (selected/'.atlas').exists()
    assert (outside/'original.txt').read_bytes()==b'original'
