import json

from agent_service.finance_agent_guidance import data_evidence_context, guidance_for_turn


def read(run='run',sha='a'*64,offset=0,total=100,rows=25,**other):
    return {'tool':'read_data_workflow_result','status':'done','observation':{
        'run_id':run,'manifest_sha256':sha,'offset':offset,'total':total,
        'rows':[{'private_customer':'DO-NOT-COPY','amount':'999999999999999.123456789012'}]*rows,**other}}


def test_first_page_is_not_whole_result_even_if_flag_claims_complete():
    turn={'agent_trace':[read(page_is_complete_result=True)]}
    result=data_evidence_context(turn,allow_values=True)['results'][0]
    assert result['covered_rows']==25 and not result['full_result_read']
    assert result['whole_result_summary_requires_new_aggregation']


def test_complete_nonoverlapping_pages_can_ground_scope_without_copying_values():
    turn={'agent_trace':[read(offset=i) for i in (0,25,50,75)]}
    scope=data_evidence_context(turn,allow_values=True)
    assert scope['results'][0]['full_result_read'] and scope['results'][0]['covered_rows']==100
    assert 'DO-NOT-COPY' not in json.dumps(scope) and '999999999999999' not in json.dumps(scope)


def test_duplicate_and_overlapping_pages_do_not_inflate_coverage():
    turn={'agent_trace':[read(offset=0),read(offset=0),read(offset=10)]}
    result=data_evidence_context(turn,allow_values=True)['results'][0]
    assert result['covered_rows']==35 and not result['full_result_read']


def test_different_versions_or_runs_cannot_be_combined_into_full_result():
    turn={'agent_trace':[read(offset=0,rows=50),read(offset=50,rows=50,sha='b'*64),
                         read(offset=50,rows=50,run='other')]}
    scope=data_evidence_context(turn,allow_values=True)
    assert len(scope['results'])==3 and all(not r['full_result_read'] for r in scope['results'])


def test_failed_calls_redacted_rows_and_inconsistent_totals_do_not_ground_summary():
    failed=read(offset=0,rows=100);failed['status']='failed'
    redacted=read();redacted['observation']['rows']='content not shared'
    assert data_evidence_context({'agent_trace':[failed,redacted]})['results']==[]
    inconsistent={'agent_trace':[read(offset=0,rows=50),read(offset=50,rows=50,total=101)]}
    result=data_evidence_context(inconsistent,allow_values=True)['results'][0]
    assert result['inconsistent_total'] and not result['full_result_read']


def test_disabled_content_permission_cannot_be_overridden_by_old_trace_rows():
    turn={'agent_trace':[read(rows=100)]}
    result=data_evidence_context(turn,allow_values=False)['results'][0]
    assert result['full_result_read'] and not result['model_may_use_values']
    assert result['whole_result_summary_requires_new_aggregation']


def test_empty_verified_result_is_distinct_from_missing_results():
    assert data_evidence_context({'agent_trace':[]})['results']==[]
    empty=data_evidence_context({'agent_trace':[read(rows=0,total=0)]},allow_values=True)['results'][0]
    assert empty['full_result_read'] and empty['total_rows']==empty['covered_rows']==0


def test_data_guidance_continues_original_request_and_does_not_pollute_generic_writing():
    assert guidance_for_turn({'text':'继续','work_request':'按办事处核对收入金额'})
    assert guidance_for_turn({'text':'开始','source_id':'attached-source'})
    assert not guidance_for_turn({'text':'帮我给朋友写一句生日祝福'})
