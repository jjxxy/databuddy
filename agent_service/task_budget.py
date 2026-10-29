"""Explicit task budgets; ordinary owner conversations have no fixed total cap.

None denotes an unlimited dimension, never a very large substitute integer.
External grants stay finite; internal roles inherit the owner's task policy.
"""
import re

KEYS=('model_calls','input_characters','output_tokens','duration_ms')
LEGACY_DEFAULTS={'model_calls':18,'input_characters':360000,'output_tokens':24000,'duration_ms':300000}
LEGACY_DEEP={'model_calls':40,'input_characters':1000000,'output_tokens':128000,'duration_ms':600000}
LEGACY_SPECIALIST={'model_calls':5,'input_characters':120000,'output_tokens':16000,'duration_ms':120000}


def _positive(value):
    if type(value) is not int or value<=0:
        raise ValueError('任务预算必须为正整数；不限额度使用明确的无限策略')
    return value


def _scoped(turn):
    return bool(turn.get('gateway_client_id') or turn.get('gateway_limits')
                or turn.get('collaboration_parent_turn_id'))


def is_interactive(turn):
    return not _scoped(turn) and (turn.get('interactive_unlimited') is True or
        'interactive_unlimited' not in turn and bool(turn.get('session_id')) and
        turn.get('interaction_mode') in ('chat','work'))


def unsupported_round_stop(turn,answer):
    """A model's old round-cap claim is not a runtime admission failure.

    Match a current-task exhaustion assertion, not financial budgets, a user's
    quoted error, daily workspace quotas, or genuine upstream rate limits.
    Actual BudgetExceeded exceptions take their separate runtime error path.
    """
    if not isinstance(answer,str):return False
    claim=re.match(r'^\s*(?:本轮|本次|当前(?:这轮|任务|对话))\s*(?:的)?\s*模型(?:调用)?预算\s*(?:已经|已)?(?:用完|耗尽|超限)(?=[，。；;,.！!\s]|$)',answer)
    return bool(claim) and all(value is None for value in effective_limits(turn).values())


def _legacy_specialist_parent(turn,raw):
    """Recognize only server-linked, pre-policy automatic local-role caps."""
    if ('collaboration_budget_inherited' in turn or raw!=LEGACY_SPECIALIST
            or (turn.get('task_contract') or {}).get('budget_policy') is not None
            or not turn.get('collaboration_parent_turn_id')
            or turn.get('gateway_client_id') or turn.get('gateway_limits')):return None
    from .store import get
    from .agent_team import ROLES
    if turn.get('collaboration_peer_id') not in ROLES or turn.get('collaboration_readonly') is not True:return None
    try:
        parent=get('turn',turn['collaboration_parent_turn_id'])
        task=get('collab_task',turn['collaboration_task_id'])
    except KeyError:return None
    if (not is_interactive(parent) or task.get('transport')!='local'
            or task.get('parent_turn_id')!=parent['id'] or task.get('child_turn_id')!=turn.get('id')
            or task.get('peer_id')!=turn['collaboration_peer_id']):return None
    if any(turn.get(key,default)!=parent.get(key,default) for key,default in
           (('project_id',None),('actor_id','owner'),('model_id',None),('model_catalog_revision',None))):return None
    if task.get('project_id')!=parent['project_id'] or task.get('actor_id','owner')!=parent.get('actor_id','owner'):return None
    return parent


def effective_limits(turn):
    """Four dimensions, each a positive integer or None (no total cap).

    Ordinary session contracts are unlimited in view, including old tasks;
    original audit records remain intact. Explicit non-interactive contracts
    and external grants keep their finite authority.
    """
    contract=turn.get('task_contract') or {}
    policy=contract.get('budget_policy')
    if policy not in (None,'unlimited','bounded'):
        raise ValueError('任务预算策略无效')
    raw=contract.get('budgets') or {}
    if not isinstance(raw,dict):raise ValueError('任务预算记录无效')
    legacy_parent=_legacy_specialist_parent(turn,raw)
    if legacy_parent is not None:return effective_limits(legacy_parent)
    if (turn.get('collaboration_budget_inherited') and turn.get('collaboration_parent_turn_id')
            and not (turn.get('gateway_client_id') or turn.get('gateway_limits'))):
        # Internal roles inherit the owner's policy; a model never chooses a
        # larger grant. Remote/gateway delegation remains explicitly bounded.
        from .store import get
        parent=get('turn',turn['collaboration_parent_turn_id'])
        inherited=effective_limits(parent)
        return {key:inherited[key] if raw.get(key) is None else
                min(_positive(raw[key]),inherited[key]) if inherited[key] is not None else _positive(raw[key]) for key in KEYS}
    if _scoped(turn):
        limits={key:_positive(raw.get(key) if raw.get(key) is not None else default)
                for key,default in LEGACY_DEFAULTS.items()}
        gateway=turn.get('gateway_limits') or {}
        if not isinstance(gateway,dict):raise ValueError('外部Agent任务预算无效')
        for key in KEYS:
            if key in gateway:limits[key]=min(limits[key],_positive(gateway[key]))
        return limits
    if is_interactive(turn) or policy=='unlimited' or policy is None and (not raw or raw==LEGACY_DEFAULTS or raw==LEGACY_DEEP):
        return dict.fromkeys(KEYS,None)
    return {key:None if raw.get(key) is None else _positive(raw[key]) for key in KEYS}


def exceeded_reason(limits,usage,*,input_characters=0,output_tokens=0,elapsed_ms=0):
    """Check the next admission without changing usage or taking reservations."""
    for value in (input_characters,output_tokens,elapsed_ms):
        if type(value) is not int or value<0:raise ValueError('模型预算计量必须是非负整数')
    projected={'model_calls':usage.get('calls',0)+1,
               'input_characters':usage.get('input_characters',0)+input_characters,
               'output_tokens':usage.get('output_tokens_budgeted',0)+output_tokens,
               'duration_ms':usage.get('duration_ms',0)+elapsed_ms}
    for key in KEYS:
        cap=limits.get(key)
        if cap is not None:
            _positive(cap)
            if projected[key]>cap or key=='duration_ms' and projected[key]>=cap:return key
    return None


def tool_step_limit(turn):
    """Retain an explicit bounded task's tool gate, without an owner 64-step cap."""
    calls=effective_limits(turn)['model_calls']
    return None if calls is None else max(0,calls-1)
