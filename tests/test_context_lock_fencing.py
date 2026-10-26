"""A background callback cannot retain write ownership after its parent exits."""
import contextvars
import threading

import pytest

from agent_service import locks
from agent_service.config import DATA
from agent_service.store import get, patch, put, uid


@pytest.mark.parametrize('reacquire',[False,True])
def test_copied_context_rejects_released_or_reused_lock(reacquire):
    identity='fencing_'+uid()
    put('system',{'value':'initial'},identity)
    lock=locks.FileLock(str(DATA/('context-'+uid()+'.lock')),timeout=1)
    with lock:
        captured=contextvars.copy_context()
        captured.run(patch,'system',identity,value='held')
    assert get('system',identity)['value']=='held'
    if reacquire:
        lock.acquire()
    try:
        with pytest.raises(locks.LockLost):captured.run(patch,'system',identity,value='stale')
        assert get('system',identity)['value']=='held'
        # The owning/current context remains usable, including after reacquire.
        patch('system',identity,value='current')
    finally:
        if reacquire:lock.release()
    assert get('system',identity)['value']=='current'


def test_background_thread_cannot_write_after_parent_releases_lock():
    identity='fencing_'+uid();put('system',{'value':'initial'},identity)
    callback_ready=threading.Event();released=threading.Event();outcomes=[]
    with locks.FileLock(str(DATA/('callback-'+uid()+'.lock')),timeout=1):
        captured=contextvars.copy_context()
        def callback():
            callback_ready.set()
            if not released.wait(3):raise AssertionError('test parent did not release')
            try:patch('system',identity,value='stale')
            except locks.LockLost:outcomes.append('fenced')
        worker=threading.Thread(target=lambda:captured.run(callback))
        worker.start();assert callback_ready.wait(3)
    released.set();worker.join(timeout=3)
    assert not worker.is_alive() and outcomes==['fenced']
    assert get('system',identity)['value']=='initial'


def test_reentrant_depth_keeps_current_generation_until_final_release():
    lock=locks.FileLock(str(DATA/('reentrant-'+uid()+'.lock')),timeout=1)
    with lock:
        with lock:
            captured=contextvars.copy_context()
            captured.run(locks.assert_owned)
        captured.run(locks.assert_owned)
    with pytest.raises(locks.LockLost):captured.run(locks.assert_owned)
