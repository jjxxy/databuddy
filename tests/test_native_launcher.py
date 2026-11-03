"""Real local process lifecycle, instance identity and authenticated readiness."""
import os
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

if sys.platform != 'linux':
    pytest.skip('Native supervisor is Linux/WSL only', allow_module_level=True)

import native_local as native


def test_native_lifecycle_uses_loopback_credentials_and_preserves_state(tmp_path, monkeypatch):
    package = tmp_path / 'agent_service'
    package.mkdir()
    (package / '__init__.py').write_text('')
    (package / 'cloud.py').write_text('''
import json,os
from http.server import BaseHTTPRequestHandler,HTTPServer
assert os.environ['NEXUS_API_HOST']=='127.0.0.1'
assert os.environ['APP_TOKEN']!=os.environ['NEXUS_OPERATOR_TOKEN']
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        if self.path=='/ready':value={'ready':True}
        elif self.path=='/api/status':
            if self.headers.get('Authorization')!='Bearer '+os.environ['APP_TOKEN']:
                self.send_response(401);self.end_headers();return
            value={role:{'nodes':[{'instance':os.environ['NEXUS_INSTANCE_ID'],'healthy':True}]}
                   for role in ('agent','worker')}
        else:self.send_response(404);self.end_headers();return
        self.send_response(200);self.end_headers();self.wfile.write(json.dumps(value).encode())
HTTPServer((os.environ['NEXUS_API_HOST'],int(os.environ['PORT'])),Handler).serve_forever()
''')
    monkeypatch.setattr(native, 'ROOT', tmp_path)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    args = SimpleNamespace(python=sys.executable, model_config=None,
                           opencode_binary=str(tmp_path / 'absent'), engine='auto', port=port, wait=8)
    state = tmp_path / 'state'
    with native.state_lock(state):
        record = native.start(args, state)
        try:
            assert native.owns_process(record)
            assert record['engine'] == 'compatibility' and not record['model_configured']
            assert native.start(args, state)['pid'] == record['pid']
            assert not native.owns_process({**record, 'instance': 'another-instance'})
            assert not native.owns_process({**record, 'identity': {'start_ticks': 'wrong', 'boot_id': 'wrong'}})
            access = native.credentials(state)
            assert native.service_ready(record, access['APP_TOKEN'])
            assert not native.service_ready(record, 'incorrect-token')
            assert (state / 'credentials.json').stat().st_mode & 0o777 == 0o600
        finally:
            assert native.stop_service(record)
            try:os.waitpid(record['pid'], 0)
            except ChildProcessError:pass
        assert not native.owns_process(record)
        assert native.credentials(state) == access
        assert (state / 'data').is_dir()


def test_stop_refuses_unrelated_process_even_with_valid_kernel_identity():
    process = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
    try:
        record = {'pid': process.pid, 'identity': native.process_identity(process.pid), 'instance': 'pretend'}
        assert not native.stop_service(record)
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_missing_explicit_model_file_does_not_launch(tmp_path):
    args = SimpleNamespace(python=sys.executable, model_config=str(tmp_path / 'missing.json'),
                           opencode_binary=str(tmp_path / 'absent'), engine='auto', port=8000, wait=1)
    with native.state_lock(tmp_path / 'state'):
        with pytest.raises(ValueError, match='Explicit model'):
            native.start(args, tmp_path / 'state')
    assert not (tmp_path / 'state/process.json').exists()


@pytest.mark.parametrize('version', ['2.1.2', 'opencode v2.1.2'])
def test_pinned_opencode_version_formats_accepted(monkeypatch, version):
    monkeypatch.setattr(native.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=version + '\n'))
    native.validate_opencode('/fixture/opencode')


@pytest.mark.parametrize('version', ['2.1.1', 'opencode v2.1.3', 'malicious 2.1.2 suffix'])
def test_unpinned_opencode_version_rejected(monkeypatch, version):
    monkeypatch.setattr(native.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=version))
    with pytest.raises(ValueError, match='requires OpenCode 2.1.2'):
        native.validate_opencode('/fixture/opencode')


def test_start_timeout_cleans_up_own_child_without_proc_identity(tmp_path, monkeypatch):
    if not (hasattr(os, 'pidfd_open') and hasattr(os, 'P_PIDFD') and hasattr(native.signal, 'pidfd_send_signal')):
        pytest.skip('Kernel PID handles are unavailable; a stopped process cannot be confirmed')
    package=tmp_path/'agent_service';package.mkdir();(package/'__init__.py').write_text('')
    (package/'cloud.py').write_text('import time; time.sleep(30)')
    monkeypatch.setattr(native,'ROOT',tmp_path)
    monkeypatch.setattr(native,'owns_process',lambda record:False)
    observed=[];original=native.stop_launched_process
    def stop(process):
        observed.append(process)
        return original(process)
    monkeypatch.setattr(native,'stop_launched_process',stop)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    args=SimpleNamespace(python=sys.executable,model_config=None,opencode_binary=str(tmp_path/'absent'),
                         engine='auto',port=port,wait=0.1)
    with native.state_lock(tmp_path/'state'):
        with pytest.raises(ValueError,match='Supervisor stopped'):
            native.start(args,tmp_path/'state')
    assert len(observed)==1 and observed[0].poll() is not None


def test_pid_handle_unavailable_reports_unconfirmed_and_never_signals(monkeypatch):
    if not hasattr(os,'pidfd_open'):
        pytest.skip('Kernel PID handles are unavailable')
    process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    try:
        def denied(*args):raise PermissionError('no kernel PID handle')
        monkeypatch.setattr(native.os,'pidfd_open',denied)
        assert native.stop_launched_process(process) is False
        assert process.poll() is None
    finally:
        process.terminate();process.wait(timeout=5)


def test_pid_handle_must_name_a_child_before_signalling(monkeypatch):
    if not (hasattr(os,'pidfd_open') and hasattr(os,'P_PIDFD') and hasattr(native.signal,'pidfd_send_signal')):
        pytest.skip('Kernel PID handles are unavailable')
    process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    try:
        def not_a_child(*args):raise ChildProcessError('not our child')
        monkeypatch.setattr(native.os,'waitid',not_a_child)
        monkeypatch.setattr(native.signal,'pidfd_send_signal',lambda *args:pytest.fail('must not signal another process'))
        assert native.stop_launched_process(process) is False
        assert process.poll() is None
    finally:
        process.terminate();process.wait(timeout=5)


def test_start_timeout_reports_unconfirmed_when_pid_handle_is_denied(tmp_path, monkeypatch):
    if not hasattr(os,'pidfd_open'):
        pytest.skip('Kernel PID handles are unavailable')
    package=tmp_path/'agent_service';package.mkdir();(package/'__init__.py').write_text('')
    (package/'cloud.py').write_text('import time; time.sleep(30)')
    monkeypatch.setattr(native,'ROOT',tmp_path)
    monkeypatch.setattr(native,'owns_process',lambda record:False)
    def denied(*args):raise PermissionError('no kernel PID handle')
    monkeypatch.setattr(native.os,'pidfd_open',denied)
    observed=[];original=native.stop_launched_process
    def stop(process):
        observed.append(process)
        return original(process)
    monkeypatch.setattr(native,'stop_launched_process',stop)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    args=SimpleNamespace(python=sys.executable,model_config=None,opencode_binary=str(tmp_path/'absent'),
                         engine='auto',port=port,wait=0.1)
    try:
        with native.state_lock(tmp_path/'state'):
            with pytest.raises(ValueError,match='Supervisor stop could not be confirmed'):
                native.start(args,tmp_path/'state')
        assert len(observed)==1 and observed[0].poll() is None
    finally:
        if observed:
            observed[0].terminate();observed[0].wait(timeout=5)


def test_unverifiable_pid_namespace_does_not_signal_or_claim_stopped(monkeypatch):
    monkeypatch.setattr(native.os,'readlink',lambda path:'different-pid-namespace')
    record={'pid':12345,'identity':{'start_ticks':'1','boot_id':'known'},'instance':'native-test'}
    monkeypatch.setattr(native.os,'kill',lambda *args:pytest.fail('must not signal an unverifiable process'))
    assert native.process_state(record)=='unknown'
    assert native.stop_service(record) is False
    assert native.process_state(None)=='missing'
    assert native.process_state({'pid':12345})=='unknown'


def test_stop_revalidates_after_pidfd_before_any_signal(monkeypatch):
    if not (hasattr(os,'pidfd_open') and hasattr(native.signal,'pidfd_send_signal')):
        pytest.skip('Kernel PID handles are unavailable')
    states=iter(('owned','different'))
    monkeypatch.setattr(native,'process_state',lambda record:next(states))
    read_fd,write_fd=os.pipe()
    try:
        monkeypatch.setattr(native.os,'pidfd_open',lambda pid:os.dup(read_fd))
        monkeypatch.setattr(native.signal,'pidfd_send_signal',lambda *args:pytest.fail('must not signal a replacement'))
        assert native.stop_service({'pid':12345}) is False
    finally:
        os.close(read_fd);os.close(write_fd)


def test_stop_keeps_unverifiable_process_record(tmp_path,monkeypatch,capsys):
    state=tmp_path/'state';state.mkdir()
    record=state/'process.json';record.write_text('{"pid":12345}')
    monkeypatch.setattr(native,'process_state',lambda record:'unknown')
    assert native.main(['stop','--state-dir',str(state)])==2
    assert record.exists()
    assert 'could not be confirmed' in capsys.readouterr().err


def test_recorded_pid_missing_or_inaccessible_are_distinct(monkeypatch):
    record={'pid':12345,'identity':{'start_ticks':'1','boot_id':'boot'},'instance':'native-test'}
    monkeypatch.setattr(native.os,'readlink',lambda path:str(os.getpid()))
    class MissingPath:
        def __init__(self,*args):pass
        def read_text(self):raise FileNotFoundError()
    monkeypatch.setattr(native,'Path',MissingPath)
    def absent(*args):raise ProcessLookupError()
    monkeypatch.setattr(native.os,'kill',absent)
    assert native.process_state(record)=='missing'
    assert native.stop_service(record) is True
    def inaccessible(*args):raise PermissionError()
    monkeypatch.setattr(native.os,'kill',inaccessible)
    assert native.process_state(record)=='unknown'
    assert native.stop_service(record) is False


def test_recorded_pid_with_different_kernel_identity_is_not_signalled(monkeypatch):
    record={'pid':12345,'identity':{'start_ticks':'expected','boot_id':'boot'},'instance':'native-test'}
    monkeypatch.setattr(native.os,'readlink',lambda path:str(os.getpid()))
    class FakePath:
        def __init__(self,path):self.path=path
        def read_text(self):
            return '12345 (other) S '+' '.join(['0']*19) if self.path.endswith('/stat') else 'boot'
    monkeypatch.setattr(native,'Path',FakePath)
    monkeypatch.setattr(native.os,'kill',lambda *args:pytest.fail('must not signal a different process'))
    assert native.process_state(record)=='different'
    assert native.stop_service(record) is False
