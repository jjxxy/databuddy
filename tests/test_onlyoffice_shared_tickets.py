"""A view token is a single-use, project-scoped bearer across API processes."""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from agent_service import onlyoffice_readonly as office
from agent_service.actor_context import actor_scope
from agent_service.store import items, transaction


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv('ATLAS_ONLYOFFICE_JWT_SECRET','fixture_secret_'+'s'*40)
    monkeypatch.setenv('ATLAS_ONLYOFFICE_BROWSER_ORIGIN','http://127.0.0.1:18080')
    monkeypatch.setenv('ATLAS_ONLYOFFICE_FILE_ORIGIN','http://127.0.0.1:18081')
    monkeypatch.setenv('ATLAS_ONLYOFFICE_SOURCE_IP','127.0.0.1')
    with transaction() as c:c.execute(delete(items).where(items.c.kind==office._ticket_kind))


@pytest.fixture
def artifact(monkeypatch,tmp_path):
    path=tmp_path/'real.docx';path.write_bytes(b'onlyoffice snapshot')
    calls=[]
    def get_file(pid,aid,fmt=None):
        from agent_service.actor_context import current_actor
        calls.append((pid,aid,fmt,current_actor()))
        return path,{'name':'safe.docx','download_format':'docx','download_sha256':'a'*64}
    monkeypatch.setattr(office.workspace_artifacts,'file',get_file)
    return path,calls


def issue():
    with actor_scope('u_'+'a'*64):
        result=office.issue('project','artifact')
    return result['config']['document']['url'].rsplit('/',1)[-1]


def test_ticket_survives_separate_api_process_and_remains_single_use(artifact):
    path,calls=artifact;token=issue()
    with transaction() as c:
        records=c.execute(select(items.c.id,items.c.body).where(items.c.kind==office._ticket_kind)).all()
    assert len(records)==1 and token not in records[0].id and token not in records[0].body
    script='''import sys\nfrom agent_service import onlyoffice_readonly as o\nfrom agent_service.actor_context import current_actor\np=sys.argv[1]\ndef f(pid,aid,fmt=None):\n assert (pid,aid,fmt,current_actor())==('project','artifact','docx','u_'+'a'*64)\n return p,{'name':'safe.docx','download_format':'docx','download_sha256':'a'*64}\no.workspace_artifacts.file=f\nassert o.redeem(sys.stdin.readline().strip(),'127.0.0.1')[0]==p\n'''
    result=subprocess.run([sys.executable,'-c',script,str(path)],input=token+'\n',text=True,
                          capture_output=True,env=dict(os.environ),timeout=20)
    assert result.returncode==0,result.stderr
    with pytest.raises(HTTPException) as exc:office.redeem(token,'127.0.0.1')
    assert exc.value.status_code==404


def test_concurrent_consumers_get_at_most_one_result(artifact):
    token=issue();barrier=Barrier(2)
    def read():
        barrier.wait()
        try:return office.redeem(token,'127.0.0.1')[0]
        except HTTPException as exc:return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:read(),range(2)))
    assert results.count(artifact[0])==1 and results.count(404)==1
    assert len(artifact[1])==2  # one issue, exactly one successful file read


def test_wrong_ip_and_invalid_token_cannot_consume_valid_ticket(artifact):
    token=issue()
    for value,ip in [(token,'127.0.0.2'),('not-a-token','127.0.0.1'),(token[:-1]+'!', '127.0.0.1')]:
        with pytest.raises(HTTPException) as error:office.redeem(value,ip)
        assert error.value.status_code==404
    assert office.redeem(token,'127.0.0.1')[0]==artifact[0]


def test_configuration_rotation_revokes_issued_ticket(monkeypatch,artifact):
    token=issue();monkeypatch.setenv('ATLAS_ONLYOFFICE_JWT_SECRET','different_fixture_secret_'+'t'*40)
    with pytest.raises(HTTPException) as error:office.redeem(token,'127.0.0.1')
    assert error.value.status_code==404 and len(artifact[1])==1


def test_archived_or_revoked_artifact_is_rechecked_and_cannot_be_replayed(monkeypatch,artifact):
    token=issue()
    def revoked(*args):raise office.workspace_artifacts.ArtifactGone('archived')
    monkeypatch.setattr(office.workspace_artifacts,'file',revoked)
    with pytest.raises(office.workspace_artifacts.ArtifactGone):office.redeem(token,'127.0.0.1')
    with pytest.raises(HTTPException) as error:office.redeem(token,'127.0.0.1')
    assert error.value.status_code==404


def test_global_capacity_released_after_expiration(monkeypatch,artifact):
    monkeypatch.setattr(office,'_max_entries',1)
    t=[100.0];monkeypatch.setattr(office.time,'time',lambda:t[0])
    old=issue()
    with pytest.raises(HTTPException) as error:issue()
    assert error.value.status_code==503
    t[0]=191.0;fresh=issue()
    with pytest.raises(HTTPException) as error:office.redeem(old,'127.0.0.1')
    assert error.value.status_code==404
    assert office.redeem(fresh,'127.0.0.1')[0]==artifact[0]
