"""Immutable guest-side transfer helper; no host filesystem extraction."""
import io
import json
import os
from pathlib import Path, PurePosixPath
import signal
import stat
import sys
import tarfile
import time

MAX_FILE=25*1024*1024
MAX_TOTAL=64*1024*1024
MAX_ENTRIES=4000


def relative(name):
    if (not name or len(name)>240 or name.startswith('/') or '\\' in name
            or len(name.split('/'))>16 or any(part in ('','.','..') or part.startswith('.atlas') for part in name.split('/'))
            or any(ord(char)<32 for char in name)):
        raise ValueError('Invalid transfer path')
    return name


def restore(source, root=Path('/workspace')):
    total=count=0
    with tarfile.open(fileobj=source,mode='r|') as archive:
        for entry in archive:
            count+=1
            if count>MAX_ENTRIES:raise ValueError('Too many input entries')
            name=relative(entry.name.rstrip('/') if entry.isdir() else entry.name)
            target=root/name
            if entry.isdir():
                target.mkdir(mode=0o700,parents=True,exist_ok=True)
                continue
            total+=entry.size
            if not entry.isreg() or entry.size<0 or entry.size>MAX_FILE or total>MAX_TOTAL:
                raise ValueError('Invalid or oversized input file')
            target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            with os.fdopen(os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),'wb') as output:
                stream=archive.extractfile(entry)
                while chunk:=stream.read(65536):output.write(chunk)


def collect(root=Path('/workspace')):
    files={};total=count=0
    for directory,dirs,names in os.walk(root,followlinks=False):
        for name in sorted([*dirs,*names]):
            path=Path(directory)/name;relative(path.relative_to(root).as_posix());count+=1
            if count>MAX_ENTRIES:raise ValueError('Too many output entries')
            before=path.lstat()
            if stat.S_ISDIR(before.st_mode):continue
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>MAX_FILE:raise ValueError('Output must be a bounded regular file')
            total+=before.st_size
            if total>MAX_TOTAL or len(files)>=1000:raise ValueError('Output checkpoint budget exceeded')
            with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as source:
                opened=os.fstat(source.fileno())
                if not stat.S_ISREG(opened.st_mode) or (before.st_dev,before.st_ino)!=(opened.st_dev,opened.st_ino):raise ValueError('Output path changed')
                content=source.read(MAX_FILE+1);after=os.fstat(source.fileno())
                if len(content)>MAX_FILE or (opened.st_size,opened.st_mtime_ns,opened.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                    raise ValueError('Output changed while checkpointing')
                files[path.relative_to(root).as_posix()]=content
    return files


def export(output,root=Path('/workspace')):
    files=collect(root)
    with tarfile.open(fileobj=output,mode='w|') as archive:
        for name,content in sorted(files.items()):
            entry=tarfile.TarInfo(name);entry.size=len(content);entry.mode=0o600;entry.uid=entry.gid=10001
            archive.addfile(entry,io.BytesIO(content))


def quiesce():
    # PID 1 is the fixed idle helper. All other guest processes have the same
    # non-root UID; no host PIDs exist in this namespace. Fail rather than
    # checkpoint a workspace with surviving background writers.
    for _ in range(8):
        pids=[int(name) for name in os.listdir('/proc') if name.isdigit() and int(name) not in (1,os.getpid())]
        if not pids:return
        for pid in pids:
            try:os.kill(pid,signal.SIGKILL)
            except ProcessLookupError:pass
        time.sleep(.05)
    raise ValueError('Guest background processes did not stop')


def idle():
    # A single PID 1, with a hard lifetime even if the controller disappears.
    # Reap orphaned workload children so they do not accumulate as zombies.
    def reap(*_):
        while True:
            try:
                if os.waitpid(-1,os.WNOHANG)[0]==0:return
            except ChildProcessError:return
    signal.signal(signal.SIGCHLD,reap)
    signal.alarm(960)
    time.sleep(960)


def main():
    mode=sys.argv[1]
    if mode=='idle':idle()
    elif mode=='import':restore(sys.stdin.buffer)
    elif mode=='export':export(sys.stdout.buffer)
    elif mode=='quiesce':quiesce()
    elif mode=='logs':
        result={}
        for name in ('stdout','stderr'):
            try:
                with os.fdopen(os.open('/tmp/atlas-'+name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):raise ValueError('Invalid log file')
                    result[name]=stream.read(64000).decode('utf-8',errors='replace')
            except FileNotFoundError:result[name]=''
        sys.stdout.write(json.dumps(result))
    else:raise ValueError('Unknown transfer operation')


if __name__=='__main__':
    main()
