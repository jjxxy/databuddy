"""Exercise real OS handles, junction refusal and cross-process lock contention."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_service import platform_files as files


def test_real_file_roundtrip_and_exclusive_create(tmp_path):
    target = tmp_path / '账目.txt'
    with os.fdopen(files.create_regular_exclusive(target), 'wb') as output:
        output.write('精确金额 118.15'.encode('utf-8'))
        output.flush()
        os.fsync(output.fileno())
    with pytest.raises(FileExistsError):
        files.create_regular_exclusive(target)
    with os.fdopen(files.open_regular_read(target), 'rb') as source:
        assert source.read().decode('utf-8') == '精确金额 118.15'
    update = tmp_path / 'new.txt'
    update.write_bytes(b'new version')
    files.atomic_replace(update, target)
    assert target.read_bytes() == b'new version'
    files.fsync_file(target)
    assert not update.exists()
    assert files.fsync_directory(tmp_path) is (os.name != 'nt')


def test_hardlinks_are_not_read_as_independent_artifacts(tmp_path):
    original = tmp_path / 'original'
    original.write_bytes(b'original')
    alias = tmp_path / 'alias'
    os.link(original, alias)
    with pytest.raises(OSError):
        files.open_regular_read(alias)


def test_symbolic_link_leaf_is_not_followed(tmp_path):
    original = tmp_path / 'private'
    original.write_bytes(b'private')
    alias = tmp_path / 'alias'
    try:
        alias.symlink_to(original)
    except OSError:
        pytest.skip('OS account cannot create symbolic links')
    with pytest.raises(OSError):
        files.open_regular_read(alias)


def test_parent_traversal_is_refused(tmp_path):
    with pytest.raises(OSError):
        files.assert_plain_path(tmp_path / '..' / 'outside')


def test_shared_guards_allow_readers_but_exclude_writer_across_processes(tmp_path):
    lock = tmp_path / 'maintenance.lock'
    source = '''
import sys
from agent_service.platform_files import file_guard
try:
    with file_guard(sys.argv[1], exclusive=sys.argv[2]=='write'):
        print('acquired')
except BlockingIOError:
    print('blocked')
'''
    def probe(mode):
        result = subprocess.run([sys.executable, '-c', source, str(lock), mode],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()
    with files.file_guard(lock):
        assert probe('read') == 'acquired'
        assert probe('write') == 'blocked'
    with files.file_guard(lock, exclusive=True):
        assert probe('read') == 'blocked'
        assert probe('write') == 'blocked'
    assert probe('write') == 'acquired'


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction and handle semantics')
def test_windows_junction_ancestors_are_refused(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'private.txt').write_bytes(b'private')
    junction = tmp_path / 'junction'
    quote = lambda path: "'" + str(path).replace("'", "''") + "'"
    command = ('New-Item -ItemType Junction -Path ' + quote(junction)
               + ' -Target ' + quote(outside) + ' | Out-Null')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                             '-Command', command], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    with pytest.raises(OSError):
        files.open_regular_read(junction / 'private.txt')
    with pytest.raises(OSError):
        files.create_regular_exclusive(junction / 'new.txt')
    incoming = tmp_path / 'incoming'
    incoming.write_bytes(b'new')
    with pytest.raises(OSError):
        files.atomic_replace(incoming, junction / 'private.txt')
    assert incoming.read_bytes() == b'new'
    assert (outside / 'private.txt').read_bytes() == b'private'
    assert not (outside / 'new.txt').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows file-share semantics')
def test_open_read_handle_denies_leaf_replacement(tmp_path):
    target = tmp_path / 'artifact'
    target.write_bytes(b'validated')
    descriptor = files.open_regular_read(target)
    try:
        with pytest.raises(PermissionError):
            target.unlink()
        assert os.read(descriptor, 32) == b'validated'
    finally:
        os.close(descriptor)
    target.unlink()


@pytest.mark.skipif(os.name != 'nt', reason='Windows state-directory DACL')
def test_private_state_directory_removes_broad_inherited_grants(tmp_path):
    import ctypes
    from ctypes import wintypes
    import re
    path = tmp_path / 'managed-state'
    files.private_directory(path)
    child = path / 'synthetic-credentials.txt'
    child.write_text('synthetic only')
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                                              ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_void_p)]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]
    security.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    def trustees(item):
        descriptor = ctypes.c_void_p()
        assert security.GetNamedSecurityInfoW(str(item), 1, 4, None, None, None, None,
                                              ctypes.byref(descriptor)) == 0
        text = wintypes.LPWSTR()
        try:
            assert security.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, 1, 4, ctypes.byref(text), None)
            return {entry.split(';')[-1] for entry in re.findall(r'\(([^)]*)\)', text.value)}
        finally:
            if text:
                files._kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
            files._kernel.LocalFree(descriptor)
    expected = {'SY', 'BA', files._user_sid()}
    assert trustees(path) == expected
    assert trustees(child) == expected
