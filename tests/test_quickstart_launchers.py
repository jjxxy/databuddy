"""Launcher contracts with a fake Docker CLI; no daemon or Windows execution."""
import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODEL_SECRET = 'fixture-model-key-never-print-3984'


def launcher(tmp_path, env_text=None):
    # Exercise shell quoting with Chinese, spaces, comma, quote and apostrophe.
    bundle = tmp_path / '业务, 空格 "引号"和单\'引号'
    bundle.mkdir()
    shutil.copyfile(ROOT / 'start.sh', bundle / 'start.sh')
    (bundle / 'quickstart.py').write_text('# fake bootstrap target\n')
    (bundle / 'compose.quickstart.yaml').write_text('services: {}\n')
    (bundle / 'llm.json').write_text(json.dumps({'api_key': MODEL_SECRET}))
    if env_text is not None:
        (bundle / '.env').write_text(env_text)
    bindir = tmp_path / 'bin'; bindir.mkdir()
    calls = tmp_path / 'docker-calls.jsonl'
    fake = bindir / 'docker'
    fake.write_text('#!' + sys.executable + '\n' + r'''
import csv, json, os, pathlib, sys
args = sys.argv[1:]
with pathlib.Path(os.environ['LAUNCHER_FAKE_LOG']).open('a') as output:
    output.write(json.dumps(args, ensure_ascii=False) + '\n')
if args[0] == 'info':
    print(os.environ.get('LAUNCHER_FAKE_OS', 'linux'))
    sys.exit(int(os.environ.get('LAUNCHER_INFO_EXIT', '0')))
if args[:2] == ['compose', 'version']:
    print('Docker Compose version fixture')
    sys.exit(0)
if args[0] == 'run':
    parts = next(csv.reader([args[args.index('--mount') + 1]]))
    source = next(part.split('=', 1)[1] for part in parts if part.startswith('source='))
    folder = pathlib.Path(source)
    if args[-1] == 'setup':
        code = int(os.environ.get('LAUNCHER_SETUP_EXIT', '0'))
        if code:
            print('请填写 llm.json 三项')
            sys.exit(code)
        env = folder / '.env'
        if not env.exists():
            env.write_text('APP_TOKEN=fixture-owner-token-keep-private\nATLAS_HTTP_PORT=8123\n')
        print('配置通过；未输出模型密钥')
    elif args[-1] == 'info':
        print('访问口令：fixture-owner-token-keep-private')
    else:
        raise AssertionError('Unknown bootstrap command')
    sys.exit(0)
if args[0] == 'compose':
    sys.exit(int(os.environ.get('LAUNCHER_COMPOSE_EXIT', '0')))
raise AssertionError('Unexpected command')
''')
    fake.chmod(0o755)
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ.get('PATH', ''),
               LAUNCHER_FAKE_LOG=str(calls), ATLAS_NO_OPEN='1', PYTHONUTF8='1')
    return bundle, calls, env


def run(bundle, env, *arguments):
    return subprocess.run(['/bin/bash', str(bundle / 'start.sh'), *arguments],
                          cwd=bundle.parent, env=env, text=True, capture_output=True, timeout=15)


def recorded(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_docker_only_up_binds_directory_and_waits_before_showing_local_info(tmp_path):
    bundle, calls, env = launcher(tmp_path)
    result = run(bundle, env)
    assert result.returncode == 0, result.stderr
    commands = recorded(calls)
    assert commands[0] == ['info', '--format', '{{.OSType}}']
    assert commands[1] == ['compose', 'version']
    assert commands[2][-3:] == ['python', 'quickstart.py', 'setup']
    assert commands[3] == ['compose', '--env-file', '.env', '-f', 'compose.quickstart.yaml',
                           'up', '-d', '--build', '--wait', '--wait-timeout', '180']
    assert commands[4][-3:] == ['python', 'quickstart.py', 'info']
    for command in (commands[2], commands[4]):
        assert command[command.index('--network') + 1] == 'none'
        assert command[command.index('--user') + 1] == f'{os.getuid()}:{os.getgid()}'
        assert command[command.index('--workdir') + 1] == '/bundle'
        parts = next(csv.reader([command[command.index('--mount') + 1]]))
        assert parts == ['type=bind', 'source=' + str(bundle), 'target=/bundle']
        assert 'python:3.12-slim' in command
    assert 'http://localhost:8123' in result.stdout
    assert 'fixture-owner-token-keep-private' in result.stdout
    assert MODEL_SECRET not in result.stdout + result.stderr + calls.read_text()
    assert 'docker.sock' not in calls.read_text()


def test_setup_failure_preserves_exit_two_and_does_not_start_services(tmp_path):
    bundle, calls, env = launcher(tmp_path); env['LAUNCHER_SETUP_EXIT'] = '2'
    result = run(bundle, env, 'up', '--no-open')
    assert result.returncode == 2
    assert '尚未启动服务' in result.stderr
    assert not any('up' in call for call in recorded(calls))
    assert not any(call[-1] == 'info' for call in recorded(calls))


def test_compose_failure_does_not_claim_ready_or_print_access_token(tmp_path):
    bundle, calls, env = launcher(tmp_path); env['LAUNCHER_COMPOSE_EXIT'] = '7'
    result = run(bundle, env, 'up')
    assert result.returncode == 7
    assert '服务未能就绪' in result.stderr
    assert 'fixture-owner-token-keep-private' not in result.stdout
    assert not any(call[-1] == 'info' for call in recorded(calls))


@pytest.mark.parametrize('action,command', [('down', ['down']), ('status', ['ps', '--all']),
                                           ('logs', ['logs', '--tail', '100', '--follow'])])
def test_management_never_bootstraps_or_deletes_volumes(tmp_path, action, command):
    content = 'APP_TOKEN=unchanged-existing-token\nATLAS_HTTP_PORT=9234\n'
    bundle, calls, env = launcher(tmp_path, content)
    result = run(bundle, env, action)
    assert result.returncode == 0, result.stderr
    commands = recorded(calls)
    assert commands[-1] == ['compose', '--env-file', '.env', '-f', 'compose.quickstart.yaml', *command]
    assert all(call[0] != 'run' for call in commands)
    assert all('-v' not in call and '--volumes' not in call for call in commands)
    assert (bundle / '.env').read_text() == content


def test_docker_checks_fail_before_bootstrap(tmp_path):
    bundle, calls, env = launcher(tmp_path); env['LAUNCHER_INFO_EXIT'] = '1'
    result = run(bundle, env, 'up')
    assert result.returncode == 1 and 'Docker 尚未就绪' in result.stderr
    assert len(recorded(calls)) == 1
    env['LAUNCHER_INFO_EXIT'] = '0'; env['LAUNCHER_FAKE_OS'] = 'windows'; calls.unlink()
    result = run(bundle, env, 'up')
    assert result.returncode == 1 and 'Linux 容器模式' in result.stderr
    assert len(recorded(calls)) == 1


@pytest.mark.parametrize('port_line', ['ATLAS_HTTP_PORT=9234', 'export ATLAS_HTTP_PORT="09234"',
                                      "ATLAS_HTTP_PORT='9234'", ' ATLAS_HTTP_PORT=9234 # local port'])
def test_open_reads_only_numeric_port_and_never_sources_env(tmp_path, port_line):
    marker = tmp_path / 'must-not-execute'
    content = f'APP_TOKEN=$(touch "{marker}")\n{port_line}\n'
    bundle, calls, env = launcher(tmp_path, content)
    result = run(bundle, env, 'open', '--no-open')
    assert result.returncode == 0, result.stderr
    assert 'http://localhost:9234' in result.stdout
    assert 'APP_TOKEN' not in result.stdout and not marker.exists()
    assert recorded(calls) == []


def test_invalid_port_and_unknown_action_do_not_run_commands(tmp_path):
    marker = tmp_path / 'must-not-execute'
    bundle, calls, env = launcher(tmp_path, f'ATLAS_HTTP_PORT=$(touch "{marker}")\n')
    result = run(bundle, env, 'open')
    assert result.returncode == 1 and not marker.exists()
    assert '数字端口' in result.stderr and recorded(calls) == []
    assert run(bundle, env, 'delete-data').returncode == 2
    assert recorded(calls) == []


def test_windows_entry_contract_is_docker_only_and_powershell_51_utf8():
    raw = (ROOT / 'start.ps1').read_bytes()
    assert raw.startswith(b'\xef\xbb\xbf')
    script = raw.decode('utf-8-sig')
    assert '[Console]::OutputEncoding = $utf8' in script
    assert 'Invoke-Expression' not in script and 'Get-Command python' not in script
    assert "'python:3.12-slim', 'python', 'quickstart.py', $Command" in script
    assert "'--mount', $mount, '--workdir', '/bundle'" in script
    assert "@('up', '-d', '--build', '--wait', '--wait-timeout', '180')" in script
    assert "'down' { Invoke-Docker -CommandArgs ($compose + @('down')) }" in script
    assert 'docker.sock' not in script and 'LLM_API_KEY' not in script
    assert 'Get-LocalUrl' in script and 'ReadAllLines' in script
    command = (ROOT / 'start.cmd').read_text()
    assert '-NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*' in command
    assert 'exit /b %atlas_exit%' in command
