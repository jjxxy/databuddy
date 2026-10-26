import logging,os,time,threading
from .locks import FileLock,LockLost
from filelock import Timeout
from sqlalchemy.exc import OperationalError,InterfaceError
from .config import DATA
from .store import put,now
from .execution import process_once,queue_due
from .conversation import tick,initialize
logger=logging.getLogger(__name__)

def main():
    with FileLock(str(DATA/('worker-'+__import__('agent_service.runtime',fromlist=['INSTANCE']).INSTANCE+'.lock')),timeout=0):
        from .runtime import preflight
        preflight()
        initialize()
        stop=threading.Event()
        collaborators=None
        if os.getenv('AGENT_ORCHESTRATION_MODE')!='separate':
            from .collaboration_runtime import start as start_collaboration
            collaborators=start_collaboration(stop)
            from .activity import run as notify
            threading.Thread(target=notify,args=(stop,),daemon=True).start()
        def beat():
            while not stop.is_set():
                from .maintenance import guard
                try:
                    with guard():__import__('agent_service.runtime',fromlist=['heartbeat']).heartbeat('worker',pid=os.getpid())
                except BlockingIOError:pass
                except (OperationalError,InterfaceError):logger.warning('Worker heartbeat database unavailable; retrying')
                stop.wait(5)
        thread=threading.Thread(target=beat,daemon=True);thread.start()
        try:
            while True:
                try:
                    if os.getenv('AGENT_ORCHESTRATION_MODE')!='separate':tick()
                    if os.getenv('SCHEDULER_MODE','worker')=='worker':queue_due()
                    processed=process_once()
                    if os.getenv('AGENT_ORCHESTRATION_MODE')!='separate':tick()
                    if not processed:stop.wait(1)
                except LockLost:raise
                except (OperationalError,InterfaceError,Timeout):
                    logger.warning('Worker database or scheduler temporarily unavailable; retrying')
                    stop.wait(5)
        finally:
            stop.set();thread.join(timeout=6)
            if collaborators:collaborators.join(timeout=6)

if __name__=='__main__':main()
