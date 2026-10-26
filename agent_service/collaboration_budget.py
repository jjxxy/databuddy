"""One durable, cross-process model budget for a coordinator and its children.

Reservations count attempted model requests, not model-reported reasoning or a
currency estimate. Missing usage remains conservatively reserved. The existing
per-turn and deployment quota gates still apply independently.
"""
import hashlib
import time
from .config import DATA
from .locks import FileLock
from .store import get, put, patch, now

KIND='collab_budget'
DEFAULTS={'model_calls':18,'input_characters':360000,'output_tokens':24000,'duration_ms':300000}
COUNTERS=('calls','input_characters','output_tokens_budgeted')


def _narrow(first,second):
    if first is None:return second
    if second is None:return first
    return min(first,second)


def _effective_record(record,root):
    """Migrate only the known historical default, never an explicit grant.

    A view migration keeps reservations intact. Old custom narrowed ledgers
    remain bounded because they may record an administrator's explicit choice.
    """
    from .task_budget import effective_limits,LEGACY_DEFAULTS,LEGACY_DEEP
    configured=effective_limits(root)
    old=record['limits']
    migrate_default=('limit_overrides' not in record
        and all(value is None for value in configured.values())
        and old in (LEGACY_DEFAULTS,LEGACY_DEEP))
    limits=dict(configured) if migrate_default else {key:_narrow(old.get(key),configured[key]) for key in DEFAULTS}
    deadline=record.get('deadline_at')
    if limits['duration_ms'] is None and migrate_default:deadline=None
    elif limits['duration_ms'] is not None and (deadline is None
            or old.get('duration_ms') is not None and limits['duration_ms']<old['duration_ms']):
        # Do not reset an explicit duration if a stored ledger predates it.
        remaining=limits['duration_ms']-max(0,int(root.get('model_usage',{}).get('duration_ms',0)))
        proposed_deadline=time.time()+max(0,remaining)/1000
        deadline=proposed_deadline if deadline is None else min(deadline,proposed_deadline)
    return dict(record,limits=limits,deadline_at=deadline,
                limit_overrides=record.get('limit_overrides',{}),budget_policy_version=2)


def _root_id(turn):
    return turn.get('collaboration_root_turn_id') or turn.get('collaboration_parent_turn_id') or turn.get('id')


def _identity(root_id):
    return 'cb_'+hashlib.sha256(str(root_id).encode()).hexdigest()[:48]


def _lock(root_id):
    return FileLock(str(DATA/('collaboration-budget-'+hashlib.sha256(str(root_id).encode()).hexdigest()[:32]+'.lock')),timeout=10)


def _reject(message):
    from .provider import BudgetExceeded
    raise BudgetExceeded(message)


def _live_root(root_id):
    root=get('turn',root_id)
    if root.get('state') in ('done','failed') or root.get('outcome')=='cancelled':
        _reject('协同主任务已结束或取消，未继续发起模型请求')
    return root


def initialize(parent_turn,children_budget=None):
    """Capture the coordinator's existing usage once, before creating children.

    ``children_budget`` may narrow limits but cannot raise the task contract's
    limits. It is trusted server configuration, never an LLM-supplied increase.
    Repeated initialization never resets usage or extends the deadline.
    """
    root_id=parent_turn['id']
    if parent_turn.get('collaboration_parent_turn_id'):
        raise ValueError('协同子任务不能再次委派，未创建额外预算')
    with _lock(root_id):
        root=_live_root(root_id)
        try:
            existing=get(KIND,_identity(root_id))
            record=_effective_record(existing,root)
            if record!=existing:record=patch(KIND,existing['id'],**{key:record[key] for key in ('limits','deadline_at','limit_overrides','budget_policy_version')})
            return _public(record)
        except KeyError:pass
        from .tasks import ensure
        contract=ensure(root)
        root=get('turn',root_id)
        from .task_budget import effective_limits
        limits=effective_limits(root)
        for key,value in (children_budget or {}).items():
            if (key not in limits or type(value) is not int or value<=0
                    or limits[key] is not None and value>limits[key]):
                raise ValueError('协同预算只能缩小本轮已经确定的限制')
            limits[key]=value
        usage=root.get('model_usage',{})
        used={key:max(0,int(usage.get(key,0))) for key in COUNTERS}
        remaining_ms=None if limits['duration_ms'] is None else limits['duration_ms']-max(0,int(usage.get('duration_ms',0)))
        if (any(limits[key] is not None and used[counter]>=limits[key]
                for key,counter in (('model_calls','calls'),('input_characters','input_characters'),('output_tokens','output_tokens_budgeted')))
                or remaining_ms is not None and remaining_ms<=0):
            _reject('主任务的剩余模型预算不足，未启动协同任务')
        pin={key:root.get(key) for key in ('model_id','model_catalog_revision')}
        record={'root_turn_id':root_id,'project_id':root['project_id'],'actor_id':root.get('actor_id','owner'),
            'limits':limits,'used':used,'baseline_usage':dict(used),'model_pin':pin,
            'deadline_at':None if remaining_ms is None else time.time()+remaining_ms/1000,
            'limit_overrides':dict(children_budget or {}),'budget_policy_version':2,'created_at':now(),'updated_at':now(),
            'per_turn':{},'policy':'attempted_model_requests_and_reserved_output; no_currency_cost_claim'}
        put(KIND,record,_identity(root_id))
        patch('turn',root_id,collaboration_budget_id=_identity(root_id))
        return _public(record)


def _public(record):
    limits=record['limits'];used=record['used']
    return {'active':True,'root_turn_id':record['root_turn_id'],'limits':dict(limits),'used':dict(used),
        'remaining':{'model_calls':None if limits['model_calls'] is None else max(0,limits['model_calls']-used['calls']),
            'input_characters':None if limits['input_characters'] is None else max(0,limits['input_characters']-used['input_characters']),
            'output_tokens':None if limits['output_tokens'] is None else max(0,limits['output_tokens']-used['output_tokens_budgeted']),
            'duration_ms':None if record['deadline_at'] is None else max(0,round((record['deadline_at']-time.time())*1000))},
        'deadline_at':record['deadline_at'],'policy':record['policy']}


def current_totals(parent):
    identity=parent if isinstance(parent,str) else _root_id(parent)
    return _public(_effective_record(get(KIND,_identity(identity)),get('turn',identity)))


def admit(turn,input_characters,output_tokens):
    """Atomically reserve one attempted call across coordinator and children.

    Must be called before the existing per-turn counters are incremented and
    before network transmission. For ordinary tasks without a ledger it is a
    no-op. A tagged child without its ledger fails closed.
    """
    if type(input_characters) is not int or input_characters<0 or type(output_tokens) is not int or output_tokens<0:
        raise ValueError('模型预算计量必须是非负整数')
    root_id=_root_id(turn)
    if not root_id:return {'active':False}
    try:get(KIND,_identity(root_id))
    except KeyError:
        if turn.get('collaboration_parent_turn_id'):_reject('协同预算记录不可用，未发起模型请求')
        return {'active':False}
    with _lock(root_id):
        root=_live_root(root_id);current=get('turn',turn['id'])
        record=get(KIND,_identity(root_id))
        if (current.get('state') in ('done','failed') or current.get('outcome')=='cancelled'
                or current.get('project_id')!=record['project_id']
                or current.get('actor_id','owner')!=record['actor_id']
                or root.get('project_id')!=record['project_id']):
            _reject('协同任务的状态、身份或项目已变化，未继续请求')
        if any(current.get(key)!=value or root.get(key)!=value for key,value in record['model_pin'].items()):
            _reject('协同任务模型版本与主任务不一致，未切换模型')
        record=_effective_record(record,root)
        limits=record['limits'];used=record['used']
        proposed={'calls':used['calls']+1,'input_characters':used['input_characters']+input_characters,
                  'output_tokens_budgeted':used['output_tokens_budgeted']+output_tokens}
        if (record['deadline_at'] is not None and time.time()>=record['deadline_at']
                or any(limits[key] is not None and proposed[counter]>limits[key]
                    for key,counter in (('model_calls','calls'),('input_characters','input_characters'),('output_tokens','output_tokens_budgeted')))):
            _reject('本轮所有 Agent 的合计模型预算或时间已用完，已有回执与成果保留')
        per_turn=dict(record.get('per_turn',{}));prior=per_turn.get(turn['id'],dict.fromkeys(COUNTERS,0))
        if current.get('collaboration_parent_turn_id'):
            from .task_budget import effective_limits,exceeded_reason
            # An unlimited coordinator cannot enlarge the child's own grant.
            child_usage=dict(prior,duration_ms=current.get('model_usage',{}).get('duration_ms',0))
            if exceeded_reason(effective_limits(current),child_usage,input_characters=input_characters,output_tokens=output_tokens):
                _reject('协同子任务的明确模型预算已用完，主任务与其他回执保留')
        per_turn[turn['id']]={'calls':prior['calls']+1,'input_characters':prior['input_characters']+input_characters,
                            'output_tokens_budgeted':prior['output_tokens_budgeted']+output_tokens}
        record=patch(KIND,record['id'],used=proposed,per_turn=per_turn,limits=record['limits'],
            deadline_at=record['deadline_at'],limit_overrides=record['limit_overrides'],budget_policy_version=2,updated_at=now())
        return _public(record)
