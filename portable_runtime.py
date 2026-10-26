"""Self-contained desktop backend host. Startup performs no remote model probes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.request import Request, ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parent


def emit(event, **value):
    print(json.dumps({'event': event, **value}, ensure_ascii=False), flush=True)


def write_json(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    temp.replace(path)
    path.chmod(0o600)


def credentials(state):
    path = state / 'credentials.json'
    if path.exists():
        value = json.loads(path.read_text(encoding='utf-8'))
    else:
        value = {'APP_TOKEN':secrets.token_urlsafe(36), 'ATLAS_OPERATOR_TOKEN':secrets.token_urlsafe(36)}
        write_json(path, value)
    if (any(not isinstance(value.get(k), str) or len(value[k]) < 24 for k in ('APP_TOKEN','ATLAS_OPERATOR_TOKEN'))
            or value['APP_TOKEN'] == value['ATLAS_OPERATOR_TOKEN']):
        raise ValueError('本机访问凭据损坏，请保留数据并从备份恢复凭据文件。')
    return value


def select_port(state, requested):
    saved = state / 'desktop-settings.json'
    settings = json.loads(saved.read_text(encoding='utf-8')) if saved.exists() else {}
    previous = requested or settings.get('port', 0)
    if type(previous) is not int or not 0 <= previous <= 65535:previous = 0
    with socket.socket() as probe:
        try:probe.bind(('127.0.0.1', previous))
        except OSError:
            if requested:raise ValueError('指定的本机端口已占用，请关闭原实例或使用自动端口。') from None
            probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    write_json(saved, {'port':port})
    return port


def environment(state, port, access, model_config, offline, workspace=None):
    kept = ('SystemRoot','SYSTEMROOT','WINDIR','TEMP','TMP','COMSPEC','NUMBER_OF_PROCESSORS',
            'PROCESSOR_ARCHITECTURE','PATHEXT','USERPROFILE','LOCALAPPDATA','APPDATA')
    env = {key:os.environ[key] for key in kept if key in os.environ}
    runtime = ROOT / 'runtime'
    git = runtime / 'git' / 'cmd/git.exe'
    opencode = runtime / 'opencode/opencode.exe'
    if not git.is_file():raise ValueError('交付包缺少 Git 运行文件，请重新解压完整交付包。')
    paths = [str(Path(sys.executable).parent), str(git.parent), str(runtime/'git/mingw64/bin'),
             str(runtime/'git/usr/bin')]
    home = state / 'home'; home.mkdir(exist_ok=True)
    env.update(access)
    env.update(PATH=os.pathsep.join(paths), PYTHONPATH=str(ROOT), PYTHONUTF8='1', PYTHONUNBUFFERED='1',
               DATA_DIR=str(state/'data'), DATABASE_URL='sqlite:///'+str(state/'data/metadata.sqlite'),
               HOME=str(home), USERPROFILE=str(home), PORT=str(port), ATLAS_API_HOST='127.0.0.1',
               ATLAS_DESKTOP_MODE='1', ATLAS_SUPERVISOR_STDIN='1', ATLAS_AUTH_MODE='token',
               ATLAS_INSTANCE_ID='desktop-'+secrets.token_hex(12), ATLAS_DEPLOYMENT_PROFILE='single',
               ATLAS_OPENCODE_RUNTIME='1', ATLAS_OPENCODE_BINARY=str(opencode), ATLAS_GIT_BINARY=str(git),
               ATLAS_NETWORK_MODE='local_only' if offline else 'connected', APP_TIMEZONE='Asia/Shanghai',
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=str(home/'gitconfig'), GIT_TERMINAL_PROMPT='0',
               RUNTIME__DLTHUB_TELEMETRY='false', DBT_SEND_ANONYMOUS_USAGE_STATS='false',
               DO_NOT_TRACK='1', DLT_TELEMETRY='false')
    if model_config:env['ATLAS_LLM_CONFIG_FILE'] = str(model_config)
    if workspace is not None:
        data = workspace / '.atlas'
        env.update(ATLAS_LOCAL_MODE='1', ATLAS_LOCAL_WORKSPACE=str(workspace),
                   DATA_DIR=str(data), DATABASE_URL='sqlite:///'+str(data/'metadata.sqlite'))
    return env


def request(url, token, path):
    value = Request(url+path, headers={'Authorization':'Bearer '+token})
    with build_opener(ProxyHandler({})).open(value, timeout=2) as response:
        return json.load(response)


def serve(args):
    from agent_service.platform_files import file_guard, assert_plain_path, private_directory
    from agent_service.process_control import spawn, stop
    base_state = Path(args.state_dir).expanduser().absolute()
    workspace = None
    if args.workspace_dir:
        workspace = Path(args.workspace_dir).expanduser().absolute()
        assert_plain_path(workspace, missing_ok=False)
        if not workspace.is_dir() or workspace == Path(workspace.anchor):
            raise ValueError('请选择一个现有的业务文件夹，不能选择磁盘根目录。')
    assert_plain_path(base_state)
    base_state.mkdir(parents=True, exist_ok=True)
    private_directory(base_state)
    state = base_state
    if workspace is not None:
        identity = hashlib.sha256(os.path.normcase(str(workspace)).encode('utf-8')).hexdigest()[:24]
        state = base_state / 'workspaces' / identity
        assert_plain_path(state)
        state.mkdir(parents=True, exist_ok=True)
        private_directory(state)
    else:
        (state/'data').mkdir(exist_ok=True)
    with file_guard(state/'desktop.lock', exclusive=True, blocking=False):
        access = credentials(state)
        port = select_port(state, args.port)
        chosen = Path(args.model_config).expanduser().absolute() if args.model_config else base_state/'llm.json'
        # Configuration is operator supplied; neither the package nor the GUI needs a secret to boot.
        model = chosen if chosen.exists() or args.model_config else None
        env = environment(state, port, access, model, args.offline, workspace)
        url = f'http://127.0.0.1:{port}'
        stop_event = threading.Event()
        def stop_signal(*unused):stop_event.set()
        for signum in (signal.SIGINT,signal.SIGTERM):signal.signal(signum,stop_signal)
        def commands():
            for line in sys.stdin:
                if line.strip()=='stop':stop_event.set();return
            stop_event.set()
        threading.Thread(target=commands,daemon=True).start()
        log_path = state/'atlas.log'
        if log_path.exists() and log_path.stat().st_size>8*1024*1024:
            log_path.replace(state/'atlas.previous.log')
        with log_path.open('ab',buffering=0) as logfile:
            process = spawn([sys.executable,'-m','agent_service.cloud'],env=env,cwd=ROOT,
                            stdin=subprocess.PIPE,stdout=logfile,stderr=subprocess.STDOUT)
            try:
                deadline=time.monotonic()+args.wait
                while time.monotonic()<deadline and not stop_event.is_set():
                    if process.poll() is not None:raise ValueError('本地服务未能启动。请打开诊断日志查看原因；原有数据已保留。')
                    try:
                        status=request(url,access['APP_TOKEN'],'/api/status')
                        ready=request(url,access['APP_TOKEN'],'/ready')
                        if ready.get('ready') and all(any(n.get('instance')==env['ATLAS_INSTANCE_ID'] and n.get('healthy')
                                for n in status.get(role,{}).get('nodes',[])) for role in ('worker','agent')):
                            break
                    except (OSError,ValueError):pass
                    stop_event.wait(.25)
                else:
                    if not stop_event.is_set():raise ValueError('本地服务启动超时，请打开诊断日志；原有数据已保留。')
                    return 0
                # The workspace token is IPC data for Electron only. Never put it in URLs or service logs.
                emit('ready',url=url,token=access['APP_TOKEN'])
                while not stop_event.wait(.5):
                    if process.poll() is not None:raise ValueError('本地服务意外停止，请重新打开工作空间。数据已保留。')
                return 0
            finally:
                if process.poll() is None:
                    try:process.stdin.write(b'stop\n');process.stdin.flush();process.wait(timeout=15)
                    except (OSError,subprocess.TimeoutExpired):stop(process,timeout=3)
                else:process.wait()


def main(argv=None):
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr,'reconfigure'):sys.stderr.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('serve',))
    parser.add_argument('--state-dir',required=True)
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--model-config')
    parser.add_argument('--workspace-dir', help='Business folder; Atlas manages only its own subdirectories.')
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--wait',type=int,default=120)
    args=parser.parse_args(argv)
    if not 0<=args.port<=65535 or not 1<=args.wait<=300:parser.error('端口或等待时间无效')
    try:return serve(args)
    except (OSError,ValueError) as exc:
        # Errors from these trusted startup checks never contain model file contents.
        emit('error',message=str(exc) if isinstance(exc,ValueError) else '本机文件或运行权限不可用，请检查诊断日志。')
        return 2


if __name__=='__main__':raise SystemExit(main())
