"""Human Stop must cancel owned async jobs, including admission/link races.

Independent regressions for the audited owner-cancellation gap. No model,
connector request or workflow processing occurs here; storage/queue are real.
"""
import threading
import time
import pytest
from sqlalchemy import select,delete as sql_delete

from agent_service import conversation,project_data as data,project_reports as reports
from agent_service.config import DATA
from agent_service.errors import Cancelled
from agent_service.jobs import jobs,get_job,list_jobs,save
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


def claimed(run):
    # Lease only this fixture's job, never consume another test's queued work.
    return save(dict(get_job(run['id']),status='running',attempts=1,
                     lease_token=uid(),lease_until=time.time()+60))


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
    pid=project();configured=connector(monkeypatch,pid,allowed_filter_keys=['office']);value=task(pid)
    run=reports.enqueue_fetch(pid,configured['id'],{'office':'East'},operation_id='audit_'+uid())
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


@pytest.mark.parametrize('link',[True,False])
def test_report_origin_covers_stop_after_admission_before_link(monkeypatch,link):
    pid=project();configured=connector(monkeypatch,pid,allowed_filter_keys=['office']);value=task(pid)
    run=reports.enqueue_fetch(pid,configured['id'],{'office':'East'},
        operation_id='audit_'+uid(),originating_turn_id=value['id'])
    assert get_job(run['id'])['originating_turn_id']==value['id']
    if link:patch('turn',value['id'],state='waiting_report_fetch',report_fetch_job=run['id'])
    stop(pid)
    assert get_job(run['id'])['status']=='cancelled' and get_job(run['id'])['cancel_requested']


def test_late_report_admission_after_stop_is_rejected(monkeypatch):
    pid=project();configured=connector(monkeypatch,pid,allowed_filter_keys=['office']);value=task(pid)
    stop(pid)
    with pytest.raises((Cancelled,ValueError)):
        reports.enqueue_fetch(pid,configured['id'],{'office':'East'},
            operation_id='audit_'+uid(),originating_turn_id=value['id'])
    assert not list_jobs(target=configured['id'])


def test_worker_rechecks_owner_cancelled_after_claim_before_read(monkeypatch):
    from agent_service import execution
    pid=project();src=source(pid);wid=registered(pid,document(src['id']));value=task(pid)
    run=data.enqueue_workflow(pid,wid,originating_turn_id=value['id'])
    leased=claimed(run)
    # Simulate a worker which already holds its lease while the owner Stop wins.
    stop(pid)
    calls=[];monkeypatch.setattr(data,'read_input',lambda *args,**kwargs:calls.append(True))
    execution.execute(leased)
    assert not calls and get_job(run['id'])['status']=='cancelled'
    assert get('data_workflow',wid).get('candidate_run')!=run['id']


def test_worker_checkpoint_cancellation_does_not_depend_on_turn_job_link(monkeypatch):
    from agent_service import execution
    pid=project();src=source(pid);wid=registered(pid,document(src['id']));value=task(pid)
    run=data.enqueue_workflow(pid,wid,originating_turn_id=value['id'])
    leased=claimed(run)
    patch('turn',value['id'],state='done',outcome='cancelled')
    calls=[];monkeypatch.setattr(data,'read_input',lambda *args,**kwargs:calls.append(True))
    execution.execute(leased)
    assert not calls and get_job(run['id'])['status']=='cancelled'


def test_running_workflow_cancel_during_source_read_cannot_create_candidate(monkeypatch):
    from agent_service import execution
    pid=project();src=source(pid);wid=registered(pid,document(src['id']));value=task(pid)
    run=data.enqueue_workflow(pid,wid,originating_turn_id=value['id'])
    leased=claimed(run)
    original=data.read_input
    def cancel_after_read(*args,**kwargs):
        result=original(*args,**kwargs)
        stop(pid)
        return result
    monkeypatch.setattr(data,'read_input',cancel_after_read)
    execution.execute(leased)
    assert get_job(run['id'])['status']=='cancelled'
    assert get('data_workflow',wid).get('candidate_run')!=run['id']


def test_real_enqueue_wins_stop_waits_for_origin_admission_then_cancels(monkeypatch):
    from agent_service import jobs as queue
    pid=project();src=source(pid);wid=registered(pid,document(src['id']));value=task(pid)
    admitted=threading.Event();release=threading.Event();stop_started=threading.Event();stop_done=threading.Event()
    results=[];errors=[];original=queue._enqueue
    def paused(*args,**kwargs):
        admitted.set();assert release.wait(5)
        return original(*args,**kwargs)
    def enqueue():
        try:results.append(data.enqueue_workflow(pid,wid,originating_turn_id=value['id']))
        except Exception as error:errors.append(error)
    def cancel():
        stop_started.set()
        try:stop(pid)
        except Exception as error:errors.append(error)
        finally:stop_done.set()
    monkeypatch.setattr(queue,'_enqueue',paused)
    producer=threading.Thread(target=enqueue);canceller=threading.Thread(target=cancel)
    producer.start()
    try:
        assert admitted.wait(5)
        canceller.start();assert stop_started.wait(5)
        assert not stop_done.wait(.1),'Stop must serialize with an already admitted owner job'
    finally:
        release.set();producer.join(6)
        if canceller.ident is not None:canceller.join(6)
    assert not producer.is_alive() and not canceller.is_alive() and not errors
    assert len(results)==1 and get_job(results[0]['id'])['status']=='cancelled'


def test_real_stop_wins_during_preparation_and_late_enqueue_is_rejected(monkeypatch):
    from agent_service import sources
    pid=project();src=source(pid);wid=registered(pid,document(src['id']));value=task(pid)
    preparing=threading.Event();release=threading.Event();errors=[];original=sources.snapshot
    def paused(ids):
        preparing.set();assert release.wait(5)
        return original(ids)
    def enqueue():
        try:data.enqueue_workflow(pid,wid,originating_turn_id=value['id'])
        except Exception as error:errors.append(error)
    monkeypatch.setattr(sources,'snapshot',paused)
    producer=threading.Thread(target=enqueue);producer.start()
    try:
        assert preparing.wait(5);stop(pid)
        assert get('turn',value['id'])['outcome']=='cancelled'
    finally:release.set();producer.join(6)
    assert not producer.is_alive() and len(errors)==1 and isinstance(errors[0],Cancelled)
    assert not list_jobs(target=wid)


def test_report_cancellation_before_final_registration_cannot_create_source(monkeypatch):
    from agent_service import execution
    from agent_service.store import all_items
    from test_project_reports import fake_download
    pid=project();configured=connector(monkeypatch,pid,allowed_filter_keys=['office']);value=task(pid)
    run=reports.enqueue_fetch(pid,configured['id'],{'office':'East'},
        operation_id='audit_'+uid(),originating_turn_id=value['id'])
    leased=claimed(run)
    monkeypatch.setattr(reports,'acquire_report',fake_download(body=b'office,amount\nEast,10.000000000001\n'))
    original=reports._register_and_complete
    def cancel_at_commit(*args,**kwargs):
        stop(pid)
        return original(*args,**kwargs)
    monkeypatch.setattr(reports,'_register_and_complete',cancel_at_commit)
    execution.execute(leased)
    assert get_job(run['id'])['status']=='cancelled'
    assert not any(item.get('report_fetch_run')==run['id'] for item in all_items('source'))


def test_workflow_tool_link_cannot_resurrect_turn_stopped_just_after_enqueue(monkeypatch):
    from agent_service import agent
    pid=project();src=source(pid);wid=registered(pid,document(src['id']));value=task(pid)
    patch('turn',value['id'],text='请整理全部订单并运行加工方案')
    original=data.enqueue_workflow;admitted=[]
    def enqueue_then_stop(*args,**kwargs):
        result=original(*args,**kwargs);admitted.append(result['id'])
        stop(pid)
        return result
    monkeypatch.setattr(data,'enqueue_workflow',enqueue_then_stop)
    agent._execute_node({'turn_id':value['id'],'step':{'kind':'tool','tool':'run_data_workflow',
        'arguments':{'workflow_id':wid}}})
    assert len(admitted)==1 and get_job(admitted[0])['status']=='cancelled'
    assert get('turn',value['id'])['state']=='done' and get('turn',value['id'])['outcome']=='cancelled'


def test_report_tool_link_cannot_resurrect_turn_stopped_just_after_enqueue(monkeypatch):
    from agent_service import agent
    pid=project();configured=connector(monkeypatch,pid,allowed_filter_keys=['office']);value=task(pid)
    patch('turn',value['id'],text='请连接报告并刷新数据，获取配置的报告')
    original=reports.enqueue_fetch;admitted=[]
    def enqueue_then_stop(*args,**kwargs):
        result=original(*args,**kwargs);admitted.append(result['id'])
        stop(pid)
        return result
    monkeypatch.setattr(reports,'enqueue_fetch',enqueue_then_stop)
    agent._execute_node({'turn_id':value['id'],'step':{'kind':'tool','tool':'acquire_report_data',
        'arguments':{'connector_id':configured['id'],'filters':{'office':'East'},'context':{}}}})
    assert len(admitted)==1 and get_job(admitted[0])['status']=='cancelled'
    assert get('turn',value['id'])['state']=='done' and get('turn',value['id'])['outcome']=='cancelled'
