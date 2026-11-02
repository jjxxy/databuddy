"""Versioned Agent runtime adapters and durable per-turn selection.

Nexus WorkStudio remains the owner of authorization, tools and execution receipts. An
adapter only drives an existing task; changing the runtime is not a migration
of a native session, and an unavailable frozen runtime never falls back.
"""
from __future__ import annotations

from typing import Protocol

from .opencode import VERSION as OPENCODE_VERSION
from .runtime_legacy import LegacyRuntime


class RuntimeAdapter(Protocol):
    name: str
    version: str
    label: str

    def available(self) -> bool: ...
    def capabilities(self) -> dict: ...
    def run(self, turn_id: str): ...


class OpenCodeRuntime:
    name = 'opencode'
    version = OPENCODE_VERSION
    label = 'OpenCode'

    def available(self):
        # Readiness of the actual service is checked on execution, not in an
        # API process that may run on another node from the Agent worker.
        from . import opencode_runtime
        return opencode_runtime.is_enabled()

    def capabilities(self):
        return {'contract_version': '1', 'tool_control': 'nexus_validated',
                'approval': 'nexus', 'progress': 'persisted_polling',
                'continuation': 'nexus_state', 'native_session': True}

    def run(self, turn_id):
        import json
        from . import agent, opencode_runtime, provider
        from .store import get, patch
        while True:
            current = get('turn', turn_id)
            if current['state'] not in ('agent', 'agent_tool'):
                return
            messages, _ = agent.planning_context(current, native=False)
            prompt = messages[0]['content'].replace(
                '只返回符合response_schema的JSON，不输出思维链。',
                '按工具协议返回结果，不输出思维链。',
            ) + ('本轮由OpenCode运行原生工具循环。每一步都调用nexus_step，并把response_schema中的对象作为函数参数；'
                 '最终答复也通过nexus_step提交，不返回模拟工具JSON或自由文本。')
            try:
                receipt = opencode_runtime.run_session(
                    current, prompt, json.loads(messages[1]['content']),
                    lambda step, call_id: agent.opencode_step(turn_id, step, call_id),
                )
            except provider.ContextWindowExceeded as exc:
                from .context_window import recover
                if exc.source == 'irreducible' or not recover(get('turn', turn_id), exc):
                    raise
                continue
            patch('turn', turn_id, opencode_receipt=receipt)
            current = get('turn', turn_id)
            # A finished native loop is not a completed Nexus WorkStudio task. Unless the
            # bridge explicitly asked the scheduler to rebuild context, an
            # active turn here has no running owner. Without this guard the next
            # tick would launch the same request repeatedly, accumulating model
            # calls and potentially repeating work. Session presence alone is
            # not an ownership marker, and valid long-running sessions remain
            # protected by the project lock while run_session is in progress.
            if current['state'] in ('agent', 'agent_tool') and not receipt.get('context_rebuild_required'):
                raise provider.ModelOutputError('Agent 执行已中断，任务尚未完成；已有步骤和成果已保留，请继续或重试')
            return receipt


_ADAPTERS: dict[str, RuntimeAdapter] = {
    'opencode': OpenCodeRuntime(),
    'nexus_legacy': LegacyRuntime(),
}


def runtime_names():
    return frozenset(_ADAPTERS)


def configured_name():
    """Explicit administrator selection, then the pre-existing deployment default."""
    from .runtime_settings import get_selected
    selected = get_selected()
    if selected:
        return selected
    from . import opencode_runtime
    return 'opencode' if opencode_runtime.is_enabled() else 'nexus_legacy'


def catalog():
    """Configured implementation availability is local, not a cluster health check."""
    return [{'name': adapter.name, 'version': adapter.version, 'label': adapter.label,
             'available': adapter.available(), 'capabilities': adapter.capabilities()} for adapter in _ADAPTERS.values()]


def snapshot(name=None):
    """Create a durable, versioned binding; do not infer remote worker health."""
    from .provider import ModelServiceError
    name = name if name is not None else configured_name()
    adapter = _ADAPTERS.get(name) if isinstance(name, str) else None
    if adapter is None:
        raise ModelServiceError('所选 Agent 执行器未安装或不受支持，任务未执行')
    return {'name': adapter.name, 'version': adapter.version}


def _has_opencode_history(turn):
    return bool(turn.get('opencode_session_id') or turn.get('opencode_version')
                or turn.get('opencode_receipt') or turn.get('opencode_retired_sessions')
                or any(isinstance(step, dict) and step.get('opencode_call_id')
                       for step in (turn.get('agent_trace') or [])))


def snapshot_for_turn(turn):
    """Resolve an old or existing task without mutating its audit history.

    Explicit bindings always win. Old tasks without a binding retain the
    runtime evidenced by a native session or receipt. Other historical tasks
    have no reliable runtime identity and use the current deployment default.
    No native session is ever treated as another engine.
    """
    from .provider import ModelServiceError
    bound = turn.get('agent_runtime')
    if bound is not None:
        if not isinstance(bound, dict) or not isinstance(bound.get('name'), str) or not isinstance(bound.get('version'), str):
            raise ModelServiceError('任务的 Agent 执行器记录无效；已有工作已保留')
        if bound['name'] != 'opencode' and _has_opencode_history(turn):
            raise ModelServiceError('任务的执行器与原生会话不一致；已有工作已保留')
        return validate_snapshot(bound)
    if _has_opencode_history(turn):
        # A recorded historical native version must not be silently rewritten.
        return validate_snapshot({'name': 'opencode', 'version': str(turn.get('opencode_version') or OPENCODE_VERSION)})
    return snapshot()


def binding_origin(turn):
    """Record when historical runtime identity was inferred, not proven."""
    if turn.get('agent_runtime_origin'):
        return turn['agent_runtime_origin']
    if turn.get('agent_runtime') is not None:
        return 'persisted_binding'
    if _has_opencode_history(turn):
        return 'historical_native_evidence'
    return 'historical_unknown_default'


def validate_snapshot(binding):
    """Validate a durable binding without assuming API and worker share a host."""
    from .provider import ModelServiceError
    if not isinstance(binding, dict) or not isinstance(binding.get('name'), str) or not isinstance(binding.get('version'), str):
        raise ModelServiceError('任务的 Agent 执行器记录无效；已有工作已保留')
    adapter = _ADAPTERS.get(binding['name'])
    if adapter is None:
        raise ModelServiceError('任务固定的 Agent 执行器不可用；已有工作已保留，请恢复原执行器')
    if binding['version'] != adapter.version:
        raise ModelServiceError('任务固定的 Agent 执行器版本不匹配；已有工作已保留，请恢复原版本')
    return {'name': adapter.name, 'version': adapter.version}


def select_for_turn(turn):
    """Bind an old task once, then validate the frozen adapter on this worker."""
    from .provider import ModelServiceError
    from .store import get, patch
    current = get('turn', turn['id'])
    binding = snapshot_for_turn(current)
    adapter = _ADAPTERS[binding['name']]
    if not adapter.available():
        raise ModelServiceError('任务固定的 Agent 执行器未启用；已有工作已保留，请恢复原执行器')
    if current.get('agent_runtime') is None:
        patch('turn', current['id'], agent_runtime=binding, agent_runtime_origin=binding_origin(current))
    return adapter
