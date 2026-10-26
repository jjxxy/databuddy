"""Linux/WSL local lifecycle management; stdlib only, no Docker or shell evaluation."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.request import ProxyHandler, Request, build_opener
import webbrowser

ROOT = Path(__file__).resolve().parent
DEFAULT_STATE = Path.home() / '.local/share/atlas-databuddy-020'
DEFAULT_PYTHON = Path.home() / '.venvs/atlas-databuddy-020/bin/python'
DEFAULT_OPENCODE = Path.home() / '.local/bin/opencode-atlas-2.1.2'


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None


@contextmanager
def state_lock(state):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    with (state / 'launcher.lock').open('a') as stream:
        os.chmod(state / 'launcher.lock', 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def process_identity(pid):
    """PID plus kernel start time and boot ID prevents signalling reused PIDs."""
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        fields = stat[stat.rfind(')') + 2:].split()
        if fields[0] == 'Z':
            return None
        return {'start_ticks': fields[19],
                'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except (OSError, IndexError):
        return None


def owns_process(record):
    if not isinstance(record, dict) or type(record.get('pid')) is not int or record['pid'] <= 1:
        return False
    pid = record['pid']
    if process_identity(pid) != record.get('identity') or not record.get('identity'):
        return False
    try:
        command = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        environment = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
        return (command[1:3] == [b'-m', b'agent_service.cloud']
                and ('ATLAS_NATIVE_INSTANCE=' + record['instance']).encode() in environment)
    except (OSError, KeyError):
        return False


def credentials(state):
    path = state / 'credentials.json'
    value = read_json(path)
    if value is None:
        value = {'APP_TOKEN': secrets.token_urlsafe(36),
                 'ATLAS_OPERATOR_TOKEN': secrets.token_urlsafe(36)}
        save_json(path, value)
    if (not isinstance(value, dict) or any(not isinstance(value.get(k), str) or len(value[k]) < 16
            for k in ('APP_TOKEN', 'ATLAS_OPERATOR_TOKEN'))
            or value['APP_TOKEN'] == value['ATLAS_OPERATOR_TOKEN']):
        raise ValueError('Invalid local credentials; restore credentials.json from your backup.')
    path.chmod(0o600)
    return value


def request_json(record, path, token=None):
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    request = Request(f'http://127.0.0.1:{record["port"]}{path}', headers=headers)
    with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
        return json.loads(response.read(512 * 1024))


def service_ready(record, token):
    try:
        if not owns_process(record) or request_json(record, '/ready').get('ready') is not True:
            return False
        status = request_json(record, '/api/status', token)
        return all(any(node.get('instance') == record['instance'] and node.get('healthy') is True
                       for node in status.get(role, {}).get('nodes', []))
                   for role in ('worker', 'agent'))
    except (OSError, ValueError, KeyError):
        return False


def display(record):
    print(f'Atlas DataBuddy v0.20.0: http://127.0.0.1:{record["port"]}')
    if record.get('model_configured'):
        print('Model configured; engine: ' + record['engine'] + '. Real model acceptance is separate from service readiness.')
    else:
        print('MODEL UNCONFIGURED: UI and data backend only; AI tasks require a real model. Compatibility engine is active; no mock model.')


def stop_service(record):
    if not owns_process(record):
        return True
    os.kill(record['pid'], signal.SIGTERM)
    deadline = time.monotonic() + 25
    while owns_process(record) and time.monotonic() < deadline:
        time.sleep(0.2)
    return not owns_process(record)


def persisted_model(state):
    database = state / 'data/metadata.sqlite'
    if not database.is_file():
        return False
    try:
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'").fetchone():
                return False
            return bool(connection.execute("SELECT 1 FROM items WHERE kind='system' AND id IN ('model-provider','model-catalog') LIMIT 1").fetchone())
    except sqlite3.Error:
        raise ValueError('Cannot inspect existing model metadata. Restore or repair the database before starting.') from None


def validate_opencode(binary):
    version = subprocess.run([str(binary), '--version'], capture_output=True, text=True, timeout=15)
    if version.returncode or version.stdout.strip() not in ('2.1.2', 'opencode v2.1.2'):
        raise ValueError('This release requires OpenCode 2.1.2. Install the pinned binary; another version is not accepted.')


def start(args, state):
    old = read_json(state / 'process.json')
    access = credentials(state)
    if owns_process(old):
        if not service_ready(old, access['APP_TOKEN']):
            raise ValueError('Atlas supervisor is running but services are not ready. Check logs or stop before restarting.')
        display(old)
        return old
    python = Path(args.python).expanduser().absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError('Virtualenv Python is missing. Follow LOCAL-DEPLOYMENT.md or provide --python.')
    model_path = Path(args.model_config).expanduser().absolute() if args.model_config else ROOT / 'llm.json'
    has_model_file = model_path.exists()
    if has_model_file:
        from agent_service.quickstart_model import load_configuration
        load_configuration(model_path)  # Validation never prints the model credentials.
    elif args.model_config:
        raise ValueError('Explicit model configuration file does not exist.')
    model_configured = has_model_file or persisted_model(state)
    binary = Path(args.opencode_binary).expanduser().absolute()
    has_binary = binary.is_file() and os.access(binary, os.X_OK)
    engine = args.engine
    if engine == 'auto':
        engine = 'opencode' if model_configured else 'compatibility'
    if engine == 'opencode' and (not model_configured or not has_binary):
        raise ValueError('OpenCode requires a valid model file and executable --opencode-binary. Use --engine compatibility explicitly if intended.')
    if engine == 'opencode':
        validate_opencode(binary)
    # A bind check avoids starting a second instance on an occupied local port.
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', args.port))
        except OSError:
            raise ValueError(f'Local port {args.port} is already in use; choose --port or stop its owner.') from None
    data = state / 'data'
    data.mkdir(exist_ok=True, mode=0o700)
    instance = 'native-' + secrets.token_hex(12)
    env = dict(os.environ)
    for key in ('ATLAS_INITIAL_MODEL', 'ATLAS_LLM_CONFIG_FILE', 'ATLAS_DROP_PRIVILEGES',
                'ATLAS_LOCAL_MODE', 'ATLAS_LOCAL_WORKSPACE', 'ATLAS_QUICKSTART_CONTAINER',
                'LLM_BASE_URL', 'LLM_API_KEY', 'LLM_MODEL'):
        env.pop(key, None)
    env.update(access)
    env.update(DATA_DIR=str(data), DATABASE_URL='sqlite:///' + str(data / 'metadata.sqlite'),
               PORT=str(args.port), ATLAS_API_HOST='127.0.0.1', ATLAS_INSTANCE_ID=instance,
               ATLAS_NATIVE_INSTANCE=instance, ATLAS_DEPLOYMENT_PROFILE='single', ATLAS_AUTH_MODE='token',
               ATLAS_OPENCODE_RUNTIME='1' if engine == 'opencode' else '0',
               ATLAS_OPENCODE_BINARY=str(binary), APP_TIMEZONE='Asia/Shanghai', PYTHONUTF8='1',
               RUNTIME__DLTHUB_TELEMETRY='false', DBT_SEND_ANONYMOUS_USAGE_STATS='false')
    if has_model_file:
        env['ATLAS_LLM_CONFIG_FILE'] = str(model_path)
    # Rotation bounds routine logs without losing the previous startup diagnosis.
    logfile = state / 'atlas.log'
    if logfile.exists() and logfile.stat().st_size > 8 * 1024 * 1024:
        logfile.replace(state / 'atlas.previous.log')
    with logfile.open('ab', buffering=0) as output:
        logfile.chmod(0o600)
        process = subprocess.Popen([str(python), '-m', 'agent_service.cloud'], cwd=ROOT,
                                   env=env, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    record = {'pid': process.pid, 'identity': process_identity(process.pid), 'instance': instance,
              'port': args.port, 'engine': engine, 'model_configured': model_configured}
    save_json(state / 'process.json', record)
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ValueError(f'Atlas exited with code {process.returncode}. See {logfile}; no model fallback was attempted.')
        if service_ready(record, access['APP_TOKEN']):
            display(record)
            return record
        time.sleep(1)
    stopped = stop_service(record)
    raise ValueError(f'Services did not become ready within {args.wait}s. See {logfile}. '
                     + ('Supervisor stopped.' if stopped else 'Supervisor still running; use stop and inspect logs.'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop', 'status', 'open', 'token', 'logs'), nargs='?', default='start')
    parser.add_argument('--state-dir', default=str(DEFAULT_STATE))
    parser.add_argument('--python', default=str(DEFAULT_PYTHON))
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--model-config')
    parser.add_argument('--opencode-binary', default=str(DEFAULT_OPENCODE))
    parser.add_argument('--engine', choices=('auto', 'opencode', 'compatibility'), default='auto')
    parser.add_argument('--wait', type=int, default=180)
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or not 1 <= args.wait <= 900:
        parser.error('port must be 1..65535 and wait 1..900 seconds')
    if sys.platform != 'linux':
        parser.error('Run through start-native.cmd on Windows, or with Linux/WSL Python.')
    state = Path(args.state_dir).expanduser().absolute()
    os.umask(0o077)
    try:
        with state_lock(state):
            record = read_json(state / 'process.json')
            if args.action == 'token':
                value = read_json(state / 'credentials.json')
                if value is None:
                    raise ValueError('Start Atlas first to create local credentials.')
                print(value['APP_TOKEN'])  # Explicit owner request only; never print the operator token.
            elif args.action == 'logs':
                path = state / 'atlas.log'
                if path.exists():
                    print('\n'.join(path.read_text(encoding='utf-8', errors='replace').splitlines()[-100:]))
                else:
                    print('No log file yet.')
            elif args.action == 'stop':
                if not stop_service(record):
                    raise ValueError('Supervisor did not stop in 25 seconds; inspect logs. No unrelated process was signalled.')
                (state / 'process.json').unlink(missing_ok=True)
                print('Atlas stopped. Data and credentials retained.')
            elif args.action == 'status':
                if not owns_process(record):
                    print('Atlas is stopped.')
                    return 1
                display(record)
                if not service_ready(record, credentials(state)['APP_TOKEN']):
                    print('Supervisor running; services not ready.')
                    return 1
                print('API, worker and agent are ready.')
            elif args.action == 'start':
                record = start(args, state)
            elif not owns_process(record):
                raise ValueError('Atlas is stopped. Run start first.')
            else:
                display(record)
        if args.action in ('start', 'open') and not args.no_open:
            webbrowser.open(f'http://127.0.0.1:{record["port"]}')
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
