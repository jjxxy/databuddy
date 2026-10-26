import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

import quickstart
from agent_service.quickstart_health import ready


def configured(path, **values):
    model = {'base_url': 'https://models.example.org/v1', 'model': 'finance-model',
             'api_key': 'FIXTURE-PRIVATE-KEY', 'share_data': True, **values}
    (path / 'llm.json').write_text(json.dumps(model))
    return model


def test_first_run_creates_only_editable_model_template_and_no_fake_connection(tmp_path):
    with pytest.raises(ValueError, match='已生成 llm.json'):
        quickstart.setup(tmp_path)
    assert (tmp_path / 'llm.json').is_file() and not (tmp_path / '.env').exists()
    with pytest.raises(ValueError, match='填写 LLM'):
        quickstart.setup(tmp_path)


def test_existing_partial_env_gains_secret_once_and_never_executes_content(tmp_path):
    configured(tmp_path)
    path = tmp_path / '.env';path.write_text('CUSTOM=literal$(touch hacked)\nAPP_TOKEN=\n')
    result = quickstart.setup(tmp_path);first = path.read_bytes()
    values = quickstart.read_env(path)
    assert result['share_data'] and len(values['APP_TOKEN']) >= 32
    assert values['CUSTOM'] == 'literal$(touch hacked)' and not (tmp_path / 'hacked').exists()
    assert not quickstart.setup(tmp_path)['secrets_generated']
    assert path.read_bytes() == first and first.count(b'APP_TOKEN=') == 1
    assert b'FIXTURE-PRIVATE-KEY' not in first


def test_legacy_three_variable_env_is_migrated_without_enabling_content_reading(tmp_path):
    (tmp_path / '.env').write_text('LLM_BASE_URL=https://models.example.org/v1\nLLM_MODEL=finance\nLLM_API_KEY=fixture\n')
    quickstart.setup(tmp_path)
    assert json.loads((tmp_path / 'llm.json').read_text())['share_data'] is False
    assert len(quickstart.read_env(tmp_path / '.env')['APP_TOKEN']) >= 32


def test_bad_model_file_is_not_echoed_and_short_existing_token_is_not_replaced(tmp_path):
    (tmp_path / 'llm.json').write_text('{"api_key":"FIXTURE-PRIVATE-KEY", BAD JSON')
    result = subprocess.run([sys.executable, str(quickstart.ROOT / 'quickstart.py'),
        'setup', '--directory', str(tmp_path)], text=True, capture_output=True)
    assert result.returncode == 2 and 'FIXTURE-PRIVATE-KEY' not in result.stdout + result.stderr
    configured(tmp_path)
    path = tmp_path / '.env';path.write_text('APP_TOKEN=short\n');original = path.read_bytes()
    with pytest.raises(ValueError, match='太短'):
        quickstart.setup(tmp_path)
    assert path.read_bytes() == original


def test_up_propagates_failed_health_and_stop_never_deletes_volume(tmp_path, monkeypatch, capsys):
    configured(tmp_path);commands = []
    monkeypatch.setattr(quickstart.shutil, 'which', lambda _: '/fixture/docker')
    def run(command, **kwargs):
        commands.append(command);return subprocess.CompletedProcess(command, 1)
    monkeypatch.setattr(quickstart.subprocess, 'run', run)
    assert quickstart.main(['up', '--directory', str(tmp_path)]) == 1
    assert '--wait' in commands[0] and '--wait-timeout' in commands[0]
    assert 'FIXTURE-PRIVATE-KEY' not in repr(commands) + capsys.readouterr().out
    quickstart.main(['down', '--directory', str(tmp_path)])
    assert commands[-1][-1] == 'down' and '-v' not in commands[-1] and '--volumes' not in commands[-1]


def test_model_secrets_excluded_from_image_and_health_covers_every_role():
    root = quickstart.ROOT
    compose = yaml.safe_load((root / 'compose.quickstart.yaml').read_text())
    atlas = compose['services']['atlas']
    assert set(compose['services']) == {'atlas'}
    assert atlas['environment']['DATABASE_URL'] == 'sqlite:////data/metadata.sqlite'
    assert atlas['cap_drop'] == ['ALL'] and set(atlas['cap_add']) == {'CHOWN','SETUID','SETGID','DAC_OVERRIDE'}
    assert 'python' in atlas['healthcheck']['test']
    assert 'llm.json' in (root / '.dockerignore').read_text().splitlines()
    assert 'llm.json' in (root / '.gitignore').read_text().splitlines()
    healthy = {'planner_mode': 'llm', 'worker': {'healthy': True}, 'agent': {'healthy': True}}
    assert ready(healthy)
    for role in ('worker', 'agent'):
        assert not ready({**healthy, role: {'healthy': False}})
    assert not ready({**healthy, 'planner_mode': 'rules'})


def test_previous_container_heartbeats_cannot_mark_replacement_ready():
    def role(instance):
        return {'healthy': True, 'nodes': [{'instance': instance, 'healthy': True}]}
    value = {'planner_mode': 'llm', 'worker': role('old-container'), 'agent': role('old-container')}
    assert not ready(value, instance='new-container')
    value['worker'] = role('new-container')
    assert not ready(value, instance='new-container')
    value['agent'] = role('new-container')
    assert ready(value, instance='new-container')
