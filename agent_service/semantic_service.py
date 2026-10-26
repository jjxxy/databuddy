"""Project-scoped semantic proposals with server-owned review receipts.

The authenticated API supplies ``actor``.  Agent tools may propose and inspect;
they must never be allowed to call review functions on a model-supplied actor.
Definitions may enter the normal project Git timeline; review status and actor
remain in metadata outside that timeline.
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Literal

from sqlalchemy import select, update

from . import workspace
from .semantic_alignment import (
    AlignmentProposal, ModelCandidate, ReviewedAlignment, ReviewedMapping,
    SemanticField, ValueMapping, canonical_sha256, review_alignment,
    review_mapping, suggest_alignment, validate_reviewed_alignment,
)
from .store import all_items, get, items, now, transaction

RECORD_KIND = 'semantic_proposal'
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024


def _owned(project_id: str, proposal_id: str) -> dict:
    get('project', project_id)
    record = get(RECORD_KIND, proposal_id)
    if record.get('project_id') != project_id:
        raise ValueError('语义建议不属于当前项目')
    _verify_record(record)
    return record


def _verify_record(record: dict) -> None:
    if canonical_sha256(record['payload']) != record['content_sha256']:
        raise ValueError('语义定义完整性校验失败，不能使用或确认')
    if record['kind'] == 'alignment':
        AlignmentProposal.model_validate(record['payload'])
    elif record['kind'] == 'value_mapping':
        ValueMapping.model_validate(record['payload'])
    else:
        raise ValueError('未知语义建议类型')
    binding = record.get('catalog_binding')
    version = record.get('schema_version')
    if (version not in (1, 2) or version == 2 and not isinstance(binding, dict)
            or version == 1 and binding is not None):
        raise ValueError('语义包绑定完整性校验失败')
    identity = [record['project_id'], record['kind'], record['content_sha256']]
    if version == 2:
        identity.append(binding)
    if record['id'] != 'sem_' + canonical_sha256(identity)[:40]:
        raise ValueError('语义定义身份完整性校验失败')
    if binding is not None:
        if (record['kind'] != 'alignment' or
                record['id'] != 'sem_' + canonical_sha256(
                    [record['project_id'], record['kind'], record['content_sha256'], binding])[:40]):
            raise ValueError('语义包绑定完整性校验失败')
        _catalog_for_binding(record, active=False)


def _without_quotes(value):
    # Source coordinates and hashes retain traceability.  Do not copy source
    # passages/row contents into semantic metadata or into a code repository.
    if isinstance(value, dict):
        return {k: ('' if k == 'quote' else _without_quotes(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_without_quotes(v) for v in value]
    return value


def _definition_payload(value) -> dict:
    payload = value.model_dump(mode='json') if hasattr(value, 'model_dump') else value
    payload = _without_quotes(payload)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError('语义定义超过2MB限制，请分批建立映射')
    if re.search(r'(?i)(?:postgres(?:ql)?|mysql)(?:\+\w+)?://[^\s]*:|\bsk-[A-Za-z0-9_-]{12,}|(?:password|api_key|authorization|connection_string)["\x27]?\s*[:=]', encoded):
        raise ValueError('语义定义不能包含连接凭证或密钥')
    return payload


def _save(project_id: str, kind: str, payload: dict, binding: str,
          catalog_binding: dict | None = None) -> dict:
    # Validate the project ID through the same path/ownership rules as project
    # files before deriving any filesystem location.
    workspace.folder(project_id)
    sha = canonical_sha256(payload)
    identity = [project_id, kind, sha]
    if catalog_binding is not None:
        identity.append(catalog_binding)
    proposal_id = 'sem_' + canonical_sha256(identity)[:40]
    with workspace.lock(project_id):
        with transaction() as connection:
            # Retirement uses the same project lock. Resolve again here, not
            # only before acquiring it, so a concurrent retirement cannot race
            # a new package-backed proposal or reuse its prior approval.
            if catalog_binding is not None:
                _catalog_for_binding({'project_id': project_id, 'payload': payload,
                                      'catalog_binding': catalog_binding}, active=True)
            existing = connection.execute(select(items.c.body).where(
                items.c.id == proposal_id, items.c.kind == RECORD_KIND).with_for_update()).first()
            if existing:
                saved = json.loads(existing[0])
                _verify_record(saved)
                return saved
            previous = connection.execute(select(items.c.body).where(items.c.kind == RECORD_KIND)).all()
            revisions = [json.loads(row[0]) for row in previous]
            versions = [r['version'] for r in revisions
                        if r.get('project_id') == project_id and r.get('kind') == kind and r.get('binding') == binding]
            record = {'id': proposal_id, 'project_id': project_id, 'kind': kind,
                      'schema_version': 1, 'version': max(versions, default=0) + 1,
                      'binding': binding, 'content_sha256': sha, 'payload': payload,
                      'status': 'proposed', 'review': None, 'created_at': now(),
                      'definition_path': None, 'definition_storage': 'metadata_only',
                      'policy': 'review_requires_authenticated_api; review_state_not_in_git'}
            if catalog_binding is not None:
                record.update(schema_version=2, catalog_binding=catalog_binding)
            # Optional versionable definition.  It carries neither actor nor
            # consent.  A later edit/restore of this file cannot authorize use.
            name = 'semantics/proposals/' + proposal_id + '.json'
            try:
                workspace.write_file(project_id, name, json.dumps(
                    {'schema_version': record['schema_version'], 'kind': kind, 'content_sha256': sha,
                     'definition': payload, **({'catalog_binding': catalog_binding}
                        if catalog_binding is not None else {})}, ensure_ascii=False, indent=2) + '\n')
                record.update(definition_path=name, definition_storage='metadata_and_project_file')
            except (ValueError, OSError):
                # Large maps/workspace quota can still use bounded DB metadata.
                # Credential checks have already run before reaching this path.
                pass
            connection.execute(items.insert().values(id=proposal_id, kind=RECORD_KIND,
                               body=json.dumps(record, ensure_ascii=False)))
    return record


def propose(project_id: str, source: SemanticField | dict,
            catalog: list[SemanticField | dict],
            model_candidates: list[ModelCandidate | dict] | None = None,
            manual_target_ids: list[str] | None = None) -> dict:
    proposal = suggest_alignment(source, catalog, model_candidates, manual_target_ids)
    payload = _definition_payload(proposal)
    return _save(project_id, 'alignment', payload, proposal.source.id)


def propose_mapping(project_id: str, mapping: ValueMapping | dict) -> dict:
    mapping = ValueMapping.model_validate(mapping)
    return _save(project_id, 'value_mapping', _definition_payload(mapping), mapping.id)


def _binding(catalog: dict, as_of_date: str) -> dict:
    if not isinstance(as_of_date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', as_of_date):
        raise ValueError('必须明确业务口径适用日，格式为 YYYY-MM-DD')
    day = date.fromisoformat(as_of_date)
    definition = catalog['payload']
    if (day < date.fromisoformat(definition['effective_from']) or
            definition['effective_to'] is not None and day > date.fromisoformat(definition['effective_to'])):
        raise ValueError('业务口径适用日不在语义包有效期内，请选择适用的历史版本')
    return {'catalog_id': catalog['id'], 'content_sha256': catalog['content_sha256'],
            'key': catalog['key'], 'name': definition['name'], 'version': catalog['version'],
            'as_of_date': as_of_date, 'trust_level': 'owner_reviewed',
            'source_snapshot_sha256': canonical_sha256(catalog['source_snapshot'])}


def _catalog_for_binding(record: dict, *, active: bool) -> dict | None:
    binding = record.get('catalog_binding')
    if binding is None:
        return None
    from .semantic_catalog import get_catalog
    catalog = get_catalog(record['project_id'], binding.get('catalog_id', ''), approved_only=active)
    if catalog['status'] not in ('approved', 'retired'):
        raise ValueError('匹配绑定的语义包没有 owner 核阅记录')
    if binding != _binding(catalog, binding.get('as_of_date')):
        raise ValueError('语义包固定版本与匹配提案不一致')
    targets = {field['id']: field for field in catalog['payload']['fields']}
    for candidate in record['payload']['candidates']:
        if candidate['target'] != targets.get(candidate['target']['id']):
            raise ValueError('匹配目标与服务器语义包字段不一致')
    return catalog


def propose_from_catalog(project_id: str, source: SemanticField | dict,
                         catalog_id: str, expected_catalog_sha256: str,
                         model_candidates: list[ModelCandidate | dict] | None = None,
                         *, as_of_date: str) -> dict:
    """Resolve targets exclusively from a reviewed immutable server package.

    The business reference date is explicit, so a historical analysis never
    silently switches to today's policy. Approval of the package does not
    approve the individual mapping; that still needs authenticated review.
    """
    from .semantic_catalog import get_approved_catalog
    catalog = get_approved_catalog(project_id, catalog_id, expected_catalog_sha256)
    binding = _binding(catalog, as_of_date)
    proposal = suggest_alignment(source, catalog['payload']['fields'], model_candidates)
    return _save(project_id, 'alignment', _definition_payload(proposal), proposal.source.id, binding)


def get_proposal(project_id: str, proposal_id: str) -> dict:
    return _owned(project_id, proposal_id)


def list_proposals(project_id: str, offset: int = 0, limit: int = 20) -> dict:
    get('project', project_id)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 40:
        raise ValueError('无效语义建议分页参数')
    records = [r for r in all_items(RECORD_KIND) if r.get('project_id') == project_id]
    records.sort(key=lambda r: (r['created_at'], r['id']), reverse=True)
    result = []
    for record in records[offset:offset + limit]:
        _verify_record(record)
        summary = {k: record[k] for k in ('id', 'kind', 'version', 'binding', 'content_sha256',
                                        'status', 'created_at', 'definition_path')}
        if record.get('catalog_binding'):
            summary['catalog_binding'] = record['catalog_binding']
        payload = record['payload']
        if record['kind'] == 'alignment':
            summary.update(source_name=payload['source']['name'], ambiguous=payload['ambiguous'],
                           candidates=[{'id': c['target']['id'], 'name': c['target']['name'],
                                        'state': c['state'], 'blockers': c['blockers']}
                                       for c in payload['candidates']])
        else:
            summary.update(mapping_id=payload['id'], mapping_version=payload['version'],
                           source_field_id=payload['source_field_id'], target_field_id=payload['target_field_id'],
                           entries=len(payload['entries']), unmapped_policy=payload['unmapped_policy'])
        result.append(summary)
    return {'items': result, 'total': len(records),
            'next_offset': offset + limit if offset + limit < len(records) else None,
            'scope': 'current_project; summaries_only; read_details_before_review'}


def _actor(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError('缺少有效的已鉴权核对人')
    return value


def _locked_record(connection, project_id, proposal_id, expected_sha256, kind):
    row = connection.execute(select(items.c.body).where(items.c.id == proposal_id,
                             items.c.kind == RECORD_KIND).with_for_update()).first()
    if row is None:
        raise KeyError(proposal_id)
    record = json.loads(row[0])
    if record.get('project_id') != project_id:
        raise ValueError('语义建议不属于当前项目')
    _verify_record(record)
    if record['kind'] != kind:
        raise ValueError('语义建议类型不匹配')
    if record['content_sha256'] != expected_sha256:
        raise ValueError('语义建议版本已变化，请重新读取后核对')
    return record


def review(project_id: str, proposal_id: str, expected_sha256: str,
           target_id: str, actor: str,
           decision: Literal['approved', 'rejected'] = 'approved',
           review_note: str = '') -> dict:
    get('project', project_id)
    actor = _actor(actor)
    if decision not in ('approved', 'rejected'):
        raise ValueError('核对决定必须是 approved 或 rejected')
    with workspace.lock(project_id), transaction() as connection:
        record = _locked_record(connection, project_id, proposal_id, expected_sha256, 'alignment')
        if decision == 'approved':
            _catalog_for_binding(record, active=True)
        if record['status'] != 'proposed':
            saved = record.get('review') or {}
            if record['status'] == decision and saved.get('target_id') == target_id:
                return record
            raise ValueError('该建议已有不可变核对记录；修改定义请创建新建议')
        receipt = review_alignment(record['payload'], target_id, expected_sha256,
                                   actor, decision, review_note)
        record.update(status=decision, review=receipt.model_dump(mode='json'), reviewed_at=now())
        connection.execute(update(items).where(items.c.id == proposal_id, items.c.kind == RECORD_KIND)
                           .values(body=json.dumps(record, ensure_ascii=False)))
    return record


def review_mapping_record(project_id: str, proposal_id: str,
                          expected_sha256: str, actor: str) -> dict:
    get('project', project_id)
    actor = _actor(actor)
    with transaction() as connection:
        record = _locked_record(connection, project_id, proposal_id, expected_sha256, 'value_mapping')
        if record['status'] != 'proposed':
            if record['status'] == 'approved':
                return record
            raise ValueError('该映射已有不可变核对记录，请创建新建议')
        receipt = review_mapping(record['payload'], expected_sha256, actor)
        record.update(status='approved', review=receipt.model_dump(mode='json'), reviewed_at=now())
        connection.execute(update(items).where(items.c.id == proposal_id, items.c.kind == RECORD_KIND)
                           .values(body=json.dumps(record, ensure_ascii=False)))
    return record


def get_reviewed_mapping(project_id: str, proposal_id: str) -> ReviewedMapping:
    record = _owned(project_id, proposal_id)
    if record['kind'] != 'value_mapping' or record['status'] != 'approved':
        raise ValueError('该值映射尚未在当前项目通过人工核对')
    review = ReviewedMapping.model_validate(record['review'])
    if canonical_sha256(review.mapping) != record['content_sha256']:
        raise ValueError('映射回执与服务器定义不一致')
    return review


def get_reviewed_alignment(project_id: str, proposal_id: str) -> ReviewedAlignment:
    record = _owned(project_id, proposal_id)
    if record['kind'] != 'alignment' or record['status'] != 'approved':
        raise ValueError('该语义匹配尚未在当前项目通过人工核对')
    _catalog_for_binding(record, active=True)
    review = ReviewedAlignment.model_validate(record['review'])
    proposal = AlignmentProposal.model_validate(record['payload'])
    target = next((c.target for c in proposal.candidates if c.target.id == review.target_id), None)
    if target is None or review.proposal_sha256 != record['content_sha256']:
        raise ValueError('匹配回执与服务器定义不一致')
    validate_reviewed_alignment(review, proposal.source, target)
    return review
