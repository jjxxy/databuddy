"""Small filesystem adapters; Windows handles never follow reparse points.

The desktop data directory belongs to the current OS user. Windows permissions
are inherited from that private directory, rather than pretending POSIX mode
bits configure an ACL. File contents are flushed on both platforms. Windows
does not expose POSIX directory fsync; recovery receipts still verify every
published byte after a process restart, without promising power-loss durability.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat


def _absolute(path):
    value = Path(path)
    if '..' in value.parts:
        raise OSError(errno.EINVAL, 'Parent traversal is not allowed')
    return value.absolute()


def assert_plain_path(path, *, missing_ok=True):
    """Reject symbolic links and Windows junctions in every existing component."""
    value = _absolute(path)
    chain = [*reversed(value.parents), value]
    for component in chain:
        try:
            info = component.lstat()
        except FileNotFoundError:
            if missing_ok:
                break
            raise
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise OSError(errno.ELOOP, 'Links and reparse points are not allowed')
    return value


if os.name == 'nt':
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.HANDLE]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL
    _INVALID = ctypes.c_void_p(-1).value
    _REPARSE = 0x400
    _DIRECTORY = 0x10

    class _FileInformation(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD), ('creation', wintypes.FILETIME),
                    ('access', wintypes.FILETIME), ('write', wintypes.FILETIME),
                    ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD),
                    ('size_low', wintypes.DWORD), ('links', wintypes.DWORD),
                    ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]

    _kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInformation)]
    _kernel.GetFileInformationByHandle.restype = wintypes.BOOL

    def _information(handle):
        info = _FileInformation()
        if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return info

    def _open_handle(path, access, disposition, *, directory=False):
        # OPEN_REPARSE_POINT inspects the link itself, not its target. Directory
        # ancestors are also held without SHARE_DELETE until the leaf is open.
        flags = 0x00200000 | (0x02000000 if directory else 0)
        value = str(path)
        if not value.startswith('\\\\?\\'):
            value = '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value
        handle = _kernel.CreateFileW(value, access, 0x1 | 0x2, None,
                                     disposition, flags, None)
        if handle == _INVALID:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = _information(handle)
            if info.attributes & _REPARSE or bool(info.attributes & _DIRECTORY) != directory:
                raise OSError(errno.ELOOP, 'Expected an ordinary file or directory, not a reparse point')
            return handle
        except BaseException:
            _kernel.CloseHandle(handle)
            raise

    @contextmanager
    def _parents(path):
        handles = []
        try:
            for parent in reversed(path.parents):
                handles.append(_open_handle(parent, 0x80, 3, directory=True))
            yield
        finally:
            for handle in reversed(handles):
                _kernel.CloseHandle(handle)

    def _windows_descriptor(path, *, create=False, writable=False):
        value = _absolute(path)
        with _parents(value):
            handle = _open_handle(value, 0x80000000 | (0x40000000 if writable else 0),
                                  1 if create else 3)
        try:
            flags = os.O_BINARY | (os.O_RDWR if writable else os.O_RDONLY)
            descriptor = msvcrt.open_osfhandle(handle, flags)
        except BaseException:
            _kernel.CloseHandle(handle)
            raise
        return descriptor


def open_regular_read(path):
    """Return an OS descriptor referring to an ordinary, non-reparse file."""
    if os.name == 'nt':
        descriptor = _windows_descriptor(path)
    else:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise OSError(errno.EINVAL, 'Expected an ordinary independent file')
    return descriptor


def create_regular_exclusive(path, mode=0o600):
    if os.name == 'nt':
        return _windows_descriptor(path, create=True, writable=True)
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)


def chmod_descriptor(descriptor, mode=0o600):
    if os.name != 'nt':
        os.fchmod(descriptor, mode)
    # Windows files inherit the desktop user's private data-directory ACL.


def atomic_replace(source, destination):
    """Replace without traversing a Windows junction in either directory chain."""
    if os.name == 'nt':
        source, destination = _absolute(source), _absolute(destination)
        with _parents(source), _parents(destination):
            assert_plain_path(source, missing_ok=False)
            assert_plain_path(destination)
            os.replace(source, destination)
    else:
        os.replace(source, destination)


def fsync_directory(path):
    if os.name == 'nt':
        assert_plain_path(path, missing_ok=False)
        return False
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def fsync_file(path):
    """Flush an owned staged file; Windows CRT _commit requires write access."""
    if os.name == 'nt':
        descriptor = _windows_descriptor(path, writable=True)
    else:
        descriptor = open_regular_read(path)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if os.name == 'nt':
    _security = ctypes.WinDLL('advapi32', use_last_error=True)
    _kernel.GetCurrentProcess.argtypes = []
    _kernel.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel.LocalFree.restype = ctypes.c_void_p
    _security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.HANDLE)]
    _security.OpenProcessToken.restype = wintypes.BOOL
    _security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                            ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD)]
    _security.GetTokenInformation.restype = wintypes.BOOL
    _security.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    _security.ConvertSidToStringSidW.restype = wintypes.BOOL
    _security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD)]
    _security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _security.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                                                  ctypes.POINTER(ctypes.c_void_p),
                                                  ctypes.POINTER(wintypes.BOOL)]
    _security.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    _security.SetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                                        ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_void_p, ctypes.c_void_p]
    _security.SetSecurityInfo.restype = wintypes.DWORD

    def _user_sid():
        token = wintypes.HANDLE()
        if not _security.OpenProcessToken(_kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = wintypes.DWORD()
            _security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            value = ctypes.create_string_buffer(size.value)
            if not _security.GetTokenInformation(token, 1, value, size, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            sid = ctypes.cast(value, ctypes.POINTER(ctypes.c_void_p)).contents.value
            text = wintypes.LPWSTR()
            if not _security.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                return text.value
            finally:
                _kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        finally:
            _kernel.CloseHandle(token)


def private_directory(path):
    """Protect an app-owned state root, before any credentials are written.

    Never call this on the user's original business directory. Windows gets a
    protected, inheritable DACL for the current user, SYSTEM and Administrators;
    inherited grants to general groups are removed from this managed root.
    """
    value = _absolute(path)
    assert_plain_path(value)
    value.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != 'nt':
        value.chmod(0o700)
        return
    sddl = 'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;' + _user_sid() + ')'
    descriptor = ctypes.c_void_p()
    if not _security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        acl = ctypes.c_void_p()
        if not _security.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present),
                                                   ctypes.byref(acl), ctypes.byref(defaulted)) or not present:
            raise OSError('Cannot establish a private state-directory ACL')
        with _parents(value):
            handle = _open_handle(value, 0x80 | 0x20000 | 0x40000, 3, directory=True)
            try:
                error = _security.SetSecurityInfo(handle, 1, 0x80000004, None, None, acl, None)
                if error:
                    raise ctypes.WinError(error)
            finally:
                _kernel.CloseHandle(handle)
    finally:
        _kernel.LocalFree(descriptor)


if os.name == 'nt':
    class _Overlapped(ctypes.Structure):
        _fields_ = [('internal', ctypes.c_size_t), ('internal_high', ctypes.c_size_t),
                    ('offset', wintypes.DWORD), ('offset_high', wintypes.DWORD),
                    ('event', wintypes.HANDLE)]

    _kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel.LockFileEx.restype = wintypes.BOOL
    _kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel.UnlockFileEx.restype = wintypes.BOOL


@contextmanager
def file_guard(path, *, exclusive=False, blocking=False):
    """Cross-process shared/exclusive maintenance lock, with a nonblocking mode."""
    if os.name != 'nt':
        import fcntl
        with Path(path).open('a') as stream:
            flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            if not blocking:
                flag |= fcntl.LOCK_NB
            fcntl.flock(stream.fileno(), flag)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return
    try:
        descriptor = create_regular_exclusive(path)
    except FileExistsError:
        descriptor = _windows_descriptor(path, writable=True)
    with os.fdopen(descriptor, 'r+b') as stream:
        handle = msvcrt.get_osfhandle(stream.fileno())
        overlapped = _Overlapped()
        flags = (0x2 if exclusive else 0) | (0 if blocking else 0x1)
        if not _kernel.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
            error = ctypes.get_last_error()
            if error == 33:
                raise BlockingIOError(errno.EAGAIN, 'Maintenance is currently in progress')
            raise ctypes.WinError(error)
        try:
            yield
        finally:
            if not _kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
                raise ctypes.WinError(ctypes.get_last_error())
