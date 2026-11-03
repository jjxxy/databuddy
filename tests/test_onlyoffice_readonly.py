import jwt
import pytest
from fastapi import HTTPException
from agent_service import onlyoffice_readonly as office
from agent_service.actor_context import actor_scope


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv('ATLAS_ONLYOFFICE_JWT_SECRET', 's' * 40)
    monkeypatch.setenv('ATLAS_ONLYOFFICE_BROWSER_ORIGIN', 'http://127.0.0.1:18080')
    monkeypatch.setenv('ATLAS_ONLYOFFICE_FILE_ORIGIN', 'http://172.17.0.1:8000')
    monkeypatch.setenv('ATLAS_ONLYOFFICE_SOURCE_IP', '172.17.0.2')
    from sqlalchemy import delete
    from agent_service.store import items,transaction
    with transaction() as connection:connection.execute(delete(items).where(items.c.kind==office._ticket_kind))


def test_scoped_single_use_signed_file(monkeypatch, tmp_path):
    seen = []
    path = tmp_path / 'snapshot.pptx';path.write_bytes(b'bounded')
    def file(pid, aid, fmt=None):
        from agent_service.actor_context import current_actor
        seen.append((current_actor(), pid, aid, fmt))
        return path, {'name': 'a.pptx', 'download_format': 'pptx', 'download_sha256': 'a' * 64}
    monkeypatch.setattr(office.workspace_artifacts, 'file', file)
    actor = 'u_' + 'e' * 64
    with actor_scope(actor):
        value = office.issue('project', 'artifact')
    config = value['config']
    assert jwt.decode(config['token'], 's' * 40, algorithms=['HS256'])['editorConfig']['mode'] == 'view'
    token = config['document']['url'].rsplit('/', 1)[-1]
    with pytest.raises(HTTPException):office.redeem(token, '172.17.0.3')
    result, _ = office.redeem(token, '172.17.0.2')
    assert result == path and seen[-1] == (actor, 'project', 'artifact', 'pptx')
    with pytest.raises(HTTPException):office.redeem(token, '172.17.0.2')


def test_reject_changed_files_expiry_and_public_origins(monkeypatch, tmp_path):
    path = tmp_path / 'a.docx';path.write_bytes(b'x')
    values = iter(['a' * 64, 'b' * 64])
    monkeypatch.setattr(office.workspace_artifacts, 'file', lambda *args: (path, {'name':'a.docx','download_format':'docx','download_sha256':next(values)}))
    token = office.issue('p', 'a')['config']['document']['url'].rsplit('/', 1)[-1]
    with pytest.raises(HTTPException) as error:office.redeem(token, '172.17.0.2')
    assert error.value.status_code == 409
    monkeypatch.setenv('ATLAS_ONLYOFFICE_FILE_ORIGIN', 'https://8.8.8.8:443')
    with pytest.raises(HTTPException):office._config()


def test_expired_link_does_not_open_file(monkeypatch, tmp_path):
    path = tmp_path / 'a.docx';path.write_bytes(b'x')
    calls = []
    def file(*args):
        calls.append(args)
        return path, {'name':'a.docx', 'download_format':'docx', 'download_sha256':'a' * 64}
    monkeypatch.setattr(office.workspace_artifacts, 'file', file)
    token = office.issue('p', 'a')['config']['document']['url'].rsplit('/', 1)[-1]
    from agent_service.store import get,put
    key=office._ticket_id(token)
    ticket=get(office._ticket_kind,key);ticket['expires']=0;put(office._ticket_kind,ticket,key)
    with pytest.raises(HTTPException):office.redeem(token, '172.17.0.2')
    assert len(calls) == 1


def test_real_project_file_handoff_is_authorized_and_single_use(monkeypatch, clean):
    import hashlib, json
    from fastapi import FastAPI, Header
    from fastapi.testclient import TestClient
    from agent_service import office as generator, workspace, workspace_artifacts
    from test_agent_handoff import project, turn
    monkeypatch.setenv('ATLAS_ONLYOFFICE_SOURCE_IP', '127.0.0.1')
    pid = project();current = turn(pid)
    workspace.write_file(pid, 'office-spec.json', json.dumps({'kind':'presentation','title':'真实项目验收','sections':[{'body':'验证 118.15'}]}, ensure_ascii=False))
    generated = generator.render(current, 'office-spec.json', '验收报告')
    item = workspace_artifacts.catalog(pid, 'office')['items'][0]
    app = FastAPI()
    def auth(authorization: str | None = Header(None)):
        if authorization != 'Bearer fixture':raise HTTPException(401, 'unauthorized')
    office.install(app, auth)
    client = TestClient(app, client=('127.0.0.1', 1234))
    endpoint = f'/api/projects/{pid}/artifacts/{item["id"]}/onlyoffice-view'
    assert client.post(endpoint).status_code == 401
    other=project()
    assert client.post(f'/api/projects/{other}/artifacts/{item["id"]}/onlyoffice-view',headers={'Authorization':'Bearer fixture'}).status_code in (404,409)
    response = client.post(endpoint, headers={'Authorization':'Bearer fixture'})
    assert response.status_code == 200
    token = response.json()['config']['document']['url'].rsplit('/', 1)[-1]
    # The document server probes PDF sources with HEAD and byte-range GETs;
    # those probes must leave the ticket usable for the full download.
    assert client.head('/api/onlyoffice/file/' + token).status_code == 200
    partial = client.get('/api/onlyoffice/file/' + token, headers={'Range': 'bytes=0-15'})
    assert partial.status_code == 206 and len(partial.content) == 16
    output = client.get('/api/onlyoffice/file/' + token)
    assert output.status_code == 200 and hashlib.sha256(output.content).hexdigest() == generated['sha256']
    assert client.get('/api/onlyoffice/file/' + token).status_code == 404


from test_workspace_artifacts import clean


def test_main_app_mounts_readonly_routes_and_limits_frame_origin(monkeypatch):
    from fastapi.testclient import TestClient
    from agent_service.api import app
    client=TestClient(app)
    assert client.post('/api/projects/p/artifacts/a/onlyoffice-view').status_code==401
    policy=client.get('/').headers['content-security-policy']
    assert "frame-src 'self' http://127.0.0.1:18080" in policy
    assert "script-src 'self'" in policy and "script-src 'self' http://127.0.0.1:18080" not in policy
    monkeypatch.delenv('ATLAS_ONLYOFFICE_JWT_SECRET')
    assert "frame-src 'self' http://127.0.0.1:18080" not in client.get('/').headers['content-security-policy']
