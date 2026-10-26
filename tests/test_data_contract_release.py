import json
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.store import get,uid
from agent_service.execution import process_once,enqueue
from agent_service import workspace
from agent_service.codegen import generate
from agent_service.releases import publish,artifact_folder
from agent_service.jobs import get_job,list_jobs

client=TestClient(app);H={'Authorization':'Bearer '+TOKEN}
def test_contract_frozen_and_enforced_before_publish():
    pid=client.post('/api/projects',headers=H,json={'name':'契约验收','request_id':uid()}).json()['id']
    response=client.post('/api/projects/'+pid+'/attachments',headers=H,files={'file':('contract.csv','id,amount\nA,10\nB,20\n'.encode(),'text/csv')},data={'request_id':uid()})
    assert response.status_code==200,response.text
    assert process_once();source=get('source',response.json()['id'])
    workspace.write_file(pid,'data-contract.json',json.dumps({'version':1,'min_rows':2,'max_rows':2,'unique':[['c_1']],'ranges':{'c_2':{'min':0}}}))
    p=generate(source,'契约应用',source['proposed_spec'],project_id=pid)
    run=enqueue('pipeline',p['id'],commit=p['commit']);assert process_once();result=get_job(run['id'])
    assert result.get('validated'),result
    folder=artifact_folder(result)
    report=json.loads((folder/'data-contract-report.json').read_text());assert report['passed']
    frozen=folder/'code'/'data-contract.json';original=frozen.read_text();frozen.unlink()
    import pytest
    with pytest.raises(ValueError,match='契约产物缺失'):publish(p['id'],run['id'])
    frozen.write_text(original);publish(p['id'],run['id'])
    previous=get('pipeline',p['id'])['published_run']
    workspace.write_file(pid,'data-contract.json',json.dumps({'version':1,'min_rows':3}))
    new=generate(source,'契约应用',source['proposed_spec'],existing=get('pipeline',p['id']),project_id=pid)
    bad=enqueue('pipeline',p['id'],commit=new['commit']);assert process_once();failed=get_job(bad['id'])
    assert failed['error_code']=='DATA_CONTRACT_FAILED',failed
    assert get('pipeline',p['id'])['published_run']==previous
