"""Pending sources wait for probing before the configured agent runs."""
import pytest

from agent_service import conversation as chat
from agent_service.store import get, patch
from test_multi_file_conversation import project, upload, submit


@pytest.mark.parametrize('status', ['queued', 'processing'])
def test_implicit_project_source_waits_then_tick_uses_agent(project, monkeypatch, status):
    source = upload(project)['id']
    patch('source', source, status=status)
    patch('project', project[0], source_id=source)
    turn = submit(project, [], text='请分析项目中的资料')
    calls = []
    monkeypatch.setattr('agent_service.agent.advance', lambda value: calls.append(value['id']))
    monkeypatch.setattr(chat, 'legacy_handle', lambda value: pytest.fail('pending input went to the legacy planner'))
    project[2].clear()
    chat.handle(turn)
    assert get('turn', turn['id'])['state'] == 'waiting_source'
    assert calls == [] and project[2] == [('probe', source)]
    patch('source', source, status='ready')
    chat.tick()
    assert calls == [turn['id']]


@pytest.mark.parametrize('mode', ['chat', 'work'])
def test_explicit_multiple_sources_wait_until_every_input_is_ready(project, monkeypatch, mode):
    ids = [upload(project)['id'], upload(project, 'notes.txt', b'notes')['id']]
    patch('source', ids[0], status='ready')
    patch('source', ids[1], status='processing')
    turn = submit(project, ids, interaction_mode=mode)
    calls = []
    monkeypatch.setattr('agent_service.agent.advance', lambda value: calls.append(value['id']))
    monkeypatch.setattr(chat, 'legacy_handle', lambda value: pytest.fail('partial inputs went to legacy planner'))
    chat.handle(turn)
    assert get('turn', turn['id'])['state'] == 'waiting_source' and calls == []
    patch('source', ids[1], status='ready')
    chat.handle(get('turn', turn['id']))
    assert calls == [turn['id']]


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'unexpected'])
def test_unreadable_implicit_project_source_never_reaches_model(project, monkeypatch, status):
    source = upload(project)['id']
    patch('source', source, status=status, error='读取失败')
    patch('project', project[0], source_id=source)
    turn = submit(project, [], text='请分析项目中的资料')
    monkeypatch.setattr('agent_service.agent.advance', lambda value: pytest.fail('unreadable input reached agent'))
    monkeypatch.setattr(chat, 'legacy_handle', lambda value: pytest.fail('unreadable input reached legacy planner'))
    chat.handle(turn)
    saved = get('turn', turn['id'])
    assert saved['state'] == 'done' and saved['outcome'] == 'needs_input'
    assert '读取失败' in saved['answer']


def test_unknown_explicit_attachment_does_not_wait_forever(project, monkeypatch):
    source = upload(project)['id']
    patch('source', source, status='cancelled')
    turn = submit(project, [source])
    monkeypatch.setattr('agent_service.agent.advance', lambda value: pytest.fail('cancelled input reached agent'))
    chat.handle(turn)
    assert get('turn', turn['id'])['outcome'] == 'needs_input'


def test_project_focus_changes_do_not_substitute_waiting_source(project, monkeypatch):
    original = upload(project)['id']
    replacement = upload(project, 'other.csv', b'id,amount\n2,9\n')['id']
    patch('project', project[0], source_id=original)
    turn = submit(project, [], text='分析当前资料')
    calls = []
    monkeypatch.setattr('agent_service.agent.advance', lambda value: calls.append(value['source_id']))
    chat.handle(turn)
    patch('project', project[0], source_id=replacement)
    patch('source', original, status='ready')
    chat.handle(get('turn', turn['id']))
    assert calls == [original]


def test_changed_implicit_source_is_not_silently_used_after_waiting(project, monkeypatch):
    source = upload(project)['id']
    patch('project', project[0], source_id=source)
    turn = submit(project, [], text='分析当前资料')
    monkeypatch.setattr('agent_service.agent.advance', lambda value: pytest.fail('changed input reached agent'))
    chat.handle(turn)
    patch('source', source, status='ready', content_hash='different')
    chat.handle(get('turn', turn['id']))
    assert get('turn', turn['id'])['outcome'] == 'needs_input'
    assert '资料已变更' in get('turn', turn['id'])['answer']


def test_ready_connection_only_still_returns_connection_result(project, monkeypatch):
    source = upload(project)['id']
    patch('source', source, status='ready')
    patch('project', project[0], source_id=source)
    turn = submit(project, [], text='连接资料')
    patch('turn', turn['id'], state='waiting_source', connect_only=True)
    calls = []
    monkeypatch.setattr(chat, 'legacy_handle', lambda value: calls.append(value['id']))
    monkeypatch.setattr('agent_service.agent.advance', lambda value: pytest.fail('connection-only prompt entered agent'))
    chat.handle(get('turn', turn['id']))
    assert calls == [turn['id']]
