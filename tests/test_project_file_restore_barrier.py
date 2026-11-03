"""Downloads must capture a single workspace version across rollback."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event, get_ident
from zipfile import ZipFile
import io
import asyncio
import hashlib
import pytest
from fastapi.testclient import TestClient

from agent_service import workspace
from agent_service.api import app, project_file
from agent_service.config import TOKEN
from test_project_data import project

HEADERS={'Authorization':'Bearer '+TOKEN}


def versions():
    pid=project()
    workspace.write_file(pid,'summary.txt','OLD')
    old=workspace.checkpoint(pid,reason='old')['commit']
    workspace.write_file(pid,'summary.txt','NEW')
    workspace.checkpoint(pid,reason='new')
    return pid,old


async def read_response(response, fail_on_body=False):
    chunks=[]
    async def send(message):
        if message['type']=='http.response.body':
            if fail_on_body:raise OSError('client disconnected')
            chunks.append(message.get('body',b''))
    async def receive():
        return {'type':'http.disconnect'}
    await response({'type':'http','asgi':{'version':'3.0','spec_version':'2.4'}},receive,send)
    return b''.join(chunks)


def test_current_file_bytes_are_captured_before_restore():
    pid,old=versions()
    response=project_file(pid,'summary.txt')
    workspace.restore(pid,old,'restore-fixed-bytes')
    assert asyncio.run(read_response(response))==b'NEW'
    assert response.snapshot.closed
    assert workspace.read_file(pid,'summary.txt')=='OLD'


def test_file_download_holds_workspace_lock_through_read(monkeypatch):
    from agent_service import platform_files
    pid,old=versions()
    paused=Event();release=Event();restore_attempt=Event();thread_id=[None]
    original_open=platform_files.open_regular_read
    original_lock=workspace.lock
    def open_file(path):
        if path==workspace.path_for(pid,'summary.txt') and not paused.is_set():
            paused.set();assert release.wait(10)
        return original_open(path)
    def lock(project_id):
        if project_id==pid and get_ident()==thread_id[0]:restore_attempt.set()
        return original_lock(project_id)
    monkeypatch.setattr(platform_files,'open_regular_read',open_file)
    monkeypatch.setattr(workspace,'lock',lock)
    def fetch():return TestClient(app).get(f'/api/projects/{pid}/file',params={'path':'summary.txt'},headers=HEADERS)
    def restore():
        thread_id[0]=get_ident()
        return workspace.restore(pid,old,'restore-read')
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading=pool.submit(fetch)
        assert paused.wait(10)
        restoring=pool.submit(restore)
        assert restore_attempt.wait(10)
        try:
            with pytest.raises(TimeoutError):restoring.result(timeout=.2)
        finally:release.set()
        response=reading.result(timeout=10)
        restoring.result(timeout=20)
    assert response.status_code==200 and response.content==b'NEW'
    assert workspace.read_file(pid,'summary.txt')=='OLD'


def test_file_request_waiting_for_restore_rechecks_revision(monkeypatch):
    pid,old=versions();waiting=Event();release=Event();original=workspace.lock
    def gated(project_id):
        if project_id==pid and not waiting.is_set():
            waiting.set();assert release.wait(10)
        return original(project_id)
    monkeypatch.setattr(workspace,'lock',gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading=pool.submit(lambda:TestClient(app).get(f'/api/projects/{pid}/file',params={'path':'summary.txt'},headers=HEADERS))
        assert waiting.wait(10)
        try:workspace.restore(pid,old,'restore-first')
        finally:release.set()
        response=reading.result(timeout=10)
    assert response.status_code>=400 and '可见性已变化' in response.text


def test_project_archive_captures_one_version():
    pid,old=versions()
    response=TestClient(app).get(f'/api/projects/{pid}/repository.zip',headers=HEADERS)
    assert response.status_code==200
    workspace.restore(pid,old,'restore-after-archive')
    with ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read('files/summary.txt')==b'NEW'
    assert workspace.read_file(pid,'summary.txt')=='OLD'


def test_large_file_download_uses_spooled_snapshot_and_preserves_all_bytes():
    pid,old=versions()
    content=(b'abcd'*300_000)+b'end'
    workspace.path_for(pid,'summary.txt').write_bytes(content)
    response=project_file(pid,'summary.txt')
    assert response.snapshot._rolled
    workspace.restore(pid,old,'restore-large')
    assert hashlib.sha256(asyncio.run(read_response(response))).digest()==hashlib.sha256(content).digest()
    assert response.snapshot.closed


def test_restore_during_stream_does_not_mix_versions():
    pid,old=versions()
    content=b'abcd'*300_000
    workspace.path_for(pid,'summary.txt').write_bytes(content)
    response=project_file(pid,'summary.txt')
    parts=[];restored=False
    async def send(message):
        nonlocal restored
        if message['type']=='http.response.body':
            parts.append(message.get('body',b''))
            if not restored:
                restored=True
                await asyncio.to_thread(workspace.restore,pid,old,'restore-during-stream')
    async def receive():return {'type':'http.disconnect'}
    asyncio.run(response({'type':'http','asgi':{'version':'3.0','spec_version':'2.4'}},receive,send))
    assert restored and response.snapshot.closed
    assert hashlib.sha256(b''.join(parts)).digest()==hashlib.sha256(content).digest()
    assert workspace.read_file(pid,'summary.txt')=='OLD'


def test_snapshot_closed_when_client_disconnects():
    from starlette.requests import ClientDisconnect
    pid,_=versions()
    workspace.path_for(pid,'summary.txt').write_bytes(b'x'*(1024*1024+1))
    response=project_file(pid,'summary.txt')
    assert response.snapshot._rolled
    with pytest.raises(ClientDisconnect):
        asyncio.run(read_response(response,fail_on_body=True))
    assert response.snapshot.closed


def test_large_historical_file_download():
    pid,_=versions()
    content=b'previous '*40000
    workspace.path_for(pid,'summary.txt').write_bytes(content)
    commit=workspace.checkpoint(pid,reason='large historical')['commit']
    workspace.path_for(pid,'summary.txt').write_bytes(b'newer')
    response=TestClient(app).get(f'/api/projects/{pid}/file',params={'path':'summary.txt','commit':commit},headers=HEADERS)
    assert response.status_code==200
    assert hashlib.sha256(response.content).digest()==hashlib.sha256(content).digest()


@pytest.mark.parametrize('route',['files','versions','repository.zip'])
def test_project_read_waiting_for_restore_is_rejected(monkeypatch,route):
    pid,old=versions();waiting=Event();release=Event();original=workspace.lock
    def gated(project_id):
        if project_id==pid and not waiting.is_set():
            waiting.set();assert release.wait(10)
        return original(project_id)
    monkeypatch.setattr(workspace,'lock',gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading=pool.submit(lambda:TestClient(app).get(f'/api/projects/{pid}/{route}',headers=HEADERS))
        assert waiting.wait(10)
        try:workspace.restore(pid,old,'restore-first-list')
        finally:release.set()
        response=reading.result(timeout=10)
    assert response.status_code>=400 and '可见性已变化' in response.text
