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
assert os.environ['ATLAS_API_HOST']=='127.0.0.1'
assert os.environ['APP_TOKEN']!=os.environ['ATLAS_OPERATOR_TOKEN']
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        if self.path=='/ready':value={'ready':True}
        elif self.path=='/api/status':
            if self.headers.get('Authorization')!='Bearer '+os.environ['APP_TOKEN']:
                self.send_response(401);self.end_headers();return
            value={role:{'nodes':[{'instance':os.environ['ATLAS_INSTANCE_ID'],'healthy':True}]}
                   for role in ('agent','worker')}
        else:self.send_response(404);self.end_headers();return
        self.send_response(200);self.end_headers();self.wfile.write(json.dumps(value).encode())
HTTPServer((os.environ['ATLAS_API_HOST'],int(os.environ['PORT'])),Handler).serve_forever()
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
        assert native.stop_service(record)
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
