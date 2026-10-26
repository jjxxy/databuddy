"""On Linux, make subprocesses terminate if their owning worker disappears."""
import os,signal,sys
if sys.platform == 'win32' and os.environ.get('ATLAS_PROCESS_START_EVENT'):
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenEventW.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.LPCWSTR]
    kernel.OpenEventW.restype=wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenEventW(0x00100000,False,os.environ.pop('ATLAS_PROCESS_START_EVENT'))
    if not handle:raise RuntimeError('无法取得受管进程启动许可')
    try:
        if kernel.WaitForSingleObject(handle,15000)!=0:raise RuntimeError('受管进程启动许可超时')
    finally:kernel.CloseHandle(handle)
if sys.platform.startswith('linux'):
    import ctypes
    parent=int(os.environ.get('ATLAS_PARENT_PID',os.getppid()))
    if ctypes.CDLL(None,use_errno=True).prctl(1,signal.SIGKILL)!=0:raise RuntimeError('无法设置父进程退出保护')
    if os.getppid()!=parent:os.kill(os.getpid(),signal.SIGKILL)
if sys.platform=='win32':
    # Windows CRT exec replacement is unreliable for embedded/venv Python.
    # The owned Job Object already covers this wrapper and all descendants.
    import subprocess
    raise SystemExit(subprocess.call(sys.argv[1:],env=os.environ,stdin=sys.stdin,stdout=sys.stdout,
                                    stderr=sys.stderr,creationflags=subprocess.CREATE_NO_WINDOW))
os.execvpe(sys.argv[1],sys.argv[1:],os.environ)
