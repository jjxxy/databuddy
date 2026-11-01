"""Bounded workers for independent projects' durable conversation queues.

Each slot calls the normal scheduler, which owns per-project locking and FIFO.
No database session, execution lease, or context-variable scope is shared among
threads.  A long-running model request therefore cannot block another project.
"""
import logging
import os
import queue
import threading

from sqlalchemy.exc import InterfaceError, OperationalError

from .locks import LockLost, Timeout


def concurrency():
    try:
        value = int(os.getenv('ATLAS_CONVERSATION_CONCURRENCY', '2'))
    except ValueError:
        raise ValueError('项目对话并发数须为 1 至 8 的整数') from None
    if not 1 <= value <= 8:
        raise ValueError('项目对话并发数须为 1 至 8 的整数')
    return value


class ConversationWorkers:
    def __init__(self, stop, tick, logger=None):
        self.stop = stop
        self.tick = tick
        self.logger = logger or logging.getLogger(__name__)
        self.errors = queue.Queue()
        self.threads = []

    def _run(self):
        while not self.stop.is_set():
            try:
                worked = self.tick()
                # A waiting source may report scheduler work without changing
                # state. Yield briefly even then to avoid a hot DB polling loop.
                self.stop.wait(.1 if worked else .5)
            except LockLost as error:
                self.errors.put(error)
                self.stop.set()
                return
            except (OperationalError, InterfaceError, Timeout):
                self.logger.warning('Agent storage temporarily unavailable')
                self.stop.wait(2)
            except Exception as error:
                # Match the former foreground loop: do not heartbeat forever
                # after an unexpected fatal scheduler failure.
                self.errors.put(error)
                self.stop.set()
                return

    def start(self):
        for index in range(concurrency()):
            thread = threading.Thread(target=self._run, name=f'atlas-project-{index + 1}', daemon=True)
            thread.start()
            self.threads.append(thread)
        return self

    def raise_if_failed(self):
        try:
            error = self.errors.get_nowait()
        except queue.Empty:
            return
        raise error

    def join(self, timeout=6):
        # Running tasks retain their own locks until completion/process exit;
        # closing one slot must not start the same turn on another slot.
        for thread in self.threads:
            thread.join(timeout=timeout)
