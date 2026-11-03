"""Read old Atlas security configuration without silently weakening policy."""
from datetime import datetime, timedelta, timezone
import pytest
from agent_service import identity, backup_cloud, runtime_settings, agent_runtime, agent_gateway, provider, model_catalog
from agent_service.store import put, delete, get, seal, uid


def _clean(monkeypatch):
    for name in ('AUTH_MODE','REQUIRE_SINGLE_OIDC_SUBJECT','S3_ENDPOINT','S3_BUCKET','S3_ACCESS_KEY_ID','S3_SECRET_ACCESS_KEY','BACKUP_KEY','S3_REGION','S3_URL_STYLE','BACKUP_TIMEOUT_SECONDS','BACKUP_RETENTION_DAYS'):
        for prefix in ('NEXUS_','ATLAS_'):monkeypatch.delenv(prefix+name,raising=False)
    monkeypatch.delenv('NEXUS_BACKUP_REPOSITORY_PREFIX',raising=False)


def test_old_oidc_configuration_cannot_silently_fall_back_to_token(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv('ATLAS_AUTH_MODE','oidc')
    monkeypatch.setenv('ATLAS_REQUIRE_SINGLE_OIDC_SUBJECT','1')
    monkeypatch.setenv('OIDC_ISSUER','https://identity.test')
    monkeypatch.setenv('OIDC_JWKS_URL','https://identity.test/keys')
    monkeypatch.setenv('OIDC_AUDIENCE','business')
    monkeypatch.setenv('OIDC_ALLOWED_SUBJECTS','alice')
    monkeypatch.setenv('OIDC_ACCEPT_PROXY_HEADER','0')
    assert identity.mode()=='oidc'
    identity.validate_config()
    monkeypatch.setenv('NEXUS_AUTH_MODE','token')
    with pytest.raises(ValueError,match='冲突'):identity.mode()
    with pytest.raises(ValueError,match='冲突'):identity.validate_config()


def test_single_subject_policy_and_conflicting_modes_fail_closed(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv('ATLAS_REQUIRE_SINGLE_OIDC_SUBJECT','1')
    with pytest.raises(ValueError,match='OIDC'):identity.validate_config()
    monkeypatch.setenv('ATLAS_AUTH_MODE','token');monkeypatch.setenv('NEXUS_AUTH_MODE','oidc')
    with pytest.raises(ValueError,match='冲突'):identity.mode()
    monkeypatch.setenv('NEXUS_AUTH_MODE','token')
    monkeypatch.setenv('NEXUS_REQUIRE_SINGLE_OIDC_SUBJECT','0')
    with pytest.raises(ValueError,match='冲突'):identity.mode()


def _backup_env(monkeypatch,prefix):
    for key,value in {'S3_ENDPOINT':'https://s3.example','S3_BUCKET':'finance',
        'S3_ACCESS_KEY_ID':'example','S3_SECRET_ACCESS_KEY':'private','BACKUP_KEY':'p'*40}.items():
        monkeypatch.setenv(prefix+key,value)


def test_old_backup_keeps_original_repository_and_supports_explicit_migration(monkeypatch):
    _clean(monkeypatch);_backup_env(monkeypatch,'ATLAS_')
    assert backup_cloud.configured()
    env=backup_cloud.environment()
    assert env['RESTIC_REPOSITORY']=='s3:https://s3.example/finance/atlas'
    assert env['RESTIC_PASSWORD']=='p'*40 and 'ATLAS_S3_SECRET_ACCESS_KEY' not in env
    monkeypatch.setenv('NEXUS_BACKUP_REPOSITORY_PREFIX','nexus')
    assert backup_cloud.environment()['RESTIC_REPOSITORY'].endswith('/nexus')
    monkeypatch.setenv('NEXUS_BACKUP_REPOSITORY_PREFIX','atlas')
    assert backup_cloud.environment()['RESTIC_REPOSITORY'].endswith('/atlas')
    monkeypatch.setenv('NEXUS_BACKUP_REPOSITORY_PREFIX','../wrong')
    with pytest.raises(ValueError):backup_cloud.environment()


def test_backup_new_and_old_conflicts_never_silently_select_credentials(monkeypatch):
    _clean(monkeypatch);_backup_env(monkeypatch,'ATLAS_');_backup_env(monkeypatch,'NEXUS_')
    assert backup_cloud.environment()['RESTIC_REPOSITORY'].endswith('/atlas')
    monkeypatch.setenv('NEXUS_S3_SECRET_ACCESS_KEY','different')
    with pytest.raises(ValueError,match='冲突'):backup_cloud.configured()
    with pytest.raises(ValueError,match='冲突'):backup_cloud.environment()


def test_old_runtime_binding_and_operator_default_preserve_version():
    assert agent_runtime.validate_snapshot({'name':'atlas_legacy','version':'1'}) == {'name':'nexus_legacy','version':'1'}
    with pytest.raises(provider.ModelServiceError,match='版本不匹配'):
        agent_runtime.validate_snapshot({'name':'atlas_legacy','version':'999'})
    with pytest.raises(provider.ModelServiceError):
        agent_runtime.snapshot_for_turn({'agent_runtime':{'name':'atlas_legacy','version':'1'},'opencode_session_id':'wrong'})
    assert runtime_settings._validated({'name':'atlas_legacy','revision':'old'})=={'name':'nexus_legacy','revision':'old'}
    with pytest.raises(ValueError):runtime_settings._validated({'name':'unknown','revision':'old'})


def test_old_agent_token_still_requires_exact_hash_and_live_grant():
    ident='agc_'+uid();token='atlas_ag_'+uid()
    put('agent_client',{'id':ident,'enabled':True,'token_hash':agent_gateway._hash(token),
        'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()},ident)
    try:
        assert agent_gateway.authenticate(token)['id']==ident
        with pytest.raises(agent_gateway.GatewayError):agent_gateway.authenticate(token+'x')
        client=get('agent_client',ident);client['enabled']=False;put('agent_client',client,ident)
        with pytest.raises(agent_gateway.GatewayError):agent_gateway.authenticate(token)
    finally:delete('agent_client',ident)


def test_old_single_model_engine_normalizes_without_rewriting_secrets(monkeypatch):
    saved=None
    try:
        try:saved=get('system','model-provider')
        except KeyError:pass
        put('system',{'secret_config':seal({'base_url':'https://model.example/v1','model':'fixture','api_key':'secret','engine':'atlas','share_data':True})},'model-provider')
        monkeypatch.setattr(model_catalog,'_active',lambda:(_ for _ in ()).throw(KeyError('no catalog')))
        assert provider.legacy_settings()['engine']=='nexus'
        assert model_catalog.owner_configuration()['models'][0]['engine']=='nexus'
    finally:
        if saved is None:delete('system','model-provider')
        else:put('system',saved,'model-provider')


def test_api_keeps_oidc_boundary_for_legacy_setting(monkeypatch):
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    from agent_service import api
    from agent_service.config import TOKEN
    _clean(monkeypatch);monkeypatch.setenv('ATLAS_AUTH_MODE','oidc')
    monkeypatch.setattr(identity,'principal',lambda request: (_ for _ in ()).throw(HTTPException(401,'OIDC required')))
    response=TestClient(api.app).get('/api/identity',headers={'Authorization':'Bearer '+TOKEN})
    assert response.status_code==401 and response.json()['detail']=='OIDC required'


def test_real_runtime_default_record_migrates_on_read_without_rewriting():
    saved=None
    try:
        try:saved=get('system','agent-runtime-default')
        except KeyError:pass
        put('system',{'name':'atlas_legacy','revision':'frozen-revision'},'agent-runtime-default')
        assert runtime_settings.get_state()=={'name':'nexus_legacy','revision':'frozen-revision'}
        assert get('system','agent-runtime-default')['name']=='atlas_legacy'
        runtime_settings.set_selected('opencode','frozen-revision')
        assert runtime_settings.get_selected()=='opencode'
    finally:
        if saved is None:delete('system','agent-runtime-default')
        else:put('system',saved,'agent-runtime-default')


def test_migrated_new_backup_vars_cannot_silently_create_empty_repository(monkeypatch):
    _clean(monkeypatch);_backup_env(monkeypatch,'NEXUS_')
    try:previous=get('system','backup-health')
    except KeyError:previous=None
    try:
        put('system',{'ok':True,'backend':'restic','snapshot_id':'abcdef01'},'backup-health')
        with pytest.raises(ValueError,match='历史备份仓路径不明确'):
            backup_cloud.environment()
        monkeypatch.setenv('NEXUS_BACKUP_REPOSITORY_PREFIX','atlas')
        assert backup_cloud.environment()['RESTIC_REPOSITORY'].endswith('/atlas')
        monkeypatch.delenv('NEXUS_BACKUP_REPOSITORY_PREFIX')
        put('system',{'ok':True,'backend':'restic','repository_prefix':'atlas'},'backup-health')
        assert backup_cloud.environment()['RESTIC_REPOSITORY'].endswith('/atlas')
    finally:
        if previous is None:delete('system','backup-health')
        else:put('system',previous,'backup-health')


def test_legacy_operator_token_and_header_keep_admin_access_but_reject_conflicts(monkeypatch):
    from fastapi.testclient import TestClient
    from agent_service import api
    from agent_service.config import TOKEN
    _clean(monkeypatch)
    monkeypatch.delenv('NEXUS_OPERATOR_TOKEN',raising=False)
    monkeypatch.setenv('ATLAS_OPERATOR_TOKEN','old-operator-secret-12345')
    monkeypatch.setenv('NEXUS_AUTH_MODE','token')
    client=TestClient(api.app);path='/api/operator/agent-runtimes'
    owner={'Authorization':'Bearer '+TOKEN}
    assert client.get(path,headers=owner).status_code==403
    assert client.get(path,headers={**owner,'X-Atlas-Operator-Token':'old-operator-secret-12345'}).status_code==200
    assert client.get(path,headers={**owner,'X-Nexus-Operator-Token':'old-operator-secret-12345'}).status_code==200
    assert client.get(path,headers={**owner,'X-Nexus-Operator-Token':'old-operator-secret-12345','X-Atlas-Operator-Token':'different'}).status_code==403
    monkeypatch.setenv('NEXUS_OPERATOR_TOKEN','different-operator-secret-12345')
    assert client.get(path,headers={**owner,'X-Atlas-Operator-Token':'old-operator-secret-12345'}).status_code==403


def test_legacy_operator_credential_cannot_be_forwarded_to_peer(monkeypatch):
    from agent_service import agent_mesh
    monkeypatch.setenv('ATLAS_OPERATOR_TOKEN','old-operator-secret-12345')
    with pytest.raises(ValueError,match='专用凭据'):
        agent_mesh._peer_config(agent_mesh.PeerInput(name='Peer',url='https://agent.example/a2a',
            remote_project_id='p',credential='old-operator-secret-12345'))


def test_legacy_mcp_environment_prefix_is_reserved(monkeypatch,tmp_path):
    import json,sys
    from agent_service import mcp_stdio
    monkeypatch.setenv('NEXUS_LOCAL_MODE','1')
    monkeypatch.setenv('NEXUS_LOCAL_WORKSPACE',str(tmp_path))
    monkeypatch.setenv('NEXUS_STDIO_TEMPLATES',json.dumps({'fixture':{'command':sys.executable,'args':[]}}))
    with pytest.raises(ValueError,match='不能覆盖'):
        mcp_stdio.validate({'command':'fixture','env':{'ATLAS_OPERATOR_TOKEN':'attacker'}})
