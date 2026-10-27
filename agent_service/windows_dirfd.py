"""Windows-only, root-handle-relative filesystem operations for local folders.

This is deliberately not a global os monkeypatch. Directory HANDLEs keep their
ancestor handles open without FILE_SHARE_DELETE. NT relative opens use the exact
parent HANDLE, never a concatenated path that could follow a replaced junction.
Only libraries writing inside an already anchored staging directory receive a
stable visible path; publication renames that same open directory HANDLE.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import errno
import os as real_os
from pathlib import Path
import re
import stat
from types import SimpleNamespace

from . import platform_files as native

if real_os.name != 'nt':
    raise ImportError('Windows directory handles are available only on Windows')

kernel = native._kernel
ntdll = ctypes.WinDLL('ntdll')


class UnicodeString(ctypes.Structure):
    _fields_ = [('Length', wintypes.USHORT), ('MaximumLength', wintypes.USHORT),
                ('Buffer', wintypes.LPWSTR)]


class ObjectAttributes(ctypes.Structure):
    _fields_ = [('Length', wintypes.ULONG), ('RootDirectory', wintypes.HANDLE),
                ('ObjectName', ctypes.POINTER(UnicodeString)), ('Attributes', wintypes.ULONG),
                ('SecurityDescriptor', ctypes.c_void_p), ('SecurityQualityOfService', ctypes.c_void_p)]


class IOStatus(ctypes.Structure):
    _fields_ = [('Status', ctypes.c_void_p), ('Information', ctypes.c_size_t)]


ntdll.NtCreateFile.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                              ctypes.POINTER(ObjectAttributes), ctypes.POINTER(IOStatus),
                              ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                              wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
ntdll.NtCreateFile.restype = ctypes.c_long
ntdll.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG
ntdll.NtSetInformationFile.argtypes = [wintypes.HANDLE, ctypes.POINTER(IOStatus),
                                      ctypes.c_void_p, wintypes.ULONG, ctypes.c_int]
ntdll.NtSetInformationFile.restype = ctypes.c_long


class FileIdentity(ctypes.Structure):
    _fields_ = [('volume', ctypes.c_ulonglong), ('identifier', ctypes.c_ubyte * 16)]


class FileBasic(ctypes.Structure):
    _fields_ = [('creation', ctypes.c_longlong), ('access', ctypes.c_longlong),
                ('write', ctypes.c_longlong), ('change', ctypes.c_longlong),
                ('attributes', wintypes.DWORD)]


kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, wintypes.DWORD]
kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL


def _relative(name):
    name = real_os.fspath(name)
    if (not isinstance(name, str) or name in ('', '.', '..') or
            any(character in name for character in ('/', '\\', ':', '\x00')) or
            name.rstrip(' .') != name or
            re.fullmatch(r'(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', name)):
        raise OSError(errno.EINVAL, 'Expected one ordinary relative Windows filename')
    if len(name.encode('utf-16-le')) > 65532:
        raise OSError(errno.ENAMETOOLONG, 'Relative filename is too long')
    return name


def _raise(status):
    if status < 0:
        raise ctypes.WinError(ntdll.RtlNtStatusToDosError(status))


def _stat(handle):
    value = native._information(handle)
    identity = FileIdentity()
    if not kernel.GetFileInformationByHandleEx(handle, 18, ctypes.byref(identity), ctypes.sizeof(identity)):
        raise ctypes.WinError(ctypes.get_last_error())
    basic = FileBasic()
    if not kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
        raise ctypes.WinError(ctypes.get_last_error())
    def ns(filetime):
        ticks = (filetime.dwHighDateTime << 32) | filetime.dwLowDateTime
        return ticks * 100 - 11644473600000000000
    mode = stat.S_IFLNK if value.attributes & 0x400 else stat.S_IFDIR if value.attributes & 0x10 else stat.S_IFREG
    return SimpleNamespace(st_mode=mode | 0o600,
                           st_ino=int.from_bytes(bytes(identity.identifier), 'little'),
                           st_dev=identity.volume, st_nlink=value.links,
                           st_uid=0, st_gid=0,
                           st_size=(value.size_high << 32) | value.size_low,
                           st_atime_ns=ns(value.access), st_mtime_ns=ns(value.write),
                           # POSIX ctime means metadata-change time. Windows
                           # creation time does not detect a writer restoring
                           # mtime after a same-size mutation during a snapshot.
                           st_ctime_ns=basic.change * 100 - 11644473600000000000)


class Directory:
    def __init__(self, handle, path, *, parent=None, ancestors=None, can_rename=False):
        self.handle, self.path, self.parent = handle, Path(path), parent
        self.ancestors, self.can_rename = ancestors, can_rename
        self.closed = False
        self.references = 1
        if parent:
            parent.retain()

    def retain(self):
        if self.closed:
            raise OSError(errno.EBADF, 'Directory handle is closed')
        self.references += 1

    def release(self):
        self.references -= 1
        if self.references == 0:
            kernel.CloseHandle(self.handle)
            if self.ancestors:
                self.ancestors.__exit__(None, None, None)
            if self.parent:
                self.parent.release()

    def close(self):
        if not self.closed:
            self.closed = True
            self.release()


def _parent(value):
    if not isinstance(value, Directory) or value.closed:
        raise OSError(errno.EBADF, 'Expected a live parent directory handle')
    return value


def _open_at(parent, name, *, directory=False, disposition=1, access=None, allow_reparse=False):
    parent = _parent(parent)
    name = _relative(name)
    buffer = ctypes.create_unicode_buffer(name)
    string = UnicodeString(len(name.encode('utf-16-le')), len(name.encode('utf-16-le')) + 2,
                           ctypes.cast(buffer, wintypes.LPWSTR))
    attributes = ObjectAttributes(ctypes.sizeof(ObjectAttributes), parent.handle,
                                  ctypes.pointer(string), 0x40, None, None)
    handle, io = wintypes.HANDLE(), IOStatus()
    options = 0x200000 | 0x20  # OPEN_REPARSE_POINT, SYNCHRONOUS_IO_NONALERT
    if not allow_reparse:
        options |= 0x1 if directory else 0x40
    desired = access if access is not None else (0x100000 | 0x80 | (0x1 | 0x20 if directory else 0x80000000))
    _raise(ntdll.NtCreateFile(ctypes.byref(handle), desired, ctypes.byref(attributes),
                             ctypes.byref(io), None, 0, 0x1 | 0x2,
                             disposition, options, None, 0))
    try:
        info = _stat(handle)
        if not allow_reparse and (stat.S_ISLNK(info.st_mode) or stat.S_ISDIR(info.st_mode) != directory):
            raise OSError(errno.ELOOP, 'Links and reparse points are not allowed')
        return handle
    except BaseException:
        kernel.CloseHandle(handle)
        raise


def open_directory(path, *, parent=None, can_rename=False):
    access = 0x100000 | 0x20000 | 0x80 | 0x1 | 0x20 | (0x10000 if can_rename else 0)
    if parent is not None:
        handle = _open_at(parent, path, directory=True, access=access)
        return Directory(handle, parent.path / path, parent=parent, can_rename=can_rename)
    value = native._absolute(path)
    ancestors = native._parents(value)
    ancestors.__enter__()
    try:
        handle = native._open_handle(value, access, 3, directory=True)
        return Directory(handle, value, ancestors=ancestors, can_rename=can_rename)
    except BaseException:
        ancestors.__exit__(None, None, None)
        raise


def _rename(handle, name, parent, *, replace=False):
    parent = _parent(parent)
    encoded = _relative(name).encode('utf-16-le')
    class Rename(ctypes.Structure):
        _fields_ = [('ReplaceIfExists', wintypes.BOOLEAN), ('RootDirectory', wintypes.HANDLE),
                    ('FileNameLength', wintypes.ULONG), ('FileName', wintypes.WCHAR * 1)]
    size = Rename.FileName.offset + len(encoded)
    buffer = ctypes.create_string_buffer(max(size, ctypes.sizeof(Rename)))
    value = ctypes.cast(buffer, ctypes.POINTER(Rename)).contents
    value.ReplaceIfExists, value.RootDirectory, value.FileNameLength = replace, parent.handle, len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + Rename.FileName.offset, encoded, len(encoded))
    io = IOStatus()
    _raise(ntdll.NtSetInformationFile(handle, ctypes.byref(io), buffer, size, 10))


def _delete(handle):
    io, remove = IOStatus(), wintypes.BOOLEAN(True)
    _raise(ntdll.NtSetInformationFile(handle, ctypes.byref(io), ctypes.byref(remove), ctypes.sizeof(remove), 13))


def owner_sid(directory):
    """Read the owner from the open directory, not a replacement named path."""
    handle = _parent(directory).handle
    security = native._security
    security.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                                         ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                         ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.POINTER(ctypes.c_void_p)]
    security.GetSecurityInfo.restype = wintypes.DWORD
    owner, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    error = security.GetSecurityInfo(handle, 1, 1, ctypes.byref(owner), None, None, None,
                                      ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    text = wintypes.LPWSTR()
    try:
        if not security.ConvertSidToStringSidW(owner, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        return text.value
    finally:
        if text:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        kernel.LocalFree(descriptor)


class WindowsOS:
    """Only the POSIX subset used by local_workspace; real file fds stay CRT fds."""
    O_DIRECTORY = 1 << 29
    O_NOFOLLOW = 1 << 30
    O_NONBLOCK = 0
    O_CLOEXEC = 0
    O_ATLAS_STAGE = 1 << 28

    def __getattr__(self, name):
        return getattr(real_os, name)

    def open(self, path, flags, mode=0o600, *, dir_fd=None):
        if flags & self.O_DIRECTORY:
            return open_directory(path, parent=dir_fd, can_rename=bool(flags & self.O_ATLAS_STAGE))
        if dir_fd is None:
            if flags & real_os.O_CREAT:
                return native.create_regular_exclusive(path, mode)
            return native.open_regular_read(path)
        writable = bool(flags & (real_os.O_WRONLY | real_os.O_RDWR))
        disposition = 2 if flags & real_os.O_CREAT and flags & real_os.O_EXCL else 3 if flags & real_os.O_CREAT else 1
        access = 0x100000 | 0x80 | 0x80000000 | (0x40000000 if writable else 0)
        handle = _open_at(dir_fd, path, disposition=disposition, access=access)
        try:
            info = _stat(handle)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError(errno.ELOOP, 'Expected an independent ordinary file')
            import msvcrt
            descriptor = msvcrt.open_osfhandle(handle.value, real_os.O_BINARY | (real_os.O_RDWR if writable else real_os.O_RDONLY))
            return descriptor
        except BaseException:
            kernel.CloseHandle(handle)
            raise

    def mkdir(self, path, mode=0o700, *, dir_fd=None):
        if dir_fd is None:
            return real_os.mkdir(path, mode)
        handle = _open_at(dir_fd, path, directory=True, disposition=2)
        kernel.CloseHandle(handle)

    def close(self, descriptor):
        if isinstance(descriptor, Directory):
            descriptor.close()
        else:
            real_os.close(descriptor)

    def fstat(self, descriptor):
        if isinstance(descriptor,Directory):
            return _stat(_parent(descriptor).handle)
        # CRT fstat can report ctime as mtime for open_osfhandle descriptors.
        # Use the same native timestamps and file IDs as relative stat, so a
        # normal read does not look like a source replacement after close.
        import msvcrt
        return _stat(msvcrt.get_osfhandle(descriptor))

    def stat(self, path, *, dir_fd=None, follow_symlinks=True):
        if dir_fd is None:
            return real_os.stat(path, follow_symlinks=follow_symlinks)
        if follow_symlinks:
            raise OSError(errno.EINVAL, 'Relative stat must explicitly refuse links')
        handle = _open_at(dir_fd, path, access=0x100000 | 0x80, allow_reparse=True)
        try:
            return _stat(handle)
        finally:
            kernel.CloseHandle(handle)

    def listdir(self, path):
        return real_os.listdir(_parent(path).path) if isinstance(path, Directory) else real_os.listdir(path)

    def fsync(self, descriptor):
        if isinstance(descriptor, Directory):
            _parent(descriptor)
            return False  # Windows has no POSIX directory-fsync equivalent.
        return real_os.fsync(descriptor)

    def replace(self, source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        if src_dir_fd is None or dst_dir_fd is None:
            return native.atomic_replace(source, destination)
        handle = _open_at(src_dir_fd, source, access=0x100000 | 0x80 | 0x10000)
        try:
            try:
                target = self.stat(destination, dir_fd=dst_dir_fd, follow_symlinks=False)
                if not stat.S_ISREG(target.st_mode) or target.st_nlink != 1:
                    raise OSError(errno.ELOOP, 'Cannot replace a link or special file')
            except FileNotFoundError:
                pass
            _rename(handle, destination, dst_dir_fd, replace=True)
        finally:
            kernel.CloseHandle(handle)

    def unlink(self, path, *, dir_fd=None):
        if dir_fd is None:
            return real_os.unlink(path)
        handle = _open_at(dir_fd, path, access=0x100000 | 0x80 | 0x10000, allow_reparse=True)
        try:
            _delete(handle)
        finally:
            kernel.CloseHandle(handle)

    def descriptor_path(self, directory):
        value = str(_parent(directory).path)
        if value.startswith('\\\\?\\'):
            return Path(value)
        return Path('\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value)

    def publish_directory(self, directory, name, parent):
        directory = _parent(directory)
        if not directory.can_rename:
            raise OSError(errno.EACCES, 'Staging handle was not opened for publication')
        _rename(directory.handle, name, parent)
        directory.path = parent.path / name

    def remove_tree(self, name, *, dir_fd):
        # Only callers holding an app-managed export parent may invoke cleanup.
        # Reparse leaves are deleted as leaves; their targets are never walked.
        name = _relative(name)
        target = open_directory(name, parent=dir_fd, can_rename=True)
        try:
            for child in self.listdir(target):
                info = self.stat(child, dir_fd=target, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    self.remove_tree(child, dir_fd=target)
                else:
                    self.unlink(child, dir_fd=target)
            _delete(target.handle)
        finally:
            target.close()


os = WindowsOS()
