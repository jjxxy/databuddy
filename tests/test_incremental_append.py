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


def test_upsert_replaces_entire_matching_row_adds_new_and_retains_unmentioned():
    pid=project()
    base,bs=adopted(pid,'id,amount\nA,9007199254740.000001\nB,2.25\n','workflows/base-upsert.json')
    delta,ds=adopted(pid,'id,amount\nA,1.000001\nC,3.50\n','workflows/delta-upsert.json')
    body={'baseline_run_id':base,'baseline_sha256':bs,'delta_run_id':delta,'delta_sha256':ds,
          'request_id':'upsert-request-1234567','mode':'upsert','confirm_append_only':False,
          'confirm_replace':True,'note':'已核对变更清单及全字段替换'}
    client=TestClient(app);path=f'/api/projects/{pid}/data-append-candidates';headers={'Authorization':'Bearer '+TOKEN}
    denied=client.post(path,json=dict(body,confirm_replace=False),headers=headers)
    assert denied.status_code==400
    response=client.post(path,json=body,headers=headers);assert response.status_code==200,response.text
    value=response.json();assert (value['added_rows'],value['replaced_rows'],value['total_rows'])==(1,1,3)
    assert '缺席的历史行保留' in value['notice']
    retry=client.post(path,json=body,headers=headers);assert retry.status_code==200 and retry.json()['run_id']==value['run_id']
    changed=client.post(path,json=dict(body,mode='append',confirm_append_only=True,confirm_replace=False),headers=headers)
    assert changed.status_code==400
    for _ in range(100):
        job=get_job(value['run_id'])
        if job['status'] not in ('queued','running','retrying'):break
        assert process_once()
    assert job['status']=='validated',job
    result=data.read_result(pid,job['id'])
    from decimal import Decimal
    assert {r['id']:Decimal(r['amount']) for r in result['rows']}=={'A':Decimal('1.000001'),'B':Decimal('2.25'),'C':Decimal('3.50')}
    assert not data.owned(pid,'data_workflow',value['workflow_id']).get('published_run')
    assert data.result_folder(pid,job['id'])[1]['requires_semantic_review'] is True


def test_upsert_rejects_duplicate_delta_grain_and_ambiguous_ack(monkeypatch):
    pid=project();base,bs=adopted(pid,'id,amount\nA,1\n','workflows/base-dupe.json')
    # A duplicate within either complete adopted input is still rejected.
    delta,ds=adopted(pid,'id,amount\nB,2\n','workflows/delta-dupe.json')
    from agent_service import incremental_append as module
    original=module._verified
    def bad_delta(pid_,rid,checksum):
        run,meta,page,schema=original(pid_,rid,checksum)
        if rid==delta:page={**page,'rows':page['rows']*2}
        return run,meta,page,schema
    monkeypatch.setattr(module,'_verified',bad_delta)
    body={'baseline_run_id':base,'baseline_sha256':bs,'delta_run_id':delta,'delta_sha256':ds,
          'request_id':'upsert-dupe-12345678','mode':'upsert','confirm_append_only':False,
          'confirm_replace':True,'note':'已核对变更和替换'}
    client=TestClient(app);path=f'/api/projects/{pid}/data-append-candidates';headers={'Authorization':'Bearer '+TOKEN}
    result=client.post(path,json=body,headers=headers);assert result.status_code==400 and '粒度重复' in result.text
    result=client.post(path,json=dict(body,confirm_append_only=True),headers=headers);assert result.status_code==400


def test_upsert_uses_all_composite_business_keys():
    pid=project()
    schema={'columns':[column('entity'),column('id'),column('amount','decimal','CNY','additive')],
            'grain':['entity','id'],'meaning':'分实体订单明细'}
    def full(content,path):
        s=source(pid,content.encode())
        doc={'version':1,'plan':{'name':'订单明细','inputs':{'detail':schema},'stages':[
            {'id':'out','kind':'select','description':'读取完整明细','input':'detail','columns':['entity','id','amount']}],
            'output':'out'},'bindings':{'detail':{'source_id':s['id'],'column_map':{'c_1':'entity','c_2':'id','c_3':'amount'}}}}
        wid=registered(pid,doc,path=path);job=run(pid,wid);data.publish_result(pid,job['id'],job['commit'])
        return job['id'],data.result_folder(pid,job['id'])[1]['handoff_sha256']
    base,bs=full('entity,id,amount\nE1,A,10\nE2,A,20\n','workflows/base-composite.json')
    delta,ds=full('entity,id,amount\nE1,A,11\n','workflows/delta-composite.json')
    body={'baseline_run_id':base,'baseline_sha256':bs,'delta_run_id':delta,'delta_sha256':ds,
          'request_id':'upsert-composite-123456','mode':'upsert','confirm_append_only':False,
          'confirm_replace':True,'note':'已核对实体及订单组合键'}
    result=TestClient(app).post(f'/api/projects/{pid}/data-append-candidates',json=body,headers={'Authorization':'Bearer '+TOKEN})
    assert result.status_code==200,result.text
    value=result.json();assert (value['added_rows'],value['replaced_rows'],value['total_rows'])==(0,1,2)
    for _ in range(100):
        job=get_job(value['run_id'])
        if job['status'] not in ('queued','running','retrying'):break
        assert process_once()
    assert job['status']=='validated',job
    from decimal import Decimal
    rows=data.read_result(pid,job['id'])['rows']
    assert {(r['entity'],r['id']):Decimal(r['amount']) for r in rows}=={('E1','A'):Decimal('11'),('E2','A'):Decimal('20')}


@pytest.mark.parametrize('kind,old,new,expected_replaced',[('decimal','1.0','1.00',1),('integer','1','01',1),('text','A','a',0)])
def test_upsert_preview_counts_match_real_sql_for_key_types(kind,old,new,expected_replaced):
    pid=project();schema={'columns':[column('id',kind,'1' if kind!='text' else ''),column('amount','decimal','CNY','additive')],
                          'grain':['id'],'meaning':'按明确业务键核对'}
    def full(key,amount,path):
        src=source(pid,f'id,amount\n{key},{amount}\n'.encode())
        doc={'version':1,'plan':{'name':'键核对','inputs':{'detail':schema},'stages':[
            {'id':'out','kind':'select','description':'读取完整明细','input':'detail','columns':['id','amount']}],
            'output':'out'},'bindings':{'detail':{'source_id':src['id'],'column_map':{'c_1':'id','c_2':'amount'}}}}
        wid=registered(pid,doc,path=path);job=run(pid,wid);data.publish_result(pid,job['id'],job['commit'])
        return job['id'],data.result_folder(pid,job['id'])[1]['handoff_sha256']
    base,bs=full(old,'10','workflows/key-base.json');delta,ds=full(new,'11','workflows/key-delta.json')
    body={'baseline_run_id':base,'baseline_sha256':bs,'delta_run_id':delta,'delta_sha256':ds,
          'request_id':'upsert-keytypes-123456','mode':'upsert','confirm_append_only':False,
          'confirm_replace':True,'note':'已核对业务键及变更'}
    response=TestClient(app).post(f'/api/projects/{pid}/data-append-candidates',json=body,headers={'Authorization':'Bearer '+TOKEN})
    assert response.status_code==200,response.text
    value=response.json();assert value['replaced_rows']==expected_replaced
    for _ in range(100):
        job=get_job(value['run_id'])
        if job['status'] not in ('queued','running','retrying'):break
        assert process_once()
    assert job['status']=='validated',job
    result=data.read_result(pid,job['id']);assert result['total']==value['total_rows']==2-expected_replaced
    from decimal import Decimal
    assert {Decimal(row['amount']) for row in result['rows']}==({Decimal('11')} if expected_replaced else {Decimal('10'),Decimal('11')})


def test_delete_explicit_matching_rows_preserves_others_and_remains_candidate():
    pid=project();base,bs=adopted(pid,'id,amount\nA,10.00\nB,2.25\nC,3.00\n','workflows/base-delete.json')
    deletion,ds=adopted(pid,'id,amount\nA,10.0\nC,3\n','workflows/delete-list.json')
    body={'baseline_run_id':base,'baseline_sha256':bs,'delta_run_id':deletion,'delta_sha256':ds,
          'request_id':'delete-request-1234567','mode':'delete','confirm_append_only':False,
          'confirm_replace':False,'confirm_delete':True,'note':'已逐项核对需删除的历史明细'}
    client=TestClient(app);path=f'/api/projects/{pid}/data-append-candidates';headers={'Authorization':'Bearer '+TOKEN}
    assert client.post(path,json={**body,'confirm_delete':False},headers=headers).status_code==400
    response=client.post(path,json=body,headers=headers);assert response.status_code==200,response.text
    value=response.json();assert (value['deleted_rows'],value['total_rows'],value['added_rows'],value['replaced_rows'])==(2,1,0,0)
    assert '未列出的记录保留' in value['notice']
    again=client.post(path,json=body,headers=headers);assert again.status_code==200 and again.json()['run_id']==value['run_id']
    for _ in range(100):
        job=get_job(value['run_id'])
        if job['status'] not in ('queued','running','retrying'):break
        assert process_once()
    assert job['status']=='validated',job
    rows=data.read_result(pid,job['id'])['rows'];assert [r['id'] for r in rows]==['B']
    assert not data.owned(pid,'data_workflow',value['workflow_id']).get('published_run')
    assert data.result_folder(pid,job['id'])[1]['requires_semantic_review'] is True


def test_delete_refuses_nonexistent_or_changed_rows_and_mixed_confirmation():
    pid=project();base,bs=adopted(pid,'id,amount\nA,10\nB,20\n','workflows/base-delete-bad.json')
    unknown,us=adopted(pid,'id,amount\nC,10\n','workflows/delete-unknown.json')
    changed,cs=adopted(pid,'id,amount\nA,11\n','workflows/delete-changed.json')
    body={'baseline_run_id':base,'baseline_sha256':bs,'delta_run_id':unknown,'delta_sha256':us,
          'request_id':'delete-invalid-1234567','mode':'delete','confirm_append_only':False,
          'confirm_replace':False,'confirm_delete':True,'note':'已核对待删除明细'}
    client=TestClient(app);path=f'/api/projects/{pid}/data-append-candidates';headers={'Authorization':'Bearer '+TOKEN}
    response=client.post(path,json=body,headers=headers);assert response.status_code==400 and '不存在的业务键' in response.text
    response=client.post(path,json={**body,'delta_run_id':changed,'delta_sha256':cs},headers=headers);assert response.status_code==400 and '原始明细' in response.text
    assert client.post(path,json={**body,'confirm_replace':True},headers=headers).status_code==400
    assert client.post(path,json={**body,'mode':'upsert','confirm_replace':True},headers=headers).status_code==400


def test_delete_all_records_creates_empty_candidate_without_automatically_adopting():
    pid=project();base,bs=adopted(pid,'id,amount\nA,10\n','workflows/delete-all-base.json')
    deletion,ds=adopted(pid,'id,amount\nA,10\n','workflows/delete-all-list.json')
    body={'baseline_run_id':base,'baseline_sha256':bs,'delta_run_id':deletion,'delta_sha256':ds,
          'request_id':'delete-all-123456789','mode':'delete','confirm_append_only':False,
          'confirm_replace':False,'confirm_delete':True,'note':'已核对全部删除明细'}
    response=TestClient(app).post(f'/api/projects/{pid}/data-append-candidates',json=body,headers={'Authorization':'Bearer '+TOKEN})
    assert response.status_code==200,response.text
    value=response.json();assert value['deleted_rows']==1 and value['total_rows']==0
    assert '候选结果为空，请特别核对' in value['notice']
    for _ in range(100):
        job=get_job(value['run_id'])
        if job['status'] not in ('queued','running','retrying'):break
        assert process_once()
    assert job['status']=='validated',job
    assert data.read_result(pid,job['id'])['rows']==[]
    assert not data.owned(pid,'data_workflow',value['workflow_id']).get('published_run')


def test_delete_refuses_one_trillionth_amount_change_and_same_id_different_mode():
    pid=project();base,bs=adopted(pid,'id,amount\nA,10.000000000001\nB,20\n','workflows/delete-precision-base.json')
    bad,ds=adopted(pid,'id,amount\nA,10.000000000000\n','workflows/delete-precision-list.json')
    good,gs=adopted(pid,'id,amount\nA,10.000000000001\n','workflows/delete-precision-match.json')
    body={'baseline_run_id':base,'baseline_sha256':bs,'delta_run_id':bad,'delta_sha256':ds,
          'request_id':'delete-precision-1234567','mode':'delete','confirm_append_only':False,
          'confirm_replace':False,'confirm_delete':True,'note':'已核对原值和删除清单'}
    client=TestClient(app);path=f'/api/projects/{pid}/data-append-candidates';headers={'Authorization':'Bearer '+TOKEN}
    response=client.post(path,json=body,headers=headers);assert response.status_code==400 and '原始明细' in response.text
    match={**body,'delta_run_id':good,'delta_sha256':gs}
    response=client.post(path,json=match,headers=headers);assert response.status_code==200,response.text
    alternate={**match,'mode':'upsert','confirm_replace':True,'confirm_delete':False}
    response=client.post(path,json=alternate,headers=headers);assert response.status_code==400 and '请求编号' in response.text


def test_delete_composite_keys_normalize_decimal_and_keep_other_entities():
    pid=project();schema={'columns':[column('entity'),column('id','decimal','1'),column('amount','decimal','CNY','additive')],
                          'grain':['entity','id'],'meaning':'分实体记录'}
    def full(content,path):
        src=source(pid,content.encode());doc={'version':1,'plan':{'name':'键核对','inputs':{'detail':schema},'stages':[
            {'id':'out','kind':'select','description':'读取完整明细','input':'detail','columns':['entity','id','amount']}],
            'output':'out'},'bindings':{'detail':{'source_id':src['id'],'column_map':{'c_1':'entity','c_2':'id','c_3':'amount'}}}}
        wid=registered(pid,doc,path=path);job=run(pid,wid);data.publish_result(pid,job['id'],job['commit'])
        return job['id'],data.result_folder(pid,job['id'])[1]['handoff_sha256']
    base,bs=full('entity,id,amount\nE1,1.00,10\nE2,1,20\n','workflows/delete-composite-base.json')
    deletion,ds=full('entity,id,amount\nE1,1.0,10\n','workflows/delete-composite-list.json')
    body={'baseline_run_id':base,'baseline_sha256':bs,'delta_run_id':deletion,'delta_sha256':ds,
          'request_id':'delete-composite-123456','mode':'delete','confirm_append_only':False,
          'confirm_replace':False,'confirm_delete':True,'note':'已核对实体和订单组合键'}
    response=TestClient(app).post(f'/api/projects/{pid}/data-append-candidates',json=body,headers={'Authorization':'Bearer '+TOKEN})
    assert response.status_code==200,response.text
    value=response.json();assert value['deleted_rows']==1 and value['total_rows']==1
    for _ in range(100):
        job=get_job(value['run_id'])
        if job['status'] not in ('queued','running','retrying'):break
        assert process_once()
    assert job['status']=='validated',job
    assert [r['entity'] for r in data.read_result(pid,job['id'])['rows']]==['E2']


def test_delete_null_and_empty_text_are_distinct_in_entire_row_comparison(monkeypatch):
    from agent_service import incremental_append as module
    pid=project();base,bs=adopted(pid,'id,amount\nA,10\n','workflows/delete-null-base.json')
    deletion,ds=adopted(pid,'id,amount\nA,10\n','workflows/delete-null-list.json')
    original=module._verified
    def mismatch(pid_,rid,checksum):
        run,meta,page,schema=original(pid_,rid,checksum)
        # Preserve real adopted result metadata while exercising nullable text
        # with values that CSV input alone cannot distinguish as null.
        schema.columns.append(type(schema.columns[0]).model_validate(column('note')))
        page={**page,'rows':[dict(r,note=None if rid==base else '') for r in page['rows']]}
        return run,meta,page,schema
    monkeypatch.setattr(module,'_verified',mismatch)
    body={'baseline_run_id':base,'baseline_sha256':bs,'delta_run_id':deletion,'delta_sha256':ds,
          'request_id':'delete-null-123456789','mode':'delete','confirm_append_only':False,
          'confirm_replace':False,'confirm_delete':True,'note':'已核对空值与空串'}
    response=TestClient(app).post(f'/api/projects/{pid}/data-append-candidates',json=body,headers={'Authorization':'Bearer '+TOKEN})
    assert response.status_code==400 and '原始明细' in response.text


def test_uncertain_retry_after_new_formal_version_requires_manual_candidate_check():
    from agent_service import incremental_append as module
    pid=project();base,bs=adopted(pid,'id,amount\nA,10\nB,20\n','workflows/delete-version-base.json')
    deletion,ds=adopted(pid,'id,amount\nA,10\n','workflows/delete-version-list.json')
    body={'baseline_run_id':base,'baseline_sha256':bs,'delta_run_id':deletion,'delta_sha256':ds,
          'request_id':'delete-version-12345678','mode':'delete','confirm_append_only':False,
          'confirm_replace':False,'confirm_delete':True,'note':'已核对删除清单'}
    first=module.create(pid,'owner',body)
    old=get_job(base);new=run(pid,old['target'],expected_draft_sha256=old['commit'])
    data.publish_result(pid,new['id'],new['commit'])
    with pytest.raises(ValueError,match='不是当前正式版本'):
        module.create(pid,'owner',body)
    still=get_job(first['run_id']);assert still['project_id']==pid
    assert still['workflow_document']['bindings']['history']['result_id']==base
