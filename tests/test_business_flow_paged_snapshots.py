"""Large snapshots verify complete ciphertext and decrypt only the requested page."""
import pytest
from agent_service import business_flow_data as flow
from agent_service.data_workflows import canonical
from agent_service.config import DATA


def context():return {'project_id':'p_page','actor_id':'owner','turn_id':'turn_page','run_id':'run_page'}


def test_paged_snapshot_slices_while_validating_entire_file(monkeypatch):
    monkeypatch.setattr(flow,'_INLINE_ROWS_LIMIT',1)
    rows=[{'amount':str(i)+'.25','label':'行'+str(i)} for i in range(1100)]
    table=flow._compact_table(context(),{'rows':rows,'kind':'source_table_v1'})
    ref=table['rows_ref'];assert ref['version']==2 and ref['row_count']==1100
    seen=[];original=flow._page_chunk
    def track(*args,**kw):seen.append(args[3]);return original(*args,**kw)
    monkeypatch.setattr(flow,'_page_chunk',track)
    assert flow.get_table_page(context(),table,250,30)==rows[250:280]
    assert seen==[0,1]
    seen.clear();flow.verify_table_snapshot(context(),table);assert not seen
    assert flow.materialize_table(context(),table)['rows']==rows
    with pytest.raises(ValueError):flow.get_table_page({**context(),'actor_id':'other'},table,0,10)
    path=DATA/'business-flow-tables'/ref['scope']/(ref['sha256']+'.enc')
    data=bytearray(path.read_bytes());data[-100]^=1;path.write_bytes(data)
    with pytest.raises(ValueError):flow.get_table_page(context(),table,0,1)


def test_legacy_v1_snapshots_remain_readable():
    ctx={**context(),'run_id':'legacy_page'};rows=[{'x':'精确'+str(i)} for i in range(300)]
    ref=flow._save_rows(ctx,rows,canonical(rows));assert ref['version']==1
    table={'rows_ref':ref}
    assert flow.get_table_page(ctx,table,200,20)==rows[200:220]
    flow.verify_table_snapshot(ctx,table)


def test_invalid_reference_and_mismatched_table_receipt_fail_closed(monkeypatch):
    from agent_service.data_workflows import digest
    monkeypatch.setattr(flow,'_INLINE_ROWS_LIMIT',1)
    ctx={**context(),'run_id':'table_receipt'};rows=[{'amount':'10.00'}]*300
    schema={'columns':[{'name':'amount','data_type':'decimal','meaning':'金额'}]}
    table=flow._compact_table(ctx,{'rows':rows,'schema':schema,'kind':'table_workflow_v1',
        'row_count':len(rows),'output_sha256':digest({'schema':schema,'rows':rows})})
    assert flow.get_table_page(ctx,table,10,5)==rows[10:15]
    with pytest.raises(ValueError):flow.get_table_page(ctx,{**table,'output_sha256':'a'*64},0,5)
    with pytest.raises(ValueError):flow.get_table_page(ctx,{**table,'row_count':299},0,5)
    with pytest.raises(ValueError):flow.verify_table_snapshot(ctx,{**table,'row_count':301})
    with pytest.raises(ValueError):flow.verify_table_snapshot(ctx,{'rows_ref':'not an object'})
    with pytest.raises(ValueError):flow.get_table_page(ctx,{'rows_ref':'not an object'},0,5)
