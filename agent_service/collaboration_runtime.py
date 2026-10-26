"""Bounded independent collaboration lanes for the durable Agent service.

The main per-project FIFO can wait for children without starving them. Global
slot locks also bound specialist model work across service processes/replicas.
"""
from concurrent.futures import ThreadPoolExecutor
import logging, os, threading, time
from .config import DATA
from .locks import FileLock,Timeout

log=logging.getLogger(__name__)

def _limit():
    value=int(os.getenv('ATLAS_AGENT_TEAM_CONCURRENCY','2'))
    if not 1<=value<=8:raise ValueError('协作Agent并发数须在1至8之间')
    return value

def _child(turn_id,limit):
    from .agent_team import run_child
    for slot in range(limit):
        try:
            with FileLock(str(DATA/('agent-team-slot-'+str(slot)+'.lock')),timeout=0):
                return run_child(turn_id)
        except Timeout:continue
    return False

def run(stop):
    from . import agent_team,agent_mesh
    limit=_limit();active={};poll=None;next_poll=0
    children=ThreadPoolExecutor(max_workers=limit,thread_name_prefix='atlas-specialist')
    remote=ThreadPoolExecutor(max_workers=1,thread_name_prefix='atlas-agent-poll')
    def inspect(future):
        try:return future.result()
        except Exception as error:
            log.warning('Collaboration background work deferred: %s',type(error).__name__)
            return False
    def reconcile():
        from .maintenance import guard
        try:
            with guard():return agent_mesh.reconcile()
        except BlockingIOError:return False
    try:
        while not stop.is_set():
            for turn_id,future in list(active.items()):
                if future.done():inspect(future);active.pop(turn_id,None)
            if poll is not None and poll.done():inspect(poll);poll=None
            try:
                for item in agent_team.runnable_children(limit=limit*2):
                    turn_id=item['id'] if isinstance(item,dict) else item
                    if turn_id not in active and len(active)<limit:active[turn_id]=children.submit(_child,turn_id,limit)
                if poll is None and time.monotonic()>=next_poll:
                    poll=remote.submit(reconcile);next_poll=time.monotonic()+.5
            except Exception as error:
                log.warning('Collaboration scheduler deferred: %s',type(error).__name__)
            stop.wait(.25)
    finally:
        children.shutdown(wait=False,cancel_futures=True)
        remote.shutdown(wait=False,cancel_futures=True)

def start(stop):
    _limit()
    thread=threading.Thread(target=run,args=(stop,),name='atlas-collaboration',daemon=True)
    thread.start();return thread
