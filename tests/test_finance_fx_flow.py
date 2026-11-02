"""Actual uploaded sample files through the frozen, reviewed FX workflow.

These are synthetic inputs; they do not validate a customer's accounting policy
or a deployed exchange-rate feed.
"""
import copy
import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from agent_service.api import app
from agent_service.business_flow_data import source_table, table_workflow, validate_source, validate_table
from agent_service.business_flow_engine import Handler, run
from agent_service.config import TOKEN
from agent_service.execution import process_once
from agent_service.store import get, uid


def _column(name,kind='text',**extra):
    return {'name':name,'data_type':kind,'meaning':name,**extra}


def _plan(*,include_nexus_row):
    def table(columns):
        return {'meaning':'已授权上传的完整资料','grain':['c_1'],
                'columns':([_column('nexus_row')] if include_nexus_row else [])+columns}
    fx_rate={'data_type':'decimal','unit':'CNY/USD','aggregation':'non_additive'}
    return {'name':'按法人与日期匹配经确认的汇率','inputs':{
        'transactions':table([_column('c_1'),_column('c_2'),_column('c_3'),_column('c_4','date'),
                              _column('c_5','decimal',unit='USD',aggregation='additive')]),
        'rates':table([_column('c_1'),_column('c_2'),_column('c_3'),_column('c_4'),_column('c_5'),
                       _column('c_6','date'),_column('c_7','date'),_column('c_8',**fx_rate)])},
        'stages':[{'kind':'lookup_effective','id':'matched','description':'按法人、币种和业务日期关联汇率',
                   'input':'transactions','right':'rates',
                   'keys':[{'left':'c_2','right':'c_2'},{'left':'c_3','right':'c_3'}],
                   'business_date':'c_4','valid_from':'c_6','valid_to':'c_7','unmapped':'error',
                   'outputs':{'c_1':_column('rate_id'),'c_3':_column('rate_from'),
                              'c_4':_column('rate_to'),'c_5':_column('rate_type'),
                              'c_6':_column('rate_start','date'),'c_7':_column('rate_end','date'),
                              'c_8':_column('rate',**fx_rate)}},
                  {'kind':'fx_convert','id':'converted','description':'逐笔使用已匹配汇率换算',
                   'input':'matched','amount_column':'c_5','currency_column':'c_3',
                   'source_currency':'USD','target_currency':'CNY','rate_column':'rate',
                   'rate_source_column':'rate_from','rate_target_column':'rate_to',
                   'rate_type_column':'rate_type','rate_type':'closing','rounding_mode':'exact',
                   'output':_column('converted_amount','decimal',unit='CNY',aggregation='additive')}],
        'output':'converted'}


def test_uploaded_sources_match_effective_rates_and_keep_verified_lineage(monkeypatch):
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    def request(method,path,**kwargs):
        response=client.request(method,path,headers=headers,**kwargs)
        assert response.status_code==200,response.text
        return response.json()
    pid=request('POST','/api/projects',json={'name':'汇率版本核对','request_id':uid()})['id']
    root='/api/projects/'+pid
    def upload(name,content):
        source=request('POST',root+'/attachments',files={'file':(name,content.encode('utf-8'),'text/csv')},
                       data={'request_id':uid(),'local_processing':'true'})
        for _ in range(100):
            source=get('source',source['id'])
            if source.get('status') in ('ready','failed'):break
            assert process_once()
        assert source['status']=='ready'
        return source
    transactions=upload('transactions.csv',
        '交易,法人,币种,业务日期,原币金额\nt1,A,USD,2026-03-31,100.01\n'
        't2,A,USD,2026-04-01,-1.25\nt3,B,USD,2026-04-01,0.00\nt4,B,USD,2026-05-01,200.00\n')
    rate_rows=('r1,A,USD,CNY,closing,2026-01-01,2026-04-01,7.125000\n'
               'r2,A,USD,CNY,closing,2026-04-01,2027-01-01,7.250000\n'
               'r3,B,USD,CNY,closing,2026-01-01,2027-01-01,7.000000\n')
    rates=upload('rates.csv','编号,法人,原币,目标币,汇率类型,开始,结束,汇率\n'+rate_rows)
    turn=request('POST',root+'/messages',json={'text':'按已确认的法人和日期汇率进行换算',
        'request_id':uid(),'source_ids':[transactions['id'],rates['id']]})
    context={'project_id':pid,'turn_id':turn['id'],'actor_id':'owner'}
    def source_node(name,source):return {'id':name,'kind':'source_table','title':'读取资料',
        'config':{'source_id':source['id'],'revision':source['revision'],'content_hash':source['content_hash']}}
    def execute(rate_source):
        nodes=[source_node('transactions',transactions),source_node('rates',rate_source),
               {'id':'convert','kind':'table_workflow','title':'按实际规则换算',
                'depends_on':['transactions','rates'],
                'inputs':{'transactions':{'source':'node','name':'transactions'},'rates':{'source':'node','name':'rates'}},
                'config':{'plan':_plan(include_nexus_row=True)}}]
        handlers={'source_table':Handler(source_table,True,True,validate_source),
                  'table_workflow':Handler(table_workflow,True,True,validate_table)}
        return run({'name':'汇率换算测试','inputs':[],'nodes':nodes,'outputs':['convert']},
                   {},handlers,context,persist=lambda value:None)
    result=execute(rates)
    assert result['status']=='succeeded',[(s.get('node_id'),s.get('error_code')) for s in result['steps']]
    output=result['outputs']['convert'];rows=output['rows']
    assert [(r['c_1'],r['c_2'],r['c_3'],r['c_4'],r['c_5'],r['rate_id'],r['rate_from'],r['rate_to'],
             r['rate_type'],r['rate_start'],r['rate_end']) for r in rows]==[
        ('t1','A','USD','2026-03-31','100.01','r1','USD','CNY','closing','2026-01-01','2026-04-01'),
        ('t2','A','USD','2026-04-01','-1.25','r2','USD','CNY','closing','2026-04-01','2027-01-01'),
        ('t3','B','USD','2026-04-01','0.00','r3','USD','CNY','closing','2026-01-01','2027-01-01'),
        ('t4','B','USD','2026-05-01','200.00','r3','USD','CNY','closing','2026-01-01','2027-01-01')]
    assert [Decimal(r['converted_amount']) for r in rows]==[
        Decimal('712.57125'),Decimal('-9.0625'),Decimal('0'),Decimal('1400')]
    assert [Decimal(r['rate']) for r in rows]==[Decimal('7.125'),Decimal('7.25'),Decimal('7'),Decimal('7')]
    assert output['complete'] is True and output['requires_semantic_review'] is True
    assert output['selection_lineage_complete'] is True
    assert {(r['source_id'],r['revision'],r['content_hash']) for r in output['source_refs']}=={
        (s['id'],s['revision'],s['content_hash']) for s in (transactions,rates)}
    assert output['stage_receipts'][-1]['checks']['rounding_scope']=='per_transaction'

    # The independent reusable data-workflow path must also require a real,
    # version-bound business review. Merely running the calculation or
    # saying the result is adopted cannot stand in for a review receipt.
    from agent_service import project_data, workspace, business_reports
    from agent_service.jobs import get_job
    from agent_service.result_delivery import read_handoff
    doc={'version':1,'plan':_plan(include_nexus_row=False),
         'bindings':{'transactions':{'source_id':transactions['id']},'rates':{'source_id':rates['id']}}}
    workspace.write_file(pid,'workflows/fx-integration.json',json.dumps(doc,ensure_ascii=False))
    workflow=project_data.register(pid,'workflows/fx-integration.json')
    job=project_data.enqueue_workflow(pid,workflow['workflow_id'])
    for _ in range(100):
        job=get_job(job['id'])
        if job['status'] not in ('queued','running','retrying'):break
        assert process_once()
    assert job['status']=='validated',job.get('workflow_failure') or job.get('error_code')
    assert job['data_result']['requires_semantic_review'] is True
    with pytest.raises(ValueError,match='复核|工作台'):
        project_data.publish_result(pid,job['id'],job['commit'])
    with pytest.raises(ValueError,match='语义|人工复核'):
        project_data.read_input(pid,{'result_id':job['id']})
    body={'run_id':job['id'],'expected_plan_sha256':job['commit'],'confirm_semantic_review':True,
          'expected_handoff_sha256':job['data_result']['handoff_sha256']}
    approved=request('POST',root+'/data-results/'+job['id']+'/publish',json=body)
    assert approved['published_run']==job['id']
    reviewed=get_job(job['id']);receipt=reviewed['semantic_review_receipt']
    assert reviewed['status']=='succeeded' and receipt['handoff_sha256']==job['data_result']['handoff_sha256']
    assert business_reports.inspect_result(pid,job['id'])['server_adopted_result'] is True
    # Legacy data with both flags incorrectly cleared cannot use the absence
    # of a flag as evidence of a human review, including the FX plan path.
    folder,meta=project_data.result_folder(pid,job['id'])
    page=read_handoff(folder/'handoff',limit=1,expected_manifest_sha256=meta['handoff_sha256'])
    forged_meta=copy.deepcopy(meta);forged_page=copy.deepcopy(page)
    forged_meta['requires_semantic_review']=False
    forged_page['context']['requires_semantic_review']=False
    import agent_service.jobs as jobs
    real_get=jobs.get_job
    def forged_get(ident):
        value=real_get(ident)
        if ident==job['id']:
            value=copy.deepcopy(value);value['data_result']=forged_meta;value.pop('semantic_review_receipt',None)
        return value
    with monkeypatch.context() as patcher:
        patcher.setattr(jobs,'get_job',forged_get)
        with pytest.raises(ValueError,match='语义复核依据不完整'):
            project_data.require_safe_numeric_lineage(pid,job['id'],root_meta=forged_meta,root_page=forged_page)

    # A real file with a matched but wrong target currency must never yield a
    # success receipt or a downloadable fabricated amount.
    wrong=upload('wrong-rate.csv','编号,法人,原币,目标币,汇率类型,开始,结束,汇率\n'+rate_rows.replace('USD,CNY,closing','USD,EUR,closing',1))
    rejected=execute(wrong)
    assert rejected['status']=='failed' and rejected['steps'][-1]['error_code']=='FX_CURRENCY_MISMATCH'
    assert 'outputs' not in rejected
    missing=upload('missing-rate.csv','编号,法人,原币,目标币,汇率类型,开始,结束,汇率\n'+
                   ''.join(line+'\n' for line in rate_rows.splitlines() if not line.startswith('r3,')))
    rejected=execute(missing)
    assert rejected['status']=='failed' and rejected['steps'][-1]['error_code']=='UNMAPPED_VALUE'
    assert 'outputs' not in rejected
    overlap=upload('overlap-rate.csv','编号,法人,原币,目标币,汇率类型,开始,结束,汇率\n'+rate_rows+
                   'r4,A,USD,CNY,closing,2026-03-01,2026-05-01,7.300000\n')
    rejected=execute(overlap)
    assert rejected['status']=='failed' and rejected['steps'][-1]['error_code']=='OVERLAPPING_EFFECTIVE_INTERVALS'
    assert 'outputs' not in rejected
