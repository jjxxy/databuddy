"""Offline, fail-closed manifest renderer for separate owner-workspace containers.

JSON is deliberately used for Compose (a YAML subset), without a YAML dependency.
This does not provision secrets, identities, TLS, infrastructure or containers.
"""
import argparse
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


class ManifestError(ValueError):
    pass


def _object(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ManifestError('Missing or unknown manifest fields')


def _text(value):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in value) or '$' in value:
        raise ManifestError('Invalid string or forbidden environment interpolation')
    return value


def _https(value, origin=False):
    value = _text(value)
    p = urlsplit(value)
    try:
        port = p.port
    except ValueError:
        raise ManifestError('Invalid URL port') from None
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.fragment or p.query or '?' in value or '#' in value or '\\' in value or not re.fullmatch(r'[a-zA-Z0-9.-]+', p.hostname) or (origin and p.path not in ('', '/')) or (port is not None and not 1 <= port <= 65535):
        raise ManifestError('Expected credential-free HTTPS URL')
    return value.rstrip('/') if origin else value


def _secret_path(value):
    value = _text(value)
    if not re.fullmatch(r'\./secrets/[a-zA-Z0-9][a-zA-Z0-9_.-]*\.json', value) or '..' in value:
        raise ManifestError('Secret path must be ./secrets/<safe-name>.json')
    return value


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError('Duplicate JSON key: ' + key)
        result[key] = value
    return result


def load_manifest(path):
    return json.loads(Path(path).read_text(), object_pairs_hook=_no_duplicate_keys)


def render(manifest):
    """Return (compose, gateway configuration); deterministic and side-effect free."""
    _object(manifest, ('schema_version', 'project', 'public_origin', 'oidc', 'workspaces'), ('gateway_port',))
    if type(manifest['schema_version']) is not int or manifest['schema_version'] != 1:
        raise ManifestError('Unsupported schema version')
    project = _text(manifest['project'])
    if not re.fullmatch(r'atlas-[a-z][a-z0-9-]{0,30}', project):
        raise ManifestError('Project must be atlas-<lowercase-name>')
    origin = _https(manifest['public_origin'], origin=True)
    oidc = manifest['oidc']
    _object(oidc, ('issuer', 'audience', 'jwks_url', 'jwks_file'))
    issuer = _https(oidc['issuer'])
    audience = _text(oidc['audience'])
    jwks_url = _https(oidc['jwks_url'])
    jwks_file = _secret_path(oidc['jwks_file'])
    port = manifest.get('gateway_port', 8080)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ManifestError('Gateway port must be an integer 1024..65535')
    workspaces = manifest['workspaces']
    if not isinstance(workspaces, list) or not 1 <= len(workspaces) <= 100:
        raise ManifestError('Expected 1..100 workspaces')
    ids, subjects, secret_paths = set(), set(), {jwks_file}
    checked = []
    for workspace in workspaces:
        _object(workspace, ('id', 'subjects', 'model_config_file'))
        wid = _text(workspace['id'])
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,30}', wid) or wid in ids:
            raise ManifestError('Workspace IDs must be unique safe lowercase names')
        ids.add(wid)
        entries = workspace['subjects']
        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise ManifestError('Expected explicit subject list')
        for subject in entries:
            _text(subject)
            if ',' in subject or '*' in subject or subject in subjects or any(c.isspace() for c in subject):
                raise ManifestError('Subjects must be exact, unique, and assigned to one workspace')
            subjects.add(subject)
        if 'model_config_file' in workspace:
            path = _secret_path(workspace['model_config_file'])
            if path in secret_paths:
                raise ManifestError('Workspace secret files may not be shared')
            secret_paths.add(path)
        checked.append(workspace)
    services, volumes, networks, secrets = {}, {}, {}, {'gateway_jwks': {'file': jwks_file}}
    gateway_workspaces = []
    for workspace in sorted(checked, key=lambda w: w['id']):
        wid = workspace['id']
        backend, volume, network = 'atlas-' + wid, 'data-' + wid, 'workspace-' + wid
        env_name = 'ATLAS_' + wid.upper().replace('-', '_') + '_APP_TOKEN'
        # Hyphens and underscores cannot collide: workspace IDs never admit underscores.
        environment = {
            'DATA_DIR': '/data', 'DATABASE_URL': 'sqlite:////data/metadata.sqlite',
            'HOME': '/data/home', 'ATLAS_AUTH_MODE': 'oidc',
            'OIDC_ISSUER': issuer, 'OIDC_AUDIENCE': audience, 'OIDC_JWKS_URL': jwks_url,
            'OIDC_ALLOWED_SUBJECTS': ','.join(sorted(workspace['subjects'])),
            'OIDC_ACCEPT_PROXY_HEADER': '0', 'ATLAS_DROP_PRIVILEGES': '0',
            'ATLAS_OPENCODE_RUNTIME': '1', 'ATLAS_NETWORK_MODE': 'connected',
            'APP_TOKEN': '${' + env_name + ':?Set a distinct random token of at least 16 characters}',
        }
        service = {
            'build': {'context': '../..', 'dockerfile': 'Dockerfile.enterprise'},
            'command': ['python', '-m', 'agent_service.isolated_runtime'],
            'user': '10001:10001', 'init': True, 'read_only': True,
            'environment': environment, 'volumes': [volume + ':/data'],
            'networks': [network], 'tmpfs': ['/tmp:rw,noexec,nosuid,size=268435456,mode=1777'],
            'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
            'mem_limit': '2g', 'cpus': 2.0, 'pids_limit': 256,
            'restart': 'unless-stopped', 'stop_grace_period': '30s',
            'healthcheck': {'test': ['CMD', 'python', '-c', "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3)"], 'interval': '30s', 'timeout': '5s', 'retries': 5},
        }
        if 'model_config_file' in workspace:
            secret = 'model-' + wid
            secrets[secret] = {'file': workspace['model_config_file']}
            service['secrets'] = [{'source': secret, 'target': 'model.json'}]
            environment['ATLAS_LLM_CONFIG_FILE'] = '/run/secrets/model.json'
        services[backend] = service
        volumes[volume] = {}
        networks[network] = {'driver': 'bridge'}
        gateway_workspaces.append({'id': wid, 'backend_url': 'http://' + backend + ':8000', 'subjects': sorted(workspace['subjects'])})
    services['gateway'] = {
        'build': {'context': '../..', 'dockerfile': 'Dockerfile.enterprise'},
        'command': ['python', '-m', 'uvicorn', 'agent_service.workspace_gateway:app', '--host', '0.0.0.0', '--port', '8080', '--no-proxy-headers'],
        'user': '10001:10001', 'init': True, 'read_only': True,
        'environment': {'ATLAS_WORKSPACE_GATEWAY_CONFIG': '/run/atlas/gateway.json'},
        'volumes': [{'type': 'bind', 'source': './gateway.json', 'target': '/run/atlas/gateway.json', 'read_only': True, 'bind': {'create_host_path': False}}],
        'secrets': [{'source': 'gateway_jwks', 'target': 'jwks.json'}],
        'networks': sorted(networks), 'ports': ['127.0.0.1:' + str(port) + ':8080'],
        'tmpfs': ['/tmp:rw,noexec,nosuid,size=67108864,mode=1777'],
        'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
        'mem_limit': '512m', 'cpus': 1.0, 'pids_limit': 64, 'restart': 'unless-stopped',
    }
    compose = {'name': project, 'services': services, 'volumes': volumes, 'networks': networks, 'secrets': secrets}
    gateway = {'schema_version': 1, 'public_origin': origin,
               'oidc': {'issuer': issuer, 'audience': audience, 'jwks_file': '/run/secrets/jwks.json'},
               'workspaces': gateway_workspaces, 'limits': {'max_concurrent_requests': 2}}
    return compose, gateway


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest')
    parser.add_argument('--output-dir', required=True, help='Use deploy/isolated for the fixed relative build context')
    args = parser.parse_args()
    compose, gateway = render(load_manifest(args.manifest))
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for name, value in (('compose.json', compose), ('gateway.json', gateway)):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    main()
