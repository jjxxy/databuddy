"""Immutable project semantic packages with authenticated owner review.

The API supplies the actor from its authenticated identity. Agent tools may
propose/read packages, but must not expose approve/retire. This module implements
an owner-workspace boundary, not organizational IT signatures or role management.
Git stores definitions only; the database alone owns approval and retirement.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import Field, model_validator
from sqlalchemy import select, update

from . import workspace
from .models import StrictModel
from .semantic_alignment import Provenance, SemanticField, canonical_sha256, compare_fields
from .store import all_items, get, items, now, transaction

RECORD_KIND = 'semantic_catalog'
MAX_PAYLOAD_BYTES = 240 * 1024
CatalogStatus = Literal['draft', 'approved', 'retired']
_SHA = re.compile(r'^[a-f0-9]{64}$')
_CREDENTIAL = re.compile(
    r'(?i)(?:[a-z][a-z0-9+.-]*://[^\s"/]*:[^\s"/]*@|'
    r'\bsk-[A-Za-z0-9_-]{12,}|\bBearer\s+[A-Za-z0-9_.-]{12,}|'
    r'(?:password|api[_-]?key|authorization|connection_string|access[_-]?token|'
    r'refresh[_-]?token|client[_-]?secret|token|signature|sig)'
    r'["\x27]?\s*[:=])')
_URL = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*://[^\s<>"\x27]+')
_PRIVATE_QUERY_KEYS = {'token', 'key', 'apikey', 'sig', 'signature', 'password',
                       'secret', 'clientsecret', 'accesstoken', 'refreshtoken',
                       'accesskey', 'accesskeyid', 'awsaccesskeyid', 'credential',
                       'authorization', 'auth'}


def _private_url(value) -> bool:
    if isinstance(value, dict):
        return any(_private_url(part) for part in value.values())
    if isinstance(value, list):
        return any(_private_url(part) for part in value)
    if not isinstance(value, str):
        return False
    for match in _URL.finditer(value):
        try:
            parsed = urlsplit(match[0])
            if parsed.username is not None or parsed.password is not None:
                return True
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
                normalized = re.sub(r'[_-]', '', key).casefold()
                if normalized in _PRIVATE_QUERY_KEYS or normalized.startswith(('xamz', 'xgoog')):
                    return True
        except ValueError:
            # A malformed URL cannot serve as a safe reproducible source locator.
            return True
    return False


class CatalogDefinition(StrictModel):
    key: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_][A-Za-z0-9_.-]*$')
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=2000)
    source: Provenance
    effective_from: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    effective_to: str | None = Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')
    fields: list[SemanticField] = Field(min_length=1, max_length=500)

    @model_validator(mode='after')
    def explicit_definition(self):
        if not self.name.strip() or not self.source.source_id.strip() or not self.source.locator.strip():
            raise ValueError('语义包名称、来源标识和定位不能为空白')
        start = date.fromisoformat(self.effective_from)
        if self.effective_to is not None and date.fromisoformat(self.effective_to) < start:
            raise ValueError('语义包有效期结束不能早于开始')
        if len({field.id for field in self.fields}) != len(self.fields):
            raise ValueError('语义包字段标识不能重复')
        for field in self.fields:
            for evidence in field.provenance:
                if (evidence.source_id != self.source.source_id or
                        evidence.content_sha256 != self.source.content_sha256):
                    raise ValueError('字段来源必须引用当前语义包的同一原件和摘要')
        return self


def _actor_hash(actor: str) -> str:
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
        raise ValueError('缺少有效的已鉴权 owner 身份')
    return hashlib.sha256(actor.encode()).hexdigest()


def _payload(value: CatalogDefinition | dict) -> dict:
    # Revalidate existing models too: nested fields are mutable Pydantic values.
    if isinstance(value, CatalogDefinition):
        value = value.model_dump(mode='json')
    definition = CatalogDefinition.model_validate(value)
    payload = definition.model_dump(mode='json')
    payload['source']['quote'] = ''
    for field in payload['fields']:
        for evidence in field['provenance']:
            evidence['quote'] = ''
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError('语义包定义超过240KB限制，请拆分语义包')
    if _CREDENTIAL.search(encoded) or _private_url(payload):
        raise ValueError('语义包定义不能包含连接凭证或密钥')
    return payload


def _catalog_id(project_id: str, sha: str) -> str:
    return 'cat_' + canonical_sha256([project_id, RECORD_KIND, sha])[:40]


def _content_hash(payload: dict, snapshot: dict) -> str:
    # The fixed server revision is part of package content, so replacing a source
    # with the same bytes still creates a new version instead of reusing consent.
    return canonical_sha256({'definition': payload, 'source_snapshot': snapshot})


def _verify(record: dict) -> None:
    try:
        payload = _payload(record['payload'])
        snapshot = record['source_snapshot']
        sha = _content_hash(payload, snapshot)
        if (payload != record['payload'] or sha != record['content_sha256'] or
                record['id'] != _catalog_id(record['project_id'], sha) or
                record['key'] != payload['key'] or type(record['version']) is not int or
                record['version'] < 1 or record['status'] not in ('draft', 'approved', 'retired') or
                not _SHA.fullmatch(record['created_actor_sha256'])):
            raise ValueError('invalid definition')
        if (snapshot['source_id'] != payload['source']['source_id'] or
                snapshot['content_sha256'] != payload['source']['content_sha256']):
            raise ValueError('invalid source snapshot')
        approval, retirement = record.get('approval'), record.get('retirement')
        if record['status'] == 'draft' and (approval is not None or retirement is not None):
            raise ValueError('draft contains review')
        if record['status'] in ('approved', 'retired'):
            if (not approval or approval['content_sha256'] != sha or
                    not _SHA.fullmatch(approval['actor_sha256']) or
                    approval['source_snapshot_sha256'] != canonical_sha256(snapshot) or
                    approval['trust_level'] != 'owner_reviewed'):
                raise ValueError('invalid approval')
        if record['status'] == 'approved' and retirement is not None:
            raise ValueError('approved contains retirement')
        if record['status'] == 'retired':
            if (not retirement or retirement['content_sha256'] != sha or
                    not _SHA.fullmatch(retirement['actor_sha256'])):
                raise ValueError('invalid retirement')
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('语义包完整性校验失败，不能使用或批准') from error


def _source_snapshot(connection, project_id: str, source: dict) -> dict:
    """Check actual bytes and a frozen server revision; never trust supplied SHA."""
    row = connection.execute(select(items.c.body).where(
        items.c.id == source['source_id'], items.c.kind == 'source').with_for_update()).first()
    if row is None:
        raise KeyError(source['source_id'])
    current = json.loads(row[0])
    if current.get('project_id') != project_id:
        raise ValueError('语义包来源不属于当前项目')
    if current.get('kind') != 'file' or current.get('archived'):
        raise ValueError('语义包需引用未归档的已登记文件原件；外部连接请先冻结导出')
    revision_id = current.get('revision_id')
    if not revision_id:
        raise ValueError('语义包来源缺少服务器冻结修订，请先登记来源修订')
    row = connection.execute(select(items.c.body).where(
        items.c.id == revision_id, items.c.kind == 'source_version')).first()
    frozen = json.loads(row[0]) if row else {}
    if (frozen.get('source_id') != current['id'] or
            frozen.get('project_id') not in (None, project_id) or
            frozen.get('revision') != current.get('revision') or
            frozen.get('content_hash') != source['content_sha256'] or
            current.get('content_hash') != source['content_sha256'] or
            frozen.get('secret_config') != current.get('secret_config')):
        raise ValueError('语义包来源摘要或冻结修订不一致，请重新读取原件')
    from .project_data import source_path
    path = source_path(frozen)
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(block)
    if hasher.hexdigest() != source['content_sha256']:
        raise ValueError('语义包来源原件内容已变化，摘要校验失败')
    return {'source_id': current['id'], 'source_revision': current['revision'],
            'source_revision_id': revision_id, 'content_sha256': hasher.hexdigest(),
            'verification': 'server_frozen_revision_and_actual_file_sha256'}


def propose(project_id: str, payload: CatalogDefinition | dict, actor: str) -> dict:
    """Create/reuse an immutable draft. No caller may submit review metadata."""
    workspace.folder(project_id)
    actor_sha = _actor_hash(actor)
    payload = _payload(payload)
    with workspace.lock(project_id):
        with transaction() as connection:
            snapshot = _source_snapshot(connection, project_id, payload['source'])
            sha = _content_hash(payload, snapshot)
            catalog_id = _catalog_id(project_id, sha)
            row = connection.execute(select(items.c.body).where(
                items.c.id == catalog_id, items.c.kind == RECORD_KIND).with_for_update()).first()
            if row:
                record = json.loads(row[0]); _verify(record)
                return record
            rows = connection.execute(select(items.c.body).where(items.c.kind == RECORD_KIND)).all()
            versions = [r['version'] for r in (json.loads(row[0]) for row in rows)
                        if r.get('project_id') == project_id and r.get('key') == payload['key']]
            version = max(versions, default=0) + 1
            name = 'semantics/catalogs/' + catalog_id + '.json'
            # This file is content-addressed and can survive a rolled-back DB
            # transaction. Keep the sequential display version in the DB only:
            # a later retry may legitimately acquire a different sequence number.
            definition = {'schema_version': 1, 'kind': RECORD_KIND,
                          'content_sha256': sha, 'definition': payload,
                          'source_snapshot': snapshot}
            content = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n'
            target = workspace.path_for(project_id, name)
            if target.exists():
                if target.read_text() != content:
                    raise ValueError('语义包版本定义文件已存在且内容不同，拒绝覆盖')
            else:
                workspace.write_file(project_id, name, content)
            record = {'id': catalog_id, 'project_id': project_id, 'schema_version': 1,
                      'key': payload['key'], 'version': version, 'payload': payload,
                      'content_sha256': sha, 'status': 'draft', 'created_at': now(),
                      'created_actor_sha256': actor_sha, 'source_snapshot': snapshot,
                      'approval': None, 'retirement': None, 'definition_path': name,
                      'trust_level': 'unreviewed',
                      'policy': 'authenticated_owner_review; no_organization_it_signature; review_state_not_in_git'}
            connection.execute(items.insert().values(id=catalog_id, kind=RECORD_KIND,
                               body=json.dumps(record, ensure_ascii=False)))
    return record


def get_catalog(project_id: str, catalog_id: str, *, approved_only: bool = False) -> dict:
    workspace.folder(project_id)
    record = get(RECORD_KIND, catalog_id)
    if record.get('project_id') != project_id:
        raise ValueError('语义包不属于当前项目')
    _verify(record)
    if approved_only and record['status'] != 'approved':
        raise ValueError('语义包尚未通过 owner 核阅或已退休')
    return record


def list_catalogs(project_id: str, *, status: CatalogStatus | None = None,
                  offset: int = 0, limit: int = 20) -> dict:
    workspace.folder(project_id)
    if (status not in (None, 'draft', 'approved', 'retired') or
            type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 40):
        raise ValueError('无效语义包筛选或分页参数')
    records = [r for r in all_items(RECORD_KIND) if r.get('project_id') == project_id
               and (status is None or r.get('status') == status)]
    records.sort(key=lambda r: (r['created_at'], r['id']), reverse=True)
    page = records[offset:offset + limit]
    for record in page:
        _verify(record)
    return {'items': page, 'total': len(records),
            'next_offset': offset + limit if offset + limit < len(records) else None,
            'scope': 'current_project; complete_fields; owner_workspace'}


def _locked(connection, project_id: str, catalog_id: str, expected_sha256: str) -> dict:
    if not isinstance(expected_sha256, str) or not _SHA.fullmatch(expected_sha256):
        raise ValueError('语义包批准必须绑定完整内容摘要')
    row = connection.execute(select(items.c.body).where(
        items.c.id == catalog_id, items.c.kind == RECORD_KIND).with_for_update()).first()
    if row is None:
        raise KeyError(catalog_id)
    record = json.loads(row[0])
    if record.get('project_id') != project_id:
        raise ValueError('语义包不属于当前项目')
    _verify(record)
    if record['content_sha256'] != expected_sha256:
        raise ValueError('语义包版本已变化，请重新读取并核对摘要')
    return record


def approve(project_id: str, catalog_id: str, expected_sha256: str, actor: str) -> dict:
    """Owner-only API entry point; no Agent tool should expose this function."""
    workspace.folder(project_id)
    actor_sha = _actor_hash(actor)
    # Match propose's project-lock -> DB-transaction order. Otherwise propose
    # locks source then catalog while approve locks catalog then source, which
    # can deadlock in PostgreSQL even though SQLite serializes these writes.
    with workspace.lock(project_id), transaction() as connection:
        record = _locked(connection, project_id, catalog_id, expected_sha256)
        if record['status'] == 'retired':
            raise ValueError('已退休的语义包不能重新批准，请创建新版本')
        if record['status'] == 'approved':
            return record
        for raw in record['payload']['fields']:
            field = SemanticField.model_validate(raw)
            blockers = compare_fields(field, field)['blockers']
            if blockers:
                raise ValueError('字段语义上下文不完整，不能批准：' + field.id + ':' + ','.join(blockers))
        snapshot = _source_snapshot(connection, project_id, record['payload']['source'])
        if snapshot != record['source_snapshot']:
            raise ValueError('语义包来源修订已变化，请创建新版本并重新核阅')
        record.update(status='approved', trust_level='owner_reviewed',
                      approval={'actor_sha256': actor_sha, 'approved_at': now(),
                                'content_sha256': expected_sha256,
                                'source_snapshot_sha256': canonical_sha256(snapshot),
                                'trust_level': 'owner_reviewed'})
        connection.execute(update(items).where(items.c.id == catalog_id, items.c.kind == RECORD_KIND)
                           .values(body=json.dumps(record, ensure_ascii=False)))
    return record


def retire(project_id: str, catalog_id: str, expected_sha256: str, actor: str) -> dict:
    workspace.folder(project_id)
    actor_sha = _actor_hash(actor)
    with workspace.lock(project_id), transaction() as connection:
        record = _locked(connection, project_id, catalog_id, expected_sha256)
        if record['status'] == 'draft':
            raise ValueError('草稿尚未批准，不能执行 approved 到 retired 的转换')
        if record['status'] == 'retired':
            return record
        record.update(status='retired', retirement={'actor_sha256': actor_sha,
                      'retired_at': now(), 'content_sha256': expected_sha256})
        connection.execute(update(items).where(items.c.id == catalog_id, items.c.kind == RECORD_KIND)
                           .values(body=json.dumps(record, ensure_ascii=False)))
    return record


def get_approved_catalog(project_id: str, catalog_id: str,
                         expected_sha256: str | None = None) -> dict:
    """Return the database-approved complete snapshot, never a client catalog."""
    record = get_catalog(project_id, catalog_id, approved_only=True)
    if expected_sha256 is not None and record['content_sha256'] != expected_sha256:
        raise ValueError('语义包摘要与本次固定版本不一致')
    return record
