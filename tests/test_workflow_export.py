import io,json,zipfile
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.store import put,uid,now
from agent_service import project_data as data,business_flows,workspace
from test_project_data import project,source,document,registered


def archive(response):
    assert response.status_code==200,response.text
    assert response.headers['content-type'].startswith('application/zip')
    return zipfile.ZipFile(io.BytesIO(response.content))


def test_data_workflow_download_is_scoped_and_exact():
    pid=project();s=source(pid);wid=registered(pid,document(s['id']));expected=data.inspect_workflow(pid,wid)['draft_sha256'];c=TestClient(app);h={'Authorization':'Bearer '+TOKEN}
    base=f'/api/projects/{pid}/data-workflows/{wid}/download';r=c.get(base,params={'expected_digest':expected},headers=h);z=archive(r)
    assert set(z.namelist())=={'README.txt','workflow.json','run.py'}
    assert '不是可独立' in z.read('README.txt').decode()
    assert b'NEXUS_TOKEN' in z.read('run.py') and TOKEN.encode() not in z.read('run.py')
    assert s['secret_config'].encode() not in b''.join(z.read(n) for n in z.namelist())
    assert c.get(base,params={'expected_digest':'0'*64},headers=h).status_code==409
    other=project();assert c.get(f'/api/projects/{other}/data-workflows/{wid}/download',params={'expected_digest':expected},headers=h).status_code>=400


def test_business_workflow_download_reads_locked_git_version_and_actor():
    pid=project();s=source(pid);tid='t_'+uid();put('turn',{'project_id':pid,'actor_id':'owner','state':'done','text':'创建流程','interaction_mode':'work','created_at':now()},tid)
    doc={'name':'收入读取','inputs':[],'nodes':[{'id':'read','kind':'source_table','title':'读取','config':{'source_id':s['id'],'revision':s['revision'],'content_hash':s['content_hash']}}],'outputs':['read']}
    f=business_flows.create(pid,'owner',tid,doc,uid());url=f"/api/projects/{pid}/business-flows/{f['id']}/download";c=TestClient(app);h={'Authorization':'Bearer '+TOKEN}
    z=archive(c.get(url,params={'expected_digest':f['candidate_digest']},headers=h));assert set(z.namelist())=={'README.txt','workflow.json','runtime-lock.json','run.py'}
    assert TOKEN.encode() not in b''.join(z.read(n) for n in z.namelist())
    assert json.loads(z.read('workflow.json'))['name']=='收入读取'
    assert c.get(url,params={'expected_digest':'0'*64},headers=h).status_code==409
    other=project();assert c.get(f"/api/projects/{other}/business-flows/{f['id']}/download",params={'expected_digest':f['candidate_digest']},headers=h).status_code>=400
    from agent_service.workflow_export import business_bundle
    import pytest
    with pytest.raises(ValueError):business_bundle(pid,f['id'],f['candidate_digest'],'u_'+'a'*64)
