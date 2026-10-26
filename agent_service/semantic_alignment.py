"""Semantic proposals and reviewed, versioned crosswalks.

Names are retrieval hints, not proof of equivalent business meaning.  Callers
must authenticate reviewers and persist receipts server-side.  A digest is an
integrity/version check, not an authorization signature.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, model_validator

from .models import StrictModel


def canonical_sha256(value: Any) -> str:
    if hasattr(value, 'model_dump'):
        value = value.model_dump(mode='json')
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _label(value: str) -> str:
    return re.sub(r'[\s_\-]+', '', unicodedata.normalize('NFKC', value).casefold())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _concrete_period(value: str, temporal_kind: str) -> bool:
    """Reject relative labels that silently change when a source refreshes."""
    try:
        if re.fullmatch(r'\d{4}', value):
            return temporal_kind == 'interval' and 1 <= int(value) <= 9999
        if re.fullmatch(r'\d{4}-\d{2}', value):
            date.fromisoformat(value + '-01')
            return temporal_kind == 'interval'
        if '/' in value:
            start, end = value.split('/')
            return temporal_kind == 'interval' and date.fromisoformat(start) <= date.fromisoformat(end)
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            date.fromisoformat(value)
            return True
        moment = datetime.fromisoformat(value)
        return temporal_kind == 'instant' and moment.tzinfo is not None
    except (ValueError, TypeError):
        return False


class Provenance(StrictModel):
    source_id: str = Field(min_length=1, max_length=128)
    locator: str = Field(min_length=1, max_length=500)
    content_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    quote: str = Field(default='', max_length=4000)


class SemanticField(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=40)
    meaning: str | None = Field(default=None, max_length=2000)
    kind: Literal['dimension', 'measure', 'attribute', 'unknown'] = 'unknown'
    # None means unknown; [] explicitly declares scalar grain.
    grain: list[str] | None = Field(default=None, max_length=20)
    unit: str | None = Field(default=None, max_length=80)
    value_format: Literal['text', 'identifier', 'integer', 'decimal', 'percent_fraction',
                          'percent_points', 'date', 'boolean', 'unknown'] = 'unknown'
    number_format: str | None = Field(default=None, max_length=200)
    temporal_kind: Literal['interval', 'instant', 'timeless', 'unknown'] = 'unknown'
    period: str | None = Field(default=None, max_length=200)
    aggregation: Literal['sum', 'count', 'mean', 'ratio', 'last', 'none', 'unknown'] = 'unknown'
    provenance: list[Provenance] = Field(default_factory=list, max_length=20)

    @model_validator(mode='after')
    def meaningful_metadata(self):
        if not self.id.strip() or not self.name.strip():
            raise ValueError('语义字段标识和名称不能为空白')
        if any(not v.strip() or len(v) > 200 for v in self.aliases):
            raise ValueError('字段别名不能为空白或超过200字符')
        if self.grain is not None:
            if any(not v.strip() or len(v) > 128 for v in self.grain):
                raise ValueError('粒度维度必须是明确标识')
            if len(set(self.grain)) != len(self.grain):
                raise ValueError('粒度维度不能重复')
        if self.temporal_kind in ('interval', 'instant') and not (self.period or '').strip():
            raise ValueError('期间或时点字段必须填写明确期间/时点')
        if self.temporal_kind in ('interval', 'instant') and not _concrete_period(self.period, self.temporal_kind):
            raise ValueError('期间必须是明确的ISO年份、年月、日期、起止日期区间或含时区的时点，不能使用本月/上月等相对描述')
        if self.temporal_kind == 'timeless' and self.period is not None:
            raise ValueError('无时间属性的字段不能同时填写期间')
        return self


class ModelCandidate(StrictModel):
    target_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)


class AlignmentCandidate(StrictModel):
    target: SemanticField
    origins: list[Literal['name', 'alias', 'model', 'manual']]
    state: Literal['review_required', 'blocked']
    blockers: list[str]
    review_reasons: list[str]
    model_reason: str | None = None


class AlignmentProposal(StrictModel):
    version: Literal[1] = 1
    source: SemanticField
    candidates: list[AlignmentCandidate]
    state: Literal['unreviewed'] = 'unreviewed'
    ambiguous: bool
    policy: Literal['labels_are_hints; explicit_review_required; input_metadata_untrusted'] = 'labels_are_hints; explicit_review_required; input_metadata_untrusted'


class ReviewedAlignment(StrictModel):
    proposal_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    source_id: str
    target_id: str
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    target_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    decision: Literal['approved', 'rejected']
    reviewer: str = Field(min_length=1, max_length=200)
    review_note: str = Field(default='', max_length=2000)
    reviewed_at: str


def _missing(field: SemanticField) -> list[str]:
    missing = []
    if not (field.meaning or '').strip():
        missing.append('meaning')
    for key in ('kind', 'value_format', 'temporal_kind'):
        if getattr(field, key) == 'unknown':
            missing.append(key)
    if field.grain is None:
        missing.append('grain')
    if field.kind == 'measure':
        if not (field.unit or '').strip():
            missing.append('unit')
        if field.aggregation == 'unknown':
            missing.append('aggregation')
    return missing


def compare_fields(source: SemanticField | dict, target: SemanticField | dict) -> dict:
    source, target = SemanticField.model_validate(source), SemanticField.model_validate(target)
    blockers = ['source_missing:' + k for k in _missing(source)]
    blockers += ['target_missing:' + k for k in _missing(target)]
    if source.kind != 'unknown' and target.kind != 'unknown' and source.kind != target.kind:
        blockers.append('kind_mismatch')
    if source.grain is not None and target.grain is not None and set(source.grain) != set(target.grain):
        blockers.append('grain_mismatch_requires_explicit_rollup_or_crosswalk')
    if source.unit is not None and target.unit is not None and _label(source.unit) != _label(target.unit):
        blockers.append('unit_mismatch_requires_explicit_conversion')
    if source.temporal_kind != 'unknown' and target.temporal_kind != 'unknown':
        if source.temporal_kind != target.temporal_kind or source.period != target.period:
            blockers.append('period_mismatch')
    if source.value_format != 'unknown' and target.value_format != 'unknown':
        compatible_numeric = {source.value_format, target.value_format} <= {'integer', 'decimal'}
        if source.value_format != target.value_format and not compatible_numeric:
            blockers.append('value_format_mismatch')
    if source.aggregation != 'unknown' and target.aggregation != 'unknown' and source.aggregation != target.aggregation:
        blockers.append('aggregation_mismatch')
    reasons = ['business_meaning_requires_human_review']
    if source.meaning and target.meaning and _label(source.meaning) != _label(target.meaning):
        reasons.append('different_definition_text')
    return {'state': 'blocked' if blockers else 'review_required',
            'blockers': blockers, 'review_reasons': reasons}


def suggest_alignment(source: SemanticField | dict, catalog: list[SemanticField | dict],
                      model_candidates: list[ModelCandidate | dict] | None = None,
                      manual_target_ids: list[str] | None = None) -> AlignmentProposal:
    source = SemanticField.model_validate(source)
    targets = [SemanticField.model_validate(t) for t in catalog]
    if len(targets) > 2000 or len({t.id for t in targets}) != len(targets):
        raise ValueError('语义目录过大或标识重复')
    indexed = {t.id: t for t in targets}
    modeled = [ModelCandidate.model_validate(v) for v in (model_candidates or [])]
    if len(modeled) > 40:
        raise ValueError('模型候选数量超过限制')
    if any(v.target_id not in indexed for v in modeled):
        raise ValueError('模型候选引用了目录中不存在的语义')
    manual = set(manual_target_ids or [])
    if len(manual) > 40 or not manual <= indexed.keys():
        raise ValueError('人工候选引用了目录中不存在的语义或超过数量限制')
    reasons = {v.target_id: v.reason for v in modeled}
    source_names = {_label(source.name), *(_label(a) for a in source.aliases)}
    matches = []
    for target in targets:
        origins = []
        if _label(source.name) == _label(target.name):
            origins.append('name')
        elif source_names & {_label(target.name), *(_label(a) for a in target.aliases)}:
            origins.append('alias')
        if target.id in reasons:
            origins.append('model')
        if target.id in manual:
            origins.append('manual')
        if not origins:
            continue
        matches.append(AlignmentCandidate(target=target, origins=origins,
                       model_reason=reasons.get(target.id), **compare_fields(source, target)))
    matches.sort(key=lambda item: (item.state == 'blocked', item.target.id))
    return AlignmentProposal(source=source, candidates=matches,
                             ambiguous=len([m for m in matches if m.state != 'blocked']) > 1)


def review_alignment(proposal: AlignmentProposal | dict, target_id: str,
                     expected_proposal_sha256: str, reviewer: str,
                     decision: Literal['approved', 'rejected'] = 'approved',
                     review_note: str = '') -> ReviewedAlignment:
    proposal = AlignmentProposal.model_validate(proposal)
    if canonical_sha256(proposal) != expected_proposal_sha256:
        raise ValueError('语义建议已变化，请重新核对当前版本')
    candidate = next((c for c in proposal.candidates if c.target.id == target_id), None)
    if candidate is None:
        raise ValueError('目标语义不是当前建议中的候选')
    # Recompute blockers: a client cannot erase them from a submitted proposal.
    comparison = compare_fields(proposal.source, candidate.target)
    if decision == 'approved' and comparison['blockers']:
        raise ValueError('语义上下文缺失或不兼容，不能确认：' + ','.join(comparison['blockers']))
    if not reviewer.strip():
        raise ValueError('必须提供经过鉴权的核对人')
    return ReviewedAlignment(proposal_sha256=expected_proposal_sha256,
        source_id=proposal.source.id, target_id=target_id,
        source_sha256=canonical_sha256(proposal.source),
        target_sha256=canonical_sha256(candidate.target), decision=decision,
        reviewer=reviewer, review_note=review_note, reviewed_at=_now())


def validate_reviewed_alignment(review: ReviewedAlignment | dict,
                                source: SemanticField | dict,
                                target: SemanticField | dict) -> bool:
    review = ReviewedAlignment.model_validate(review)
    source, target = SemanticField.model_validate(source), SemanticField.model_validate(target)
    if review.decision != 'approved':
        raise ValueError('语义匹配尚未通过人工核对')
    if (source.id != review.source_id or target.id != review.target_id
            or canonical_sha256(source) != review.source_sha256
            or canonical_sha256(target) != review.target_sha256):
        raise ValueError('语义定义已变化，旧核对回执失效')
    if compare_fields(source, target)['blockers']:
        raise ValueError('语义不兼容，不能使用该匹配')
    return True


class MappingEntry(StrictModel):
    source: str = Field(max_length=500)
    target: str = Field(max_length=500)


class ValueMapping(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1, strict=True)
    source_field_id: str = Field(min_length=1, max_length=128)
    target_field_id: str = Field(min_length=1, max_length=128)
    entries: list[MappingEntry] = Field(min_length=1, max_length=20000)
    unmapped_policy: Literal['error', 'keep', 'null'] = 'error'
    cardinality: Literal['many_to_one'] = 'many_to_one'

    @model_validator(mode='after')
    def functional_mapping(self):
        sources = [e.source for e in self.entries]
        if len(set(sources)) != len(sources):
            raise ValueError('同一来源值不能对应多条映射；省份到多个办事处需要额外维度或明确分摊规则')
        return self


class ReviewedMapping(StrictModel):
    mapping: ValueMapping
    mapping_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    reviewer: str = Field(min_length=1, max_length=200)
    reviewed_at: str
    decision: Literal['approved'] = 'approved'

    @model_validator(mode='after')
    def unchanged(self):
        if canonical_sha256(self.mapping) != self.mapping_sha256:
            raise ValueError('值映射已变化，旧核对回执失效')
        if not self.reviewer.strip():
            raise ValueError('必须提供经过鉴权的核对人')
        return self


def review_mapping(mapping: ValueMapping | dict, expected_sha256: str,
                   reviewer: str) -> ReviewedMapping:
    mapping = ValueMapping.model_validate(mapping)
    if canonical_sha256(mapping) != expected_sha256:
        raise ValueError('值映射版本已变化，请重新核对')
    return ReviewedMapping(mapping=mapping, mapping_sha256=expected_sha256,
                           reviewer=reviewer, reviewed_at=_now())


def apply_value_mapping(rows: list[dict], field: str, reviewed: ReviewedMapping | dict,
                        output_field: str | None = None,
                        expected_sha256: str | None = None) -> dict:
    """Apply a server-loaded reviewed crosswalk. Never duplicate or drop rows."""
    reviewed = ReviewedMapping.model_validate(reviewed)
    # Existing Pydantic instances may have mutable nested entries.  Recheck even
    # if model_validate returns the same instance without revalidation.
    if canonical_sha256(reviewed.mapping) != reviewed.mapping_sha256:
        raise ValueError('值映射已变化，旧核对回执失效')
    if expected_sha256 is not None and reviewed.mapping_sha256 != expected_sha256:
        raise ValueError('映射摘要与本次固定版本不一致')
    mapping = reviewed.mapping
    lookup = {e.source: e.target for e in mapping.entries}
    output = output_field or field
    if not field or not output:
        raise ValueError('映射字段不能为空')
    result = []
    matched = unmapped = 0
    for row in rows:
        if field not in row:
            raise ValueError('数据缺少映射字段：' + field)
        if output != field and output in row:
            raise ValueError('映射输出字段已存在，拒绝覆盖')
        raw = row[field]
        if not isinstance(raw, str) and raw is not None:
            raise ValueError('枚举映射要求文本值；数字标识请显式按原格式转换，不能隐式丢失前导零')
        if raw in lookup:
            value = lookup[raw]
            matched += 1
        else:
            unmapped += 1
            if mapping.unmapped_policy == 'error':
                raise ValueError('存在未核对的枚举值，映射已停止；未删除或复制任何数据行')
            value = raw if mapping.unmapped_policy == 'keep' else None
        result.append(dict(row, **{output: value}))
    return {'rows': result, 'receipt': {'mapping_id': mapping.id,
            'version': mapping.version, 'mapping_sha256': reviewed.mapping_sha256,
            'input_field': field, 'output_field': output,
            'source_field_id': mapping.source_field_id, 'target_field_id': mapping.target_field_id,
            'input_rows': len(rows), 'output_rows': len(result),
            'matched': matched, 'unmapped': unmapped, 'unmapped_policy': mapping.unmapped_policy,
            'semantics_complete': unmapped == 0}}


class ExtractedFact(StrictModel):
    field: SemanticField
    # Decimal quantities are passed as strings; this module does not round them.
    value: str | int | bool | None
    provenance: list[Provenance] = Field(min_length=1, max_length=20)
    extraction_status: Literal['unverified', 'reviewed'] = 'unverified'


def align_fact(fact: ExtractedFact | dict,
               catalog: list[SemanticField | dict],
               model_candidates: list[ModelCandidate | dict] | None = None) -> dict:
    fact = ExtractedFact.model_validate(fact)
    missing = _missing(fact.field)
    if missing:
        raise ValueError('提取数值缺少语义上下文：' + ','.join(missing))
    if fact.field.value_format in ('integer', 'decimal', 'percent_fraction', 'percent_points'):
        if isinstance(fact.value, bool) or fact.value is None:
            raise ValueError('数值事实必须包含明确数值，不能用布尔或空值替代')
        try:
            value = Decimal(str(fact.value))
        except Exception:
            raise ValueError('数值事实无法按声明格式识别') from None
        if not value.is_finite():
            raise ValueError('数值事实不能包含 NaN 或 Infinity')
        if fact.field.value_format == 'integer' and value != value.to_integral_value():
            raise ValueError('整数事实不能包含小数')
    proposal = suggest_alignment(fact.field, catalog, model_candidates)
    return {'fact': fact.model_dump(mode='json'),
            'fact_sha256': canonical_sha256(fact),
            'proposal': proposal.model_dump(mode='json'),
            'proposal_sha256': canonical_sha256(proposal),
            'ready_for_automatic_consumption': False,
            'policy': 'unstructured_source_and_semantic_match_require_separate_review'}
