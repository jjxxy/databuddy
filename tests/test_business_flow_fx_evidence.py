"""Full run-bound FX detail for an aggregated result, not invented row lineage."""
import copy
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.execution import process_once
from agent_service.jobs import get_job
from agent_service.store import get, uid, unseal
from test_finance_fx_flow import _column, _plan


def test_fx_detail_is_immutable_paged_and_bound_to_the_real_aggregate(tmp_path,monkeypatch):
    from agent_service import provider
    from agent_service.actor_context import actor_scope
    from agent_service.business_flow_data import _table_folder, get_table_page
    from agent_service.business_flows import get_run_fx_evidence, _fx_evidence, _fx_evidence_plan
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'','model':'','share_data':True})
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    def request(method,path,**kwargs):
        response=client.request(method,path,headers=headers,**kwargs)
        assert response.status_code==200,response.text
        return response.json()
    pid=request('POST','/api/projects',json={'name':'汇率明细复核','request_id':uid()})['id'];base='/api/projects/'+pid
    def upload(name,body):
        item=request('POST',base+'/attachments',files={'file':(name,body.encode(),'text/csv')},
                     data={'request_id':uid(),'local_processing':'true'})
        for _ in range(100):
            source=get('source',item['id'])
            if source.get('status') in ('ready','failed'):break
            assert process_once()
        assert source['status']=='ready'
        return source
    entries=''.join(f't{i},{"A" if i<130 else "B" if i<230 else "C"},USD,2026-03-31,1.00\n' for i in range(260))
    transactions=upload('transactions.csv','交易,法人,币种,业务日期,原币金额\n'+entries)
    rates=upload('rates.csv','编号,法人,原币,目标币,汇率类型,开始,结束,汇率\nr1,A,USD,CNY,closing,2026-01-01,2026-04-01,7.125\nr2,B,USD,CNY,closing,2026-01-01,2026-04-01,7.125\nr3,C,USD,CNY,closing,2026-01-01,2026-04-01,7.125\n')
    turn=request('POST',base+'/messages',json={'text':'核对汇率和收入','request_id':uid(),
                                               'source_ids':[transactions['id'],rates['id']]})
    plan=copy.deepcopy(_plan(include_nexus_row=True))
    plan['stages'].append({'kind':'filter','id':'chosen','description':'排除 C','input':'converted',
                           'conditions':[{'column':'c_2','operator':'neq','value':'C'}]})
    plan['stages'].append({'kind':'aggregate','id':'total','description':'按法人汇总换算金额',
        'input':'chosen','by':['c_2'],'meaning':'各法人换算金额',
        'aggregations':[{'function':'sum','column':'converted_amount',
                         'output':_column('amount','decimal',unit='CNY',aggregation='additive')}]})
    plan['output']='total'
    def source_node(name,value):return {'id':name,'kind':'source_table','title':'读取资料',
        'config':{'source_id':value['id'],'revision':value['revision'],'content_hash':value['content_hash']}}
    nodes=[source_node('transactions',transactions),source_node('rates',rates),
           {'id':'process','kind':'table_workflow','title':'换算后汇总',
            'depends_on':['transactions','rates'],
            'inputs':{'transactions':{'source':'node','name':'transactions'},'rates':{'source':'node','name':'rates'}},
            'config':{'plan':plan}}]
    doc={'name':'汇率核对流程','inputs':[],'nodes':nodes,'outputs':['process']}
    flow=request('POST',base+'/business-flows',json={'request_id':uid(),'turn_id':turn['id'],'document':doc})
    path=base+'/business-flows/'+flow['id'];job=request('POST',path+'/runs',
         json={'request_id':uid(),'expected_digest':flow['candidate_digest'],'inputs':{}})
    for _ in range(100):
        state=get_job(job['run_id'])
        if state['status'] not in ('queued','running','retrying'):break
        assert process_once()
    status=request('GET',path+'/runs/'+job['run_id']);assert status['status']=='succeeded',status
    item=next(step for step in status['steps'] if step['node_id']=='process')
    assert item['evidence_stages']==[{'stage_id':'converted','row_count':260}]
    assert item['contribution_available'] is True
    total=request('GET',path+'/runs/'+job['run_id']+'/tables/process')
    assert Decimal(total['rows'][0]['amount'])==Decimal('926.25')
    contribution_url=path+'/runs/'+job['run_id']+'/tables/process/contributions'
    contrib=request('GET',contribution_url+'?row_index=0&offset=100&limit=100')
    assert contrib['total']==130 and len(contrib['rows'])==30 and contrib['row_index']==0
    assert contrib['group']=={'c_2':'A'} and contrib['aggregations'][0]['function']=='sum'
    assert contrib['output_sha256']==total['output_sha256'] and not contrib['financial_verified']
    assert sum(Decimal(row['converted_amount']) for row in request('GET',contribution_url+'?row_index=0&limit=100')['rows'])==Decimal('712.5')
    group_b=request('GET',contribution_url+'?row_index=1&limit=100')
    assert group_b['group']=={'c_2':'B'} and group_b['total']==100 and len(group_b['rows'])==100
    assert all(row['c_2']=='B' for row in group_b['rows'])
    assert client.get(contribution_url+'?row_index=2',headers=headers).status_code==400
    assert client.get(contribution_url+'?row_index=0&limit=101',headers=headers).status_code==400
    detail_url=path+'/runs/'+job['run_id']+'/tables/process/evidence/converted'
    first=request('GET',detail_url+'?offset=0&limit=100');third=request('GET',detail_url+'?offset=200&limit=100')
    assert first['total']==260 and len(first['rows'])==100 and len(third['rows'])==60
    assert first['rows'][0]['rate_id']=='r1' and Decimal(first['rows'][0]['converted_amount'])==Decimal('7.125')
    assert first['financial_verified'] is False and first['final_output_sha256']==total['output_sha256']
    assert '不是对某一汇总行' in first['notice']
    assert client.get(detail_url+'?limit=101',headers=headers).status_code==400
    assert client.get(detail_url.replace('/converted','/matched'),headers=headers).status_code==400
    foreign=request('POST','/api/projects',json={'name':'无权项目','request_id':uid()})['id']
    assert client.get(detail_url.replace('/'+pid+'/','/'+foreign+'/'),headers=headers).status_code==400
    with actor_scope('u_'+'f'*64):
        with pytest.raises(ValueError):get_run_fx_evidence(pid,flow['id'],job['run_id'],'process','converted')
    state=unseal(get('run',job['run_id'])['secret_flow_checkpoint']);output=state['steps'][-1]['output']
    evidence=output['evidence_tables'][0]
    raw_job=get_job(job['run_id'])
    tampered=copy.deepcopy(output)
    tampered['workflow_output_stage']='converted'
    with pytest.raises(ValueError):_fx_evidence_plan(raw_job,'process',tampered)
    tampered=copy.deepcopy(output)
    tampered['stage_receipts'][-1]['inputs'][0]['table']='rates'
    with pytest.raises(ValueError):_fx_evidence_plan(raw_job,'process',tampered)
    tampered=copy.deepcopy(output)
    tampered['stage_receipts'][-1]['inputs'][0]['sha256']='0'*64
    with pytest.raises(ValueError):_fx_evidence(tampered,'converted')
    from agent_service.business_flows import get_run_contributions, _aggregate_contribution
    with actor_scope('u_'+'f'*64):
        with pytest.raises(ValueError):get_run_contributions(pid,flow['id'],job['run_id'],'process',0)
    tampered=copy.deepcopy(output);tampered['contribution_evidence']['output_sha256']='0'*64
    with pytest.raises(ValueError):_aggregate_contribution(raw_job,'process',tampered)
    tampered=copy.deepcopy(output);tampered['stage_receipts'][-1]['inputs'][0]['sha256']='0'*64
    with pytest.raises(ValueError):_aggregate_contribution(raw_job,'process',tampered)
    context={'project_id':pid,'actor_id':'owner','turn_id':turn['id'],'run_id':job['run_id']}
    with pytest.raises(ValueError,match='运行'):
        get_table_page(dict(context,run_id='different-run'),{**evidence,'kind':'table_workflow_v1'},0,1)
    folder,_=_table_folder(context);snapshot=folder/(evidence['rows_ref']['sha256']+'.enc')
    original=snapshot.read_bytes()
    contribution_snapshot=folder/(output['contribution_evidence']['rows_ref']['sha256']+'.enc')
    contribution_original=contribution_snapshot.read_bytes()
    review=status['review']
    review_body={'expected_checkpoint_sha256':review['checkpoint_sha256'],
                 'items':[{'node_id':item['node_id'],'kinds':item['kinds'],'note':'已核对资料和换算业务口径'} for item in review['items']]}
    review_url=path+'/runs/'+job['run_id']+'/review'
    publish_body={'run_id':job['run_id'],'expected_digest':flow['candidate_digest']}
    try:
        snapshot.write_bytes(original[:-1]+bytes([original[-1]^1]))
        assert client.get(detail_url,headers=headers).status_code==400
        response=client.post(review_url,headers=headers,json=review_body)
        assert response.status_code==400 and '快照' in response.text
        snapshot.unlink()
        response=client.post(path+'/publish',headers=headers,json=publish_body)
        assert response.status_code==400 and '快照' in response.text
    finally:snapshot.write_bytes(original)
    try:
        contribution_snapshot.write_bytes(contribution_original[:-1]+bytes([contribution_original[-1]^1]))
        assert client.get(contribution_url+'?row_index=0',headers=headers).status_code==400
        assert client.post(review_url,headers=headers,json=review_body).status_code==400
        assert client.post(path+'/publish',headers=headers,json=publish_body).status_code==400
    finally:contribution_snapshot.write_bytes(contribution_original)
    assert request('GET',detail_url+'?limit=1')['rows'][0]['rate_id']=='r1'
    if review['required']:
        assert request('POST',review_url,json=review_body)['review']['approved']
    try:
        snapshot.unlink()
        response=client.post(path+'/publish',headers=headers,json=publish_body)
        assert response.status_code==400 and '快照' in response.text
    finally:snapshot.write_bytes(original)
    assert request('POST',path+'/publish',json=publish_body)['published_run']==job['run_id']
    # A snapshot quota failure must fail the run rather than publish truncated rows.
    from agent_service import business_flow_data
    monkeypatch.setattr(business_flow_data,'_MAX_RUN_BYTES',1)
    limited=request('POST',path+'/runs',json={'request_id':uid(),'expected_digest':flow['candidate_digest'],'inputs':{}})
    for _ in range(100):
        limited_job=get_job(limited['run_id'])
        if limited_job['status'] not in ('queued','running','retrying'):break
        assert process_once()
    assert limited_job['status']=='failed'
    assert client.get(path+'/runs/'+limited['run_id']+'/tables/process/evidence/converted',headers=headers).status_code==400
