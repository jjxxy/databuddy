"""Configured report orchestration; downloads are mocked, storage/queue are real."""
import hashlib
import json
import time
from pathlib import Path
import pytest
from sqlalchemy import select,update
from agent_service import project_reports as pr
from agent_service.config import DATA
from agent_service.store import get,put,patch,uid,unseal,engine,items
from agent_service.jobs import jobs,get_job,list_jobs
from agent_service.errors import TaskError
from agent_service.report_acquisition import validate_download,ReportError


@pytest.fixture(autouse=True)
def finish_owned_test_jobs():
    with engine.connect() as c:before=set(c.execute(select(jobs.c.id)).scalars())
    yield
    with engine.begin() as c:
        c.execute(update(jobs).where(jobs.c.id.not_in(before),jobs.c.status.in_(('queued','running','retrying'))).values(status='cancelled'))


def project():
    pid='p_'+uid();put('project',{'name':'报告获取编排'},pid);return pid


def connector(monkeypatch,pid,**overrides):
    monkeypatch.setattr('agent_service.connectors.safe_api_url',lambda u:'203.0.113.21')
    data={'name':'办事处收入报告','url':'https://private-reports.example/export?token=disposable-url-secret',
          'headers':{'Authorization':'Bearer disposable-auth-secret'},'format':'csv',
          'verification':{'expected_columns':['office','amount'],'filter_columns':{'office':'office'}},
          'default_context':{'unit':'CNY'}}
    data.update(overrides)
    return pr.configure(pid,data)


def running(job):
    token=uid()
    with engine.begin() as c:
        c.execute(update(jobs).where(jobs.c.id==job['id']).values(status='running',attempts=1,lease_token=token,lease_until=time.time()+60))
    value=get_job(job['id']);value['_deadline']=time.monotonic()+120
    folder=DATA/'runs'/value['id']/'attempt-1';folder.mkdir(parents=True,exist_ok=True)
    return value,folder


def fake_download(captured=None,body=b'office,amount\nShanghai,10.123456789012\n'):
    def download(request,destination,*,headers):
        if captured is not None:captured.append({'request':request,'headers':headers})
        destination.mkdir(parents=True,exist_ok=True)
        file=destination/('actual.'+request.format);file.write_bytes(body)
        return dict(validate_download(file,request),file=file.name,acquisition_id='a_'+request.request_id,
                    scope_id=request.scope_id,request_id=request.request_id)
    return download


def sources_for(rid):
    from agent_service.store import all_items
    return [s for s in all_items('source') if s.get('report_fetch_run')==rid]


def test_configuration_and_job_private_values_are_sealed(monkeypatch):
    pid=project();c=connector(monkeypatch,pid)
    public=pr.list_connectors(pid)
    assert set(public[0])=={'id','name','status','formats'}
    assert not any(secret in json.dumps(public) for secret in ('private-reports','disposable-url-secret','disposable-auth-secret','headers'))
    with engine.connect() as conn:
        stored=conn.execute(select(items.c.body).where(items.c.id==c['id'])).scalar()
    assert 'disposable' not in stored and 'private-reports' not in stored
    cfg=unseal(get('report_connector',c['id'])['secret_config'])
    assert cfg['headers']['Authorization']=='Bearer disposable-auth-secret'
    queued=pr.enqueue_fetch(pid,c['id'],{'office':'Shanghai'})
    row=get_job(queued['id'])
    assert 'disposable' not in json.dumps(row) and 'secret_report_snapshot' in row
    assert unseal(row['secret_report_snapshot'])['config']['headers']==cfg['headers']
    assert 'secret_report_snapshot' not in queued


def test_connector_scope_and_unapproved_query_changes_are_rejected(monkeypatch):
    pid=project();other=project();c=connector(monkeypatch,pid)
    assert pr.list_connectors(other)==[]
    with pytest.raises(ValueError,match='不属于'):pr.enqueue_fetch(other,c['id'],{'office':'Shanghai'})
    with pytest.raises(ValueError,match='未在此连接'):pr.enqueue_fetch(pid,c['id'],{'redirect_url':'http://metadata/'})
    with pytest.raises(ValueError,match='缺少'):pr.enqueue_fetch(pid,c['id'])
    with pytest.raises(ValueError,match='不能由获取请求修改'):pr.enqueue_fetch(pid,c['id'],{'office':'Shanghai'},{'unit':'USD'})


def test_active_duplicate_queue_same_parameters_reuses_but_new_filters_do_not(monkeypatch):
    pid=project();c=connector(monkeypatch,pid)
    first=pr.enqueue_fetch(pid,c['id'],{'office':'Shanghai'})
    assert pr.enqueue_fetch(pid,c['id'],{'office':'Shanghai'})['id']==first['id']
    with pytest.raises(ValueError,match='已有不同条件'):pr.enqueue_fetch(pid,c['id'],{'office':'Beijing'})
    assert len(list_jobs(target=c['id']))==1


def test_download_becomes_immutable_project_source_and_atomic_probe(monkeypatch):
    pid=project();c=connector(monkeypatch,pid);seen=[]
    queued=pr.enqueue_fetch(pid,c['id'],{'office':'Shanghai'},operation_id='server-turn-step')
    run,folder=running(queued)
    monkeypatch.setattr(pr,'acquire_report',fake_download(seen))
    pr.execute_job(run,folder)
    done=get_job(run['id']);assert done['status']=='succeeded'
    source=get('source',done['source_id']);cfg=unseal(source['secret_config'])
    assert source['project_id']==pid and source['report_fetch_run']==run['id'] and source['revision']==1
    assert (DATA/'uploads'/cfg['stored_name']).read_bytes()==b'office,amount\nShanghai,10.123456789012\n'
    assert cfg['report_receipt']['filters_verified'] and not cfg['report_receipt']['context_verified']
    assert seen[0]['request'].request_id==run['id']
    assert seen[0]['request'].scope_id.startswith('report_')
    assert seen[0]['headers']['Authorization']=='Bearer disposable-auth-secret'
    probes=[j for j in list_jobs(target=source['id']) if j['kind']=='probe']
    assert len(probes)==1 and probes[0]['status']=='queued'
    view=pr.read_fetch(pid,run['id'])
    assert view['receipt']['rows']==1 and view['receipt']['filters_verified']
    assert not any(secret in json.dumps(view) for secret in ('disposable','headers','secret_report','Shanghai'))
    assert 'disposable' not in (folder/'report-receipt.json').read_text()
    assert pr.enqueue_fetch(pid,c['id'],{'office':'Shanghai'},operation_id='server-turn-step')['id']==run['id']
    with pytest.raises(ValueError,match='不属于'):pr.read_fetch(project(),run['id'])


def test_crash_replay_does_not_duplicate_source_revision_or_probe(monkeypatch):
    pid=project();c=connector(monkeypatch,pid);queued=pr.enqueue_fetch(pid,c['id'],{'office':'Shanghai'})
    monkeypatch.setattr(pr,'acquire_report',fake_download())
    run,folder=running(queued);pr.execute_job(run,folder)
    source_id=get_job(run['id'])['source_id']
    again,folder=running(queued);pr.execute_job(again,folder)
    assert get_job(run['id'])['source_id']==source_id and len(sources_for(run['id']))==1
    assert len(list_jobs(target=source_id))==1
    source=get('source',source_id)
    assert get('source_version',source['revision_id'])['revision']==1


def test_queued_config_is_frozen_when_owner_changes_credentials(monkeypatch):
    pid=project();c=connector(monkeypatch,pid);queued=pr.enqueue_fetch(pid,c['id'],{'office':'Shanghai'})
    frozen_revision=get_job(queued['id'])['report_connector_revision']
    cfg=pr.ReportConnectorInput(name='更新后的报告',url='https://different.example/export',format='csv',
                               headers={'Authorization':'Bearer new-secret'},allowed_filter_keys=['office'])
    pr.configure(pid,cfg,connector_id=c['id'])
    seen=[];monkeypatch.setattr(pr,'acquire_report',fake_download(seen))
    run,folder=running(queued);pr.execute_job(run,folder)
    assert seen[0]['headers']['Authorization']=='Bearer disposable-auth-secret'
    assert seen[0]['request'].url.startswith('https://private-reports.example/')
    source=get('source',get_job(run['id'])['source_id'])
    assert source['report_connector_revision']==frozen_revision


def test_disabled_connector_does_not_start_queued_download(monkeypatch):
    pid=project();c=connector(monkeypatch,pid);queued=pr.enqueue_fetch(pid,c['id'],{'office':'Shanghai'})
    patch('report_connector',c['id'],enabled=False)
    def forbidden(*a,**k):raise AssertionError('must not download')
    monkeypatch.setattr(pr,'acquire_report',forbidden)
    run,folder=running(queued)
    with pytest.raises(TaskError,match='已停用'):pr.execute_job(run,folder)
    assert not sources_for(run['id'])


def test_lost_lease_after_download_cannot_create_source(monkeypatch):
    pid=project();c=connector(monkeypatch,pid);queued=pr.enqueue_fetch(pid,c['id'],{'office':'Shanghai'})
    real=fake_download()
    def lose(request,destination,*,headers):
        receipt=real(request,destination,headers=headers)
        with engine.begin() as conn:conn.execute(update(jobs).where(jobs.c.id==queued['id']).values(lease_token='new-worker'))
        return receipt
    monkeypatch.setattr(pr,'acquire_report',lose)
    run,folder=running(queued)
    with pytest.raises(TaskError,match='租约已失效'):pr.execute_job(run,folder)
    assert not sources_for(run['id'])


def test_final_registration_rechecks_lease_even_if_checkpoint_were_stale(monkeypatch):
    pid=project();c=connector(monkeypatch,pid);queued=pr.enqueue_fetch(pid,c['id'],{'office':'Shanghai'})
    monkeypatch.setattr(pr,'acquire_report',fake_download())
    original=pr._copy_verified
    def expire(*a,**kw):
        original(*a,**kw)
        with engine.begin() as conn:conn.execute(update(jobs).where(jobs.c.id==queued['id']).values(lease_until=time.time()-1))
    monkeypatch.setattr(pr,'_copy_verified',expire)
    monkeypatch.setattr('agent_service.execution.checkpoint',lambda r:None)
    run,folder=running(queued)
    with pytest.raises(RuntimeError,match='租约已失效'):pr.execute_job(run,folder)
    assert not sources_for(run['id'])


def test_private_download_errors_never_reach_public_job_error(monkeypatch):
    pid=project();c=connector(monkeypatch,pid);queued=pr.enqueue_fetch(pid,c['id'],{'office':'Shanghai'})
    def fail(*a,**kw):raise ReportError('url=https://private/token=super-secret authorization=more-secret','HTTPS://TOKEN-SECRET')
    monkeypatch.setattr(pr,'acquire_report',fail)
    run,folder=running(queued)
    with pytest.raises(TaskError) as value:pr.execute_job(run,folder)
    assert 'secret' not in str(value.value) and value.value.code=='REPORT_FAILED'
    assert not sources_for(run['id'])


def test_json_wrapper_and_xlsx_selection_preserved_for_probe(monkeypatch):
    pid=project();c=connector(monkeypatch,pid,format='json',verification={'json_rows_path':'rows','expected_columns':['office','amount']})
    queued=pr.enqueue_fetch(pid,c['id'])
    monkeypatch.setattr(pr,'acquire_report',fake_download(body=b'{"rows":[{"office":"Shanghai","amount":1}]}'))
    run,folder=running(queued);pr.execute_job(run,folder)
    source=get('source',get_job(run['id'])['source_id'])
    assert unseal(source['secret_config'])['records_path']=='rows'


def test_untrusted_receipt_path_and_changed_bytes_do_not_register(monkeypatch):
    pid=project();c=connector(monkeypatch,pid);queued=pr.enqueue_fetch(pid,c['id'],{'office':'Shanghai'})
    real=fake_download()
    def wrong(request,destination,*,headers):
        value=real(request,destination,headers=headers);value['file']='../../secrets';return value
    monkeypatch.setattr(pr,'acquire_report',wrong)
    run,folder=running(queued)
    with pytest.raises(TaskError,match='路径无效'):pr.execute_job(run,folder)
    assert not sources_for(run['id'])


def test_public_receipt_never_exposes_source_url_and_keeps_omission_explicit():
    receipt={'endpoint':{'path':'/bearer-secret'},'columns':['c_'+str(i) for i in range(150)],'rows':1,
             'filters_requested':{'office':'private-user-input'},'context_declared':{'unit':'private-unit'}}
    once=pr._public_receipt(receipt);twice=pr._public_receipt(once)
    assert len(twice['columns'])==100 and twice['column_count']==150 and not twice['columns_complete']
    assert 'bearer-secret' not in json.dumps(twice) and 'private-user-input' not in json.dumps(twice)
