"""Check that committed Compose services have a usable image definition.

This is a static configuration check, not a Docker build or deployment test.
"""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_roles_resolve_the_committed_image_and_keep_compatibility_mode():
    config = yaml.safe_load((ROOT / 'compose.yaml').read_text())
    roles = {'api', 'worker', 'agent', 'dagster-web', 'dagster-daemon'}
    assert roles <= config['services'].keys()
    for name in roles:
        service = config['services'][name]
        build = service['build']
        context = build if isinstance(build, str) else build.get('context', '.')
        dockerfile = 'Dockerfile' if isinstance(build, str) else build.get('dockerfile', 'Dockerfile')
        path = ROOT / context / dockerfile
        assert path.is_file(), f'{name} refers to missing {path}'
        assert service['environment']['NEXUS_OPENCODE_RUNTIME'] == '0'
        assert service['command'], f'{name} must start its own role'
    contents = (ROOT / 'Dockerfile').read_text()
    assert 'COPY requirements.lock ' in contents
    assert 'pip install --no-cache-dir -r /tmp/requirements.lock' in contents
    assert 'USER 10001:10001' in contents
    assert 'deploy/python-cache-entrypoint.sh' in contents
    assert 'node /usr/local/bin/node' in contents
    assert (ROOT / 'deploy/python-cache-entrypoint.sh').is_file()
