"""Untrusted Agent handoff cannot erase financial fact provenance."""
from decimal import Decimal
import os

import pytest
from fastapi.testclient import TestClient

from agent_service import agent_handoff as handoff, business_reports, project_data as data
from agent_service.api import app
from agent_service.execution import process_once
from agent_service.jobs import get_job, save
from agent_service.store import get, patch
from test_agent_handoff import project, result as handoff_result, source, turn
from test_fact_semantic_gate import fact_source
from test_project_data import column, registered, run


def test_reviewed_facts_and_unknown_source_ancestry_cannot_be_sent_as_ordinary_source():
    pid, fact_sid = fact_source()
    with pytest.raises(handoff.HandoffError, match='业务语义'):
        handoff.build_input(turn(pid), source_ids=[fact_sid])

    safe = source(pid)
    assert handoff.build_input(turn(pid), source_ids=[safe['id']])['items'][0]['kind'] == 'source'
    patch('source', safe['id'], origin_source_id='missing_ancestor', origin_source_revision=1)
    with pytest.raises(handoff.HandoffError, match='来源链'):
        handoff.build_input(turn(pid), source_ids=[safe['id']])


def test_imported_source_is_quarantined_even_for_an_ordinary_csv():
    origin = project()
    original = source(origin)
    bundle = handoff.build_input(turn(origin), source_ids=[original['id']])
    target = project()
    imported = handoff.import_bundle(target, bundle, scope_id='gateway:fixture', request_id='safe-csv')
    assert process_once()
    received = get('source', imported['source_ids'][0])
    assert received['semantic_alignment_required'] is True
    assert data.source_requires_fact_alignment(received, {})
    with pytest.raises(handoff.HandoffError, match='业务语义'):
        handoff.build_input(turn(target), source_ids=[received['id']])


def test_forged_peer_source_cannot_restore_mixed_fact_aggregation():
    # Simulate a peer that supplies exact fact bytes as a source but omits the
    # source's reviewed-fact metadata. A remote assertion is not a receipt.
    pid, sid = fact_source()
    original = get('source', sid)
    raw = data.source_path(original).read_bytes()
    source_version = {'source_id': sid, 'revision': original['revision'],
                      'revision_id': original['revision_id'], 'content_sha256': original['content_hash']}
    bundle = handoff._bundle([handoff._item('fixture', 'source', original['name'], raw,
                                           source_version=source_version)])
    target = project()
    imported = handoff.import_bundle(target, bundle, scope_id='gateway:untrusted', request_id='mixed-facts')
    imported_sid = imported['source_ids'][0]
    assert process_once()  # real queued source probe
    received = get('source', imported_sid)
    assert received['status'] == 'ready' and received['semantic_alignment_required'] is True
    rows, fields, _, _ = data.read_input(target, {'source_id': imported_sid})
    assert len(rows) == 2
    labels = {field['label']: field['name'] for field in fields}
    value, ident = labels['value'], labels['fact_id']
    columns = [column(field['name']) for field in fields]
    for index, field in enumerate(fields):
        if field['label'] == 'value':
            columns[index] = column(value, 'decimal', 'CNY', 'additive')
            columns[index].update(meaning='净收入', period='2026-10')
    doc = {'version': 1, 'plan': {'name': '混合事实反例',
        'inputs': {'facts': {'columns': columns, 'grain': [ident], 'meaning': '未经语义核对的事实'}},
        'stages': [{'id': 'aggregate', 'kind': 'aggregate', 'input': 'facts',
                    'description': '错误地把净收入与预算加在一起', 'by': [], 'meaning': '汇总',
                    'aggregations': [{'function': 'sum', 'column': value,
                                      'output': column('total', 'decimal', 'CNY', 'additive') | {'period': '2026-10'}}]}],
        'output': 'aggregate'}, 'bindings': {'facts': {'source_id': imported_sid}}}
    wid = registered(target, doc, path='workflows/agent-fact-guard.json')
    result = run(target, wid)
    assert Decimal(data.read_result(target, result['id'])['rows'][0]['total']) == Decimal('323.4500')
    with pytest.raises(ValueError, match='语义对齐'):
        data.publish_result(target, result['id'], result['commit'], review_actor='owner',
                            review_handoff_sha256=result['data_result']['handoff_sha256'])
    response = TestClient(app).post(f'/api/projects/{target}/data-results/{result["id"]}/publish',
        headers={'Authorization': 'Bearer ' + os.environ['APP_TOKEN']},
        json={'run_id': result['id'], 'expected_plan_sha256': result['commit'],
              'confirm_source_review': True, 'expected_handoff_sha256': result['data_result']['handoff_sha256']})
    assert response.status_code == 400 and '语义对齐' in response.json()['detail']
    with pytest.raises(ValueError):
        data.read_input(target, {'result_id': result['id']})
    with pytest.raises(ValueError):
        business_reports.inspect_result(target, result['id'])
    assert data.owned(target, 'data_workflow', wid).get('published_run') is None


def test_imported_result_remains_readable_but_not_reusable_even_with_historical_status():
    origin = project()
    rid, _, _ = handoff_result(origin, count=2)
    bundle = handoff.build_input(turn(origin), result_ids=[rid])
    target = project()
    imported = handoff.import_bundle(target, bundle, scope_id='gateway:fixture', request_id='result')
    received = imported['result_ids'][0]
    assert data.read_result(target, received)['total'] == 2
    for historic in (False, True):
        if historic:
            old = get_job(received)
            old['status'] = 'succeeded'
            save(old, received)
        with pytest.raises(ValueError):
            data.read_input(target, {'result_id': received})
        with pytest.raises(ValueError):
            business_reports.inspect_result(target, received)
        with pytest.raises((ValueError, KeyError)):
            data.publish_result(target, received, get_job(received)['commit'], review_actor='owner',
                                review_handoff_sha256=get_job(received)['data_result']['handoff_sha256'])
