"""Isolated debug execution; its receipts are never full-flow receipts."""
from .business_flow_engine import BusinessFlow, Handler, digest, run, _MAX_INPUT

def run_debug(document, frozen_inputs, handlers, context, selected_nodes, *, allowed_kinds,
              checkpoint=None, persist=None):
    """Execute an explicitly dependency-closed subset as an isolated debug run.

    The host must validate project ownership and the full-flow runtime lock and
    supply a separate durable debug store. This envelope is never a full-flow
    receipt and must never be accepted for publishing. No upstream steps are
    implicitly run and no historical outputs are reused.
    """
    if not callable(persist):raise ValueError('缺少调试记录持久化服务')
    flow=BusinessFlow.model_validate(document)
    full=flow.model_dump(mode='json')
    if not isinstance(selected_nodes,list) or not selected_nodes or len(selected_nodes)>50 or any(not isinstance(x,str) for x in selected_nodes):
        raise ValueError('请选择 1 至 50 个要运行的节点')
    chosen=set(selected_nodes)
    if len(chosen)!=len(selected_nodes):raise ValueError('选择的节点不能重复')
    by_id={n.id:n for n in flow.nodes}
    if not chosen<=set(by_id):raise ValueError('选择的节点不属于当前流程')
    if not isinstance(allowed_kinds,(set,frozenset)) or any(not isinstance(x,str) for x in allowed_kinds):
        raise ValueError('调试运行策略无效')
    ordered=[n for n in flow.nodes if n.id in chosen]
    missing=sorted({dep for node in ordered for dep in node.depends_on if dep not in chosen})
    if missing:raise ValueError('所选节点缺少上游依赖：'+', '.join(missing))
    for node in ordered:
        handler=handlers.get(node.kind) if isinstance(handlers,dict) else None
        if node.kind not in allowed_kinds or not isinstance(handler,Handler) or not handler.replay_safe:
            raise ValueError('所选节点不允许在隔离调试中运行：'+node.id)
    required={r.name for n in ordered for r in n.inputs.values() if r.source=='input'}
    if not isinstance(frozen_inputs,dict) or set(frozen_inputs)!=required:raise ValueError('调试输入与所选节点不一致')
    subset={'version':1,'name':flow.name,'inputs':[i for i in flow.inputs if i in required],
            'nodes':[n.model_dump(mode='json') for n in ordered],'outputs':[n.id for n in ordered]}
    if flow.input_specs is not None:subset['input_specs']={k:v.model_dump(mode='json') for k,v in flow.input_specs.items() if k in required}
    scope={'flow_sha256':digest(full),'selected_nodes':[n.id for n in ordered],
           'subset_sha256':digest(subset),'inputs_sha256':digest(frozen_inputs,_MAX_INPUT)}
    prior=None
    if checkpoint is not None:
        if not isinstance(checkpoint,dict) or checkpoint.get('version')!=1 or checkpoint.get('kind')!='business_flow_debug' or checkpoint.get('scope')!=scope or checkpoint.get('publishable') is not False:
            raise ValueError('调试记录与冻结的流程、节点或输入不一致')
        prior=checkpoint.get('state')
        if not isinstance(prior,dict):raise ValueError('调试记录不完整')
    def save(state):
        persist({'version':1,'kind':'business_flow_debug','scope':scope,'publishable':False,'state':state})
    result=run(subset,frozen_inputs,handlers,context,checkpoint=prior,persist=save)
    return {'version':1,'kind':'business_flow_debug','scope':scope,'publishable':False,'state':result}
