"""Slow source probes cannot put an old source over a newer version."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import json
import os
import subprocess
import sys

import pytest

from agent_service import execution,planner,sources
from agent_service.connectors import SourceError
from agent_service.errors import TaskError
from agent_service.store import get,patch,put,uid


def source():
    return put('source',{'project_id':'p_'+uid(),'kind':'file','name':'input.csv','status':'queued',
                         'revision':1,'revision_id':'old-version','content_hash':'a'*64,
                         'secret_config':'old-encrypted-config','profile':{'fields':[]}},'s_'+uid())


def test_probe_rejects_concurrent_source_replacement_without_losing_new_fields(monkeypatch):
    saved=source();before=Event();release=Event();original=sources.patch_probe_if_current
    def paused(*args,**kwargs):
        before.set();assert release.wait(5)
        return original(*args,**kwargs)
    monkeypatch.setattr(sources,'patch_probe_if_current',paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        work=pool.submit(planner.persist_node,{'source':saved,'profile':{'fields':[],'sample_rows':1}})
        assert before.wait(5)
        patch('source',saved['id'],revision=2,revision_id='new-version',content_hash='b'*64,
              secret_config='new-encrypted-config',status='queued',extra_metadata='newer')
        release.set()
        with pytest.raises(SourceError) as caught:work.result(timeout=5)
    assert caught.value.code=='SOURCE_VERSION_CHANGED'
    current=get('source',saved['id'])
    assert current['revision']==2 and current['content_hash']=='b'*64
    assert current['secret_config']=='new-encrypted-config' and current['status']=='queued'
    assert current['extra_metadata']=='newer' and current['profile']=={'fields':[]}


def test_probe_updates_only_its_fields_preserving_unrelated_new_metadata():
    saved=source();patch('source',saved['id'],extra_metadata='concurrently added')
    planner.persist_node({'source':saved,'profile':{'fields':[],'sample_rows':2},'suggestions':[],'planner_mode':'rules'})
    current=get('source',saved['id'])
    assert current['status']=='ready' and current['profile']['sample_rows']==2
    assert current['extra_metadata']=='concurrently added'
    assert current['revision']==1 and current['content_hash']=='a'*64


def test_probe_subprocess_rejects_source_changed_before_connector_read(tmp_path):
    saved=source();fingerprint=sources.probe_fingerprint(saved)
    patch('source',saved['id'],revision=2,revision_id='new-version',content_hash='b'*64,status='queued')
    error=tmp_path/'probe-error.json';env=dict(os.environ,NEXUS_PROBE_INPUT_SHA256=fingerprint)
    # The dummy encrypted config is deliberately invalid: the explicit version
    # error proves the connector was not allowed to read the newer source.
    result=subprocess.run([sys.executable,'-m','agent_service.probe',saved['id'],str(error)],
                          env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==3 and json.loads(error.read_text())['code']=='SOURCE_VERSION_CHANGED'
    assert get('source',saved['id'])['revision']==2


@pytest.mark.parametrize('code',['SOURCE_VERSION_CHANGED','SOURCE_FAILED'])
def test_stale_probe_failure_does_not_mark_new_source_failed(monkeypatch,tmp_path,code):
    saved=source()
    run={'id':'probe_'+uid(),'target':saved['id'],'kind':'probe','commit':None,'scheduled_key':None,
         'lease_token':'fixture','attempts':1,'max_attempts':1}
    monkeypatch.setattr(execution,'DATA',tmp_path)
    monkeypatch.setattr(execution,'get_job',lambda _: {'lease_token':'fixture'})
    monkeypatch.setattr(execution,'patch_run',lambda obj,**values:obj.update(values))
    monkeypatch.setattr(execution,'checkpoint',lambda _:None)
    def invoke(*args,**kwargs):
        patch('source',saved['id'],revision=2,revision_id='new-version',content_hash='b'*64,status='queued')
        raise TaskError('探查未完成',code)
    monkeypatch.setattr(execution,'invoke',invoke)
    monkeypatch.setattr(execution,'record',lambda *args,**kwargs:None)
    execution.execute(run)
    assert run['status']=='failed' and run['error_code']==code
    current=get('source',saved['id'])
    assert current['status']=='queued' and current['revision']==2 and 'error_code' not in current


def test_current_probe_failure_still_marks_source_failed(monkeypatch,tmp_path):
    saved=source()
    run={'id':'probe_'+uid(),'target':saved['id'],'kind':'probe','commit':None,'scheduled_key':None,
         'lease_token':'fixture','attempts':1,'max_attempts':1}
    monkeypatch.setattr(execution,'DATA',tmp_path)
    monkeypatch.setattr(execution,'get_job',lambda _: {'lease_token':'fixture'})
    monkeypatch.setattr(execution,'patch_run',lambda obj,**values:obj.update(values))
    monkeypatch.setattr(execution,'checkpoint',lambda _:None)
    monkeypatch.setattr(execution,'invoke',lambda *args,**kwargs: (_ for _ in ()).throw(TaskError('文件不可读','SOURCE_FAILED')))
    monkeypatch.setattr(execution,'record',lambda *args,**kwargs:None)
    execution.execute(run)
    assert run['status']=='failed' and run['error_code']=='SOURCE_FAILED'
    current=get('source',saved['id'])
    assert current['status']=='failed' and current['error_code']=='SOURCE_FAILED'
