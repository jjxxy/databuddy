"""Append-only reuse never infers a CDC feed or automatically adopts output."""
import pytest
from fastapi.testclient import TestClient
from agent_service import project_data as data
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.execution import process_once
from agent_service.jobs import get_job
from test_project_data import project, source, registered, run, column


def adopted(pid,content,path,unit='CNY'):
    s=source(pid,content.encode())
    schema={'columns':[column('id'),column('amount','decimal',unit,'additive')],
            'grain':['id'],'meaning':'订单明细'}
    doc={'version':1,'plan':{'name':'正式订单','inputs':{'detail':schema},'stages':[
        {'id':'out','kind':'select','description':'读取明细','input':'detail','columns':['id','amount']}],'output':'out'},
        'bindings':{'detail':{'source_id':s['id'],'column_map':{'c_1':'id','c_2':'amount'}}}}
    wid=registered(pid,doc,path=path);job=run(pid,wid);data.publish_result(pid,job['id'],job['commit'])
    return job['id'],data.result_folder(pid,job['id'])[1]['handoff_sha256']


def case(duplicate=False):
    pid=project();base,bs=adopted(pid,'id,amount\nA,9007199254740.000001\n','workflows/base.json')
    rows='id,amount\nA,1.000001\n' if duplicate else 'id,amount\nB,1.000001\n'
    delta,ds=adopted(pid,rows,'workflows/delta.json')
    body={'baseline_run_id':base,'baseline_sha256':bs,'delta_run_id':delta,'delta_sha256':ds,
          'request_id':'append-request-1234567','confirm_append_only':True,'note':'按已核对的新增明细追加'}
    return pid,body


def test_real_append_candidate_is_exact_versioned_and_idempotent():
    pid,body=case();client=TestClient(app);path=f'/api/projects/{pid}/data-append-candidates';headers={'Authorization':'Bearer '+TOKEN}
    assert client.post(path,json=body).status_code==401
    first=client.post(path,json=body,headers=headers);assert first.status_code==200,first.text
    value=first.json();assert value['total_rows']==2 and '不是 CDC' in value['notice']
    second=client.post(path,json=body,headers=headers);assert second.status_code==200,second.text
    assert second.json()['run_id']==value['run_id'] and second.json()['plan_sha256']==value['plan_sha256']
    third=client.post(path,json=dict(body,request_id='another-request-123456'),headers=headers)
    assert third.status_code==200 and third.json()['run_id']==value['run_id']
    changed=dict(body,note='其他不同的核对依据')
    assert client.post(path,json=changed,headers=headers).status_code==400
    for _ in range(100):
        job=get_job(value['run_id'])
        if job['status'] not in ('queued','running','retrying'):break
        assert process_once()
    assert job['status']=='validated',job
    result=data.read_result(pid,job['id']);assert result['total']==2
    assert [x['amount'] for x in result['rows']]==['9007199254740.000001','1.000001']
    assert not data.owned(pid,'data_workflow',value['workflow_id']).get('published_run')
    assert client.post(path,json=dict(body,baseline_sha256='0'*64),headers=headers).status_code==400


def test_append_rejects_duplicate_business_keys_and_unconfirmed_input():
    pid,body=case(duplicate=True);client=TestClient(app);path=f'/api/projects/{pid}/data-append-candidates';headers={'Authorization':'Bearer '+TOKEN}
    response=client.post(path,json=body,headers=headers);assert response.status_code==400 and '粒度重复' in response.text
    assert client.post(path,json=dict(body,confirm_append_only=False),headers=headers).status_code==400
    other=project();assert client.post(f'/api/projects/{other}/data-append-candidates',json=body,headers=headers).status_code==400


def test_concurrent_retries_share_the_same_job_and_frozen_inputs():
    from concurrent.futures import ThreadPoolExecutor
    pid,body=case();client=TestClient(app);path=f'/api/projects/{pid}/data-append-candidates';headers={'Authorization':'Bearer '+TOKEN}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(client.post,path,json=body,headers=headers) for _ in range(2)]
        responses=[f.result(timeout=20) for f in futures]
    assert all(r.status_code==200 for r in responses),[r.text for r in responses]
    assert responses[0].json()['run_id']==responses[1].json()['run_id']
    assert responses[0].json()['baseline_sha256']==body['baseline_sha256']
    assert responses[0].json()['delta_sha256']==body['delta_sha256']


def test_mismatched_currency_and_same_request_mutation_are_rejected():
    pid,body=case();different,checksum=adopted(pid,'id,amount\nC,2.00\n','workflows/usd.json',unit='USD')
    client=TestClient(app);path=f'/api/projects/{pid}/data-append-candidates';headers={'Authorization':'Bearer '+TOKEN}
    response=client.post(path,json=dict(body,delta_run_id=different,delta_sha256=checksum),headers=headers)
    assert response.status_code==400 and '单位' in response.text
    accepted=client.post(path,json=body,headers=headers);assert accepted.status_code==200,accepted.text
    conflict=dict(body,baseline_run_id=body['delta_run_id'],baseline_sha256=body['delta_sha256'],
                  delta_run_id=body['baseline_run_id'],delta_sha256=body['baseline_sha256'])
    response=client.post(path,json=conflict,headers=headers)
    assert response.status_code==400 and '请求编号' in response.text


def test_concurrent_new_adoption_never_substitutes_the_selected_snapshot(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from agent_service import incremental_append as module
    pid,body=case();old_job=get_job(body['baseline_run_id']);new_job=run(pid,old_job['target'],expected_draft_sha256=old_job['commit'])
    entered=threading.Event();release=threading.Event();original=data._enqueue_workflow
    def paused(*args,**kwargs):
        entered.set();assert release.wait(10);return original(*args,**kwargs)
    monkeypatch.setattr(data,'_enqueue_workflow',paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future=pool.submit(module.create,pid,'owner',body)
        if not entered.wait(10):future.result(timeout=1);pytest.fail('did not reach the admission barrier')
        try:
            data.publish_result(pid,new_job['id'],new_job['commit'])
        finally:
            release.set()
        result=future.result(timeout=20)
    frozen=get_job(result['run_id'])['workflow_document']['bindings']
    assert frozen['history']['result_id']==body['baseline_run_id'] and frozen['history']['result_id']!=new_job['id']
    assert result['baseline_sha256']==body['baseline_sha256']
    assert '不会自动切换版本' in result['notice']
