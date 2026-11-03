"""Snapshot access follows project lifecycle and survives full workspace backup."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
from agent_service.actor_context import actor_scope
from agent_service.store import put, seal, uid, now
from agent_service import business_flow_api_capture as capture, workspace_lifecycle
from agent_service.business_flow_data import table_workflow
from test_project_data import project


def test_api_capture_obeys_actor_and_project_recycle_bin(monkeypatch):
    from agent_service import connectors
    pid=project();tid='t_'+uid();rid='r_'+uid();sid='s_'+uid()
    put('turn',{'project_id':pid,'actor_id':'owner','state':'done','outcome':'needs_input',
        'text':'采集收入','interaction_mode':'work','created_at':now()},tid)
    secret=seal({'url':'https://fixture.invalid/records'})
    put('source',{'project_id':pid,'name':'收入 API','kind':'api','status':'ready','revision':1,'secret_config':secret},sid)
    pin={'source_id':sid,'revision':1,'config_sha256':hashlib.sha256(secret.encode()).hexdigest()}
    rows=[{'c_1':'东部','c_2':'118.15'}];fields=[{'name':'c_1','label':'地区'},{'name':'c_2','label':'金额'}]
    monkeypatch.setattr(connectors,'read_source',lambda source:(rows,fields,{'complete':True},False))
    context={'project_id':pid,'actor_id':'owner','turn_id':tid,'run_id':rid,'node_id':'collect'}
    result=capture.run(context,pin,{})
    plan={'version':1,'name':'收入','inputs':{'table':{'meaning':'收入','grain':['nexus_row'],'columns':[
        {'name':'nexus_row','data_type':'text','meaning':'行号'},{'name':'c_1','data_type':'text','meaning':'地区'},
        {'name':'c_2','data_type':'decimal','meaning':'收入','unit':'CNY','aggregation':'additive'}]}},
        'stages':[{'id':'sum','kind':'aggregate','description':'合计','input':'table','by':[],'meaning':'总收入',
        'aggregations':[{'function':'sum','column':'c_2','output':{'name':'amount','data_type':'decimal','meaning':'合计','unit':'CNY'}}]}],'output':'sum'}
    def process():return table_workflow({**context,'node_id':'sum'},{'plan':plan},{'table':result})
    assert process()['rows'][0]['amount']=='118.15'
    with actor_scope('u_'+'1'*64):
        with pytest.raises(ValueError):process()
    workspace_lifecycle.delete_project(pid)
    with pytest.raises(workspace_lifecycle.WorkspaceDeleted):process()
    with pytest.raises(workspace_lifecycle.WorkspaceDeleted):capture.run(context,pin,{})
    workspace_lifecycle.restore_project(pid)
    assert process()['rows'][0]['amount']=='118.15'


def test_encrypted_api_snapshot_survives_backup_and_restore(tmp_path):
    root=Path(__file__).resolve().parents[1]
    source=tmp_path/'source';target=tmp_path/'restored';archive=tmp_path/'snapshot.nexus'
    environment=dict(os.environ,DATA_DIR=str(source),DATABASE_URL='sqlite:///'+str(source/'metadata.sqlite'),
        PYTHONPATH=str(root),APP_TOKEN='snapshot-fixture-only-access-token')
    environment.pop('NEXUS_LOCAL_MODE',None);environment.pop('NEXUS_LOCAL_WORKSPACE',None)
    script=('import hashlib; from agent_service.store import put,seal; from agent_service.maintenance import backup; '
        'from agent_service.business_flow_api_capture import _bytes,_snapshot_digest; '
        "put('project',{'name':'synthetic'},'project1'); "
        "put('turn',{'project_id':'project1','actor_id':'owner','state':'done','outcome':'needs_input'},'turn1'); "
        "secret=seal({'url':'https://fixture.invalid'}); "
        "put('source',{'project_id':'project1','kind':'api','status':'ready','revision':1,'secret_config':secret},'source1'); "
        "scope={'project_id':'project1','actor_id':'owner','turn_id':'turn1','run_id':'run1','node_id':'read'}; "
        "rows=[{'amount':'118.15'}]; fields=[{'name':'amount','label':'amount'}]; "
        "payload={'scope':scope,'source_id':'source1','revision':1,'config_sha256':hashlib.sha256(secret.encode()).hexdigest(),"
        "'content_hash':hashlib.sha256(_bytes({'rows':rows,'fields':fields})).hexdigest(),'rows':rows,'fields':fields,"
        "'evidence':{'complete':True},'collected_at':'2026-01-01T00:00:00+00:00'}; "
        "payload['snapshot_sha256']=_snapshot_digest(payload); "
        "put('flow_api_snapshot',{'project_id':'project1','actor_id':'owner','status':'complete','secret_payload':seal(payload)},'fas_fixture'); "
        f'backup({str(archive)!r})')
    made=subprocess.run([sys.executable,'-c',script],env=environment,cwd=root,capture_output=True,timeout=30)
    assert made.returncode==0,made.stderr.decode('utf-8',errors='backslashreplace')
    with zipfile.ZipFile(archive) as content:
        metadata=content.read('metadata.jsonl').decode('utf-8')
    assert 'flow_api_snapshot' in metadata and '118.15' not in metadata
    environment.update(DATA_DIR=str(target),DATABASE_URL='sqlite:///'+str(target/'metadata.sqlite'))
    restored=subprocess.run([sys.executable,'-m','agent_service.maintenance','restore',str(archive)],env=environment,cwd=root,capture_output=True,timeout=30)
    assert restored.returncode==0,restored.stderr.decode('utf-8',errors='backslashreplace')
    check=("import json; from agent_service.store import get,unseal; from agent_service.business_flow_api_capture import verify; "
        "p=unseal(get('flow_api_snapshot','fas_fixture')['secret_payload']); "
        "ref={'snapshot_id':'fas_fixture','capture_node_id':'read','source_id':'source1','revision':1,"
        "'content_hash':p['content_hash'],'snapshot_sha256':p['snapshot_sha256']}; "
        "print(json.dumps(verify(p['scope'],ref)['rows']))")
    read=subprocess.run([sys.executable,'-c',check],env=environment,cwd=root,capture_output=True,timeout=30)
    assert read.returncode==0,read.stderr.decode('utf-8',errors='backslashreplace')
    assert json.loads(read.stdout)==[{'amount':'118.15'}]
