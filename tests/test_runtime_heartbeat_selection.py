"""Agent heartbeat reports new-task default separately from local enablement."""
import pytest

from agent_service import agent_runtime, runtime


@pytest.mark.parametrize(('selected','enabled','planner'), [
    ('opencode', True, 'opencode'),
    ('opencode', False, 'opencode'),
    ('atlas_legacy', True, 'legacy'),
    ('atlas_legacy', False, 'legacy'),
])
def test_heartbeat_reports_selection_and_local_enablement(monkeypatch, selected, enabled, planner):
    saved=[]
    monkeypatch.setattr(runtime, 'put', lambda kind, value, key: saved.append((kind, dict(value), key)))
    monkeypatch.setattr(agent_runtime, 'configured_name', lambda: selected)
    monkeypatch.setenv('ATLAS_OPENCODE_RUNTIME', '1' if enabled else '0')
    runtime.heartbeat('agent')
    assert len(saved)==2
    for _, value, _ in saved:
        assert value['planner_mode']==planner
        assert value['agent_runtime_default']==selected
        assert value['opencode_enabled_locally'] is enabled
        assert value['agent_runtime_config_valid'] is True
        assert 'agent_runtime_ready' not in value  # enablement is not health


def test_heartbeat_fails_closed_without_exposing_config_exception(monkeypatch):
    saved=[]
    monkeypatch.setattr(runtime, 'put', lambda kind, value, key: saved.append(dict(value)))
    def broken():
        raise ValueError('configuration contained TEST_SECRET')
    monkeypatch.setattr(agent_runtime, 'configured_name', broken)
    runtime.heartbeat('agent')
    assert len(saved)==2
    for value in saved:
        assert value['planner_mode']=='unknown'
        assert value['agent_runtime_default'] is None
        assert value['agent_runtime_config_valid'] is False
        assert 'TEST_SECRET' not in str(value)


def test_selection_change_is_reflected_without_restarting_node(monkeypatch):
    from agent_service import runtime_settings
    saved=[]
    selected=['opencode']
    monkeypatch.setattr(runtime, 'put', lambda kind, value, key: saved.append(dict(value)))
    monkeypatch.setattr(runtime_settings, 'get_selected', lambda: selected[0])
    monkeypatch.setenv('ATLAS_OPENCODE_RUNTIME','1')
    runtime.heartbeat('agent')
    selected[0]='atlas_legacy'
    runtime.heartbeat('agent')
    assert [saved[i]['planner_mode'] for i in (0,2)]==['opencode','legacy']
    assert all(value['opencode_enabled_locally'] for value in saved)


def test_separate_worker_is_not_an_agent_node(monkeypatch):
    saved=[]
    monkeypatch.setattr(runtime, 'put', lambda kind, value, key: saved.append(dict(value)))
    monkeypatch.setenv('AGENT_ORCHESTRATION_MODE', 'separate')
    runtime.heartbeat('worker')
    assert all('planner_mode' not in value for value in saved)
