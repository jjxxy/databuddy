"""Owned subprocess trees on POSIX and Windows, without shell/PATH utilities."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid
import shutil


def executable(name):
    selected = os.getenv('ATLAS_' + name.upper() + '_BINARY')
    if selected:
        path = Path(selected)
        if not path.is_absolute() or not path.is_file():raise ValueError('随包运行文件缺失：' + name)
        return str(path)
    if os.getenv('ATLAS_DESKTOP_MODE')=='1':raise ValueError('桌面包未提供运行文件：' + name)
    return shutil.which(name) or name


if os.name == 'nt':
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.SetEvent.argtypes = [wintypes.HANDLE]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong), ('PerJobUserTimeLimit', ctypes.c_longlong),
                    ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ('ReadOperationCount', 'WriteOperationCount',
                    'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', BASIC_LIMIT), ('IoInfo', IO_COUNTERS),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


class OwnedProcess:
    def __init__(self, process, job=None, start_event=None):
        self.process, self.job, self.start_event = process, job, start_event

    def __getattr__(self, name):
        return getattr(self.process, name)

    def _close_job(self):
        if self.job:
            kernel.CloseHandle(self.job)
            self.job = None
        if self.start_event:
            kernel.CloseHandle(self.start_event)
            self.start_event = None

    def terminate(self):
        if os.name == 'nt':
            if self.job:
                kernel.TerminateJobObject(self.job, 1)
            elif self.process.poll() is None:
                self.process.terminate()
        else:
            try: os.killpg(self.pid, signal.SIGTERM)
            except ProcessLookupError: pass

    def kill(self):
        if os.name == 'nt':
            self.terminate()
        else:
            try: os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError: pass

    def wait(self, timeout=None):
        result = self.process.wait(timeout=timeout)
        self._close_job()  # Kill descendants even if their original parent already exited.
        return result

    def __del__(self):
        self._close_job()


def spawn(args, **kwargs):
    """Gate child execution until Windows assigns its kill-on-close job.

    Linux uses the existing parent-death wrapper. On Windows, a named event
    prevents the child from launching descendants before job assignment.
    """
    supplied_env=kwargs.pop('env',None)
    env = dict(os.environ if supplied_env is None else supplied_env)
    env.setdefault('PYTHONUTF8','1')
    env['ATLAS_PARENT_PID'] = str(os.getpid())
    job = event = None
    if os.name == 'nt':
        job = kernel.CreateJobObjectW(None, None)
        if not job: raise ctypes.WinError(ctypes.get_last_error())
        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel.CloseHandle(job)
            raise ctypes.WinError(ctypes.get_last_error())
        name = 'Local\\AtlasStart-' + uuid.uuid4().hex
        event = kernel.CreateEventW(None, True, False, name)
        if not event:
            kernel.CloseHandle(job)
            raise ctypes.WinError(ctypes.get_last_error())
        env['ATLAS_PROCESS_START_EVENT'] = name
        kwargs['creationflags'] = kwargs.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW
    else:
        kwargs['start_new_session'] = True
    try:
        # Absolute wrapper path also works with an intentionally restricted MCP env.
        wrapper = str(Path(__file__).with_name('child.py').resolve())
        process = subprocess.Popen([sys.executable, '-X', 'utf8', wrapper, *map(str, args)], env=env, **kwargs)
        if os.name == 'nt':
            if not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(int(process._handle))):
                process.kill(); process.wait(timeout=5)
                raise ctypes.WinError(ctypes.get_last_error())
            kernel.SetEvent(event)
        owned = OwnedProcess(process, job, event)
        event = None  # Keep the named event alive until the child has opened it.
        return owned
    except BaseException:
        if job: kernel.CloseHandle(job)
        raise
    finally:
        if event: kernel.CloseHandle(event)


def stop(process, timeout=5):
    process.terminate()
    try: return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=5)
