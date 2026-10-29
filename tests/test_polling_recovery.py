"""Rate limits and lost browser registration never require deleting user data."""
from collections import deque
import time

import pytest
from fastapi.testclient import TestClient

from agent_service import activity, api
from agent_service.config import TOKEN
from agent_service.conversation import create_project, default_session, ProjectInput
from agent_service.store import all_items, delete, get, uid

H = {'Authorization': 'Bearer '+TOKEN}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('ATLAS_AUTH_MODE','token')
    monkeypatch.setattr(api,'_requests',{})
    kinds=('project','conversation','browser_device','browser_view')
    before={kind:{v['id'] for v in all_items(kind)} for kind in kinds}
    yield TestClient(api.app)
    for kind,prior in before.items():
        for value in all_items(kind):
            if value['id'] not in prior:delete(kind,value['id'])


def test_old_300_request_threshold_is_no_longer_a_block(client):
    api._requests[('testclient',True)]=deque([time.time()]*300)
    assert client.get('/api/identity',headers=H).status_code==200
    assert len(api._requests[('testclient',True)])==301


def test_3000_limit_is_per_ip_and_still_returns_retry_after(client):
    api._requests[('testclient',True)]=deque([time.time()]*2999)
    assert client.get('/api/identity',headers=H).status_code==200
    response=client.get('/api/identity',headers=H)
    assert response.status_code==429 and response.headers['Retry-After']=='60'
    other=TestClient(api.app,client=('192.0.2.5',50000))
    assert other.get('/api/identity',headers=H).status_code==200
    assert client.get('/health').status_code==200


def test_old_window_expires_and_authentication_is_not_bypassed(client):
    api._requests[('testclient',True)]=deque([time.time()-61]*3000)
    assert client.get('/api/identity',headers=H).status_code==200
    assert len(api._requests[('testclient',True)])==1
    assert client.get('/api/activity',params={'device_id':'d_'+'a'*32}).status_code==401


def test_same_device_can_reregister_without_clearing_project_or_saved_draft(client):
    pid=create_project(ProjectInput(name='Keep project and draft',request_id=uid()))['id']
    cid=default_session(pid);did='d_'+uid()+uid()[:8]
    assert client.post('/api/browser/register',json={'device_id':did},headers=H).status_code==200
    original=activity.save_view(did,activity.ViewInput(project_id=pid,session_id=cid,
        draft='必须保留的未完成草稿',writer='fixture-tab'))
    delete('browser_device',did)
    response=client.get('/api/activity',params={'device_id':did},headers=H)
    assert response.status_code==404 and response.json()['code']=='browser_device_missing'
    assert client.post('/api/browser/register',json={'device_id':did},headers=H).status_code==200
    assert client.get('/api/activity',params={'device_id':did},headers=H).status_code==200
    saved=activity.read_view(did,pid,cid)
    assert saved['draft']=='必须保留的未完成草稿' and saved['revision']==original['revision']
    assert get('project',pid)['name']=='Keep project and draft'
    unrelated=client.get('/api/projects/nonexistent',headers=H)
    assert unrelated.status_code==404 and unrelated.json().get('code')!='browser_device_missing'
