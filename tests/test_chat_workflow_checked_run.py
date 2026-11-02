from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service import project_data as data,workspace
from agent_service.execution import process_once
from agent_service.jobs import get_job
from agent_service.store import get
from test_project_data import project, source, document, registered
import json

def test_checked_run_freezes_confirmed_plan_and_idempotency():
 client=TestClient(app);h={'Authorization':'Bearer '+TOKEN};pid=project();src=source(pid);doc=document(src['id']);wid=registered(pid,doc);url=f'/api/projects/{pid}/data-workflows/{wid}/run-checked';sha=data.inspect_workflow(pid,wid)['draft_sha256'];body={'expected_draft_sha256':sha,'request_id':'valid_request_123456789'}
 denied=client.post(url,headers=h,json=dict(body,expected_draft_sha256='f'*64));assert denied.status_code==400
 first=client.post(url,headers=h,json=body);assert first.status_code==200,first.text;run=first.json();assert run['commit']==sha
 again=client.post(url,headers=h,json=body);assert again.status_code==200 and again.json()['id']==run['id']
 other=client.post(url,headers=h,json=dict(body,request_id='other_request_123456789'));assert other.status_code==409
 new=dict(doc);new['plan']=dict(doc['plan'],name='Different plan');workspace.write_file(pid,'workflows/orders.json',json.dumps(new));data.register(pid,'workflows/orders.json')
 stale=client.post(url,headers=h,json=body);assert stale.status_code==400
 for _ in range(50):
  if get_job(run['id'])['status'] not in ('queued','running','retrying'):break
  assert process_once()
 done=get_job(run['id']);assert done['status']=='validated' and done['commit']==sha and done['records']==215
 assert get('data_workflow',wid)['published_run'] is None
 bad=project();assert client.post(f'/api/projects/{bad}/data-workflows/{wid}/run-checked',headers=h,json=body).status_code in (400,404)


def test_checked_manual_rerun_never_replaces_adopted_result():
 from test_project_data import run
 client=TestClient(app);h={'Authorization':'Bearer '+TOKEN};pid=project();src=source(pid);wid=registered(pid,document(src['id']));old=run(pid,wid);data.publish_result(pid,old['id'],old['commit'])
 body={'expected_draft_sha256':old['commit'],'request_id':'rerun_request_123456789'}
 response=client.post(f'/api/projects/{pid}/data-workflows/{wid}/run-checked',headers=h,json=body);assert response.status_code==200,response.text
 rid=response.json()['id']
 for _ in range(50):
  if get_job(rid)['status'] not in ('queued','running','retrying'):break
  assert process_once()
 job=get_job(rid);w=get('data_workflow',wid)
 assert job['status']=='validated',job
 assert w['published_run']==old['id'] and w['candidate_run']==rid
 assert job['manual_candidate_only'] is True
