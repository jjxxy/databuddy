"""Snapshot temporary files remain private and are cleaned on failures."""
import os
import pytest
from agent_service import business_flow_data as flow, platform_files
from agent_service.data_workflows import canonical


def test_owner_temp_closes_and_cleans_when_protection_fails(tmp_path,monkeypatch):
    def fail(*args):raise RuntimeError('protection failed')
    monkeypatch.setattr(platform_files,'chmod_descriptor',fail)
    with pytest.raises(RuntimeError,match='protection failed'):
        flow._write_owner(tmp_path/'.owner.json',{'owner':'test'})
    assert list(tmp_path.iterdir())==[]


def test_owner_temp_closes_when_fdopen_fails(tmp_path,monkeypatch):
    opened=[];original=flow.tempfile.mkstemp
    def capture(*a,**kw):
        result=original(*a,**kw);opened.append(result[0]);return result
    def fail(*args,**kwargs):raise RuntimeError('fdopen failed')
    monkeypatch.setattr(flow.tempfile,'mkstemp',capture)
    monkeypatch.setattr(flow.os,'fdopen',fail)
    with pytest.raises(RuntimeError,match='fdopen failed'):
        flow._write_owner(tmp_path/'.owner.json',{'owner':'test'})
    with pytest.raises(OSError):os.fstat(opened[0])
    assert list(tmp_path.iterdir())==[]


@pytest.mark.parametrize('paged',[False,True])
def test_encrypted_snapshot_failure_cleans_temp_and_does_not_publish(monkeypatch,paged):
    context={'project_id':'protection','actor_id':'owner','turn_id':'test','run_id':'paged' if paged else 'legacy'}
    rows=[{'amount':'12.34'}];raw=canonical(rows);folder,_=flow._table_folder(context)
    original=platform_files.chmod_descriptor;calls=[]
    def fail_snapshot(fd,mode):
        calls.append(fd)
        if len(calls)==2:raise RuntimeError('protection failed')
        return original(fd,mode)
    monkeypatch.setattr(platform_files,'chmod_descriptor',fail_snapshot)
    with pytest.raises(RuntimeError,match='protection failed'):
        (flow._save_rows_paged if paged else flow._save_rows)(context,rows,raw)
    assert (folder/'.owner.json').is_file()
    assert not list(folder.glob('.pending-*')) and not list(folder.glob('*.enc'))
