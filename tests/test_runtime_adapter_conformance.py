"""Runtime selection must preserve durable work and fail closed on drift.

These tests use an isolated Nexus WorkStudio task and controlled runtime boundaries.  They
do not claim that any third-party runtime or real model has been exercised.
"""
import pytest

from agent_service import agent, opencode_runtime, provider
from agent_service.store import delete, get, patch, now, uid
from test_opencode_agent_bridge import turn


def test_legacy_task_keeps_its_frozen_runtime_when_default_changes(turn, monkeypatch):
    from agent_service import agent_runtime
    from agent_service.runtime_legacy import LegacyRuntime

    captured = []
    monkeypatch.setattr(opencode_runtime, 'is_enabled', lambda: False)
    monkeypatch.setattr(LegacyRuntime, 'run', lambda self, turn_id: captured.append(turn_id))
    selected = agent_runtime.snapshot('nexus_legacy')
    patch('turn', turn['id'], agent_runtime=selected)

    # A deployment/configuration change must affect new work only.  It must
    # never reroute the already running task to a different engine.
    monkeypatch.setattr(opencode_runtime, 'is_enabled', lambda: True)
    monkeypatch.setattr(opencode_runtime, 'run_session', lambda *a, **kw: pytest.fail('silently switched to OpenCode'))
    agent.advance(get('turn', turn['id']))
    current = get('turn', turn['id'])
    assert captured == [turn['id']]
    assert current['agent_runtime'] == selected
    assert current['state'] == 'agent'


@pytest.mark.parametrize('saved', [
    {'name': 'not-installed-runtime', 'version': '1'},
    {'name': 'nexus_legacy', 'version': 'unexpected-old-version'},
])
def test_unknown_or_drifted_runtime_pauses_without_execution(turn, monkeypatch, saved):
    from agent_service.runtime_legacy import LegacyRuntime

    monkeypatch.setattr(LegacyRuntime, 'run', lambda *a, **kw: pytest.fail('executed an incompatible runtime'))
    monkeypatch.setattr(opencode_runtime, 'run_session', lambda *a, **kw: pytest.fail('silently switched engine'))
    patch('turn', turn['id'], agent_runtime=saved)
    agent.advance(get('turn', turn['id']))
    current = get('turn', turn['id'])
    assert current['agent_runtime'] == saved
    assert current['state'] in ('done', 'failed')
    assert current.get('resume_available') is True
    assert not current.get('agent_trace')


def test_unavailable_opencode_never_falls_back_to_legacy(turn, monkeypatch):
    from agent_service import agent_runtime
    from agent_service.runtime_legacy import LegacyRuntime

    selected = agent_runtime.snapshot('opencode')
    patch('turn', turn['id'], agent_runtime=selected, opencode_session_id='persisted-session')
    monkeypatch.setattr(opencode_runtime, 'is_enabled', lambda: False)
    monkeypatch.setattr(LegacyRuntime, 'run', lambda *a, **kw: pytest.fail('lost runtime affinity'))
    agent.advance(get('turn', turn['id']))
    current = get('turn', turn['id'])
    assert current['agent_runtime'] == selected
    assert current['opencode_session_id'] == 'persisted-session'
    assert current['state'] in ('done', 'failed')
    assert current.get('resume_available') is True


def test_pre_adapter_opencode_session_cannot_silently_migrate(turn, monkeypatch):
    from agent_service import agent_runtime

    monkeypatch.setattr(opencode_runtime, 'is_enabled', lambda: False)
    current = patch('turn', turn['id'], opencode_session_id='historical-native-session', opencode_version='2.1.2')
    with pytest.raises(provider.ModelServiceError):
        agent_runtime.select_for_turn(current)
    assert get('turn', turn['id'])['opencode_session_id'] == 'historical-native-session'


def test_incompatible_runtime_must_not_execute_an_inflight_write(turn, monkeypatch):
    from agent_service import workspace

    patch('turn', turn['id'], agent_runtime={'name': 'nexus_legacy', 'version': 'unavailable-version'},
          state='agent_tool', agent_trace=[{'id': 'persisted-call', 'tool': 'write_file',
          'arguments': {'path': 'reports/native.md', 'content': 'must not execute'},
          'status': 'started', 'started_at': now()}])
    agent.advance(get('turn', turn['id']))
    current = get('turn', turn['id'])
    assert not workspace.path_for(turn['project_id'], 'reports/native.md').exists()
    assert current.get('resume_available') is True
    assert current['agent_trace'][0]['status'] == 'started'


def test_submissions_freeze_default_and_resume_inherits_original(monkeypatch):
    from agent_service import conversation, model_catalog, runtime_settings

    monkeypatch.setattr(model_catalog, 'select', lambda *a, **kw: {})
    selected = ['nexus_legacy']
    monkeypatch.setattr(runtime_settings, 'get_selected', lambda: selected[0])
    project = conversation.create_project(conversation.ProjectInput(name='Runtime selection', request_id='runtime-'+uid()))
    ids = []
    try:
        first = conversation.submit(project['id'], conversation.TurnInput(text='Prepare a report', request_id='runtime-first-'+uid()))
        ids.append(first['id'])
        binding = get('turn', first['id'])['agent_runtime']
        assert binding['name'] == 'nexus_legacy'
        patch('turn', first['id'], state='done', outcome='paused', resume_available=True)
        selected[0] = 'opencode'

        resumed = conversation.submit(project['id'], conversation.TurnInput(text='继续', resume_turn_id=first['id'], request_id='runtime-resume-'+uid()))
        ids.append(resumed['id'])
        fresh = conversation.submit(project['id'], conversation.TurnInput(text='A new task', request_id='runtime-fresh-'+uid()))
        ids.append(fresh['id'])
        assert get('turn', resumed['id'])['agent_runtime'] == binding
        assert get('turn', fresh['id'])['agent_runtime']['name'] == 'opencode'
        assert get('turn', first['id'])['agent_runtime'] == binding
    finally:
        for identity in ids:
            delete('turn', identity)
        delete('project', project['id'])


@pytest.mark.parametrize('tool', ['prepare_work_action', 'retired_unknown_tool'])
def test_explicit_continue_rejects_unknown_external_effect_and_keeps_old_task(monkeypatch, tool):
    from agent_service import conversation, model_catalog, runtime_settings

    monkeypatch.setattr(model_catalog, 'select', lambda *a, **kw: {})
    monkeypatch.setattr(runtime_settings, 'get_selected', lambda: 'nexus_legacy')
    project = conversation.create_project(conversation.ProjectInput(name='Effect recovery', request_id='effect-'+uid()))
    ids = []
    try:
        first = conversation.submit(project['id'], conversation.TurnInput(text='Send a work item', request_id='effect-old-'+uid()))
        ids.append(first['id'])
        trace = [{'id': 'uncertain-effect', 'tool': tool, 'arguments': {}, 'status': 'started', 'started_at': now()}]
        patch('turn', first['id'], state='done', outcome='paused', resume_available=True, agent_trace=trace)
        with pytest.raises(ValueError, match='结果尚未确认'):
            conversation.submit(project['id'], conversation.TurnInput(text='继续', resume_turn_id=first['id'], request_id='effect-resume-'+uid()))
        saved = get('turn', first['id'])
        assert saved['resume_available'] is True
        assert saved['agent_trace'] == trace
    finally:
        for identity in ids:
            delete('turn', identity)
        delete('project', project['id'])


def test_read_only_started_trace_may_be_recovered():
    from agent_service.conversation import assert_resumable_effects

    assert_resumable_effects({'agent_trace': [{'tool': 'read_work_action', 'status': 'started'}]})


@pytest.mark.parametrize('tool', ['prepare_work_action', 'retired_unknown_tool'])
def test_worker_restart_never_reexecutes_unknown_effect(turn, monkeypatch, tool):
    from agent_service import agent_runtime

    monkeypatch.setattr(agent, 'execute', lambda *a, **kw: pytest.fail('uncertain operation was reexecuted'))
    trace = [{'id': 'uncertain-effect', 'tool': tool, 'arguments': {}, 'status': 'started', 'started_at': now()}]
    patch('turn', turn['id'], agent_runtime=agent_runtime.snapshot('nexus_legacy'), state='agent_tool', agent_trace=trace)
    agent.advance(get('turn', turn['id']))
    saved = get('turn', turn['id'])
    assert saved['state'] in ('done', 'failed')
    assert saved.get('resume_available') is True
    assert saved['agent_trace'] == trace


@pytest.mark.parametrize('tool', ['prepare_work_action', 'retired_unknown_tool'])
def test_native_call_replay_never_reexecutes_unknown_effect(turn, monkeypatch, tool):
    monkeypatch.setattr(agent, 'execute', lambda *a, **kw: pytest.fail('uncertain operation was reexecuted'))
    trace = [{'id': 'uncertain-effect', 'opencode_call_id': 'session:call', 'tool': tool,
              'arguments': {}, 'status': 'started', 'started_at': now()}]
    patch('turn', turn['id'], state='agent_tool', agent_trace=trace)
    response = agent.opencode_step(turn['id'], {'kind': 'tool', 'tool': tool, 'arguments': {}}, 'session:call')
    saved = get('turn', turn['id'])
    assert response['terminal'] and response['observation']['code'] == 'OPERATION_RESULT_UNKNOWN'
    assert saved.get('resume_available') is True and saved['agent_trace'] == trace


def test_worker_restart_may_resume_read_only_call(turn, monkeypatch):
    from agent_service import agent_runtime

    called = []
    monkeypatch.setattr(agent, 'execute', lambda _turn, step: called.append(step.tool) or {'state': 'done'})
    trace = [{'id': 'read-call', 'tool': 'read_work_action', 'arguments': {'action_id': 'existing'},
              'status': 'started', 'started_at': now()}]
    patch('turn', turn['id'], agent_runtime=agent_runtime.snapshot('nexus_legacy'), state='agent_tool', agent_trace=trace)
    agent.advance(get('turn', turn['id']))
    assert called == ['read_work_action']
    assert get('turn', turn['id'])['agent_trace'][0]['status'] == 'done'
