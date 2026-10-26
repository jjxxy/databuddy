import copy

import pytest
from pydantic import ValidationError

from agent_service.semantic_alignment import (
    SemanticField, ValueMapping, ReviewedMapping, align_fact, apply_value_mapping,
    canonical_sha256, compare_fields, review_alignment, review_mapping,
    suggest_alignment, validate_reviewed_alignment,
)


def measure(identifier='revenue', **extra):
    return SemanticField(id=identifier, name='净收入', meaning='已付款订单金额扣除退款',
        kind='measure', grain=['office', 'month'], unit='CNY', value_format='decimal',
        temporal_kind='interval', period='2026-10', aggregation='sum', **extra)


def mapping(**extra):
    return ValueMapping(id='province-office', version=1, source_field_id='province',
        target_field_id='office', entries=[{'source':'安徽','target':'合肥办事处'},
                                        {'source':'江苏','target':'南京办事处'},
                                        {'source':'浙江','target':'南京办事处'}], **extra)


def test_equal_names_never_auto_confirm_semantics():
    source=measure('uploaded'); target=measure()
    proposal=suggest_alignment(source,[target])
    assert proposal.state=='unreviewed' and not proposal.ambiguous
    assert proposal.candidates[0].state=='review_required'
    receipt=review_alignment(proposal,target.id,canonical_sha256(proposal),'owner')
    assert validate_reviewed_alignment(receipt,source,target)


@pytest.mark.parametrize('change,reason',[
    ({'unit':'万元'},'unit_mismatch_requires_explicit_conversion'),
    ({'period':'2026-09'},'period_mismatch'),
    ({'grain':['province','month']},'grain_mismatch_requires_explicit_rollup_or_crosswalk'),
    ({'value_format':'percent_fraction'},'value_format_mismatch'),
    ({'aggregation':'ratio'},'aggregation_mismatch'),
])
def test_incompatible_context_blocks_even_after_model_suggestion(change,reason):
    source=measure('uploaded'); target=measure().model_copy(update=change)
    proposal=suggest_alignment(source,[target],model_candidates=[{'target_id':target.id,'reason':'模型认为字段同名'}])
    assert reason in proposal.candidates[0].blockers
    with pytest.raises(ValueError,match='不能确认'):
        review_alignment(proposal,target.id,canonical_sha256(proposal),'owner')


def test_percentage_fraction_and_points_are_not_implicitly_converted():
    source=measure('uploaded').model_copy(update={'unit':'%','value_format':'percent_fraction'})
    target=measure().model_copy(update={'unit':'%','value_format':'percent_points'})
    assert 'value_format_mismatch' in compare_fields(source,target)['blockers']


def test_alias_ambiguity_requires_explicit_target_and_definition_review():
    source=measure('uploaded').model_copy(update={'name':'收入'})
    net=measure('net',aliases=['收入'])
    gross=measure('gross',aliases=['收入']).model_copy(update={'meaning':'订单原始金额，不扣退款'})
    proposal=suggest_alignment(source,[net,gross])
    assert proposal.ambiguous and len(proposal.candidates)==2
    assert all(c.state=='review_required' for c in proposal.candidates)
    review=review_alignment(proposal,'net',canonical_sha256(proposal),'owner')
    assert validate_reviewed_alignment(review,source,net)


def test_same_label_without_context_cannot_be_reviewed_as_match():
    source=SemanticField(id='unknown',name='净收入')
    proposal=suggest_alignment(source,[measure()])
    assert proposal.candidates[0].state=='blocked'
    with pytest.raises(ValueError):review_alignment(proposal,'revenue',canonical_sha256(proposal),'owner')


def test_model_cannot_invent_catalog_ids_or_clear_blockers():
    with pytest.raises(ValueError,match='不存在'):
        suggest_alignment(measure('source'),[measure()],model_candidates=[{'target_id':'invented','reason':'test'}])
    proposal=suggest_alignment(measure('source'),[measure().model_copy(update={'unit':'USD'})]).model_dump()
    proposal['candidates'][0].update(blockers=[],state='review_required')
    with pytest.raises(ValueError):review_alignment(proposal,'revenue',canonical_sha256(proposal),'owner')


def test_editing_definition_or_review_proposal_invalidates_old_receipt():
    source=measure('source'); target=measure()
    proposal=suggest_alignment(source,[target]); sha=canonical_sha256(proposal)
    review=review_alignment(proposal,'revenue',sha,'owner')
    changed=target.model_copy(update={'meaning':'新的口径'})
    with pytest.raises(ValueError,match='旧核对回执失效'):
        validate_reviewed_alignment(review,source,changed)
    proposal.source.meaning='新的原始解释'
    with pytest.raises(ValueError,match='建议已变化'):
        review_alignment(proposal,'revenue',sha,'owner')


def test_many_to_one_crosswalk_preserves_all_rows():
    proposed=mapping(); reviewed=review_mapping(proposed,canonical_sha256(proposed),'owner')
    rows=[{'province':'安徽','amount':'1.23'},{'province':'江苏','amount':'2'},{'province':'浙江','amount':'3'}]
    value=apply_value_mapping(rows,'province',reviewed,output_field='office')
    assert len(value['rows'])==3 and value['rows'][0]['office']=='合肥办事处'
    assert value['rows'][1]['office']==value['rows'][2]['office']=='南京办事处'
    assert value['receipt']['input_rows']==value['receipt']['output_rows']==3
    assert value['receipt']['semantics_complete'] and 'office' not in rows[0]


def test_province_to_multiple_offices_is_not_a_functional_mapping():
    bad=mapping().model_dump()
    bad['entries'].append({'source':'安徽','target':'芜湖办事处'})
    with pytest.raises(ValueError,match='多个办事处'):
        ValueMapping.model_validate(bad)


def test_unmapped_values_fail_closed_and_never_partially_modify_input():
    proposed=mapping(); reviewed=review_mapping(proposed,canonical_sha256(proposed),'owner')
    rows=[{'province':'安徽'},{'province':'未知省份'}]; before=copy.deepcopy(rows)
    with pytest.raises(ValueError,match='未核对的枚举值'):
        apply_value_mapping(rows,'province',reviewed)
    assert rows==before


@pytest.mark.parametrize('policy,expected',[('keep','未知'),('null',None)])
def test_explicit_unmapped_policy_is_reported_as_incomplete(policy,expected):
    proposed=mapping(unmapped_policy=policy)
    reviewed=review_mapping(proposed,canonical_sha256(proposed),'owner')
    result=apply_value_mapping([{'province':'未知'}],'province',reviewed)
    assert result['rows'][0]['province']==expected
    assert result['receipt']['unmapped']==1 and not result['receipt']['semantics_complete']


def test_crosswalk_revisions_require_new_review_and_do_not_overwrite_fields():
    proposed=mapping(); reviewed=review_mapping(proposed,canonical_sha256(proposed),'owner')
    changed=reviewed.model_dump(); changed['mapping']['version']=2
    with pytest.raises(ValueError,match='旧核对回执失效'):
        ReviewedMapping.model_validate(changed)
    with pytest.raises(ValueError,match='固定版本'):
        apply_value_mapping([{'province':'安徽'}],'province',reviewed,expected_sha256='0'*64)
    with pytest.raises(ValueError,match='拒绝覆盖'):
        apply_value_mapping([{'province':'安徽','office':'old'}],'province',reviewed,output_field='office')


def test_in_place_mapping_change_invalidates_existing_model_instance():
    proposed=mapping(); reviewed=review_mapping(proposed,canonical_sha256(proposed),'owner')
    reviewed.mapping.entries[0].target='new office'
    with pytest.raises(ValueError,match='旧核对回执失效'):
        apply_value_mapping([{'province':'安徽'}],'province',reviewed)


def test_numeric_enum_ids_cannot_silently_lose_leading_zeroes():
    proposed=mapping(); reviewed=review_mapping(proposed,canonical_sha256(proposed),'owner')
    with pytest.raises(ValueError,match='前导零'):
        apply_value_mapping([{'province':1}],'province',reviewed)


def fact(**updates):
    value={'field':measure('extracted').model_dump(),'value':'123.456789012',
           'provenance':[{'source_id':'report','locator':'page 3 table 2 row 4 amount',
                          'content_sha256':'a'*64,'quote':'10月合肥办事处净收入123.456789012元'}]}
    value.update(updates);return value


def test_unstructured_fact_keeps_value_context_and_provenance_unverified():
    aligned=align_fact(fact(),[measure()])
    assert aligned['fact']['value']=='123.456789012'
    assert aligned['fact']['provenance'][0]['locator'].startswith('page 3')
    assert not aligned['ready_for_automatic_consumption']
    assert aligned['proposal']['state']=='unreviewed'


def test_unstructured_facts_require_context_provenance_and_finite_quantities():
    missing=fact();missing['field']['meaning']=None
    with pytest.raises(ValueError,match='上下文'):
        align_fact(missing,[measure()])
    with pytest.raises(ValidationError):align_fact(fact(provenance=[]),[measure()])
    for value in ('NaN','Infinity','not a number',True,None):
        with pytest.raises(ValueError):align_fact(fact(value=value),[measure()])


def test_schema_rejects_unknown_fields_and_noninteger_versions():
    with pytest.raises(ValidationError):SemanticField(id='x',name='x',approve=True)
    bad=mapping().model_dump();bad['version']=True
    with pytest.raises(ValidationError):ValueMapping.model_validate(bad)


@pytest.mark.parametrize('period',['上月','本月','2026-13','2026-10-31/2026-10-01'])
def test_periods_are_fixed_to_real_context_not_relative_labels(period):
    value=measure().model_dump();value['period']=period
    with pytest.raises(ValidationError):SemanticField.model_validate(value)
