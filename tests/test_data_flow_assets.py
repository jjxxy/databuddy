import copy
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from agent_service import data_flow_assets, project_data as data, workspace
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.store import now,patch,put,uid
from test_project_data import project, source, document, registered, run


def test_data_code_round_trip_and_restore():
    pid=project();s=source(pid);doc=document(s['id'])
    stage={'id':'SelectOrders','kind':'sql','description':'读取完整订单','inputs':['complete_rows'],
           'sql':'SELECT * FROM complete_rows','output_schema':doc['plan']['inputs']['orders']}
    doc['plan']['stages'].append(stage);doc['plan']['output']=stage['id']
    wid=registered(pid,doc);digest=data.inspect_workflow(pid,wid)['draft_sha256']
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    base=f'/api/projects/{pid}/data-workflows/{wid}'
    params={'expected_digest':digest}
    assert client.get(base+'/code-assets',params=params).status_code in (401,403)
    view=client.get(base+'/code-assets',params=params,headers=headers).json()
    assert view['storage']=='git' and view['flow_id']==wid
    groups=view['groups'];references=[ref for group in groups for ref in group['nodeIds']]
    assert references==['orders','complete_rows','SelectOrders','__overview_result__']
    assert groups == view['display_groups']
    assert groups[1]['nodeIds'] == ['complete_rows','SelectOrders']
    definition=next(n for n in view['nodes'] if n['id']=='SelectOrders')
    def content(path):
        return client.get(base+'/code-asset',params={**params,'commit':view['commit'],'path':path},headers=headers)
    assert content(definition['paths'][0]).json()['content']==stage['sql']
    assert 'def _sql(' in content(definition['handler_path']).json()['content']
    assert any('/data_workflows/validate_sql.py' in f for f in definition['paths'])
    assert json.loads(content(definition['path']).json()['content'])['inputs']==['complete_rows']
    assert content('../../credentials.json').status_code==404
    assert client.get(base.replace(pid,project())+'/code-assets',params=params,headers=headers).status_code>=400
    # Editing the existing workflow removes its obsolete files and creates a
    # new version. A late browser click cannot mix the two definitions.
    revised=copy.deepcopy(doc);revised['plan']['stages'].pop();revised['plan']['output']='complete_rows'
    edit=client.put(base,json={'document':revised,'expected_sha256':digest},headers=headers)
    assert edit.status_code==200,edit.text
    assert content(definition['path']).status_code==409
    assert not workspace.path_for(pid,'data-flows/'+wid+'/'+definition['paths'][0]).exists()
    saved=run(pid,wid)
    assert saved['records']==215
    workspace.restore(pid,view['commit'],'restore-data-flow-code')
    assert content(definition['paths'][0]).json()['content']==stage['sql']


def test_historical_type_based_groups_are_projected_without_changing_the_saved_overview(monkeypatch):
    pid=project();s=source(pid);doc=document(s['id'])
    doc['plan']['stages'].append({'id':'sort_rows','kind':'sql','description':'按编号整理明细',
        'inputs':['complete_rows'],'sql':'SELECT * FROM complete_rows ORDER BY id',
        'output_schema':doc['plan']['inputs']['orders']})
    doc['plan']['output']='sort_rows'
    phases={kind:(kind,kind) for kind in ('source','select','sql','output')}
    with monkeypatch.context() as old:
        old.setattr(data_flow_assets,'_display_groups',lambda nodes:data_flow_assets.overview_groups(nodes,phases))
        wid=registered(pid,doc)
    sha=data.inspect_workflow(pid,wid)['draft_sha256']
    manifest=data_flow_assets.get_assets(pid,wid,sha)
    assert len(manifest['groups'])==4
    assert [g['nodeIds'] for g in manifest['display_groups']]==[['orders'],['complete_rows','sort_rows'],['__overview_result__']]
    frozen=data_flow_assets.get_assets(pid,wid,sha,manifest['commit'],'overview.json')
    assert json.loads(frozen['content'])['groups']==manifest['groups']


def test_selected_chat_updates_same_project_code_and_rejects_stale_edit():
    pid=project();s=source(pid);doc=document(s['id']);wid=registered(pid,doc)
    path='workflows/orders.json';original=data.inspect_workflow(pid,wid)['draft_sha256']
    selected={'kind':'data','id':wid,'digest':original}
    first,second=[put('turn',{'project_id':pid,'actor_id':'owner','state':'done','created_at':now(),
                             'text':'修改加工规则','selected_workflow':selected},'t_'+uid()) for _ in range(2)]
    doc['plan']['stages'][0]['description']='保留核对字段'
    content=json.dumps(doc,ensure_ascii=False)
    receipt=data.write_chat_file(pid,path,content,first)
    assert receipt['workflow_id']==wid and workspace.read_file(pid,path)==content
    index=data_flow_assets.get_assets(pid,wid,receipt['digest'])
    stage=next(n for n in index['nodes'] if n['id']=='complete_rows')
    assert json.loads(data_flow_assets.get_assets(pid,wid,receipt['digest'],index['commit'],stage['path'])['content'])['description']=='保留核对字段'
    doc['plan']['stages'][0]['description']='核对收入明细'
    with pytest.raises(HTTPException) as stale:data.write_chat_file(pid,path,json.dumps(doc),second)
    assert stale.value.status_code==409 and workspace.read_file(pid,path)==content
    assert data.write_chat_file(pid,path,json.dumps(doc),first)['digest']!=receipt['digest']
    invalid=copy.deepcopy(doc);invalid['bindings']['orders']['source_id']='missing_source'
    with pytest.raises(KeyError):data.write_chat_file(pid,path,json.dumps(invalid),first)
    assert workspace.read_file(pid,path)==json.dumps(doc)


def test_old_data_definition_shows_only_recoverable_rules():
    pid=project();s=source(pid);wid=registered(pid,document(s['id']))
    sha=data.inspect_workflow(pid,wid)['draft_sha256']
    patch('data_workflow',wid,code_asset_hashes=None)
    view=data_flow_assets.get_assets(pid,wid,sha)
    assert view['storage']=='derived' and view['commit'] is None
    assert not any(f['path'].endswith('.py') for f in view['files'])
    assert json.loads(data_flow_assets.get_assets(pid,wid,sha,path='io.json')['content'])['inputs']['orders']['source_id']==s['id']


def test_asset_replacement_rejects_direct_writes_and_rolls_back(monkeypatch):
    pid=project();prefix='data-flows/wf_'+'a'*40
    workspace.write_flow_assets(pid,prefix,{'dag.json':'original','nodes/n_old.json':'old'})
    for change in (lambda:workspace.write_file(pid,prefix+'/dag.json','x'),lambda:workspace.delete_file(pid,prefix+'/dag.json')):
        with pytest.raises(ValueError,match='随流程版本生成'):change()
    write=workspace._write_text
    def fail(project_id,path,value):
        if path.endswith('/n_fail.json'):raise OSError('disk full')
        return write(project_id,path,value)
    monkeypatch.setattr(workspace,'_write_text',fail)
    with pytest.raises(OSError,match='disk full'):
        workspace.write_flow_assets(pid,prefix,{'dag.json':'new','nodes/n_fail.json':'new'})
    assert workspace.read_file(pid,prefix+'/dag.json')=='original'
    assert workspace.read_file(pid,prefix+'/nodes/n_old.json')=='old'


def test_tampered_committed_data_node_cannot_be_shown_or_exported():
    pid=project();s=source(pid);wid=registered(pid,document(s['id']))
    sha=data.inspect_workflow(pid,wid)['draft_sha256']
    view=data_flow_assets.get_assets(pid,wid,sha)
    file=view['nodes'][0]['path']
    workspace.path_for(pid,'data-flows/'+wid+'/'+file).write_text('wrong',encoding='utf-8')
    workspace.checkpoint(pid,reason='simulate external file edit')
    client=TestClient(app);base=f'/api/projects/{pid}/data-workflows/{wid}'
    for suffix in ('/code-assets','/download'):
        assert client.get(base+suffix,params={'expected_digest':sha},headers={'Authorization':'Bearer '+TOKEN}).status_code==409
