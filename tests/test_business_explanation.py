"""A business explanation must be bound to the selected frozen artifact."""
import json
import hashlib

import pytest
from fastapi.testclient import TestClient

from agent_service import business_reports as reports, project_data, workspace_artifacts
from agent_service.api import app
from agent_service.config import DATA, TOKEN
from agent_service.execution import process_once
from agent_service.jobs import get_job
from agent_service.sources import revision
from agent_service.store import now, patch, put, seal, uid
from test_business_reports import real_result, recipe, schema


client = TestClient(app)
HEADERS = {'Authorization':'Bearer '+TOKEN}


def create_report():
    pid, rid = real_result()
    definition = recipe()
    definition['expected_schema_sha256'] = reports.inspect_result(pid, rid)['schema_sha256']
    report = reports.render(pid, rid, definition)
    artifact = next(item for item in workspace_artifacts.catalog(pid, kind='report')['items']
                    if item['name'] == definition['title'])
    return pid, rid, report, artifact


def url(pid, aid):
    return f'/api/projects/{pid}/artifacts/{aid}/explanation'


def test_real_report_uses_frozen_sources_steps_and_exact_report_values():
    pid, rid, report, artifact = create_report()
    response = client.get(url(pid,artifact['id']),headers=HEADERS)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value['report'] == {'id':report['id'],'title':'经营月报','period':'2026-09'}
    assert value['run_id'] == rid and value['integrity']['status'] == 'verified'
    assert value['sources'][0]['name'] == 'month.csv'
    assert value['sources'][0]['revision'] == 1 and value['sources'][0]['rows'] == 2
    assert value['sources'][0]['source_version_state'] == 'current'
    assert len(value['sources'][0]['content_hash']) == 64
    assert value['sources'][0]['selection'] is None
    assert value['steps'][0]['label'] == '选择字段'
    assert value['steps'][0]['input_rows'] == value['steps'][0]['output_rows'] == 2
    assert '本期净收入' in value['steps'][0]['description']
    assert value['metrics'][0]['value'] == '300.000002'  # Never a JSON float.
    assert value['metrics'][1]['value'] == '0.000002'
    assert '实际值 300.000002，比较基数 300' in value['metrics'][1]['description']
    assert '变化率保留12位' in value['metrics'][1]['description']
    assert '原始小数比值，1 相当于 100%' in value['metrics'][1]['description']
    assert '未按日期列逐行过滤' in value['metrics'][0]['description']
    assert any(check['label']=='用户声明的假设' and '预算使用已核准版本' in check['detail']
               and check['status']=='not_checked' for check in value['checks'])
    assert value['result_artifact_id']
    preview=client.get(f'/api/projects/{pid}/artifacts/{value["result_artifact_id"]}/preview',headers=HEADERS)
    assert preview.status_code==200 and preview.json()['total']==2
    download=client.get(f'/api/projects/{pid}/artifacts/{value["result_artifact_id"]}/download',
                        params={'format':'xlsx'},headers=HEADERS)
    assert download.status_code==200 and download.content[:2]==b'PK'
    with pytest.raises(ValueError,match='版本'):
        project_data.result_download(pid,rid,'xlsx',expected_handoff_sha256='0'*64)
    assert any(check['status'] == 'not_checked' and check['label'] == '数据验收规则' for check in value['checks'])
    assert any('不能证明本次逐项人工复核' in check['detail'] or '未记录逐次人工复核' in check['detail'] for check in value['checks'])
    assert any('不提供每个结果数字' in text for text in value['limitations'])
    assert 'secret_config' not in response.text and 'source_snapshot' not in response.text


def test_historical_source_name_and_revision_are_not_taken_from_current_draft():
    pid, rid, report, artifact = create_report()
    sid = next(iter(get_job(rid)['source_snapshot']))
    revision(patch('source',sid,name='renamed-current.csv'))
    response = client.get(url(pid,artifact['id']),headers=HEADERS)
    assert response.status_code == 200, response.text
    source = response.json()['sources'][0]
    assert source['name'] == 'month.csv' and source['revision'] == 1
    assert source['source_version_state']=='changed'
    assert 'renamed-current.csv' not in response.text
    route=f'/api/projects/{pid}/sources/{sid}/artifact'
    exact=client.get(route,params={'revision':1,'expected_sha256':source['content_hash']},headers=HEADERS)
    assert exact.status_code==200 and exact.json()['name']=='month.csv'
    wrong=client.get(route,params={'revision':1,'expected_sha256':'0'*64},headers=HEADERS)
    assert wrong.status_code==409
    assert client.get(route,params={'expected_sha256':source['content_hash']},headers=HEADERS).status_code==409
    from agent_service.store import all_items
    old=next(item for item in all_items('source_version') if item.get('source_id')==sid and item.get('revision')==1)
    patch('source_version',old['id'],content_hash='0'*64)
    assert client.get(route,params={'revision':1,'expected_sha256':source['content_hash']},headers=HEADERS).status_code==409


def test_cross_project_archived_and_tampered_evidence_never_return_verified():
    pid, rid, report, artifact = create_report()
    other = put('project',{'name':'other','created_at':now()},'p_'+uid())['id']
    assert client.get(url(other,artifact['id']),headers=HEADERS).status_code == 404
    assert client.get(url(pid,artifact['id'])).status_code == 401
    manifest = reports.report_file(pid,report['id'],'manifest')
    original = manifest.read_bytes()
    try:
        manifest.write_bytes(original+b' ')
        assert client.get(url(pid,artifact['id']),headers=HEADERS).status_code == 409
    finally:
        manifest.write_bytes(original)
    folder, _ = project_data.result_folder(pid,rid)
    context = folder/'handoff'/'context.json'
    original = context.read_bytes()
    try:
        context.write_bytes(original+b' ')
        assert client.get(url(pid,artifact['id']),headers=HEADERS).status_code == 409
    finally:
        context.write_bytes(original)
    from agent_service.store import get
    refs=get('workspace_artifact',artifact['id'])['source_refs']
    patch('workspace_artifact',artifact['id'],source_refs=[{'result_id':'other','handoff_sha256':refs[0]['handoff_sha256']}])
    assert client.get(url(pid,artifact['id']),headers=HEADERS).status_code==409
    patch('workspace_artifact',artifact['id'],source_refs=refs)
    checksum=get('business_report',report['id'])['manifest_sha256']
    patch('business_report',report['id'],manifest_sha256='0'*64)
    assert client.get(url(pid,artifact['id']),headers=HEADERS).status_code==409
    patch('business_report',report['id'],manifest_sha256=checksum)
    patch('workspace_artifact',artifact['id'],archived=True)
    assert client.get(url(pid,artifact['id']),headers=HEADERS).status_code == 410


def test_old_numeric_contracts_are_never_relabelled_as_precisely_verified():
    from agent_service.business_explanation import _contract_checks
    declared={'version':1,'ranges':{'amount':{'max':'9007199254740992'}},
              'totals':{'amount':{'expected':'0'}}}
    checksum=hashlib.sha256(json.dumps(declared,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    old={'passed':True,'checks':[{'name':'range:amount','status':'pass','failures':0},
                                 {'name':'total:amount','status':'pass','failures':0},
                                 {'name':'row_count','status':'pass','failures':0}],'contract_sha256':checksum,'rows':2}
    checks = _contract_checks(old,declared,expected_rows=2)
    assert [item['status'] for item in checks] == ['needs_review','needs_review','not_checked']
    checked=[dict(item,failures=0) for item in old['checks']]
    new = _contract_checks(dict(old,checks=checked,numeric_policy='exact_decimal_v1'),declared,expected_rows=2)
    assert [item['status'] for item in new] == ['passed','passed','not_checked']
    assert all('业务核对' in item['detail'] for item in new[:2])
    assert '空值需要另行配置非空检查' in new[0]['detail']
    assert '预期总额及容差' in new[1]['detail']
    incomplete=_contract_checks(dict(old,checks=checked[1:],numeric_policy='exact_decimal_v1'),declared,expected_rows=2)
    assert next(item for item in incomplete if item['label']=='数值范围：amount')['status']=='not_checked'
    assert next(item for item in incomplete if item['label']=='总额对账：amount')['status']=='needs_review'
    with_bounds=dict(declared,min_rows=2,max_rows=9)
    checksum=hashlib.sha256(json.dumps(with_bounds,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    bounded=_contract_checks(dict(old,checks=checked,numeric_policy='exact_decimal_v1',contract_sha256=checksum),with_bounds,expected_rows=2)
    assert bounded[-1]['status']=='passed' and '至少 2 行' in bounded[-1]['detail'] and '至多 9 行' in bounded[-1]['detail']
    wrong_rows=_contract_checks(dict(old,checks=checked,numeric_policy='exact_decimal_v1'),declared,expected_rows=3)
    assert all(item['status']=='needs_review' for item in wrong_rows)
    mismatched=_contract_checks(dict(old,checks=checked,numeric_policy='exact_decimal_v1'),declared,expected_rows=2,evidence_consistent=False)
    assert all(item['status']=='needs_review' for item in mismatched)


def test_frozen_report_value_is_not_replaced_by_a_current_plan():
    pid, rid, report, artifact = create_report()
    run = get_job(rid)
    document = run['workflow_document']
    path = 'workflows/month.json'
    from agent_service import workspace
    draft = json.loads(workspace.read_file(pid,path))
    draft['plan']['stages'][0]['description'] = '修改后的草稿不代表历史执行'
    workspace.write_file(pid,path,json.dumps(draft,ensure_ascii=False))
    response = client.get(url(pid,artifact['id']),headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()['metrics'][0]['value'] == '300.000002'
    assert '修改后的草稿' not in response.text
    assert run['workflow_document'] == document


def test_rounding_assumptions_apply_to_the_part_of_the_calculation_actually_rounded():
    pid,rid=real_result();definition=recipe()
    definition['expected_schema_sha256']=reports.inspect_result(pid,rid)['schema_sha256']
    definition['metrics'][0]['rounding_scale']=2
    report=reports.render(pid,rid,definition)
    artifact=next(item for item in workspace_artifacts.catalog(pid,kind='report')['items'] if item['name']==definition['title'])
    response=client.get(url(pid,artifact['id']),headers=HEADERS)
    assert response.status_code==200,response.text
    total,variance=response.json()['metrics']
    assert total['value']=='300.000002' and '未应用舍入参数' in total['description']
    assert variance['value']=='0.000002' and '变化率保留12位' in variance['description']
    assert '实际值 300.000002，比较基数 300' in variance['description']


@pytest.mark.parametrize('baseline,expected_delta,expected_rate',[
    ('0.10','5.00','0.50'), ('0.00','15.00','不适用（比较基数为0）')])
def test_report_explains_fraction_difference_and_zero_baseline_without_guessing(baseline,expected_delta,expected_rate):
    from agent_service import workspace
    pid=put('project',{'name':'百分比证据','created_at':now()},'p_'+uid())['id']
    raw=('office,actual,budget\nEast,0.15,'+baseline+'\n').encode()
    filename=uid()+'.csv';path=DATA/'uploads'/filename;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    source=revision(put('source',{'project_id':pid,'name':'ratio.csv','kind':'file',
        'secret_config':seal({'stored_name':filename}),'content_hash':hashlib.sha256(raw).hexdigest(),
        'bytes':len(raw),'status':'ready','created_at':now()},'s_'+uid()))
    declared=schema()
    declared['grain']=[]  # A single row is valid but has no declared business key.
    for column in declared['columns'][1:]:
        column.update(unit='1',value_format='percent_fraction',aggregation='non_additive')
    doc={'version':1,'plan':{'name':'比例核对','inputs':{'detail':declared},'stages':[
        {'id':'keep','kind':'select','description':'保留已声明结果','input':'detail','columns':['office','actual','budget']}],
        'output':'keep'},'bindings':{'detail':{'source_id':source['id'],'column_map':{'c_1':'office','c_2':'actual','c_3':'budget'}}}}
    workspace.write_file(pid,'workflows/ratio.json',json.dumps(doc,ensure_ascii=False))
    workflow=project_data.register(pid,'workflows/ratio.json')
    run=project_data.enqueue_workflow(pid,workflow['workflow_id']);assert process_once()
    definition=recipe(declared)
    definition['metrics']=[dict(definition['metrics'][1],reduction='value',rounding_scale=2,zero_policy='null')]
    definition['expected_schema_sha256']=reports.inspect_result(pid,run['id'])['schema_sha256']
    reports.render(pid,run['id'],definition)
    artifact=next(item for item in workspace_artifacts.catalog(pid,kind='report')['items'] if item['name']==definition['title'])
    response=client.get(url(pid,artifact['id']),headers=HEADERS)
    assert response.status_code==200,response.text
    explanation=response.json()
    assert next(item for item in explanation['steps'][0]['checks'] if item['label']=='声明的行粒度')['status']=='not_checked'
    metric=explanation['metrics'][0]
    assert metric['value']==expected_delta and metric['unit']=='百分点'
    assert '相减后乘以100' in metric['description'] and expected_rate in metric['description']
    if baseline=='0.00':assert '变化率保留' not in metric['description'] and '原始小数比值' not in metric['description']
    else:assert '原始小数比值，1 相当于 100%' in metric['description']
    definition['metrics'][0]['kind']='ratio'
    report=reports.render(pid,run['id'],definition)
    from agent_service.store import get
    artifact=next(item for item in workspace_artifacts.catalog(pid,kind='report')['items']
                  if get('workspace_artifact',item['id'])['private']['report_id']==report['id'])
    ratio=client.get(url(pid,artifact['id']),headers=HEADERS).json()['metrics'][0]
    assert '实际值除以比较基数' in ratio['description']
    if baseline=='0.00':
        assert ratio['value']=='不适用（比较基数为0）' and ratio['unit']==''
        assert '原始小数比值' not in ratio['description'] and '比率保留' not in ratio['description']
    else:
        assert ratio['value']=='1.50' and ratio['unit']=='1'
        assert '原始小数比值，1 相当于 100%' in ratio['description']


def test_result_preview_rejects_a_different_result_manifest(monkeypatch):
    pid,_,_,artifact=create_report()
    explanation=client.get(url(pid,artifact['id']),headers=HEADERS).json()
    result_id=explanation['result_artifact_id'];original=project_data.read_result
    monkeypatch.setattr(project_data,'read_result',lambda *a,**k:dict(original(*a,**k),manifest_sha256='0'*64))
    assert client.get(f'/api/projects/{pid}/artifacts/{result_id}/preview',headers=HEADERS).status_code==409


def test_missing_frozen_source_does_not_fall_back_to_current_version(monkeypatch):
    from agent_service import business_explanation
    pid,_,_,artifact=create_report();original=business_explanation.get_job
    monkeypatch.setattr(business_explanation,'get_job',lambda rid:dict(original(rid),source_snapshot={}))
    response=client.get(url(pid,artifact['id']),headers=HEADERS)
    assert response.status_code==200,response.text
    source=response.json()['sources'][0]
    assert source['availability']=='unknown' and source['source_id'] is None and source['content_hash'] is None
    assert source['name']=='来源名称未记录'
    assert source['source_version_state']=='unknown'


@pytest.mark.parametrize('reason',['missing','other_project','same_revision_changed_hash','archived'])
def test_current_version_state_is_unknown_when_current_record_cannot_be_trusted(monkeypatch,reason):
    from agent_service import business_explanation
    pid,_,_,artifact=create_report();original=business_explanation.get
    def replacement(kind,ident):
        value=original(kind,ident)
        if kind=='source':
            if reason=='missing':raise KeyError(ident)
            if reason=='other_project':return dict(value,project_id='different-project')
            if reason=='same_revision_changed_hash':return dict(value,content_hash='0'*64)
            if reason=='archived':return dict(value,archived=True)
        return value
    monkeypatch.setattr(business_explanation,'get',replacement)
    response=client.get(url(pid,artifact['id']),headers=HEADERS)
    assert response.status_code==200,response.text
    assert response.json()['sources'][0]['source_version_state']=='unknown'


def test_non_report_does_not_fake_a_narrative():
    pid,_,_,artifact=create_report()
    result_id=client.get(url(pid,artifact['id']),headers=HEADERS).json()['result_artifact_id']
    response=client.get(url(pid,result_id),headers=HEADERS)
    assert response.status_code==409 and '暂未记录可验证的业务依据' in response.text


def test_actual_multisheet_workbook_shows_only_frozen_selected_sheet_and_range():
    from openpyxl import Workbook
    from agent_service import workspace
    pid=put('project',{'name':'多页签证据','created_at':now()},'p_'+uid())['id']
    filename=uid()+'.xlsx';path=DATA/'uploads'/filename;path.parent.mkdir(parents=True,exist_ok=True)
    book=Workbook();other=book.active;other.title='其他期间';other.append(['其他数据']);other.append(['不得作为本次来源'])
    sheet=book.create_sheet('本期明细');sheet.append(['本期说明']);sheet.append(['办事处','实际收入','预算'])
    sheet.append(['East','100.000001','100']);sheet.append(['West','200.000001','200'])
    book.save(path);book.close();raw=path.read_bytes()
    source=revision(put('source',{'project_id':pid,'name':'经营底稿.xlsx','kind':'file',
        'secret_config':seal({'stored_name':filename}),'content_hash':hashlib.sha256(raw).hexdigest(),
        'bytes':len(raw),'status':'ready','created_at':now()},'s_'+uid()))
    doc={'version':1,'plan':{'name':'本期明细','inputs':{'detail':schema()},'stages':[
        {'id':'keep','kind':'select','description':'保留本期所需字段','input':'detail','columns':['office','actual','budget']}],
        'output':'keep'},'bindings':{'detail':{'source_id':source['id'],
            'options':{'sheet':'本期明细','cell_range':'A2:C4','header_rows':[2],'data_start_row':3,'data_end_row':4},
            'column_map':{'c_1':'office','c_2':'actual','c_3':'budget'}}}}
    workspace.write_file(pid,'workflows/xlsx.json',json.dumps(doc,ensure_ascii=False))
    workflow=project_data.register(pid,'workflows/xlsx.json')
    run=project_data.enqueue_workflow(pid,workflow['workflow_id']);assert process_once()
    definition=recipe();definition['expected_schema_sha256']=reports.inspect_result(pid,run['id'])['schema_sha256']
    reports.render(pid,run['id'],definition)
    artifact=next(item for item in workspace_artifacts.catalog(pid,kind='report')['items'] if item['name']==definition['title'])
    response=client.get(url(pid,artifact['id']),headers=HEADERS)
    assert response.status_code==200,response.text
    value=response.json();selection=value['sources'][0]['selection']
    assert selection=={'sheet':'本期明细','range':[1,2,3,4],'header_rows':[2]}
    assert value['sources'][0]['content_hash']==hashlib.sha256(raw).hexdigest()
    assert value['sources'][0]['rows']==2 and value['metrics'][0]['value']=='300.000002'
    assert any('如果 Excel 含公式' in text and '未重新计算' in text for text in value['limitations'])
    assert '其他期间' not in response.text


def test_explanation_matches_real_filter_calculation_and_grouped_result():
    from agent_service import workspace
    pid,old_run=real_result()
    doc=get_job(old_run)['workflow_document']
    delta={'name':'delta','data_type':'decimal','meaning':'本期实际与预算差额',
           'unit':'CNY','period':'2026-09','aggregation':'additive'}
    total=dict(delta,name='delta_total',meaning='办事处差额合计')
    doc['plan']['stages']=[
        {'id':'scope','kind':'filter','description':'按收入过滤','input':'detail',
         'conditions':[{'column':'actual','operator':'gte','value':'150'}]},
        {'id':'difference','kind':'compute','description':'计算实际与预算差额','input':'scope',
         'calculations':[{'left':'actual','right':'budget','operation':'subtract','output':delta}]},
        {'id':'summary','kind':'aggregate','description':'按办事处汇总','input':'difference','by':['office'],
         'meaning':'办事处差额汇总','aggregations':[{'function':'sum','column':'delta','output':total}]}]
    doc['plan']['output']='summary'
    workspace.write_file(pid,'workflows/multistep.json',json.dumps(doc,ensure_ascii=False))
    workflow=project_data.register(pid,'workflows/multistep.json')
    run=project_data.enqueue_workflow(pid,workflow['workflow_id']);assert process_once()
    definition={'version':1,'title':'差额核对','period':'2026-09',
                'expected_schema_sha256':reports.inspect_result(pid,run['id'])['schema_sha256'],
                'metrics':[{'id':'delta','label':'差额合计','kind':'sum','column':'delta_total'}]}
    reports.render(pid,run['id'],definition)
    artifact=next(item for item in workspace_artifacts.catalog(pid,kind='report')['items'] if item['name']==definition['title'])
    response=client.get(url(pid,artifact['id']),headers=HEADERS)
    assert response.status_code==200,response.text
    result=response.json();assert [step['label'] for step in result['steps']]==['筛选数据','计算指标','汇总数据']
    assert (result['steps'][0]['input_rows'],result['steps'][0]['output_rows'])==(2,1)
    assert result['steps'][0]['inputs'][0]['name']=='month.csv'
    assert result['steps'][1]['inputs'][0]['name']=='第1步结果'
    assert result['steps'][2]['inputs'][0]['name']=='第2步结果'
    assert '本期净收入 大于或等于 "150"' in result['steps'][0]['description']
    assert '本期实际与预算差额 = 本期净收入 减 本期净收入预算' in result['steps'][1]['description']
    assert '按办事处汇总' in result['steps'][2]['description']
    assert result['metrics'][0]['value']=='0.000001'


def test_report_period_explains_actual_period_column_filter():
    from agent_service import workspace
    pid=put('project',{'name':'期间筛选证据','created_at':now()},'p_'+uid())['id']
    raw=b'office,actual,budget,month\nEast,100,90,2026-09\nWest,200,180,2026-08\n'
    filename=uid()+'.csv';path=DATA/'uploads'/filename;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    source=revision(put('source',{'project_id':pid,'name':'periods.csv','kind':'file',
        'secret_config':seal({'stored_name':filename}),'content_hash':hashlib.sha256(raw).hexdigest(),
        'bytes':len(raw),'status':'ready','created_at':now()},'s_'+uid()))
    declared=schema()
    for column in declared['columns'][1:]:column['period']=''
    declared['columns'].append({'name':'month','data_type':'text','meaning':'会计期间'})
    declared['grain']=['office','month']
    doc={'version':1,'plan':{'name':'期间明细','inputs':{'detail':declared},'stages':[
        {'id':'keep','kind':'select','description':'保留期间字段','input':'detail',
         'columns':['office','actual','budget','month']}], 'output':'keep'},
         'bindings':{'detail':{'source_id':source['id'],
          'column_map':{'c_1':'office','c_2':'actual','c_3':'budget','c_4':'month'}}}}
    workspace.write_file(pid,'workflows/period.json',json.dumps(doc,ensure_ascii=False))
    workflow=project_data.register(pid,'workflows/period.json')
    run=project_data.enqueue_workflow(pid,workflow['workflow_id']);assert process_once()
    definition=recipe(declared);definition['period_column']='month'
    definition['expected_schema_sha256']=reports.inspect_result(pid,run['id'])['schema_sha256']
    reports.render(pid,run['id'],definition)
    artifact=next(item for item in workspace_artifacts.catalog(pid,kind='report')['items'] if item['name']==definition['title'])
    response=client.get(url(pid,artifact['id']),headers=HEADERS)
    assert response.status_code==200,response.text
    metric=response.json()['metrics'][0]
    assert metric['rows']==1 and metric['value']=='100'
    assert '按期间字段 会计期间 等于 2026-09 筛选' in metric['description']


@pytest.mark.parametrize('historic',[False,True])
def test_explanation_distinguishes_real_exact_contract_from_a_frozen_historic_receipt(monkeypatch,historic):
    from agent_service import workspace, data_contracts
    pid,previous=real_result()
    doc=get_job(previous)['workflow_document']
    doc['contract']={'version':1,'ranges':{'actual':{'max':'1000'}},
                     'totals':{'actual':{'expected':'300.000002'}}}
    workspace.write_file(pid,'workflows/contract.json',json.dumps(doc,ensure_ascii=False))
    workflow=project_data.register(pid,'workflows/contract.json')
    with monkeypatch.context() as patcher:
        if historic:
            current=data_contracts.evaluate
            def previous_receipt(connection,contract):
                receipt=current(connection,contract)
                receipt.pop('numeric_policy',None)
                return receipt
            patcher.setattr(data_contracts,'evaluate',previous_receipt)
        run=project_data.enqueue_workflow(pid,workflow['workflow_id']);assert process_once()
        if historic:
            # Reproduce the earlier report creation before the new reuse gate;
            # the historical report and receipt remain untouched by the API.
            patcher.setattr(data_contracts,'require_exact_numeric_evidence',lambda *args,**kwargs:None)
        definition=recipe();definition['expected_schema_sha256']=reports.inspect_result(pid,run['id'])['schema_sha256']
        report=reports.render(pid,run['id'],definition)
    from agent_service.store import get
    artifact=next(item for item in workspace_artifacts.catalog(pid,kind='report')['items']
                  if get('workspace_artifact',item['id'])['private']['report_id']==report['id'])
    response=client.get(url(pid,artifact['id']),headers=HEADERS)
    assert response.status_code==200,response.text
    checks=response.json()['checks']
    range_check=next(item for item in checks if item['label']=='数值范围：actual')
    total_check=next(item for item in checks if item['label']=='总额对账：actual')
    assert range_check['status']==total_check['status']==('needs_review' if historic else 'passed')
    if historic:
        assert '重新运行' in range_check['detail'] and '重新运行' in total_check['detail']
    else:
        assert '空值需要另行配置非空检查' in range_check['detail']
        assert '300.000002' in total_check['detail']


@pytest.mark.parametrize('downstream',[False,True])
def test_saved_historic_report_remains_readable_but_unsafe_sql_ancestry_needs_review(monkeypatch,downstream):
    from agent_service import workspace, data_workflows
    from agent_service.store import get
    pid,previous=real_result()
    doc=get_job(previous)['workflow_document']
    doc['plan']['stages']=[{'id':'keep','kind':'sql','description':'历史计算',
        'inputs':['detail'],'sql':'SELECT office, CAST(actual / 1 AS DECIMAL(38,12)) AS actual, budget FROM detail',
        'output_schema':schema()}]
    doc['plan']['output']='keep'
    workspace.write_file(pid,'workflows/old-sql.json',json.dumps(doc,ensure_ascii=False))
    workflow=project_data.register(pid,'workflows/old-sql.json')
    with monkeypatch.context() as patcher:
        # Simulate execution, adoption and report generation under the earlier
        # rules, then restore current safety checks before opening the report.
        patcher.setattr(data_workflows,'_validate_exact_sql_numeric',lambda tree:None)
        patcher.setattr(project_data,'require_safe_numeric_lineage',lambda *args,**kwargs:None)
        old=project_data.enqueue_workflow(pid,workflow['workflow_id']);assert process_once()
        project_data.publish_result(pid,old['id'],get_job(old['id'])['commit'])
        selected=old['id']
        if downstream:
            newer={'version':1,'plan':{'name':'上游结果再加工','inputs':{'detail':schema()},'stages':[
                {'id':'keep','kind':'select','description':'保留字段','input':'detail','columns':['office','actual','budget']}],
                'output':'keep'},'bindings':{'detail':{'result_id':old['id']}}}
            workspace.write_file(pid,'workflows/downstream.json',json.dumps(newer,ensure_ascii=False))
            descendant=project_data.register(pid,'workflows/downstream.json')
            child=project_data.enqueue_workflow(pid,descendant['workflow_id']);assert process_once()
            project_data.publish_result(pid,child['id'],get_job(child['id'])['commit'])
            selected=child['id']
        definition=recipe();definition['expected_schema_sha256']=reports.inspect_result(pid,selected)['schema_sha256']
        report=reports.render(pid,selected,definition)
    artifact=next(item for item in workspace_artifacts.catalog(pid,kind='report')['items']
                  if get('workspace_artifact',item['id'])['private']['report_id']==report['id'])
    response=client.get(url(pid,artifact['id']),headers=HEADERS)
    assert response.status_code==200,response.text
    warning=next(item for item in response.json()['checks'] if item['label']=='历史数值计算')
    assert warning['status']=='needs_review' and '可能丢失精度' in warning['detail']
    assert 'SELECT' not in response.text and 'CAST(' not in response.text
    if downstream:
        original=project_data.result_folder
        def unavailable(project,run_id,**kwargs):
            if run_id==old['id']:raise KeyError(run_id)
            return original(project,run_id,**kwargs)
        with monkeypatch.context() as patcher:
            patcher.setattr(project_data,'result_folder',unavailable)
            missing=client.get(url(pid,artifact['id']),headers=HEADERS)
        assert missing.status_code==200,missing.text
        warning=next(item for item in missing.json()['checks'] if item['label']=='历史数值计算')
        assert warning['status']=='needs_review' and '无法完整核对' in warning['detail']


def test_actual_historic_float_rounding_is_visible_and_not_certified(monkeypatch):
    from test_numeric_lineage_history import unsafe_sql_history
    from agent_service.store import get
    pid,_,_,_,report=unsafe_sql_history(monkeypatch)
    artifact=next(item for item in workspace_artifacts.catalog(pid,kind='report')['items']
                  if get('workspace_artifact',item['id'])['private']['report_id']==report['id'])
    response=client.get(url(pid,artifact['id']),headers=HEADERS)
    assert response.status_code==200,response.text
    explanation=response.json()
    assert explanation['metrics'][0]['value']=='9007199254740992.000000000000'
    warning=next(item for item in explanation['checks'] if item['label']=='历史数值计算')
    assert warning['status']=='needs_review' and '可能丢失精度' in warning['detail']
    assert 'SELECT' not in response.text and 'CAST(' not in response.text
