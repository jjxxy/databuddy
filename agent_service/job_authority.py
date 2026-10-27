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
_turn_held=ContextVar('atlas_job_turn_locks',default=frozenset())


@contextmanager
def turn_guard(identity):
    """Serialize owner-job admission/publication with that turn's Stop."""
    from .locks import FileLock,assert_owned
    from .config import DATA
    import hashlib
    if identity in _turn_held.get():
        assert_owned();yield;return
    path=DATA/('turn-jobs-'+hashlib.sha256(identity.encode()).hexdigest()+'.lock')
    with FileLock(str(path),timeout=10):
        token=_turn_held.set(_turn_held.get()|{identity})
        try:yield
        finally:_turn_held.reset(token)


def _workspace_origin(run):
    identity=run.get('originating_turn_id')
    if identity is None:return None
    try:
        if not isinstance(identity,str) or not identity:raise ValueError()
        turn=get('turn',identity)
        if (turn.get('project_id')!=run.get('project_id') or turn.get('gateway_client_id')
                or turn.get('state') in ('done','failed') or turn.get('outcome')=='cancelled'):
            raise ValueError()
        from .workspace_lifecycle import assert_turn_live
        from .agent_team import assert_turn_allowed
        assert_turn_live(turn);assert_turn_allowed(turn)
        return identity
    except (KeyError,ValueError):raise Cancelled() from None


@contextmanager
def admission_guard(extra):
    value=extra or {};authority=value.get('gateway_authority')
    if authority and isinstance(authority,dict) and isinstance(authority.get('client_id'),str):
        with client_guard(authority['client_id']):yield
    elif value.get('originating_turn_id') is not None:
        identity=value['originating_turn_id']
        if not isinstance(identity,str) or not identity:raise Cancelled()
        with turn_guard(identity):
            _workspace_origin(value)
            yield
    else:yield

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
            _workspace_origin(run)
            return
        if not authority:raise Cancelled()
    # Unmarked legacy rows remain conservative; never stamp them as trusted
    # workspace admissions merely because the current caller lacks a grant.
    if 'gateway_authority' in run and not authority:raise Cancelled()
    if not authority:
        if _legacy_gateway_job(run):raise Cancelled()
        _workspace_origin(run)
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
    elif run.get('originating_turn_id') is not None:
        identity=run['originating_turn_id']
        if not isinstance(identity,str) or not identity:raise Cancelled()
        with turn_guard(identity):
            check(run)
            yield
    else:
        check(run)
        yield


def cancel_owned_async_jobs(turn):
    """Use immutable origin, or an unambiguous legacy link; never a target."""
    from sqlalchemy import select
    from .jobs import jobs,engine,render,request_cancel,ACTIVE
    links={'data_workflow':'data_workflow_job','report_fetch':'report_fetch_job'}
    with engine.connect() as connection:
        rows=connection.execute(select(jobs).where(jobs.c.kind.in_(links),jobs.c.status.in_(ACTIVE))).mappings().all()
    others=None;cancelled=[]
    for row in rows:
        run=render(row)
        if (run.get('project_id')!=turn['project_id'] or run.get('gateway_authority')
                or (run.get('authority_origin') or {}).get('kind')=='gateway'):continue
        origin=run.get('originating_turn_id')
        if origin is not None:
            if origin!=turn['id']:continue
        else:
            link=links[run['kind']]
            if turn.get(link)!=run['id']:continue
            if others is None:others=all_items('turn')
            if any(other['id']!=turn['id'] and other.get('project_id')==turn['project_id']
                   and other.get('state') not in ('done','failed') and other.get(link)==run['id'] for other in others):continue
        try:request_cancel(run['id']);cancelled.append(run['id'])
        except KeyError:pass
    return cancelled


def same_owner(existing,extra):
    """Do not borrow, transfer, or cancel another caller's active work."""
    expected=(extra or {}).get('gateway_authority')
    if existing.get('gateway_authority')!=expected:
        raise ValueError('已有其他授权范围的加工任务，请等待完成后重试')
    check(existing)
