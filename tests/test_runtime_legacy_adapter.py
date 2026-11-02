"""Nexus WorkStudio's bundled runtime executes the actual planner and durable tool graph."""
import json

import pytest

from agent_service import provider, workspace
from agent_service.runtime_legacy import LegacyRuntime
from agent_service.store import get, patch
from test_model_plan_recovery import call, choice, model
from test_opencode_agent_bridge import turn


def test_legacy_adapter_runs_real_model_tool_and_finishes_with_saved_receipt(turn, model, monkeypatch):
    _, requests, responses = model
    writes = []
    write_file = workspace.write_file
    def record_write(*args, **kwargs):
        writes.append(args)
        return write_file(*args, **kwargs)
    monkeypatch.setattr(workspace, 'write_file', record_write)
    responses.extend([
        choice(calls=[call('write_file', {'path': 'reports/native.md', 'content': '受控报告'})]),
        choice(calls=[call('finish_task', {'answer': '报告已保存', 'status': 'complete'})]),
    ])
    runtime = LegacyRuntime()
    assert runtime.available()
    with provider.task_scope(turn['id']):
        runtime.run(turn['id'])
    intermediate = get('turn', turn['id'])
    assert intermediate['state'] == 'agent'
    assert len(intermediate['agent_trace']) == 1
    assert intermediate['agent_trace'][0]['status'] == 'done'
    assert workspace.read_file(turn['project_id'], 'reports/native.md') == '受控报告'
    # A freshly constructed adapter represents a later worker invocation; it
    # must continue using the persisted receipt without re-executing the write.
    with provider.task_scope(turn['id']):
        LegacyRuntime().run(turn['id'])
    saved = get('turn', turn['id'])
    assert saved['outcome'] == 'verified_complete'
    assert len(saved['agent_trace']) == 1
    assert len(requests) == 2
    assert len(writes) == 1


def test_legacy_adapter_cannot_grant_chat_mode_write_access(turn, model):
    config, requests, responses = model
    config['tool_protocol'] = 'json'
    patch('turn', turn['id'], interaction_mode='chat')
    forbidden = {'kind': 'tool', 'tool': 'write_file',
                 'arguments': {'path': 'reports/native.md', 'content': '不得写入'}}
    responses.extend([choice(json.dumps(forbidden, ensure_ascii=False)) for _ in range(3)])
    with provider.task_scope(turn['id']), pytest.raises(provider.ModelOutputError):
        LegacyRuntime().run(turn['id'])
    assert len(requests) == 3
    assert not workspace.path_for(turn['project_id'], 'reports/native.md').exists()
    assert not get('turn', turn['id']).get('agent_trace')
