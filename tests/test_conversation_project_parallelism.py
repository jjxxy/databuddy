"""Independent project conversations can progress without duplicate execution."""
import contextvars
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from agent_service import conversation, tasks, workspace
from agent_service.conversation_workers import ConversationWorkers, concurrency
from agent_service.config import DATA
from agent_service.locks import FileLock, LockLost, Timeout, _owned
from agent_service.store import delete, get, patch, put, uid


def test_live_project_does_not_block_another_project_and_fifo_is_preserved(monkeypatch):
    monkeypatch.setenv('ATLAS_CONVERSATION_CONCURRENCY', '2')
    first_project, second_project = 'p_'+uid(), 'p_'+uid()
    first, next_same, other = 't_'+uid(), 't_'+uid(), 't_'+uid()
    for pid in (first_project, second_project):
        put('project', {'name': 'Parallel test'}, pid)
    for index, (tid, pid) in enumerate(((first, first_project), (next_same, first_project), (other, second_project))):
        put('turn', {'project_id': pid, 'interaction_mode': 'work', 'text': 'test',
                     'state': 'agent', 'created_at': f'2026-01-01T00:00:0{index}Z'}, tid)

    actual_all = conversation.all_items
    ids = {first, next_same, other}
    monkeypatch.setattr(conversation, 'all_items', lambda kind:
                        [item for item in actual_all(kind) if item['id'] in ids]
                        if kind == 'turn' else actual_all(kind))
    monkeypatch.setattr(workspace, 'begin', lambda *_: None)
    monkeypatch.setattr(workspace, 'recover', lambda: None)
    monkeypatch.setattr(workspace, 'finalize', lambda *_: None)
    monkeypatch.setattr(tasks, 'ensure', lambda *_: None)
    first_started, release_first = threading.Event(), threading.Event()
    other_started, second_same_started = threading.Event(), threading.Event()
    lock = threading.Lock()
    calls = []

    def handle(turn):
        # A worker's context is independent: it owns the project lock, not
        # the supervisor connection created by another thread.
        locks = _owned.get()
        assert any(item.path.endswith('chat-'+turn['project_id']+'.lock') for item, _ in locks)
        assert not any(item.path.endswith('test-supervisor.lock') for item, _ in locks)
        with lock:
            calls.append(turn['id'])
        if turn['id'] == first:
            first_started.set()
            assert release_first.wait(10)
        elif turn['id'] == other:
            other_started.set()
        elif turn['id'] == next_same:
            second_same_started.set()
        patch('turn', turn['id'], state='done')

    monkeypatch.setattr(conversation, 'handle', handle)
    stop = threading.Event()
    supervisor = FileLock(str(DATA/'test-supervisor.lock'), timeout=1)
    supervisor.acquire()
    workers = ConversationWorkers(stop, conversation.tick).start()
    try:
        assert first_started.wait(5)
        assert other_started.wait(5), 'another project was blocked by the first model call'
        assert not second_same_started.is_set(), 'a later turn in the same project ran concurrently'
        with lock:
            assert calls.count(first) == 1 and calls.count(other) == 1
        release_first.set()
        assert second_same_started.wait(5)
        workers.raise_if_failed()
    finally:
        release_first.set()
        stop.set()
        workers.join()
        supervisor.release()
        for tid in ids:
            delete('turn', tid)
        for pid in (first_project, second_project):
            delete('project', pid)
    assert all(not thread.is_alive() for thread in workers.threads)
    assert get_failure_free_counts(calls, first, next_same, other)


def get_failure_free_counts(calls, *turns):
    return all(calls.count(turn) == 1 for turn in turns)


def test_lost_execution_lock_stops_all_workers_and_is_reported(monkeypatch):
    monkeypatch.setenv('ATLAS_CONVERSATION_CONCURRENCY', '2')
    stop = threading.Event()
    def lost():
        raise LockLost('lost project execution ownership')
    workers = ConversationWorkers(stop, lost).start()
    try:
        assert stop.wait(5)
        with pytest.raises(LockLost):
            workers.raise_if_failed()
    finally:
        stop.set()
        workers.join()


def test_inline_worker_stops_project_slots_when_supervisor_lock_is_lost(monkeypatch):
    from agent_service import activity, collaboration_runtime, runtime, worker

    monkeypatch.setenv('ATLAS_CONVERSATION_CONCURRENCY', '2')
    monkeypatch.setenv('AGENT_ORCHESTRATION_MODE', 'inline')
    monkeypatch.setattr(runtime, 'preflight', lambda: None)
    monkeypatch.setattr(runtime, 'heartbeat', lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, 'initialize', lambda: None)
    monkeypatch.setattr(worker, 'tick', lambda: False)
    monkeypatch.setattr(worker, 'process_once', lambda: pytest.fail('job ran after supervisor lock loss'))
    monkeypatch.setattr(activity, 'run', lambda stop: stop.wait(1))
    class Collaborators:
        def join(self, timeout=None): pass
    monkeypatch.setattr(collaboration_runtime, 'start', lambda stop: Collaborators())
    def lost():
        raise LockLost('supervisor lock lost')
    monkeypatch.setattr(worker, 'assert_owned', lost)
    created = []
    start = ConversationWorkers.start
    def tracked_start(self):
        created.append(self)
        return start(self)
    monkeypatch.setattr(ConversationWorkers, 'start', tracked_start)
    with pytest.raises(LockLost, match='supervisor lock lost'):
        worker.main()
    assert len(created) == 1 and created[0].stop.is_set()
    assert all(not thread.is_alive() for thread in created[0].threads)


def test_failure_stops_scheduler_without_releasing_another_live_project(monkeypatch):
    """Another slot's failure cannot free a live owner's lock or permit replay."""
    monkeypatch.setenv('ATLAS_CONVERSATION_CONCURRENCY', '2')
    first_pid, second_pid = 'p_'+uid(), 'p_'+uid()
    first, second = 't_'+uid(), 't_'+uid()
    for pid in (first_pid, second_pid):
        put('project', {'name': 'Ownership test'}, pid)
    for index, (tid, pid) in enumerate(((first, first_pid), (second, second_pid))):
        put('turn', {'project_id': pid, 'interaction_mode': 'work', 'text': 'test',
                     'state': 'agent', 'created_at': f'2026-01-01T00:00:0{index}Z'}, tid)
    original_all = conversation.all_items
    ids = {first, second}
    monkeypatch.setattr(conversation, 'all_items', lambda kind:
                        [item for item in original_all(kind) if item['id'] in ids]
                        if kind == 'turn' else original_all(kind))
    monkeypatch.setattr(workspace, 'begin', lambda *_: None)
    monkeypatch.setattr(workspace, 'recover', lambda: None)
    monkeypatch.setattr(workspace, 'finalize', lambda *_: None)
    monkeypatch.setattr(tasks, 'ensure', lambda *_: None)
    holding, release = threading.Event(), threading.Event()
    captured = []

    def handle(turn):
        locks = _owned.get()
        assert any(item.path.endswith('chat-'+turn['project_id']+'.lock') for item, _ in locks)
        if turn['id'] == first:
            captured.append(contextvars.copy_context())
            holding.set()
            assert release.wait(10)
            patch('turn', first, state='done')
        else:
            assert holding.wait(5)
            raise LockLost('second project lost ownership')

    monkeypatch.setattr(conversation, 'handle', handle)
    stop = threading.Event()
    workers = ConversationWorkers(stop, conversation.tick).start()
    try:
        assert holding.wait(5) and stop.wait(5)
        with pytest.raises(LockLost):
            workers.raise_if_failed()
        with pytest.raises(Timeout):
            with FileLock(str(DATA/('chat-'+first_pid+'.lock')), timeout=0):
                pytest.fail('live project lost its lock while still running')
        release.set()
        workers.join()
        assert get('turn', first)['state'] == 'done'
        # A delayed bridge callback carrying the released project context is
        # rejected even if another worker later acquires the same lock path.
        with pytest.raises(LockLost):
            captured[0].run(patch, 'turn', first, text='stale callback')
        assert get('turn', first)['text'] == 'test'
    finally:
        release.set(); stop.set(); workers.join()
        for tid in ids:
            delete('turn', tid)
        for pid in (first_pid, second_pid):
            delete('project', pid)


def test_old_deferred_tasks_do_not_starve_a_new_project(monkeypatch):
    pid, tid = 'p_'+uid(), 't_'+uid()
    put('project', {'name': 'Fairness test'}, pid)
    put('turn', {'project_id': pid, 'interaction_mode': 'work', 'text': 'test',
                 'state': 'queued', 'created_at': '2026-01-01T00:01:00Z'}, tid)
    # Inject many deferred tasks without changing any other project's storage.
    # A 100-row slice before checking retry gates previously hid the live turn.
    deferred = [{'id': f'deferred-{index}', 'project_id': f'other-{index}',
                 'state': 'agent', 'created_at': '2026-01-01T00:00:00Z',
                 'model_retry_at': time.time()+3600} for index in range(101)]
    actual_all = conversation.all_items
    monkeypatch.setattr(conversation, 'all_items', lambda kind:
                        deferred + [get('turn', tid)] if kind == 'turn' else actual_all(kind))
    monkeypatch.setattr(workspace, 'begin', lambda *_: None)
    monkeypatch.setattr(workspace, 'recover', lambda: None)
    monkeypatch.setattr(workspace, 'finalize', lambda *_: None)
    monkeypatch.setattr(tasks, 'ensure', lambda *_: None)
    called = []
    def handle(turn):
        called.append(turn['id'])
        patch('turn', turn['id'], state='done')
    monkeypatch.setattr(conversation, 'handle', handle)
    try:
        assert conversation.tick() is True
        assert called == [tid]
    finally:
        delete('turn', tid)
        delete('project', pid)


def test_concurrent_recovery_does_not_repeat_a_completed_restore(monkeypatch):
    """Real Git-backed interrupted restore must not delete later writes twice."""
    pid = 'p_'+uid()
    put('project', {'name': 'Restore race test'}, pid)
    workspace.write_file(pid, 'memo.md', 'before')
    target = workspace.checkpoint(pid)
    workspace.write_file(pid, 'memo.md', 'after')
    workspace.checkpoint(pid)
    checkpoint = workspace._checkpoint
    monkeypatch.setattr(workspace, '_checkpoint', lambda *_: (_ for _ in ()).throw(RuntimeError('simulated crash')))
    with pytest.raises(RuntimeError, match='simulated crash'):
        workspace.restore(pid, target['commit'], 'interrupted')
    assert get('restore', 'restore-'+pid)['state'] == 'applying'
    monkeypatch.setattr(workspace, '_checkpoint', checkpoint)

    # Force both callers to observe "applying" before either takes the lock.
    original_all, original_restore = workspace.all_items, workspace._apply_restore
    barrier = threading.Barrier(2)
    calls = []
    def list_items(kind):
        values = original_all(kind)
        if kind == 'restore':
            values = [item for item in values if item['project_id'] == pid]
            barrier.wait(timeout=8)
        return values
    def restore_once(journal):
        calls.append(journal['id'])
        result = original_restore(journal)
        # A later project write must survive the other caller's stale snapshot.
        workspace.path_for(pid, 'after-recovery.md').write_text('keep this', encoding='utf-8')
        return result
    monkeypatch.setattr(workspace, 'all_items', list_items)
    monkeypatch.setattr(workspace, '_apply_restore', restore_once)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(workspace.recover) for _ in range(2)]
            for future in futures:
                future.result(timeout=15)
        assert calls == ['restore-'+pid]
        assert get('restore', 'restore-'+pid)['state'] == 'done'
        assert workspace.read_file(pid, 'memo.md') == 'before'
        assert workspace.read_file(pid, 'after-recovery.md') == 'keep this'
    finally:
        delete('restore', 'restore-'+pid)
        delete('project', pid)


@pytest.mark.parametrize('value', ['0', '9', 'bad'])
def test_invalid_concurrency_is_rejected(monkeypatch, value):
    monkeypatch.setenv('ATLAS_CONVERSATION_CONCURRENCY', value)
    with pytest.raises(ValueError):
        concurrency()
