"""Dedicated durable conversation loop; data jobs cannot block model planning."""
import logging,os,time,threading
from .locks import FileLock,Timeout,LockLost
from sqlalchemy.exc import OperationalError,InterfaceError
from .config import DATA
from .conversation import initialize,tick
from .store import put,now

def main():
    with FileLock(str(DATA/('agent-'+__import__('agent_service.runtime',fromlist=['INSTANCE']).INSTANCE+'.lock')),timeout=0):
        from .runtime import preflight
        preflight()
        initialize()
        stop=threading.Event()
        from .collaboration_runtime import start as start_collaboration
        collaborators=start_collaboration(stop)
        from .activity import run as notify
        notifier=threading.Thread(target=notify,args=(stop,),daemon=True);notifier.start()
        def beat():
            from .maintenance import guard
            from .company_search import purge
            from .store import all_items
            last_purge=0
            while not stop.is_set():
                try:
                    with guard():
                        __import__('agent_service.runtime',fromlist=['heartbeat']).heartbeat('agent',pid=os.getpid())
                        from .work_runtime import reconcile
                        reconcile()
                        if time.monotonic()-last_purge>60:
                            for project in all_items('project'):
                                if project.get('company_private'):purge(project['id'],expired_only=True)
                            last_purge=time.monotonic()
                except (OperationalError,InterfaceError,BlockingIOError):pass
                stop.wait(5)
        thread=threading.Thread(target=beat,daemon=True);thread.start()
        try:
            while True:
                try:
                    worked=tick()
                    if not worked:stop.wait(.5)
                except LockLost:raise
                except (OperationalError,InterfaceError,Timeout):
                    logging.getLogger(__name__).warning('Agent storage temporarily unavailable')
                    stop.wait(2)
        finally:stop.set();thread.join(timeout=6);collaborators.join(timeout=6)

if __name__=='__main__':main()
