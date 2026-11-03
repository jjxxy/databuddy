"""Version comparison must bind both commits to one project and remain read-only."""
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service import workspace
from test_project_data import project

H={'Authorization':'Bearer '+TOKEN}


def test_compare_real_commits_handles_text_binary_and_cross_project():
    pid=project();client=TestClient(app);root=f'/api/projects/{pid}/versions/compare'
    workspace.write_file(pid,'report.md','收入：100 元\n说明：旧口径\n')
    (workspace.folder(pid)/'files'/'slides.pptx').write_bytes(b'PK\0old')
    old=workspace.checkpoint(pid,reason='旧版')['commit']
    workspace.write_file(pid,'report.md','收入：120 元\n说明：新口径\n')
    workspace.write_file(pid,'added.txt','新增内容\n')
    (workspace.folder(pid)/'files'/'slides.pptx').write_bytes(b'PK\0new')
    new=workspace.checkpoint(pid,reason='新版')['commit']
    r=client.get(root,params={'base':old,'target':new},headers=H)
    assert r.status_code==200,r.text
    body=r.json();assert body['base']==old and body['target']==new
    assert {'path':'files/report.md','status':'M'} in body['files']
    assert {'path':'files/added.txt','status':'A'} in body['files']
    r=client.get(root,params={'base':old,'target':new,'path':'files/report.md'},headers=H)
    assert r.status_code==200,r.text
    assert '-收入：100 元' in r.json()['file']['diff'] and '+收入：120 元' in r.json()['file']['diff']
    assert not r.json()['file']['truncated']
    binary=client.get(root,params={'base':old,'target':new,'path':'files/slides.pptx'},headers=H)
    assert binary.status_code==200 and binary.json()['file']['kind']=='unavailable'
    assert client.get(root,params={'base':old,'target':new,'path':'files/other'},headers=H).status_code==400
    assert client.get(root,params={'base':old,'target':new}).status_code==401
    other=project();other_commit=workspace.checkpoint(other,reason='other')['commit']
    assert client.get(root,params={'base':old,'target':other_commit},headers=H).status_code==400
    assert workspace.read_file(pid,'report.md')=='收入：120 元\n说明：新口径\n'


def test_compare_rejects_unrecorded_or_invalid_version():
    pid=project();client=TestClient(app);version=workspace.checkpoint(pid)['commit']
    root=f'/api/projects/{pid}/versions/compare'
    assert client.get(root,params={'base':'HEAD','target':version},headers=H).status_code==400
    assert client.get(root,params={'base':'0'*40,'target':version},headers=H).status_code==400


def test_missing_final_newline_is_not_joined_with_the_new_value():
    pid=project();client=TestClient(app)
    workspace.write_file(pid,'note.txt','旧')
    before=workspace.checkpoint(pid)['commit']
    workspace.write_file(pid,'note.txt','新')
    after=workspace.checkpoint(pid)['commit']
    response=client.get(f'/api/projects/{pid}/versions/compare',
                        params={'base':before,'target':after,'path':'files/note.txt'},headers=H)
    assert response.status_code==200,response.text
    diff=response.json()['file']['diff']
    assert '-旧\n\\ No newline at end of file\n+新\n' in diff


def _fake_git(tmp_path, body):
    import sys
    program=tmp_path/'fake-git'
    program.write_text('#!'+sys.executable+'\n'+body,encoding='utf-8')
    program.chmod(0o700)
    return str(program)


def test_git_output_is_stopped_at_limit_without_temporary_files(tmp_path,monkeypatch):
    import os
    import pytest
    from agent_service import version_compare
    pid_file=tmp_path/'pid'
    fake=_fake_git(tmp_path,'import os,sys\nopen('+repr(str(pid_file))+',"w").write(str(os.getpid()))\nwhile True: os.write(sys.stdout.fileno(),b"x"*65536)\n')
    monkeypatch.setattr(version_compare,'executable',lambda name:fake)
    with pytest.raises(ValueError,match='过大'):
        version_compare._git(tmp_path,'diff',limit=1024)
    pid=int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):os.kill(pid,0)
    assert sorted(p.name for p in tmp_path.iterdir())==['fake-git','pid']


def test_git_silent_hang_times_out_and_is_reaped(tmp_path,monkeypatch):
    import os
    import pytest
    from agent_service import version_compare
    pid_file=tmp_path/'pid'
    fake=_fake_git(tmp_path,'import os,time\nopen('+repr(str(pid_file))+',"w").write(str(os.getpid()))\ntime.sleep(30)\n')
    monkeypatch.setattr(version_compare,'executable',lambda name:fake)
    monkeypatch.setattr(version_compare,'GIT_TIMEOUT_SECONDS',.5)
    with pytest.raises(ValueError,match='超时'):
        version_compare._git(tmp_path,'diff',limit=1024)
    with pytest.raises(ProcessLookupError):os.kill(int(pid_file.read_text()),0)
