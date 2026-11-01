import logging,os,time,threading
from .locks import FileLock,LockLost,assert_owned
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
        if os.getenv('AGENT_ORCHESTRATION_MODE')!='separate':
            from .conversation_workers import ConversationWorkers,concurrency
            concurrency()  # validate before starting background threads
        stop=threading.Event()
        collaborators=None
        conversations=None
        if os.getenv('AGENT_ORCHESTRATION_MODE')!='separate':
            from .collaboration_runtime import start as start_collaboration
            collaborators=start_collaboration(stop)
            conversations=ConversationWorkers(stop,tick,logger).start()
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
            while not stop.is_set():
                try:
                    # The supervisor lock belongs to this thread. Check it
                    # here rather than sharing its PostgreSQL connection with
                    # the independent project slots.
                    assert_owned()
                    if conversations:conversations.raise_if_failed()
                    if os.getenv('SCHEDULER_MODE','worker')=='worker':queue_due()
                    processed=process_once()
                    assert_owned()
                    if conversations:conversations.raise_if_failed()
                    if not processed:stop.wait(1)
                except LockLost:raise
                except (OperationalError,InterfaceError,Timeout):
                    logger.warning('Worker database or scheduler temporarily unavailable; retrying')
                    stop.wait(5)
            if conversations:conversations.raise_if_failed()
        finally:
            stop.set();thread.join(timeout=6)
            if conversations:conversations.join()
            if collaborators:collaborators.join(timeout=6)

if __name__=='__main__':main()
