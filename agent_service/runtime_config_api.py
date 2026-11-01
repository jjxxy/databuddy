"""Operator-only runtime configuration, separate from business UI access."""
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from . import runtime_settings


class RuntimeSelection(BaseModel):
    name: str | None = Field(max_length=64, pattern=r'^[a-z][a-z0-9_]*$')
    expected_revision: str = Field(min_length=1, max_length=64)


def install(app, auth, operator_auth):
    @app.get('/api/operator/agent-runtimes', dependencies=[Depends(operator_auth)])
    def runtimes():
        from . import agent_runtime
        from .runtime import nodes
        return {'selection': runtime_settings.get_state(),
                'effective_name': agent_runtime.configured_name(),
                'adapters': agent_runtime.catalog(),
                'agent_nodes': [{key: node.get(key) for key in ('healthy','runtime_revision','planner_mode',
                                'agent_runtime_default','opencode_enabled_locally','agent_runtime_config_valid')}
                                for node in nodes('agent')],
                'availability_scope': 'api_process_only',
                'notice': '可用性只反映当前 API 进程配置，不代表 Agent 节点或模型已通过验证；已提交任务继续使用其固定执行器。'}

    @app.put('/api/operator/agent-runtimes', dependencies=[Depends(operator_auth)])
    def configure(value: RuntimeSelection):
        try:
            return {'selection': runtime_settings.set_selected(value.name,value.expected_revision)}
        except runtime_settings.SettingsConflict as exc:
            raise HTTPException(status_code=409,detail=str(exc)) from None
