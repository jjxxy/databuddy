"""Shared-filesystem locks plus PostgreSQL session locks for cross-node ownership.

Fail closed after database failover. These connections must not use a transaction
pooler. The filesystem must support coherent POSIX locking (e.g. NFSv4/EFS).
"""
import contextvars,hashlib,os,time
from pathlib import Path
from filelock import FileLock as LocalLock,Timeout
from sqlalchemy import create_engine,text
from .config import DATA,DATABASE_URL
_owned=contextvars.ContextVar('atlas_owned_locks',default=())
_engine=None
class LockLost(RuntimeError):pass

def assert_owned():
    for lock,generation in _owned.get():
        # A copied Context may outlive the thread that acquired the lock. A
        # released lock, even if the same object was subsequently reacquired,
        # must never turn that stale Context into an unlocked writer.
        if lock.depth<=0 or lock.generation!=generation:
            raise LockLost('执行权已释放，未继续写入')
        if lock.connection is not None:
            try:
                alive=lock.connection.execute(text('SELECT pg_backend_pid()')).scalar()
                if alive!=lock.backend:raise LockLost('执行权已失效，等待其他节点恢复')
            except Exception:raise LockLost('执行权已失效，未继续写入') from None

class FileLock:
    def __init__(self,path,timeout=-1,**kwargs):
        self.path=str(path);self.timeout=timeout;self.local=LocalLock(path,timeout=timeout,**kwargs);self.connection=None;self.backend=None;self.token=None;self.depth=0;self.generation=0
    def acquire(self,timeout=None,**kwargs):
        if self.depth:self.depth+=1;return self
        limit=self.timeout if timeout is None else timeout;start=time.monotonic()
        self.local.acquire(timeout=limit,**kwargs)
        if DATABASE_URL.startswith(('postgresql:','postgresql+')):
            try:
                global _engine
                if _engine is None:_engine=create_engine(DATABASE_URL,pool_pre_ping=True,pool_size=2,max_overflow=4,pool_timeout=5,connect_args={'connect_timeout':5})
                self.connection=_engine.connect().execution_options(isolation_level='AUTOCOMMIT')
                name=str(Path(self.path).relative_to(DATA))
                self.key=int.from_bytes(hashlib.sha256(name.encode()).digest()[:8],'big',signed=True)
                while not self.connection.execute(text('SELECT pg_try_advisory_lock(:key)'),{'key':self.key}).scalar():
                    if limit>=0 and time.monotonic()-start>=limit:raise Timeout(self.path)
                    time.sleep(.05)
                self.backend=self.connection.execute(text('SELECT pg_backend_pid()')).scalar()
            except BaseException:
                if self.connection:self.connection.close();self.connection=None
                self.local.release();raise
        self.generation+=1
        self.token=_owned.set((*_owned.get(),(self,self.generation)));self.depth=1;return self
    def release(self,force=False):
        if self.depth>1 and not force:self.depth-=1;return
        if not self.depth:return
        if self.token is not None:_owned.reset(self.token)
        try:
            if self.connection is not None:
                try:
                    ok=self.connection.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':self.key}).scalar()
                    if not ok:self.connection.invalidate()
                except Exception:self.connection.invalidate()
                finally:self.connection.close();self.connection=None
        finally:self.depth=0;self.local.release(force=force)
    def __enter__(self):return self.acquire()
    def __exit__(self,*args):self.release()
