"""Financial source semantics cannot be erased by a workflow declaration."""
import hashlib

import pytest
from openpyxl import Workbook
from pydantic import ValidationError

from agent_service import business_reports, project_data as data
from agent_service.config import DATA
from agent_service.data_workflows import WorkflowError, WorkflowPlan, execute
from agent_service.execution import process_once
from agent_service.jobs import get_job
from agent_service.sources import revision
from agent_service.store import get, now, patch, put, seal, uid
from agent_service.workbook_reader import WorkbookReadError, read_table
from test_project_data import project, registered
from test_workbook_reader import patch_cells


def column(name, kind='text', unit='', **extra):
    return dict(name=name, data_type=kind, meaning=name, unit=unit, **extra)


def schema(columns, grain=('id',)):
    return dict(columns=columns, grain=list(grain), meaning='One financial observation per id')


def workbook_source(pid, rows, cached=None, formats=None):
    book = Workbook()
    for row in rows:
        book.active.append(row)
    for coordinate, fmt in (formats or {}).items():
        book.active[coordinate].number_format = fmt
    name = uid() + '.xlsx'
    path = DATA / 'uploads' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    if cached:
        patch_cells(path, cached)
    return put('source', dict(project_id=pid, kind='file', name='财务底稿.xlsx',
        secret_config=seal({'stored_name': name}), content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        bytes=path.stat().st_size, status='ready', created_at=now()), 's_' + uid())


def normalize_document(sid, *, declared_unit='', source_unit=None, multiplier='1', currency='CNY'):
    rule = dict(column='amount_text', kind='decimal', unit_multiplier=multiplier,
                output=column('amount', 'decimal', 'CNY', aggregation='additive'))
    if source_unit is not None:
        rule['source_unit'] = source_unit
    if currency is not None:
        rule['currency'] = currency
    plan = dict(name='Normalize source financial amount', inputs={'raw': schema([
        column('id'), column('amount_text', unit=declared_unit)])}, stages=[dict(
            kind='normalize', id='typed', input='raw', description='Explicit financial unit conversion',
            rules=[rule])], output='typed')
    return dict(version=1, plan=plan, bindings={'raw': dict(source_id=sid,
        column_map={'c_1': 'id', 'c_2': 'amount_text'})})


def queued_result(pid, document):
    wid = registered(pid, document, 'workflows/financial-' + uid() + '.json')
    queued = data.enqueue_workflow(pid, wid)
    assert process_once()
    return get_job(queued['id'])


def test_explicit_workbook_unit_cannot_be_erased_by_declaring_text():
    pid = project()
    source = workbook_source(pid, [['编号', '金额（万元）'], ['A', '2']])
    result = queued_result(pid, normalize_document(source['id']))
    assert result['status'] == 'failed', result
    assert not result.get('validated')
    with pytest.raises(ValueError):
        data.result_folder(pid, result['id'])


@pytest.mark.parametrize('source_unit', [None, '元'])
def test_normalize_policy_must_bind_the_preserved_source_unit(source_unit):
    pid = project()
    source = workbook_source(pid, [['编号', '金额（万元）'], ['A', '2']])
    document = normalize_document(source['id'], declared_unit='万元', source_unit=source_unit)
    result = queued_result(pid, document)
    assert result['status'] == 'failed', result
    assert result['workflow_failure']['stage_id'] == 'typed'


def test_known_currency_unit_scale_is_checked_even_without_currency_flag():
    document = normalize_document('unused', declared_unit='万元', source_unit='万元', currency=None)
    with pytest.raises(ValidationError):
        WorkflowPlan.model_validate(document['plan'])


def test_valid_conversion_retains_original_value_unit_and_exact_base_amount():
    pid = project()
    source = workbook_source(pid, [['编号', '金额（万元）'], ['A', '2.123456789012']])
    result = queued_result(pid, normalize_document(source['id'], declared_unit='万元',
        source_unit='万元', multiplier='10000'))
    assert result['status'] == 'validated', result
    page = data.read_result(pid, result['id'])
    assert page['rows'][0] == dict(id='A', amount_text='2.123456789012', amount='21234.567890120000')
    assert {c['name']: c['unit'] for c in page['schema']} == {
        'id': '', 'amount_text': '万元', 'amount': 'CNY'}


def test_source_unit_change_cannot_replace_the_published_financial_result():
    pid = project()
    source = workbook_source(pid, [['编号', '金额（万元）'], ['A', '2']])
    document = normalize_document(source['id'], declared_unit='万元', source_unit='万元', multiplier='10000')
    result = queued_result(pid, document)
    assert result['status'] == 'validated', result
    data.publish_result(pid, result['id'], result['commit'])
    replacement = workbook_source(pid, [['编号', '金额（元）'], ['A', '2']])
    revision(patch('source', source['id'], secret_config=replacement['secret_config'],
        content_hash=replacement['content_hash'], bytes=replacement['bytes']))
    queued = data.enqueue_workflow(pid, result['target'], use_published=True)
    assert process_once()
    failed = get_job(queued['id'])
    assert failed['status'] == 'failed' and not failed.get('validated')
    assert get('data_workflow', result['target'])['published_run'] == result['id']
    assert data.read_result(pid, result['id'])['rows'][0]['amount'] == '20000'


def test_cached_formula_unknown_freshness_is_preserved_through_downstream_result():
    pid = project()
    source = workbook_source(pid, [['编号', '金额（元）'], ['A', '=1+1']], {'B2': {'value': '2'}})
    declared = schema([column('id'), column('amount', 'decimal', '元', aggregation='additive')])
    document = dict(version=1, plan=dict(name='Preserve cached formula uncertainty',
        inputs={'raw': declared}, stages=[dict(kind='select', id='selected', input='raw',
            description='Keep cached source amount', columns=['id', 'amount'])], output='selected'),
        bindings={'raw': dict(source_id=source['id'], column_map={'c_1': 'id', 'c_2': 'amount'})})
    first = queued_result(pid, document)
    assert first['status'] == 'validated', first
    page = data.read_result(pid, first['id'])
    assert page['context']['requires_semantic_review'] is True
    evidence = page['context']['input_evidence']['raw']
    assert evidence['formula_cache_freshness'] == 'unknown'
    assert evidence['special_cells']['2']['c_2']['cache_freshness'] == 'unknown'
    document['bindings'] = {'raw': {'result_id': first['id']}}
    second = queued_result(pid, document)
    assert second['status'] == 'validated', second
    assert data.read_result(pid, second['id'])['context']['requires_semantic_review'] is True


def test_excel_precision_history_risk_keeps_exact_stored_value_and_blocks_unadopted_brief():
    pid = project()
    source = workbook_source(pid, [['编号', '金额（元）'], ['A', 1234567890123456]])
    preview = data.preview_source(pid, {'source_id': source['id']})
    assert preview['complete_source_read'] is True
    assert preview['evidence']['requires_semantic_review'] is True
    assert preview['evidence']['semantic_review_reasons'] == ['EXCEL_PRECISION_LIMIT']
    declared = schema([column('id'), column('amount', 'decimal', '元', aggregation='additive')])
    document = dict(version=1, plan=dict(name='Keep exact stored value with precision warning',
        inputs={'raw': declared}, stages=[dict(kind='select', id='selected', input='raw',
            description='Keep source amount and its potential precision history risk',
            columns=['id', 'amount'])], output='selected'),
        bindings={'raw': dict(source_id=source['id'], column_map={'c_1': 'id', 'c_2': 'amount'})})
    first = queued_result(pid, document)
    assert first['status'] == 'validated', first
    page = data.read_result(pid, first['id'])
    assert page['rows'][0]['amount'] == '1234567890123456'
    assert page['context']['requires_semantic_review'] is True
    assert page['context']['semantic_review_reasons'] == ['EXCEL_PRECISION_LIMIT']
    assert first['data_result']['semantic_review_reasons'] == ['EXCEL_PRECISION_LIMIT']
    with pytest.raises(ValueError, match='待核对'):
        business_reports.inspect_result(pid, first['id'])
    document['bindings'] = {'raw': {'result_id': first['id']}}
    second = queued_result(pid, document)
    assert second['status'] == 'validated', second
    downstream = data.read_result(pid, second['id'])
    assert downstream['context']['semantic_review_reasons'] == ['EXCEL_PRECISION_LIMIT']
    assert downstream['context']['requires_semantic_review'] is True
    with pytest.raises(ValueError, match='待核对'):
        business_reports.inspect_result(pid, second['id'])


@pytest.mark.parametrize('operation', ['lookup', 'max'])
def test_nonadditive_measure_cannot_be_relabelled_additive(operation):
    measure = column('balance', 'decimal', 'CNY', aggregation='non_additive')
    output = column('picked', 'decimal', 'CNY', aggregation='additive')
    inputs = {'raw': schema([column('id'), measure])}
    rows = {'raw': [dict(id='A', balance='12'), dict(id='B', balance='15')]}
    if operation == 'max':
        stage = dict(kind='aggregate', id='picked', input='raw', by=[], meaning='Maximum balance',
            description='Cannot strengthen additivity', aggregations=[dict(function='max', column='balance', output=output)])
    else:
        inputs['keys'] = schema([column('id')])
        rows['keys'] = [dict(id='A'), dict(id='B')]
        stage = dict(kind='lookup', id='picked', input='keys', right='raw', left_key='id',
            right_key='id', outputs={'balance': output}, description='Cannot strengthen additivity')
    plan = dict(name='Preserve nonadditivity', inputs=inputs, stages=[stage], output='picked')
    with pytest.raises(WorkflowError):
        execute(plan, rows)


@pytest.mark.parametrize('fmt', ['0"%"', '0\\%'])
def test_literal_percent_is_points_and_cannot_be_misdeclared_as_fraction(fmt):
    pid = project()
    source = workbook_source(pid, [['编号', '同比'], ['A', 15]], formats={'B2': fmt})
    table = read_table(data.source_path(source))
    assert table['fields'][1]['stored_semantics'] == 'percent_points'
    rate = column('rate', 'decimal', '%', value_format='percent_fraction')
    document = dict(version=1, plan=dict(name='Preserve literal percent format',
        inputs={'raw': schema([column('id'), rate])}, stages=[dict(kind='select', id='selected',
            input='raw', description='Preserve source representation', columns=['id', 'rate'])],
        output='selected'), bindings={'raw': dict(source_id=source['id'],
            column_map={'c_1': 'id', 'c_2': 'rate'})})
    assert queued_result(pid, document)['status'] == 'failed'
    rate['value_format'] = 'percent_points'
    result = queued_result(pid, document)
    assert result['status'] == 'validated', result
    assert data.read_result(pid, result['id'])['rows'][0]['rate'] == '15'


@pytest.mark.parametrize('fmt,code', [
    ('0,,"万元"', 'NUMERIC_DISPLAY_SCALE_UNVERIFIED'),
    ('0%%', 'PERCENTAGE_FORMAT_UNSUPPORTED'),
    ('0%;0"%"', 'PERCENTAGE_FORMAT_UNSUPPORTED'),
])
def test_unsupported_display_scaling_is_inspectable_but_cannot_be_executed(fmt, code):
    pid = project()
    source = workbook_source(pid, [['编号', '金额'], ['A', 100000000]], formats={'B2': fmt})
    with pytest.raises(WorkbookReadError) as caught:
        read_table(data.source_path(source))
    assert caught.value.code == code
    preview = data.preview_source(pid, {'source_id': source['id']})
    assert preview['rows'][0]['c_2'] == '100000000'
    assert not preview['complete_source_read']


@pytest.mark.parametrize('header,formats', [
    ('金额（万元）', {'B2': '0%', 'B3': '0%'}),
    ('金额（元）', {'B2': '0"万元"'}),
    ('金额', {'B2': '0"万元"', 'B3': '0"元"'}),
])
def test_conflicting_header_or_row_format_units_cannot_define_a_single_measure(header, formats):
    pid = project()
    source = workbook_source(pid, [['编号', header], ['A', 2], ['B', 3]], formats=formats)
    with pytest.raises(WorkbookReadError) as caught:
        read_table(data.source_path(source))
    assert caught.value.code == 'UNIT_FORMAT_CONFLICT'
    preview = data.preview_source(pid, {'source_id': source['id']})
    assert not preview['complete_source_read']


@pytest.mark.parametrize('operation', ['add', 'subtract', 'multiply', 'divide'])
def test_arithmetic_cannot_turn_known_nonadditive_values_into_additive_output(operation):
    fields = [column('id'), column('a', 'decimal', '1', aggregation='non_additive'),
              column('b', 'decimal', '1', aggregation='non_additive')]
    plan = dict(name='Preserve nonadditivity through arithmetic', inputs={'raw': schema(fields)},
        stages=[dict(kind='compute', id='computed', input='raw', description='Cannot infer additive output',
            calculations=[dict(left='a', right='b', operation=operation,
                output=column('result', 'decimal', '1', aggregation='additive'))])], output='computed')
    with pytest.raises(WorkflowError) as caught:
        execute(plan, {'raw': [dict(id='A', a='2', b='1')]})
    assert caught.value.code == 'NON_ADDITIVE'
