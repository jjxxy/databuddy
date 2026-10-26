"""Explicit localized text policies through real CSV ingestion and queued runs."""
import csv
import hashlib
import io
import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent_service import project_data as data, workspace
from agent_service.config import DATA
from agent_service.data_workflows import WorkflowPlan, execute
from agent_service.execution import process_once
from agent_service.jobs import get_job
from agent_service.sources import revision
from agent_service.store import get, now, patch, put, seal, uid
from agent_service.workbook_reader import WorkbookReadError, parse_localized_value


def col(name, kind='text', unit='', **kw):
    return {'name': name, 'data_type': kind, 'meaning': name, 'unit': unit, **kw}


def policy():
    columns = [col(n) for n in ('id', 'office', 'amount_text', 'date_text', 'rate_text')]
    normalize = {'kind': 'normalize', 'id': 'typed', 'input': 'raw',
        'description': 'Preserve original text and apply explicitly declared policies', 'rules': [
            {'column': 'amount_text', 'kind': 'decimal', 'currency': 'CNY',
             'output': col('amount', 'decimal', 'CNY', aggregation='additive')},
            {'column': 'date_text', 'kind': 'date', 'date_format': '%Y年%m月%d日',
             'output': col('event_date', 'date')},
            {'column': 'rate_text', 'kind': 'percent', 'missing_policy': 'null', 'missing_tokens': ['—'],
             'output': col('rate', 'decimal', '%', value_format='percent_fraction')}]}
    return {'name': 'Declared Chinese financial text normalization',
        'inputs': {'raw': {'columns': columns, 'grain': ['id'], 'meaning': 'One raw business record per id'}},
        'stages': [normalize, {'kind': 'aggregate', 'id': 'offices', 'input': 'typed',
            'description': 'Sum exact amounts after text normalization', 'by': ['office'],
            'meaning': 'CNY amount by office', 'aggregations': [
                {'function': 'sum', 'column': 'amount',
                 'output': col('total', 'decimal', 'CNY', aggregation='additive')}]}],
        'output': 'offices'}


def raw_rows():
    return [dict(zip(('id', 'office', 'amount_text', 'date_text', 'rate_text'), row)) for row in [
        ['A', '合肥', '￥1,234.56元', '2026年10月21日', '12.5％'],
        ['B', '合肥', '（234.56）', '2026年10月22日', '25%'],
        ['C', '南京', 'RMB2,000.000000000001', '2026年10月23日', '—']]]


def csv_data(rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader(); writer.writerows(rows)
    return stream.getvalue().encode('utf-8-sig')


def save_source(pid, rows, sid=None):
    content = csv_data(rows)
    name = uid() + '.csv'
    path = DATA / 'uploads' / name
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
    values = {'project_id': pid, 'name': 'localized.csv', 'kind': 'file',
        'secret_config': seal({'stored_name': name}), 'content_hash': hashlib.sha256(content).hexdigest(),
        'bytes': len(content), 'status': 'ready', 'created_at': now()}
    if sid:
        values.pop('kind')
    return revision(patch('source', sid, **values) if sid else put('source', values, 's_' + uid()))


def test_real_csv_normalize_aggregate_failed_refresh_keeps_published_result():
    pid = put('project', {'name': 'Localized normalization acceptance', 'created_at': now()}, 'p_' + uid())['id']
    source = save_source(pid, raw_rows())
    document = {'version': 1, 'plan': policy(), 'bindings': {'raw': {'source_id': source['id'],
        'column_map': dict(zip(('c_1', 'c_2', 'c_3', 'c_4', 'c_5'), raw_rows()[0]))}},
        'contract': {'version': 1, 'min_rows': 2, 'max_rows': 2, 'unique': [['office']],
                     'totals': {'total': {'expected': '3000.000000000001', 'tolerance': '0'}}}}
    workspace.write_file(pid, 'workflows/localized.json', json.dumps(document, ensure_ascii=False))
    wid = data.register(pid, 'workflows/localized.json')['workflow_id']
    queued = data.enqueue_workflow(pid, wid)
    assert process_once()
    run = get_job(queued['id'])
    assert run['status'] == 'validated', run
    page = data.read_result(pid, run['id'])
    totals = {r['office']: Decimal(r['total']) for r in page['rows']}
    assert totals == {'合肥': Decimal('1000.00'), '南京': Decimal('2000.000000000001')}
    folder, meta = data.result_folder(pid, run['id'])
    receipts = json.loads((folder / 'stage-receipts.json').read_text())
    assert receipts[0]['kind'] == 'normalize'
    assert receipts[0]['lineage']['rules'][0]['currency'] == 'CNY'
    assert 'original_text_columns_retained' in receipts[0]['checks']['localized_parsing']
    assert meta['requires_semantic_review'] is True
    data.publish_result(pid, run['id'], run['commit'])
    bad = raw_rows(); bad[0]['amount_text'] = '1,23'
    save_source(pid, bad, source['id'])
    queued = data.enqueue_workflow(pid, wid, use_published=True)
    assert process_once()
    failed = get_job(queued['id'])
    assert failed['status'] == 'failed', failed
    assert failed['error_code'] == 'LOCALIZED_VALUE_INVALID'
    assert failed['workflow_failure']['stage_id'] == 'typed'
    assert get('data_workflow', wid)['published_run'] == run['id']


def test_normalized_detail_retains_text_and_exact_date_rate_and_explicit_scale():
    plan = policy(); plan['stages'] = plan['stages'][:1]; plan['output'] = 'typed'
    output = execute(plan, {'raw': raw_rows()})['output']['rows']
    assert output[0]['amount_text'] == '￥1,234.56元'
    assert output[0]['amount'] == '1234.56' and output[0]['event_date'] == '2026-10-21'
    assert output[0]['rate'] == '0.125' and output[2]['rate'] is None
    rule = plan['stages'][0]['rules'][0]
    rule.update(source_unit='万元', unit_multiplier='10000')
    rows = raw_rows(); rows[0]['amount_text'] = '1.234567万元'
    rows[1]['amount_text'] = '0万元'; rows[2]['amount_text'] = '2万元'
    assert execute(plan, {'raw': rows})['output']['rows'][0]['amount'] == '12345.670000'
    rule['unit_multiplier'] = '1'
    with pytest.raises(ValidationError, match='matching base-currency multiplier'):
        WorkflowPlan.model_validate(plan)


@pytest.mark.parametrize('text,kwargs,code', [
    ('￥123', {'kind': 'decimal'}, 'LOCALIZED_CURRENCY_REQUIRED'),
    ('1万元', {'kind': 'decimal', 'currency': 'CNY'}, 'LOCALIZED_VALUE_INVALID'),
    ('1,23', {'kind': 'decimal'}, 'LOCALIZED_VALUE_INVALID'),
    ('（-123）', {'kind': 'decimal'}, 'LOCALIZED_VALUE_INVALID'),
    ('15', {'kind': 'percent'}, 'LOCALIZED_PERCENT_SIGN_REQUIRED'),
    ('—', {'kind': 'decimal', 'missing_tokens': ['—']}, 'LOCALIZED_MISSING_VALUE'),
    ('03/04', {'kind': 'date'}, 'LOCALIZED_POLICY_INVALID'),
    (None, {'kind': 'date', 'missing_policy': 'null'}, 'LOCALIZED_POLICY_INVALID'),
    ('0.1234567890121', {'kind': 'decimal'}, 'LOCALIZED_PRECISION'),
    ('0.123456789012' + '0' * 110 + '1', {'kind': 'decimal'}, 'LOCALIZED_PRECISION'),
])
def test_rejects_undeclared_or_ambiguous_localized_text(text, kwargs, code):
    with pytest.raises(WorkbookReadError) as exc:
        parse_localized_value(text, **kwargs)
    assert exc.value.code == code


def test_explicit_missing_is_null_never_zero_and_currency_receipt_is_exact():
    assert parse_localized_value('—', kind='decimal', missing_policy='null', missing_tokens=['—'])['value'] is None
    result = parse_localized_value('（￥1,234.56）', kind='decimal', currency='CNY')
    assert result['value'] == Decimal('-1234.56')
    assert result['receipt']['raw_text'] == '（￥1,234.56）'
    assert 'accounting_parentheses_to_negative' in result['receipt']['transformations']
