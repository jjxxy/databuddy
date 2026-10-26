"""Human Stop must cancel owned async jobs, including admission/link races.

Independent regressions for the audited owner-cancellation gap. No model,
connector request or workflow processing occurs here; storage/queue are real.
"""
import pytest
from sqlalchemy import select,delete as sql_delete

from agent_service import conversation,project_data as data,project_reports as reports
from agent_service.config import DATA
from agent_service.errors import Cancelled
from agent_service.jobs import jobs,get_job,list_jobs
from agent_service.store import engine,items,get,put,patch,uid,now
from test_project_data import project,source,document,registered
from test_project_reports import connector


@pytest.fixture(autouse=True)
def isolated_records():
    assert DATA.resolve().name.startswith('atlas-tests-')
    with engine.connect() as connection:
        before_items=set(connection.execute(select(items.c.id)).scalars())
        before_jobs=set(connection.execute(select(jobs.c.id)).scalars())
    yield
    with engine.begin() as connection:
        connection.execute(sql_delete(jobs).where(jobs.c.id.not_in(before_jobs)))
        connection.execute(sql_delete(items).where(items.c.id.not_in(before_items)))


def task(pid,session=None):
    return put('turn',{'project_id':pid,'session_id':session or conversation.default_session(pid),
        'actor_id':'owner','text':'Synthetic queued work','interaction_mode':'work',
        'state':'agent','created_at':now()},'t_'+uid())


def stop(pid):
    return conversation.submit(pid,conversation.TurnInput(text='停止',request_id='stop_'+uid()))


@pytest.mark.parametrize('link',[True,False],ids=['linked_job','admitted_before_turn_link'])
def test_owner_stop_cancels_queued_workflow_by_exact_origin_even_before_link(link):
    pid=project();src=source(pid);wid=registered(pid,document(src['id']));value=task(pid)
    run=data.enqueue_workflow(pid,wid,originating_turn_id=value['id'])
    assert run['originating_turn_id']==value['id']
    if link:patch('turn',value['id'],state='waiting_data_workflow',data_workflow_job=run['id'])
    stop(pid)
    assert get('turn',value['id'])['outcome']=='cancelled'
    cancelled=get_job(run['id'])
    assert cancelled.get('cancel_requested') is True
    assert cancelled['status']=='cancelled'


def test_owner_cancel_before_final_enqueue_rejects_late_workflow_admission():
    pid=project();src=source(pid);wid=registered(pid,document(src['id']));value=task(pid)
    stop(pid)
    assert get('turn',value['id'])['outcome']=='cancelled'
    with pytest.raises((Cancelled,ValueError)):
        data.enqueue_workflow(pid,wid,originating_turn_id=value['id'])
    assert not list_jobs(target=wid)


def test_owner_stop_cancels_linked_report_fetch_without_contacting_connector(monkeypatch):
    pid=project();configured=connector(monkeypatch,pid);value=task(pid)
    run=reports.enqueue_fetch(pid,configured['id'],operation_id='audit_'+uid())
    patch('turn',value['id'],state='waiting_report_fetch',report_fetch_job=run['id'])
    stop(pid)
    assert get('turn',value['id'])['outcome']=='cancelled'
    cancelled=get_job(run['id'])
    assert cancelled.get('cancel_requested') is True
    assert cancelled['status']=='cancelled'


def test_stop_does_not_cancel_workflow_owned_by_another_conversation():
    pid=project();src=source(pid)
    mine=registered(pid,document(src['id']),path='workflows/mine.json')
    other=registered(pid,document(src['id']),path='workflows/other.json')
    other_session=conversation.create_session(pid,conversation.SessionInput(request_id='other_'+uid()))['id']
    first=task(pid);second=task(pid,other_session)
    own=data.enqueue_workflow(pid,mine,originating_turn_id=first['id'])
    foreign=data.enqueue_workflow(pid,other,originating_turn_id=second['id'])
    patch('turn',first['id'],state='waiting_data_workflow',data_workflow_job=own['id'])
    patch('turn',second['id'],state='waiting_data_workflow',data_workflow_job=foreign['id'])
    stop(pid)
    assert get('turn',second['id'])['state']=='waiting_data_workflow'
    assert get_job(foreign['id'])['status']=='queued' and not get_job(foreign['id']).get('cancel_requested')
    assert get_job(own['id']).get('cancel_requested') is True


def test_stop_does_not_cancel_another_conversations_active_run_borrowed_by_this_turn():
    pid=project();src=source(pid);wid=registered(pid,document(src['id']))
    other_session=conversation.create_session(pid,conversation.SessionInput(request_id='origin_'+uid()))['id']
    owner=task(pid,other_session);borrower=task(pid)
    original=data.enqueue_workflow(pid,wid,originating_turn_id=owner['id'])
    borrowed=data.enqueue_workflow(pid,wid,originating_turn_id=borrower['id'])
    assert borrowed['id']==original['id'] and borrowed['originating_turn_id']==owner['id']
    patch('turn',owner['id'],state='waiting_data_workflow',data_workflow_job=original['id'])
    patch('turn',borrower['id'],state='waiting_data_workflow',data_workflow_job=borrowed['id'])
    stop(pid)
    assert get('turn',borrower['id'])['outcome']=='cancelled'
    assert get('turn',owner['id'])['state']=='waiting_data_workflow'
    assert get_job(original['id'])['status']=='queued' and not get_job(original['id']).get('cancel_requested')
