"""Version-bound section edits retain originals and structured financial data."""
import hashlib
import json
import pytest
from fastapi.testclient import TestClient
from agent_service import office, office_edit, workspace, workspace_artifacts
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.store import get
from test_project_data import project


def sample(kind='presentation'):
    pid=project();spec={'kind':kind,'title':'经营报告','sections':[{'heading':'概况','body':'收入为100元',
         'bullets':['已核对','旧结论'],'table':{'columns':['地区','金额'],'rows':[['华东','100']]},
         'chart':{'title':'收入','unit':'元','points':[{'label':'华东','value':'100'}]}}]}
    if kind=='document':spec['sections'][0].pop('chart')
    text=json.dumps(spec,ensure_ascii=False);workspace.write_file(pid,'reports/source.json',text)
    result=office.render({'project_id':pid,'text':'生成报告','interaction_mode':'work'},'reports/source.json','原报告')
    original=office.artifact(pid,result['artifact_id'])[0].read_bytes()
    item=next(x for x in workspace_artifacts.catalog(pid)['items'] if x['kind']=='office' and x['sha256']==result['sha256'])
    return pid,item,text,original


def payload(info):
    return {'request_id':'edit_request_12345678','expected_artifact_sha256':info['artifact_sha256'],
            'expected_spec_sha256':info['spec_sha256'],'section':0,'field':'body','value':'人工修订的文字', 'name':'修改后报告'}


@pytest.mark.parametrize('kind',['presentation','document'])
def test_edit_creates_new_verified_file_with_original_and_data_preserved(kind):
    pid,item,original_spec,old_bytes=sample(kind);info=office_edit.inspect(pid,item['id'])
    assert item.get('office_edit_available') is True
    data=payload(info);result=office_edit.edit(pid,item['id'],'owner',data)
    assert result['artifact_id']!=get('workspace_artifact',item['id'])['private']['office_id']
    assert result['source_sha256']==hashlib.sha256(workspace.read_file(pid,result['draft_path']).encode()).hexdigest()
    new=office._load_spec(workspace.read_file(pid,result['draft_path']))
    old=office._load_spec(original_spec)
    assert new.sections[0].body=='人工修订的文字'
    assert new.sections[0].table==old.sections[0].table and new.sections[0].chart==old.sections[0].chart
    assert workspace.read_file(pid,'reports/source.json')==original_spec
    assert office.artifact(pid,get('workspace_artifact',item['id'])['private']['office_id'])[0].read_bytes()==old_bytes
    assert office.artifact(pid,result['artifact_id'])[1]['sha256']==result['sha256']
    assert office_edit.edit(pid,item['id'],'owner',data)==result
    assert len([x for x in workspace.history(pid) if x['commit']==result['work_commit']])==1


def test_stale_wrong_project_and_request_conflicts_are_rejected():
    pid,item,_,_=sample();info=office_edit.inspect(pid,item['id']);data=payload(info)
    with pytest.raises((KeyError,ValueError)):office_edit.inspect(project(),item['id'])
    wrong=dict(data,expected_spec_sha256='0'*64)
    with pytest.raises(ValueError,match='版本'):office_edit.edit(pid,item['id'],'owner',wrong)
    office_edit.edit(pid,item['id'],'owner',data)
    changed=dict(data,value='另一段文本')
    with pytest.raises(ValueError,match='请求编号'):office_edit.edit(pid,item['id'],'owner',changed)
    workspace.write_file(pid,'reports/source.json','{"kind":"document","title":"其他"}')
    with pytest.raises(ValueError,match='不一致'):office_edit.inspect(pid,item['id'])


def test_replayed_result_refuses_a_changed_revision_spec():
    pid,item,_,_=sample();info=office_edit.inspect(pid,item['id']);body=payload(info)
    result=office_edit.edit(pid,item['id'],'owner',body)
    workspace.write_file(pid,result['draft_path'],'{"kind":"document","title":"篡改"}')
    with pytest.raises(ValueError,match='编辑方案已变化'):
        office_edit.edit(pid,item['id'],'owner',body)


def test_http_requires_auth_and_rejects_invented_fields():
    pid,item,_,_=sample();path=f'/api/projects/{pid}/artifacts/{item["id"]}/office-edit';client=TestClient(app)
    assert client.get(path).status_code==401
    headers={'Authorization':'Bearer '+TOKEN};response=client.get(path,headers=headers)
    assert response.status_code==200,response.text
    body=payload(response.json());body['chart']='invented'
    assert client.post(path,headers=headers,json=body).status_code==422


def test_changed_draft_cannot_be_published(monkeypatch):
    pid,item,_,_=sample();info=office_edit.inspect(pid,item['id']);body=payload(info)
    original=office.render
    def changed(*args,**kwargs):
        workspace.write_file(pid,args[1],'{"kind":"document","title":"篡改"}')
        return original(*args,**kwargs)
    monkeypatch.setattr(office,'render',changed)
    with pytest.raises(ValueError,match='冻结输入不一致'):office_edit.edit(pid,item['id'],'owner',body)


def test_route_generates_version_and_retries_with_identical_request():
    pid,item,_,_=sample();client=TestClient(app);path=f'/api/projects/{pid}/artifacts/{item["id"]}/office-edit';headers={'Authorization':'Bearer '+TOKEN}
    info=client.get(path,headers=headers).json();body=payload(info)
    result=client.post(path,headers=headers,json=body)
    assert result.status_code==200,result.text
    assert result.json()['original_artifact_id']==item['id']
    again=client.post(path,headers=headers,json=body)
    assert again.status_code==200 and again.json()==result.json()
    assert client.get(result.json()['download']['url'],headers=headers).status_code==200


def test_deleted_project_cannot_publish_manual_edit(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from agent_service import workspace_lifecycle, store
    pid,item,_,_=sample();info=office_edit.inspect(pid,item['id']);body=payload(info)
    entered=threading.Event();release=threading.Event();original=office._build_file
    def delayed(*args,**kwargs):
        original(*args,**kwargs);entered.set();assert release.wait(5)
    monkeypatch.setattr(office,'_build_file',delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task=pool.submit(office_edit.edit,pid,item['id'],'owner',body)
        assert entered.wait(10);workspace_lifecycle.delete_project(pid);release.set()
        with pytest.raises(ValueError):task.result(timeout=10)
    # A pending receipt is not a downloadable committed output.
    records=[x for x in store.all_items('office_edit') if x.get('project_id')==pid]
    assert len(records)==1 and records[0]['status']=='pending'
    assert not [x for x in store.all_items('office_artifact') if x.get('project_id')==pid and x.get('state')=='committed' and x['id']!=get('workspace_artifact',item['id'])['private']['office_id']]


def test_same_request_is_serialized_and_returns_same_completion(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    pid,item,_,_=sample();info=office_edit.inspect(pid,item['id']);body=payload(info)
    entered=threading.Event();release=threading.Event();original=office.render;calls=[]
    def delayed(*args,**kwargs):
        calls.append(1);entered.set();assert release.wait(5)
        return original(*args,**kwargs)
    monkeypatch.setattr(office,'render',delayed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(office_edit.edit,pid,item['id'],'owner',body)
        assert entered.wait(5)
        second=pool.submit(office_edit.edit,pid,item['id'],'owner',body)
        release.set();one=first.result(timeout=10);two=second.result(timeout=10)
    assert one==two and len(calls)==1
