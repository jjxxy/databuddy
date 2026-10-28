"""Financial quality gates must not label rounded float checks as exact."""
import hashlib
import json
import shutil
import time

import duckdb
import pytest

from agent_service import business_reports, project_data
from agent_service.data_contracts import evaluate, evaluate_verified_parquet, require_exact_numeric_evidence, validate
from agent_service.config import DATA
from agent_service.jobs import save
from agent_service.result_delivery import _json, _pack, _unpack
from agent_service.store import uid


def numeric_table(kind, values):
    connection = duckdb.connect()
    connection.execute('CREATE SCHEMA business')
    connection.execute('CREATE TABLE business.business_objects(amount ' + kind + ')')
    for value in values:
        connection.execute('INSERT INTO business.business_objects VALUES (?)', [value])
    return connection


@pytest.mark.parametrize('amount,bounds,expected', [
    ('9007199254740993', {'max': '9007199254740992'}, False),
    ('9007199254740993', {'max': '9007199254740993'}, True),
    ('-9007199254740993', {'min': '-9007199254740992'}, False),
    ('-9007199254740993', {'min': '-9007199254740993'}, True),
    ('0.100000000001', {'max': '0.100000000000'}, False),
    ('-0.000000000001', {'min': '0'}, False),
    ('99999999999999999999999999.999999999999',
     {'max': '99999999999999999999999999.999999999999'}, True),
    ('1.250000000000', {'min': 1, 'max': '1.25'}, True),
])
def test_real_duckdb_decimal_range_never_rounds_intermediate(amount, bounds, expected):
    connection = numeric_table('DECIMAL(38,12)', [amount])
    try:
        report = evaluate(connection, {'version': 1, 'ranges': {'amount': bounds}})
        assert report['passed'] is expected
        assert report['numeric_policy'] == 'exact_decimal_v1'
    finally:
        connection.close()


@pytest.mark.parametrize('threshold', [0.1, 1.25, float('nan'), float('inf'), True,
    9007199254740992, '1e2', '1E-2', 'NaN', '0.1234567890123',
    '100000000000000000000000000', '+1', '1.', ''])
def test_range_requires_bounded_fixed_decimal_text_or_safe_integer(threshold):
    with pytest.raises(ValueError, match='十进制|范围阈值'):
        validate({'version': 1, 'ranges': {'amount': {'max': threshold}}})


def test_numeric_text_rejects_scientific_notation_rounding_and_bad_values():
    connection = numeric_table('VARCHAR', ['-1', '0.0000000000001', '1e1', 'bad', None])
    try:
        report = evaluate(connection, {'version': 1, 'ranges': {'amount': {'min': 0}}})
        assert not report['passed']
        assert next(x for x in report['checks'] if x['name'] == 'range:amount')['failures'] == 4
        # Range validation permits null only when a separate not_null rule does.
        assert not evaluate(connection, {'version': 1, 'totals': {'amount': {'expected': '-1'}}})['passed']
    finally:
        connection.close()


@pytest.mark.parametrize('kind,contract', [
    ('DOUBLE', {'ranges': {'amount': {'min': 0}}}),
    ('FLOAT', {'totals': {'amount': {'expected': '1.25'}}}),
    ('DECIMAL(38,13)', {'ranges': {'amount': {'min': 0}}}),
    ('BOOLEAN', {'ranges': {'amount': {'min': 0}}}),
])
def test_non_exact_or_unsupported_column_types_fail_closed(kind, contract):
    connection = numeric_table(kind, [None])
    try:
        with pytest.raises(ValueError, match='不支持精确金额校验'):
            evaluate(connection, {'version': 1, **contract})
    finally:
        connection.close()


def test_view_cannot_hide_float_by_casting_it_back_to_decimal():
    connection = duckdb.connect()
    try:
        connection.execute('CREATE SCHEMA business')
        connection.execute('CREATE TABLE input(amount DOUBLE)')
        connection.execute('INSERT INTO input VALUES (9007199254740993)')
        connection.execute('CREATE VIEW business.business_objects AS SELECT CAST(amount AS DECIMAL(38,12)) amount FROM input')
        with pytest.raises(ValueError, match='视图转换'):
            evaluate(connection, {'version': 1, 'ranges': {'amount': {'max': '9007199254740992'}}})
    finally:
        connection.close()


def test_release_reader_controls_parquet_and_rechecks_hash(tmp_path):
    path = tmp_path / 'exact.parquet'
    with duckdb.connect() as connection:
        connection.execute("COPY (SELECT '9007199254740993'::DECIMAL(38,12) AS amount) TO ? (FORMAT PARQUET)", [str(path)])
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    contract = {'version': 1, 'ranges': {'amount': {'max': '9007199254740992'}}}
    assert not evaluate_verified_parquet(contract, path, checksum)['passed']
    with pytest.raises(ValueError, match='完整性'):
        evaluate_verified_parquet(contract, path, '0'*64)


def test_old_numeric_receipt_requires_exact_policy_and_matching_frozen_contract():
    contract = {'version': 1, 'ranges': {'amount': {'min': 0}}}
    connection = numeric_table('DECIMAL(38,12)', ['1'])
    try:
        current = evaluate(connection, contract)
    finally:
        connection.close()
    require_exact_numeric_evidence(current, contract, expected_rows=1)
    for damaged in ({k: v for k, v in current.items() if k != 'numeric_policy'},
                    dict(current, contract_sha256='0'*64),
                    dict(current, checks=[]),
                    dict(current, passed=False),
                    dict(current, rows=0),
                    dict(current, rows=True)):
        with pytest.raises(ValueError, match='重新运行'):
            require_exact_numeric_evidence(damaged, contract, expected_rows=1)
    with pytest.raises(ValueError, match='重新运行'):
        require_exact_numeric_evidence(current, contract)
    require_exact_numeric_evidence({'passed': True, 'checks': [{'name': 'row_count', 'status': 'pass', 'failures': 0}]},
                                   {'version': 1, 'min_rows': 1})


def _old_style_copy(run, *, remove_policy=True, receipt_rows=None):
    """Create a synthetic, internally hash-consistent pre-fix receipt."""
    old_id = uid()
    original = DATA / run['artifact_dir']
    destination = DATA / 'runs' / old_id
    shutil.copytree(original, destination)
    handoff = destination / 'handoff'
    context_path = handoff / 'context.json'
    context = _unpack(json.loads(context_path.read_text(encoding='utf-8')))
    if remove_policy:context['data_contract'].pop('numeric_policy')
    if receipt_rows is not None:context['data_contract']['rows']=receipt_rows
    context_path.write_text(_json(_pack(context))+'\n', encoding='utf-8')
    manifest_path = handoff / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    raw = context_path.read_bytes()
    manifest['files']['context.json'] = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
    manifest_path.write_text(_json(manifest)+'\n', encoding='utf-8')
    meta = dict(run['data_result'], run_id=old_id,
                handoff_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                contract=context['data_contract'])
    (destination / 'data-result.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    old = dict(run, id=old_id, artifact_dir=destination.relative_to(DATA).as_posix(), data_result=meta,
               status='validated', scheduled_key=None, dedup_key=None, lease_token=None, lease_until=None)
    save(old, old_id)
    return old_id, meta


def test_old_numeric_result_readable_but_cannot_be_reused_published_or_reported():
    from test_project_data import project, source, document, registered, run
    from agent_service.execution import process_once
    from agent_service.jobs import get_job
    pid = project(); source_record = source(pid)
    document_value = document(source_record['id'], exact_total=True)
    document_value['contract']['ranges'] = {'amount': {'min': 0}}
    workflow_id = registered(pid, document_value)
    current = run(pid, workflow_id)
    assert current['data_result']['contract']['numeric_policy'] == 'exact_decimal_v1'
    old_id, old_meta = _old_style_copy(current)
    assert project_data.read_result(pid, old_id)['total'] == 215
    with pytest.raises(ValueError, match='重新运行'):
        project_data.publish_result(pid, old_id, old_meta['plan_sha256'])
    with pytest.raises(ValueError, match='重新运行'):
        project_data.read_input(pid, {'result_id': old_id})
    with pytest.raises(ValueError, match='重新运行'):
        business_reports.inspect_result(pid, old_id)
    # A fully hash-consistent receipt for only part of the data must not be
    # labelled as a check over all 215 records, even with the new policy marker.
    short_id, short_meta = _old_style_copy(current, remove_policy=False, receipt_rows=1)
    assert project_data.read_result(pid, short_id)['total'] == 215
    for use in (lambda: project_data.publish_result(pid, short_id, short_meta['plan_sha256']),
                lambda: project_data.read_input(pid, {'result_id': short_id}),
                lambda: business_reports.inspect_result(pid, short_id)):
        with pytest.raises(ValueError, match='重新运行'):use()
    # A new downstream worker must expose a controlled, actionable error;
    # neither raw records nor a traceback are returned in its failure.
    downstream = document(source_record['id'])
    downstream['bindings'] = {'orders': {'result_id': old_id}}
    downstream['contract'] = None
    downstream_id = registered(pid, downstream, path='workflows/use-old-result.json')
    queued = project_data.enqueue_workflow(pid, downstream_id)
    assert process_once()
    failed = get_job(queued['id'])
    assert failed['status'] == 'failed' and failed['error_code'] == 'DATA_CONTRACT_EXACTNESS'
    assert failed['workflow_failure']['code'] == 'DATA_CONTRACT_EXACTNESS'
    assert '原始资料重新运行' in failed['error'] and 'Traceback' not in failed['error']
    diagnostic = project_data.workflow_run_diagnostic(pid, downstream_id, queued['id'])
    assert '十进制字符串' in diagnostic['next_step']
    assert project_data.publish_result(pid, current['id'], current['commit'])['published_run'] == current['id']
    assert project_data.result_download(pid, current['id'], 'handoff',
        expected_handoff_sha256=current['data_result']['handoff_sha256']).is_file()
    for format_name in ('handoff','xlsx','csv'):
        with pytest.raises(ValueError, match='版本已变化'):
            project_data.result_download(pid, current['id'], format_name, expected_handoff_sha256='0'*64)
    # A foreign project never obtains even the read-only result.
    with pytest.raises(ValueError, match='不属于'):
        project_data.read_result(project(), old_id)
    # The worker's automatic adoption path must enforce the same gate.
    old = dict(current, id=old_id, artifact_dir='runs/'+old_id, data_result=old_meta,
               status='running', lease_token='test-worker-lease', lease_until=time.time()+60,
               scheduled_key=None, dedup_key=None)
    save(old, old_id)
    with pytest.raises(ValueError, match='重新运行'):
        project_data.publish_result(pid, old_id, old_meta['plan_sha256'], lease_token='test-worker-lease')


def test_worker_reports_unsupported_precision_as_actionable_error():
    import io
    from openpyxl import Workbook
    from agent_service.execution import process_once
    from agent_service.jobs import get_job
    from agent_service.sources import revision
    from agent_service.store import now,put,seal
    from test_project_data import project, registered
    book = Workbook(); sheet = book.active
    sheet.append(['id', 'flag']); sheet.append(['A', True])
    output = io.BytesIO(); book.save(output); book.close(); raw = output.getvalue()
    name = uid()+'.xlsx'; path = DATA/'uploads'/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(raw)
    pid = project()
    source = revision(put('source', {'project_id':pid,'name':'flags.xlsx','kind':'file',
        'secret_config':seal({'stored_name':name}),'content_hash':hashlib.sha256(raw).hexdigest(),
        'bytes':len(raw),'status':'ready','created_at':now()}, 's_'+uid()))
    schema = {'columns':[{'name':'id','data_type':'text','meaning':'编号'},
                         {'name':'flag','data_type':'boolean','meaning':'状态'}],
              'grain':['id'],'meaning':'一行一条记录'}
    doc = {'version':1,'plan':{'name':'布尔列校验','inputs':{'rows':schema},
        'stages':[{'id':'keep','kind':'select','input':'rows','description':'保留记录','columns':['id','flag']}],
        'output':'keep'},'bindings':{'rows':{'source_id':source['id'],'column_map':{'c_1':'id','c_2':'flag'}}},
        'contract':{'version':1,'ranges':{'flag':{'min':0}}}}
    wid = registered(pid, doc)
    queued = project_data.enqueue_workflow(pid, wid)
    assert process_once()
    failed = get_job(queued['id'])
    assert failed['status'] == 'failed' and failed['error_code'] == 'DATA_CONTRACT_EXACTNESS', failed
    assert '不支持精确金额校验' in failed['error'] and 'Traceback' not in failed['error']


def test_legacy_numeric_publication_rejects_old_receipt_but_keeps_formal_bytes():
    from fastapi.testclient import TestClient
    from agent_service.api import app
    from agent_service.config import TOKEN
    from agent_service.codegen import generate
    from agent_service.execution import process_once,enqueue
    from agent_service.jobs import get_job
    from agent_service.releases import artifact_folder,publish
    from agent_service.store import get
    from agent_service import workspace
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    pid=client.post('/api/projects',headers=headers,json={'name':'旧数值验收','request_id':uid()}).json()['id']
    uploaded=client.post('/api/projects/'+pid+'/attachments',headers=headers,
        files={'file':('amount.csv',b'id,amount\nA,10\nB,20\n','text/csv')},data={'request_id':uid()})
    assert uploaded.status_code==200 and process_once()
    source=get('source',uploaded.json()['id'])
    contract={'version':1,'ranges':{'c_2':{'min':0}}}
    workspace.write_file(pid,'data-contract.json',json.dumps(contract))
    pipeline=generate(source,'旧契约处理',source['proposed_spec'],project_id=pid)
    queued=enqueue('pipeline',pipeline['id'],commit=pipeline['commit']);assert process_once()
    current=get_job(queued['id']);assert current['status']=='validated',current
    assert current['data_contract']['numeric_policy']=='exact_decimal_v1'
    publish(pipeline['id'],current['id'])
    folder=artifact_folder(current);original=(folder/'business_objects.parquet').read_bytes()
    manifest_path=folder/'output.json';manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    old_report=dict(current['data_contract']);old_report.pop('numeric_policy')
    manifest['data_contract']=old_report;manifest_path.write_text(json.dumps(manifest,ensure_ascii=False),encoding='utf-8')
    current['data_contract']=old_report;save(current,current['id'])
    for rollback in (False,True):
        with pytest.raises(ValueError,match='原始资料重新运行'):
            publish(pipeline['id'],current['id'],rollback=rollback)
        assert get('pipeline',pipeline['id'])['published_run']==current['id']
        assert (folder/'business_objects.parquet').read_bytes()==original
    # Even matching job/manifest/receipt counts must be checked against actual
    # Parquet rows before a new publication or rollback can proceed.
    old_report['numeric_policy']='exact_decimal_v1';old_report['rows']=1
    manifest['data_contract']=old_report;manifest['records']=1
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False),encoding='utf-8')
    current.update(data_contract=old_report,records=1);save(current,current['id'])
    with pytest.raises(ValueError,match='行数不一致'):
        publish(pipeline['id'],current['id'])
    assert get('pipeline',pipeline['id'])['published_run']==current['id']
    new_run=enqueue('pipeline',pipeline['id'],commit=pipeline['commit']);assert process_once()
    refreshed=get_job(new_run['id'])
    assert refreshed['status']=='succeeded' and refreshed['data_contract']['numeric_policy']=='exact_decimal_v1', refreshed
    assert get('pipeline',pipeline['id'])['published_run']==refreshed['id']
