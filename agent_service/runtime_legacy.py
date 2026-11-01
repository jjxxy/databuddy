"""Adapter for Atlas's existing durable, model-driven planning loop.

The Atlas worker invokes one validated graph step at a time and schedules the
next step from persisted task state. No external CLI or third-party runtime is
installed by this adapter. Tool permissions and completion checks continue to
be enforced by the same Atlas planner and execution graph.
"""


class LegacyRuntime:
    name = 'atlas_legacy'
    version = '1'
    label = 'Atlas 自有执行器'

    def available(self):
        # The implementation is bundled; the selected model's availability
        # and data authorization are checked separately during execution.
        return True

    def capabilities(self):
        return {'contract_version': '1', 'tool_control': 'atlas_validated',
                'approval': 'atlas', 'progress': 'persisted_polling',
                'continuation': 'atlas_state', 'native_session': False}

    def run(self, turn_id):
        from .agent import agent_graph
        return agent_graph.invoke({'turn_id': turn_id})
