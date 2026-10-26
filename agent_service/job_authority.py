"""Durable machine-task authority for queued work, not model-supplied metadata.

A gateway grant is checked at admission and again before work/publication. The
client lock serializes final publication with cancellation and grant changes.
Completed effects are retained; cancellation does not roll them back.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from .store import get,all_items
from .errors import Cancelled

_held=ContextVar('atlas_job_authority_locks',default=frozenset())

@contextmanager
def client_guard(identity):
    from .agent_gateway import _lock
    if identity in _held.get():
        # Context propagation must not reuse a lock after its owning scope exits.
        from .locks import assert_owned
        assert_owned()
        yield
        return
    with _lock(identity):
        token=_held.set(_held.get()|{identity})
        try:yield
        finally:_held.reset(token)


def for_turn(identity):
    if not identity:return None
    from .agent_gateway import assert_turn_allowed
    turn=get('turn',identity)
    if not turn.get('gateway_client_id'):return None
    assert_turn_allowed(turn)
    if turn.get('state') in ('done','failed'):raise Cancelled()
    return {'turn_id':turn['id'],'client_id':turn['gateway_client_id'],
            'client_revision':turn['gateway_client_revision'],'actor_id':turn['actor_id'],
            'project_id':turn['project_id']}


def _legacy_gateway_job(run):
    """Old linked (or admitted-before-link) machine jobs cannot become owner jobs."""
    if run.get('kind')!='data_workflow':return False
    from .conversation import stable
    for turn in all_items('turn'):
        if not turn.get('gateway_client_id'):continue
        if turn.get('data_workflow_job')==run['id']:return True
        origin=turn.get('agent_origin',turn['id'])
        steps=[s.get('id') for s in turn.get('agent_trace',[]) if s.get('tool')=='run_data_workflow']
        if run.get('scheduled_key') in {stable('dataop_',origin,s) for s in steps+['run_data_workflow'] if s}:return True
    return False


def check(run):
    authority=run.get('gateway_authority')
    if 'authority_origin' in run:
        marker=run['authority_origin']
        if (not isinstance(marker,dict) or set(marker)!={'version','kind'}
                or type(marker['version']) is not int or marker['version']!=1
                or marker['kind'] not in ('workspace','gateway')):raise Cancelled()
        if marker['kind']=='workspace':
            if 'gateway_authority' in run:raise Cancelled()
            return
        if not authority:raise Cancelled()
    # Unmarked legacy rows remain conservative; never stamp them as trusted
    # workspace admissions merely because the current caller lacks a grant.
    if 'gateway_authority' in run and not authority:raise Cancelled()
    if not authority:
        if _legacy_gateway_job(run):raise Cancelled()
        return
    try:
        if not isinstance(authority,dict) or set(authority)!={'turn_id','client_id','client_revision','actor_id','project_id'}:raise ValueError()
        current=for_turn(authority['turn_id'])
        if current!=authority or run.get('project_id')!=authority['project_id']:raise ValueError()
    except (KeyError,ValueError):raise Cancelled() from None


@contextmanager
def guard(run):
    authority=run.get('gateway_authority')
    if authority and isinstance(authority,dict) and isinstance(authority.get('client_id'),str):
        with client_guard(authority['client_id']):
            check(run)
            yield
    else:
        check(run)
        yield


def same_owner(existing,extra):
    """Do not borrow, transfer, or cancel another caller's active work."""
    expected=(extra or {}).get('gateway_authority')
    if existing.get('gateway_authority')!=expected:
        raise ValueError('已有其他授权范围的加工任务，请等待完成后重试')
    check(existing)
