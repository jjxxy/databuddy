"""First-run model files, private endpoint selection, and secret-safe startup."""
import json
import os
import subprocess
import sys

import pytest

from agent_service.quickstart_model import load_configuration


def configuration(**updates):
    return {'base_url': 'https://model.test/v1', 'model': 'fixture-model',
            'api_key': 'FIXTURE-QUICKSTART-SECRET', **updates}


def write_configuration(tmp_path, value=None):
    path = tmp_path / 'llm.json'
    path.write_text(json.dumps(configuration() if value is None else value), encoding='utf-8')
    path.chmod(0o600)
    return path


def test_configuration_loader_has_no_runtime_imports_and_safe_defaults(tmp_path):
    path = write_configuration(tmp_path)
    value = load_configuration(path)
    assert value['share_data'] is False and value['tool_protocol'] == 'json'
    assert value['base_url'] == 'https://model.test/v1'
    script = "import sys;from agent_service.quickstart_model import load_configuration;load_configuration(sys.argv[1]);assert 'agent_service.config' not in sys.modules;assert 'agent_service.provider' not in sys.modules"
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, str(path)],
                            capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('updates', [
    {'base_url': None}, {'model': 123}, {'api_key': ''}, {'api_key': {'nested': 'secret'}},
    {'share_data': 'true'}, {'share_data': 1}, {'tool_protocol': 'automatic'},
    {'engine': 'unknown'}, {'model': 'x' * 121}, {'unexpected': 'secret'},
])
def test_configuration_rejects_missing_types_extra_fields_and_invalid_values(tmp_path, updates):
    path = write_configuration(tmp_path, configuration(**updates))
    with pytest.raises(ValueError) as error:load_configuration(path)
    assert 'FIXTURE-' not in str(error.value)


@pytest.mark.parametrize('raw', [
    b'{"base_url":"https://model.test/v1","model":"fixture","api_key":"secret","share_data":NaN}',
    b'{"base_url":"https://model.test/v1","model":"fixture","api_key":"secret","api_key":"duplicate"}',
    b'[]', b'not-json FIXTURE-QUICKSTART-SECRET', b'\xff',
])
def test_configuration_rejects_ambiguous_or_non_json_without_reflecting_input(tmp_path, raw):
    path = tmp_path / 'llm.json'; path.write_bytes(raw)
    with pytest.raises(ValueError) as error:load_configuration(path)
    assert 'FIXTURE-' not in str(error.value) and 'duplicate' not in str(error.value)


def test_configuration_is_bounded_and_requires_a_regular_file(tmp_path):
    path = write_configuration(tmp_path, configuration(api_key='s' * 65536))
    with pytest.raises(ValueError, match='64KB'):load_configuration(path)
    with pytest.raises(ValueError):load_configuration(tmp_path)
    with pytest.raises(ValueError):load_configuration(tmp_path / 'missing.json')
    if hasattr(os, 'mkfifo'):
        fifo = tmp_path / 'not-a-file'; os.mkfifo(fifo)
        with pytest.raises(ValueError, match='普通文件'):load_configuration(fifo)


@pytest.mark.parametrize('url,container,expected', [
    ('https://model.test/v1/chat/completions/', False, 'https://model.test/v1'),
    ('https://model.test/chat/completions', False, 'https://model.test'),
    ('http://localhost:11434/v1/chat/completions', True, 'http://host.docker.internal:11434/v1'),
    ('http://127.0.0.1:8000/prefix/v1/', True, 'http://host.docker.internal:8000/prefix/v1'),
    ('http://[::1]:8000/v1', True, 'http://host.docker.internal:8000/v1'),
    ('http://localhost:11434/v1', False, 'http://localhost:11434/v1'),
    ('https://private-model.test:8443/v1', True, 'https://private-model.test:8443/v1'),
])
def test_endpoint_normalization_preserves_user_file_port_and_path(tmp_path, url, container, expected):
    path = write_configuration(tmp_path, configuration(base_url=url))
    original = path.read_bytes()
    assert load_configuration(path, container=container)['base_url'] == expected
    assert path.read_bytes() == original


@pytest.mark.parametrize('url', [
    'https://169.254.169.254/v1', 'https://metadata.google.internal/v1',
    'https://metadata.azure.internal./v1', 'https://[fe80::1]/v1',
    'https://[::ffff:169.254.169.254]/v1',
    'https://user:FIXTURE-QUICKSTART-SECRET@model.test/v1',
    'https://model.test/v1?api_key=FIXTURE-QUICKSTART-SECRET',
])
def test_explicit_model_endpoint_never_grants_metadata_or_url_credentials(tmp_path, url):
    path = write_configuration(tmp_path, configuration(base_url=url))
    with pytest.raises(ValueError) as error:load_configuration(path, container=True)
    assert 'FIXTURE-' not in str(error.value)


def subprocess_environment(tmp_path, path):
    data = tmp_path / 'workspace-data'
    environment={'PATH': os.environ.get('PATH', ''), 'DATA_DIR': str(data),
            'DATABASE_URL': 'sqlite:///' + str(data / 'metadata.sqlite'),
            'APP_TOKEN': 'fixture-only-access-token-123456789',
            'ATLAS_LLM_CONFIG_FILE': str(path), 'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
    for key in ('SystemRoot','SYSTEMROOT','WINDIR'):
        if os.environ.get(key):environment[key]=os.environ[key]
    return environment


def test_cloud_file_provisioning_is_once_only_encrypted_and_drops_bootstrap_environment(tmp_path):
    path = write_configuration(tmp_path, configuration(base_url='http://localhost:11434/v1/chat/completions', share_data=True))
    environment = subprocess_environment(tmp_path, path)
    environment.update(ATLAS_QUICKSTART_CONTAINER='1', API_ALLOWED_HOSTS='existing.test')
    script = '''import json,os
from agent_service import provider
from agent_service.store import put,seal,unseal,get
from agent_service.cloud import prepare
calls=[]
def configure(value):
    raw=value.model_dump();raw['api_key']=value.api_key.get_secret_value();calls.append(raw['base_url'])
    put('system',{'secret_config':seal(raw)},'model-provider')
provider.configure=configure
assert prepare()==8000
assert 'ATLAS_LLM_CONFIG_FILE' not in os.environ and 'ATLAS_INITIAL_MODEL' not in os.environ
assert provider.settings()['share_data'] is True
assert provider.settings()['api_key']=='FIXTURE-QUICKSTART-SECRET'
assert 'host.docker.internal' in os.environ['API_ALLOWED_HOSTS'].split(',')
assert 'existing.test' in os.environ['API_ALLOWED_HOSTS'].split(',')
# A later UI-selected endpoint wins over the bootstrap file after restart.
current=provider.settings();current.update(base_url='https://later-ui.test/v1',share_data=False)
put('system',{'secret_config':seal(current)},'model-provider')
os.environ['ATLAS_LLM_CONFIG_FILE']=__import__('sys').argv[1]
os.environ['API_ALLOWED_HOSTS']='existing.test'
assert prepare()==8000
assert provider.settings()['base_url']=='https://later-ui.test/v1'
assert provider.settings()['share_data'] is False
assert os.environ['API_ALLOWED_HOSTS']=='existing.test,later-ui.test'
assert calls==['http://host.docker.internal:11434/v1']
assert 'FIXTURE-QUICKSTART-SECRET' not in get('system','model-provider')['secret_config']
print('BOOTSTRAP_ONCE_AND_RESTART_OK')
'''
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, str(path)], env=environment,
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'BOOTSTRAP_ONCE_AND_RESTART_OK'
    assert 'FIXTURE-' not in result.stdout + result.stderr


def test_cloud_reads_private_configuration_before_switching_identity(tmp_path):
    path = write_configuration(tmp_path)
    environment = subprocess_environment(tmp_path, path)
    environment['ATLAS_DROP_PRIVILEGES'] = '1'
    script = '''import os,sys
from pathlib import Path
from agent_service import cloud
# The host test environment has only UID 0 mapped. Replace the identity system
# calls with a boundary that makes the original file unavailable, so this
# verifies read ordering without claiming a real UID 10001 container test.
os.geteuid=lambda:0
os.chown=lambda *args:None
os.setgroups=lambda *args:None
os.setgid=lambda *args:None
def switched(uid):
    assert uid==10001
    Path(sys.argv[1]).unlink()
os.setuid=switched
from agent_service import provider
from agent_service.store import put,seal
def configure(value):
    assert not Path(sys.argv[1]).exists()
    assert value.api_key.get_secret_value()=='FIXTURE-QUICKSTART-SECRET'
    raw=value.model_dump();raw['api_key']=value.api_key.get_secret_value()
    put('system',{'secret_config':seal(raw)},'model-provider')
provider.configure=configure
assert cloud.prepare()==8000
assert provider.status()['configured'] is True
print('READ_BEFORE_IDENTITY_SWITCH_OK')
'''
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, str(path)], env=environment,
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'READ_BEFORE_IDENTITY_SWITCH_OK'
    assert 'FIXTURE-' not in result.stdout + result.stderr


@pytest.mark.parametrize('failure', ['invalid-file', 'both-methods'])
def test_cloud_model_read_errors_do_not_log_private_configuration(tmp_path, failure):
    path = write_configuration(tmp_path)
    environment = subprocess_environment(tmp_path, path)
    if failure == 'invalid-file':path.write_text('invalid JSON FIXTURE-QUICKSTART-SECRET',encoding='utf-8')
    else:environment['ATLAS_INITIAL_MODEL'] = json.dumps(configuration())
    result = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'agent_service.cloud'], env=environment,
                            capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 2
    assert 'FIXTURE-' not in result.stdout + result.stderr
    assert 'Cloud configuration or storage initialization failed' in result.stderr
