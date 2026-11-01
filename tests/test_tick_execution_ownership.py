"""Scheduler ownership and unexpected native-loop exit regressions."""
import threading

import pytest

from agent_service import agent, conversation, opencode_runtime, tasks, workspace
from agent_service.store import delete, get, now, patch, put, uid
from test_opencode_agent_bridge import turn


def test_live_project_lock_prevents_a_second_tick_from_starting_the_same_turn(monkeypatch):
    """A live owner, not the elapsed time, determines if execution is busy."""
    pid, tid = 'p_' + uid(), 't_' + uid()
    put('project', {'name': 'concurrent scheduler test'}, pid)
    put('turn', {'project_id': pid, 'interaction_mode': 'work', 'text': 'test',
                 'state': 'agent', 'created_at': now()}, tid)
    entered, release = threading.Event(), threading.Event()
    calls, failures = [], []
    real_all = conversation.all_items
    monkeypatch.setattr(conversation, 'all_items',
                        lambda kind: [value for value in real_all(kind) if value['id'] == tid]
                        if kind == 'turn' else real_all(kind))
    monkeypatch.setattr(workspace, 'begin', lambda *_: None)
    monkeypatch.setattr(workspace, 'recover', lambda: None)
    monkeypatch.setattr(workspace, 'finalize', lambda *_: None)
    monkeypatch.setattr(tasks, 'ensure', lambda *_: None)

    def handle(value):
        calls.append(value['id'])
        entered.set()
        if not release.wait(10):
            failures.append('first execution timed out')
        patch('turn', tid, state='done')

    monkeypatch.setattr(conversation, 'handle', handle)
    worker = threading.Thread(target=conversation.tick)
    try:
        worker.start()
        assert entered.wait(5)
        # No arbitrary age threshold is needed, including for long jobs.
        assert conversation.tick() is False
        assert calls == [tid]
    finally:
        release.set()
        worker.join(10)
        delete('turn', tid)
        delete('project', pid)
    assert not worker.is_alive() and not failures


@pytest.mark.parametrize('session_id', [None, 'native-session'])
def test_native_loop_unexpected_return_pauses_instead_of_relaunching(turn, monkeypatch, session_id):
    """A stored session ID does not imply that a model loop is still alive."""
    calls = []
    monkeypatch.setattr(opencode_runtime, 'is_enabled', lambda: True)
    monkeypatch.setattr(agent, 'planning_context', lambda *_args, **_kw:
                        ([{'content': 'instruction'}, {'content': '{}'}], None))
    def ended(current, *_args):
        calls.append(current['id'])
        if session_id:
            patch('turn', current['id'], opencode_session_id=session_id)
        return {'session_id': session_id, 'state': 'agent', 'context_rebuild_required': False}
    monkeypatch.setattr(opencode_runtime, 'run_session', ended)
    agent.advance(get('turn', turn['id']))
    saved = get('turn', turn['id'])
    assert saved['state'] == 'done' and saved['outcome'] == 'paused'
    assert saved['resume_available'] is True and len(calls) == 1
    assert '尚未完成' in saved['answer']


def test_context_rebuild_return_does_not_falsely_end_a_live_task(turn, monkeypatch):
    monkeypatch.setattr(opencode_runtime, 'is_enabled', lambda: True)
    monkeypatch.setattr(agent, 'planning_context', lambda *_args, **_kw:
                        ([{'content': 'instruction'}, {'content': '{}'}], None))
    monkeypatch.setattr(opencode_runtime, 'run_session',
                        lambda *_args: {'session_id': None, 'state': 'agent',
                                        'context_rebuild_required': True})
    agent.advance(get('turn', turn['id']))
    saved = get('turn', turn['id'])
    assert saved['state'] == 'agent' and not saved.get('answer')
