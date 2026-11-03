"""Linux/WSL local lifecycle management; stdlib only, no Docker or shell evaluation."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import select
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.request import ProxyHandler, Request, build_opener
import webbrowser

ROOT = Path(__file__).resolve().parent
DEFAULT_STATE = Path.home() / '.local/share/nexus-databuddy-020'
LEGACY_STATE = Path.home() / '.local/share/atlas-databuddy-020'
DEFAULT_PYTHON = Path.home() / '.venvs/nexus-databuddy-020/bin/python'
DEFAULT_OPENCODE = Path.home() / '.local/bin/opencode-nexus-2.1.2'


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


def process_state(record):
    """Return owned, missing, different or unknown without signalling a process."""
    if record is None:
        return 'missing'
    if (not isinstance(record, dict) or type(record.get('pid')) is not int or record['pid'] <= 1
            or not isinstance(record.get('identity'), dict) or not isinstance(record.get('instance'), str)):
        return 'unknown'
    pid = record['pid']
    try:
        # A /proc mount from a different PID namespace cannot identify this PID.
        if os.readlink('/proc/self') != str(os.getpid()):
            return 'unknown'
        stat = Path(f'/proc/{pid}/stat').read_text()
        fields = stat[stat.rfind(')') + 2:].split()
        if fields[0] == 'Z':
            return 'missing'
        identity = {'start_ticks': fields[19],
                    'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
        if identity != record['identity']:
            return 'different'
        command = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        environment = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
        return ('owned' if command[1:3] == [b'-m', b'agent_service.cloud']
                and ('NEXUS_NATIVE_INSTANCE=' + record['instance']).encode() in environment else 'different')
    except FileNotFoundError:
        # hidepid and concurrent exit are distinct; kill(0) never sends a signal.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return 'missing'
        except OSError:
            pass
        return 'unknown'
    except (OSError, IndexError, ValueError):
        return 'unknown'


def owns_process(record):
    return process_state(record) == 'owned'


def credentials(state):
    path = state / 'credentials.json'
    value = read_json(path)
    if value is None:
        value = {'APP_TOKEN': secrets.token_urlsafe(36),
                 'NEXUS_OPERATOR_TOKEN': secrets.token_urlsafe(36)}
        save_json(path, value)
    if not isinstance(value,dict):raise ValueError('Invalid local credentials; restore credentials.json from your backup.')
    legacy=value.get('ATLAS_OPERATOR_TOKEN');current=value.get('NEXUS_OPERATOR_TOKEN')
    if legacy is not None and current is not None and legacy!=current:
        raise ValueError('Conflicting legacy and current administrator credentials; no data changed.')
    if current is None and legacy is not None:value=dict(value,NEXUS_OPERATOR_TOKEN=legacy)
    if (not isinstance(value, dict) or any(not isinstance(value.get(k), str) or len(value[k]) < 16
            for k in ('APP_TOKEN', 'NEXUS_OPERATOR_TOKEN'))
            or value['APP_TOKEN'] == value['NEXUS_OPERATOR_TOKEN']):
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
    print(f'Nexus WorkStudio v0.20.0: http://127.0.0.1:{record["port"]}')
    if record.get('model_configured'):
        print('Model configured; engine: ' + record['engine'] + '. Real model acceptance is separate from service readiness.')
    else:
        print('MODEL UNCONFIGURED: UI and data backend only; AI tasks require a real model. Compatibility engine is active; no mock model.')


def stop_service(record):
    """Stop only the verified recorded process, using a non-reusable handle."""
    state = process_state(record)
    if state == 'missing':
        return True
    if state != 'owned':
        return False
    if not (hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal')):
        return False
    try:
        fd = os.pidfd_open(record['pid'])
    except OSError:
        return process_state(record) == 'missing'
    try:
        # Recheck after acquiring the handle; never signal a replacement PID.
        if process_state(record) != 'owned':
            return False
        try:
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            poller = select.poll()
            poller.register(fd, select.POLLIN)
            return bool(poller.poll(25000))
        except OSError:
            return False
    finally:
        os.close(fd)


def stop_launched_process(process):
    """Stop our still-owned child through a kernel PID handle, never a reused PID.

    A pidfd names the original process even if a numeric PID is later reused.
    waitid(P_PIDFD) also verifies it is our child before signalling. If this
    kernel cannot provide those guarantees, report an unconfirmed stop.
    """
    if process.poll() is not None:
        return True
    if not (hasattr(os, 'pidfd_open') and hasattr(os, 'P_PIDFD')
            and hasattr(os, 'waitid') and hasattr(signal, 'pidfd_send_signal')):
        return False
    try:
        fd = os.pidfd_open(process.pid)
    except OSError:
        return process.poll() is not None
    try:
        try:
            status = os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if status is None:
                signal.pidfd_send_signal(fd, signal.SIGTERM)
            process.wait(timeout=25)
            return True
        except (OSError, ChildProcessError, subprocess.TimeoutExpired):
            return False
    finally:
        os.close(fd)


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
    old_state = process_state(old)
    if old_state in ('unknown', 'different'):
        raise ValueError('Existing supervisor identity cannot be confirmed; inspect the process record before starting. No process was signalled.')
    if old_state == 'owned':
        if not service_ready(old, access['APP_TOKEN']):
            raise ValueError('Nexus WorkStudio supervisor is running but services are not ready. Check logs or stop before restarting.')
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
    for key in ('NEXUS_INITIAL_MODEL', 'NEXUS_LLM_CONFIG_FILE', 'NEXUS_DROP_PRIVILEGES',
                'NEXUS_LOCAL_MODE', 'NEXUS_LOCAL_WORKSPACE', 'NEXUS_QUICKSTART_CONTAINER',
                'LLM_BASE_URL', 'LLM_API_KEY', 'LLM_MODEL'):
        env.pop(key, None)
    env.update(access)
    env.update(DATA_DIR=str(data), DATABASE_URL='sqlite:///' + str(data / 'metadata.sqlite'),
               PORT=str(args.port), NEXUS_API_HOST='127.0.0.1', NEXUS_INSTANCE_ID=instance,
               NEXUS_NATIVE_INSTANCE=instance, NEXUS_DEPLOYMENT_PROFILE='single', NEXUS_AUTH_MODE='token',
               NEXUS_OPENCODE_RUNTIME='1' if engine == 'opencode' else '0',
               NEXUS_OPENCODE_BINARY=str(binary), APP_TIMEZONE='Asia/Shanghai', PYTHONUTF8='1',
               RUNTIME__DLTHUB_TELEMETRY='false', DBT_SEND_ANONYMOUS_USAGE_STATS='false')
    if has_model_file:
        env['NEXUS_LLM_CONFIG_FILE'] = str(model_path)
    # Rotation bounds routine logs without losing the previous startup diagnosis.
    logfile = state / 'nexus.log'
    if logfile.exists() and logfile.stat().st_size > 8 * 1024 * 1024:
        logfile.replace(state / 'nexus.previous.log')
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
            raise ValueError(f'Nexus WorkStudio exited with code {process.returncode}. See {logfile}; no model fallback was attempted.')
        if service_ready(record, access['APP_TOKEN']):
            display(record)
            return record
        time.sleep(1)
    stopped = stop_launched_process(process)
    if stopped:
        (state / 'process.json').unlink(missing_ok=True)
    raise ValueError(f'Services did not become ready within {args.wait}s. See {logfile}. '
                     + ('Supervisor stopped.' if stopped else 'Supervisor stop could not be confirmed; inspect the process and logs.'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop', 'status', 'open', 'token', 'logs'), nargs='?', default='start')
    parser.add_argument('--state-dir')
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
    os.umask(0o077)
    try:
        if args.state_dir is None:
            if os.path.lexists(DEFAULT_STATE) and os.path.lexists(LEGACY_STATE):
                raise ValueError('Both old and new workspace state directories exist; select one explicitly with --state-dir. No data changed.')
            chosen=LEGACY_STATE if os.path.lexists(LEGACY_STATE) else DEFAULT_STATE
        else:chosen=args.state_dir
        state = Path(chosen).expanduser().absolute()
        with state_lock(state):
            record = read_json(state / 'process.json')
            if args.action == 'token':
                value = read_json(state / 'credentials.json')
                if value is None:
                    raise ValueError('Start Nexus WorkStudio first to create local credentials.')
                print(value['APP_TOKEN'])  # Explicit owner request only; never print the operator token.
            elif args.action == 'logs':
                path = state / 'nexus.log'
                if path.exists():
                    print('\n'.join(path.read_text(encoding='utf-8', errors='replace').splitlines()[-100:]))
                else:
                    print('No log file yet.')
            elif args.action == 'stop':
                current_state = process_state(record)
                if current_state == 'different':
                    raise ValueError('Recorded PID identifies a different process; no process was signalled. Inspect the process record.')
                if current_state == 'unknown':
                    raise ValueError('Supervisor identity could not be confirmed; no process was signalled. Inspect the process record.')
                if not stop_service(record):
                    raise ValueError('Supervisor identity or shutdown could not be confirmed; inspect the process record and logs. No unrelated process was signalled.')
                (state / 'process.json').unlink(missing_ok=True)
                print('Nexus WorkStudio stopped. Data and credentials retained.')
            elif args.action == 'status':
                current_state = process_state(record)
                if current_state != 'owned':
                    print({'missing':'Nexus WorkStudio is stopped.', 'different':'Recorded PID identifies a different process; inspect the process record.', 'unknown':'Supervisor identity cannot be confirmed; inspect the process record.'}[current_state])
                    return 1
                display(record)
                if not service_ready(record, credentials(state)['APP_TOKEN']):
                    print('Supervisor running; services not ready.')
                    return 1
                print('API, worker and agent are ready.')
            elif args.action == 'start':
                record = start(args, state)
            elif process_state(record) != 'owned':
                raise ValueError('Supervisor is stopped or its identity cannot be confirmed. Inspect the process record before opening.')
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
