"""Loss of an ownership session during an otherwise successful DB write."""
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from agent_service import locks, store
from agent_service.config import DATA


class Backend:
    def __init__(self):
        self.calls = 0

    def execute(self, _query, *_args):
        self.calls += 1
        if self.calls > 1:
            raise OSError('session terminated during transaction')
        return SimpleNamespace(scalar=lambda: 123)


@pytest.mark.parametrize('operation', ['put', 'patch', 'delete', 'transaction'])
def test_session_loss_during_write_rolls_back(operation):
    identity = 'lock-transaction-' + store.uid()
    store.put('system', {'value': 'before'}, identity)
    lock = SimpleNamespace(depth=1, generation=1, connection=Backend(), backend=123)
    token = locks._owned.set(((lock, 1),))
    try:
        with pytest.raises(locks.LockLost):
            if operation == 'put':
                store.put('system', {'value': 'after'}, identity)
            elif operation == 'patch':
                store.patch('system', identity, value='after')
            elif operation == 'delete':
                store.delete('system', identity)
            else:
                with store.transaction() as connection:
                    connection.execute(update(store.items).where(store.items.c.id == identity)
                                       .values(body='{"value":"after"}'))
        assert store.get('system', identity)['value'] == 'before'
    finally:
        locks._owned.reset(token)
        store.delete('system', identity)


def test_failed_postgres_lock_acquisition_discards_possibly_locked_session(monkeypatch):
    class Connection:
        invalidated = False
        closed = False
        def execution_options(self, **_kwargs):return self
        def execute(self, statement, *_args):
            if 'pg_try_advisory_lock' in str(statement):
                return SimpleNamespace(scalar=lambda: True)
            raise OSError('lost session after lock acquisition')
        def invalidate(self):self.invalidated = True
        def close(self):self.closed = True
    connection = Connection()
    monkeypatch.setattr(locks, 'DATABASE_URL', 'postgresql://unused')
    monkeypatch.setattr(locks, '_engine', SimpleNamespace(connect=lambda: connection))
    lock = locks.FileLock(str(DATA / ('acquisition-fault-' + store.uid() + '.lock')), timeout=1)
    with pytest.raises(OSError):lock.acquire()
    assert connection.invalidated and connection.closed
    assert lock.connection is None and lock.depth == 0
    assert not lock.local.is_locked
