"""Snapshot quotas account for new scoped data without deleting legacy files."""
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import pytest

from agent_service import business_flow_data as flow, data_workflows, connectors
from agent_service.store import uid,seal


def context(actor=None,project=None):
    return {'actor_id':actor or uid(),'project_id':project or 'p_'+uid(),'turn_id':'t_'+uid(),'run_id':uid()}


def save(ctx,char):
    rows=[{'amount':char*600000}]
    return flow._save_rows(ctx,rows,data_workflows.canonical(rows))


def quotas(monkeypatch,actor='1',project='1'):
    monkeypatch.setenv('ATLAS_FLOW_SNAPSHOT_TOTAL_MB','100')
    monkeypatch.setenv('ATLAS_FLOW_SNAPSHOT_ACTOR_MB',actor)
    monkeypatch.setenv('ATLAS_FLOW_SNAPSHOT_PROJECT_MB',project)


def test_quota_is_per_actor_and_project_and_preserves_existing_files(monkeypatch):
    quotas(monkeypatch)
    first=context();saved=save(first,'a')
    size=flow.snapshot_storage_usage(first['project_id'],first['actor_id'])
    assert size['project_bytes']>=600000 and size['actor_bytes']>=size['project_bytes']
    # Same bytes in the same run are idempotent even when a quota is full.
    assert save(first,'a')==saved
    with pytest.raises(ValueError,match='用户'):
        save(context(actor=first['actor_id']),'b')
    assert flow._read_rows(first,saved)[0]['amount']=='a'*600000
    # A separate actor has a separate quota and cannot decrypt the first run.
    different=context();save(different,'b')
    with pytest.raises(ValueError,match='不一致'):
        flow._read_rows(different,saved)


def test_project_quota_is_independent_within_same_actor(monkeypatch):
    quotas(monkeypatch,actor='10',project='1')
    first=context();save(first,'a')
    with pytest.raises(ValueError,match='项目'):
        save(context(actor=first['actor_id'],project=first['project_id']),'b')
    other=context(actor=first['actor_id']);save(other,'b')
    assert flow.snapshot_storage_usage(first['project_id'],first['actor_id'])['actor_bytes']>=1200000


def test_legacy_snapshot_is_not_deleted_and_attributed_when_reused(monkeypatch):
    quotas(monkeypatch,actor='10',project='10')
    ctx=context();ref=save(ctx,'x');folder,_=flow._table_folder(ctx)
    marker=folder/'.owner.json';marker.unlink()
    assert flow.snapshot_storage_usage(ctx['project_id'],ctx['actor_id'])['has_unindexed_legacy']
    assert flow._read_rows(ctx,ref)[0]['amount']=='x'*600000
    assert save(ctx,'x')==ref and marker.exists()
    assert flow.snapshot_storage_usage(ctx['project_id'],ctx['actor_id'])['project_bytes']>=600000


def test_tampered_ownership_index_fails_closed(monkeypatch):
    quotas(monkeypatch,actor='10',project='10')
    ctx=context();save(ctx,'x');folder,_=flow._table_folder(ctx)
    marker=folder/'.owner.json';original=marker.read_bytes();marker.write_bytes(original.replace(b'"version":1',b'"version":2'))
    with pytest.raises(ValueError,match='归属索引损坏'):
        flow.snapshot_storage_usage(ctx['project_id'],ctx['actor_id'])
    marker.write_bytes(original)


def test_digest_streams_exact_canonical_bytes_and_workflow_size_is_exact():
    value={'b':[Decimal('123.450'),date(2026,11,1),'中文',None],'a':{'z':True}}
    assert data_workflows.digest(value)==sha256(data_workflows.canonical(value)).hexdigest()
    schema=data_workflows.TableSchema.model_validate({'meaning':'a','grain':['key'],'columns':[{'name':'key','data_type':'text','meaning':'k'}]})
    table=data_workflows._table(schema,[{'key':'甲'},{'key':'乙'}],data_workflows.Budget(),float('inf'))
    assert table['_bytes']==len(data_workflows.canonical({'schema':schema.model_dump(),'rows':table['rows']}))
    with pytest.raises(data_workflows.WorkflowError,match='byte budget'):
        data_workflows._table(schema,[{'key':'a'*2000}],data_workflows.Budget(max_bytes=1024),float('inf'))


def test_csv_cell_budget_aborts_without_returning_partial_rows(monkeypatch):
    monkeypatch.setattr(connectors,'_CSV_MAX_CELLS',3)
    with pytest.raises(connectors.SourceError) as error:
        connectors._file_rows({'secret_config':seal({}),'name':'wide.csv'},100,Path('wide.csv'),b'a,b\n1,2\n3,4\n')
    assert error.value.code=='SOURCE_CELL_BUDGET'
