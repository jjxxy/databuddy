"""Native Windows folder guarantees, including junctions and real ACLs."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

import pytest

from agent_service import local_storage,local_workspace as local,platform_files
from test_local_workspace import local_folder

pytestmark=pytest.mark.skipif(os.name!='nt',reason='Native Windows local-folder semantics')


def junction(alias,target):
    quote=lambda value:"'"+str(value).replace("'","''")+"'"
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
        'New-Item -ItemType Junction -Path '+quote(alias)+' -Target '+quote(target)+' | Out-Null'],
        capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr.decode(errors='replace')


def acl(path):
    security=ctypes.WinDLL('advapi32',use_last_error=True)
    security.GetNamedSecurityInfoW.argtypes=[wintypes.LPWSTR,ctypes.c_int,wintypes.DWORD,
        ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p)]
    security.GetNamedSecurityInfoW.restype=wintypes.DWORD
    security.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes=[ctypes.c_void_p,wintypes.DWORD,
        wintypes.DWORD,ctypes.POINTER(wintypes.LPWSTR),ctypes.c_void_p]
    security.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype=wintypes.BOOL
    descriptor,text=ctypes.c_void_p(),wintypes.LPWSTR()
    assert security.GetNamedSecurityInfoW(str(path),1,4,None,None,None,None,ctypes.byref(descriptor))==0
    try:
        assert security.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor,1,4,ctypes.byref(text),None)
        return text.value
    finally:
        if text:platform_files._kernel.LocalFree(ctypes.cast(text,ctypes.c_void_p))
        platform_files._kernel.LocalFree(descriptor)


def test_relative_handles_reject_junctions_streams_device_names_and_case_aliases(local_folder,tmp_path):
    root,pid=local_folder
    (root/'资料').mkdir();original=root/'safe.csv';original.write_bytes(b'id,n\n1,100\n')
    outside=tmp_path/'private';outside.mkdir();(outside/'secret.csv').write_bytes(b'private')
    junction(root/'redirect',outside)
    assert {item['name'] for item in local.files()['entries']}=={'资料','safe.csv'}
    for name in ('redirect/secret.csv','safe.csv:hidden.csv','CON.csv','safe.csv ',
                 'ATLAS工作区/code.txt','atlas成果/result.csv','SECRETS.JSON','../private/secret.csv'):
        with pytest.raises((ValueError,OSError)):local._copy_input(name)
    with pytest.raises(ValueError):local.import_files(pid,['safe.csv','SAFE.csv'],'case-review-request')
    assert original.read_bytes()==b'id,n\n1,100\n'
    assert (outside/'secret.csv').read_bytes()==b'private'


def test_live_directory_handles_pin_names_without_changing_global_os(local_folder,tmp_path):
    root,pid=local_folder;(root/'资料').mkdir()
    before=getattr(os,'O_DIRECTORY',None)
    with local._directory('资料') as descriptor:
        with pytest.raises(PermissionError):(root/'资料').rename(root/'swapped')
        with pytest.raises(PermissionError):root.rename(tmp_path/'moved-root')
        assert local.os.listdir(descriptor)==[]
    assert getattr(os,'O_DIRECTORY',None)==before
    assert local.os is not os


def test_snapshot_detects_same_size_write_even_when_mtime_is_restored(local_folder,monkeypatch):
    root,pid=local_folder
    original=root/'income.csv';original.write_bytes(b'id,n\n1,2\n')
    before=original.stat()
    actual_fstat=local.os.fstat;calls=0
    def fstat(descriptor):
        nonlocal calls
        info=actual_fstat(descriptor)
        if stat.S_ISREG(info.st_mode) and info.st_size==9:
            calls+=1
            if calls==2:
                original.write_bytes(b'id,n\n1,9\n')
                os.utime(original,ns=(before.st_atime_ns,before.st_mtime_ns))
                return actual_fstat(descriptor)
        return info
    monkeypatch.setattr(local.os,'fstat',fstat)
    with pytest.raises(ValueError,match='变化|替换'):local._copy_input('income.csv')


def test_storage_uses_sid_owner_and_only_protects_managed_metadata(tmp_path,monkeypatch):
    from agent_service.windows_dirfd import os as windows,owner_sid
    root=tmp_path/'财务 月结';root.mkdir();original=root/'原始底稿.csv';original.write_bytes(b'id,n\n1,100\n')
    original_hash=hashlib.sha256(original.read_bytes()).hexdigest()
    before=(acl(root),acl(original))
    monkeypatch.setenv('ATLAS_LOCAL_WORKSPACE',str(root));monkeypatch.setenv('DATA_DIR',str(root/'.atlas'))
    metadata=local_storage.prepare_storage(None,None)
    assert local_storage.prepare_storage(None,None)==metadata
    assert json.loads((metadata/'local-workspace.identity').read_text())=={'format':'atlas-local-v1'}
    handle=windows.open(metadata,windows.O_DIRECTORY|windows.O_RDONLY)
    try:assert owner_sid(handle)==platform_files._user_sid()
    finally:windows.close(handle)
    assert 'D:P' in acl(metadata)
    assert (acl(root),acl(original))==before
    assert hashlib.sha256(original.read_bytes()).hexdigest()==original_hash


def test_existing_unmarked_or_foreign_metadata_is_not_adopted(tmp_path,monkeypatch):
    from agent_service import windows_dirfd
    root=tmp_path/'work';root.mkdir();metadata=root/'.atlas';metadata.mkdir()
    original=metadata/'do-not-touch.txt';original.write_bytes(b'private existing folder')
    before=acl(metadata)
    monkeypatch.setenv('ATLAS_LOCAL_WORKSPACE',str(root));monkeypatch.setenv('DATA_DIR',str(metadata))
    with pytest.raises(ValueError):local_storage.prepare_storage(None,None)
    assert original.read_bytes()==b'private existing folder' and acl(metadata)==before
    (metadata/'local-workspace.identity').write_text(json.dumps({'format':'atlas-local-v1'}))
    monkeypatch.setattr(windows_dirfd,'owner_sid',lambda directory:'S-1-5-21-foreign-test-owner')
    with pytest.raises(ValueError,match='其他系统用户'):local_storage.prepare_storage(None,None)
    assert not (metadata/'home').exists() and acl(metadata)==before


def test_same_open_stage_handle_is_published_without_overwriting_existing_export(local_folder):
    root,pid=local_folder
    with local._directory(local.OUTPUT_DIR+'/'+pid,managed=True,create=True) as parent:
        local.os.mkdir('.stage',dir_fd=parent)
        stage=local.os.open('.stage',local.os.O_DIRECTORY|local.os.O_RDONLY|local.os.O_ATLAS_STAGE,dir_fd=parent)
        try:
            (local.os.descriptor_path(stage)/'value.txt').write_bytes(b'verified')
            local.os.mkdir('existing',dir_fd=parent)
            (parent.path/'existing'/'original.txt').write_bytes(b'old export')
            with pytest.raises(FileExistsError):local.os.publish_directory(stage,'existing',parent)
            local.os.publish_directory(stage,'new-export',parent)
            assert (parent.path/'existing'/'original.txt').read_bytes()==b'old export'
            assert (parent.path/'new-export'/'value.txt').read_bytes()==b'verified'
        finally:local.os.close(stage)


def test_failed_stage_cleanup_removes_junction_leaf_without_visiting_target(local_folder,tmp_path):
    root,pid=local_folder;outside=tmp_path/'untouched';outside.mkdir();(outside/'original').write_bytes(b'original')
    with local._directory(local.OUTPUT_DIR+'/'+pid,managed=True,create=True) as parent:
        local.os.mkdir('.partial-stage',dir_fd=parent)
        stage=local.os.open('.partial-stage',local.os.O_DIRECTORY|local.os.O_RDONLY|local.os.O_ATLAS_STAGE,dir_fd=parent)
        junction(local.os.descriptor_path(stage)/'redirect',outside)
        local.os.close(stage)
        local.os.remove_tree('.partial-stage',dir_fd=parent)
        assert not (parent.path/'.partial-stage').exists()
    assert (outside/'original').read_bytes()==b'original'
