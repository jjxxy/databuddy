import copy
import json
from pathlib import Path

import pytest
from agent_service.isolated_deployment import ManifestError, load_manifest, render


@pytest.fixture
def manifest():
    return json.loads((Path(__file__).parents[1] / 'deploy/isolated/manifest.example.json').read_text())


def test_topology_and_independent_auth(manifest):
    compose, gateway = render(manifest)
    assert len(compose['networks']) == len(compose['volumes']) == 2
    assert gateway['limits']['max_concurrent_requests'] == 2
    assert set(compose['services']) == {'gateway', 'atlas-alice', 'atlas-bob'}
    data_mounts, networks, tokens, model_secrets = [], [], [], []
    for wid in ('alice', 'bob'):
        backend = compose['services']['atlas-' + wid]
        assert 'ports' not in backend and 'network_mode' not in backend
        assert backend['user'] == '10001:10001' and backend['read_only']
        assert backend['cap_drop'] == ['ALL']
        assert backend['security_opt'] == ['no-new-privileges:true']
        assert backend['mem_limit'] and backend['cpus'] and backend['pids_limit']
        assert len(backend['networks']) == len(backend['volumes']) == 1
        data_mounts += backend['volumes']
        networks += backend['networks']
        env = backend['environment']
        assert env['ATLAS_AUTH_MODE'] == 'oidc'
        assert env['OIDC_ACCEPT_PROXY_HEADER'] == '0'
        assert env['OIDC_ALLOWED_SUBJECTS'] == 'idp-' + wid + '-exact-sub'
        assert env['DATABASE_URL'] == 'sqlite:////data/metadata.sqlite'
        assert env['HOME'] == '/data/home'
        assert env['OIDC_ISSUER'] == gateway['oidc']['issuer']
        assert env['OIDC_AUDIENCE'] == gateway['oidc']['audience']
        tokens.append(env['APP_TOKEN'])
        model_secrets.append(backend['secrets'][0]['source'])
    assert len(set(data_mounts)) == len(set(networks)) == len(set(tokens)) == len(set(model_secrets)) == 2
    front = compose['services']['gateway']
    assert front['ports'] == ['127.0.0.1:8080:8080']
    assert set(front['networks']) == set(networks)
    assert all(mount['read_only'] and '/data' not in mount['target'] for mount in front['volumes'])
    assert 'APP_TOKEN' not in front['environment']
    serialized = json.dumps(compose)
    assert 'docker.sock' not in serialized and 'privileged' not in serialized


def test_deterministic_order(manifest):
    first = render(manifest)
    manifest['workspaces'].reverse()
    assert render(manifest) == first


@pytest.mark.parametrize('field,value', [
    ('id', '../alice'), ('id', 'ALICE'), ('id', '${WORKSPACE}'), ('id', 'a_b'),
    ('subjects', []), ('subjects', ['*']), ('subjects', ['alice,bob']),
    ('subjects', ['alice', 'alice']), ('subjects', [' alice']), ('subjects', ['a\nb']),
    ('subjects', ['${SUBJECT}']), ('subjects', ['a b']),
    ('model_config_file', '/etc/passwd'), ('model_config_file', './secrets/../key.json'),
    ('model_config_file', './secrets/${MODEL}.json'),
    ('model_config_file', './secrets/a.json:ro'),
    ('database_url', 'sqlite:////shared/db'), ('volume', 'shared'),
    ('network', 'shared'), ('backend_url', 'http://shared:8000'),
])
def test_invalid_workspace_rejected(manifest, field, value):
    manifest['workspaces'][0][field] = value
    with pytest.raises(ManifestError):
        render(manifest)


@pytest.mark.parametrize('mutation', ['id', 'subject', 'model'])
def test_duplicates_rejected(manifest, mutation):
    first, second = manifest['workspaces']
    if mutation == 'id':
        second['id'] = first['id']
    elif mutation == 'subject':
        second['subjects'] = first['subjects']
    else:
        second['model_config_file'] = first['model_config_file']
    with pytest.raises(ManifestError):
        render(manifest)


@pytest.mark.parametrize('field,value', [
    ('gateway_port', True), ('gateway_port', 80), ('gateway_port', 65536),
    ('project', 'shared'), ('project', '${PROJECT}'), ('schema_version', True),
    ('public_origin', 'http://atlas.example.com'),
    ('public_origin', 'https://atlas.example.com?'),
    ('public_origin', 'https://atlas.example.com#'),
    ('public_origin', 'https://atlas example.com'),
    ('public_origin', 'https://user:password@atlas.example.com'),
    ('public_origin', 'https://atlas.example.com/path'),
    ('public_origin', 'https://atlas.example.com?redirect=evil'),
    ('public_origin', 'https://atlas.example.com/${PATH}'),
    ('workspaces', []), ('env_file', '.env'),
])
def test_invalid_manifest_rejected(manifest, field, value):
    manifest[field] = value
    with pytest.raises(ManifestError):
        render(manifest)


def test_duplicate_json_keys_rejected(tmp_path):
    path = tmp_path / 'manifest.json'
    path.write_text('{"schema_version":1,"schema_version":1}')
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_no_manifest_mutation(manifest):
    original = copy.deepcopy(manifest)
    render(manifest)
    assert manifest == original


def test_committed_templates_match_renderer(manifest):
    root = Path(__file__).parents[1] / 'deploy/isolated'
    compose, gateway = render(manifest)
    assert json.loads((root / 'compose.json').read_text()) == compose
    assert json.loads((root / 'gateway.json').read_text()) == gateway


def test_fresh_instance_requires_model_provisioning(manifest):
    del manifest['workspaces'][0]['model_config_file']
    with pytest.raises(ManifestError):
        render(manifest)


@pytest.mark.parametrize('name,value', [
    ('DATA_DIR', '/shared'), ('HOME', '/root'), ('ATLAS_AUTH_MODE', 'token'),
    ('OIDC_ACCEPT_PROXY_HEADER', '1'),
])
def test_runtime_rejects_weakened_boundary(monkeypatch, name, value):
    from agent_service import isolated_runtime
    for key, setting in {'DATA_DIR': '/data', 'HOME': '/data/home',
                         'ATLAS_AUTH_MODE': 'oidc', 'OIDC_ACCEPT_PROXY_HEADER': '0'}.items():
        monkeypatch.setenv(key, setting)
    monkeypatch.setattr(isolated_runtime.os, 'geteuid', lambda: 10001)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        isolated_runtime.main()


def test_runtime_rejects_root(monkeypatch):
    from agent_service import isolated_runtime
    monkeypatch.setattr(isolated_runtime.os, 'geteuid', lambda: 0)
    with pytest.raises(ValueError):
        isolated_runtime.main()
