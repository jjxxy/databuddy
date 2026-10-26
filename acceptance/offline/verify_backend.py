"""Exercise the shipped Windows backend with synthetic data and no AI calls.

Run with the bundle's runtime/python/python.exe. State is created outside the app
directory. IPC access tokens stay in memory and are never written to evidence.
This checks application local-only policy, not an OS firewall or a clean VM.
"""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
import csv
from decimal import Decimal
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[2]
CSV = (b'order,customer,amount,refund,status\n'
       b'A1,Acme,100.25,10.10,paid\nA2,Acme,20,0,pending\nA3,Acme,30,2,paid\n')


def identity():
    return 'offline_' + uuid.uuid4().hex


class Host:
    def __init__(self, app, state, model=None, *, offline=True, workspace=None):
        self.app, self.state, self.model = app, state, model
        self.offline = offline
        self.workspace = workspace
        self.process = None
        self.token = None
        self.url = None
        self.lines = deque(maxlen=100)

    def start(self):
        interpreter = self.app / 'runtime/python/python.exe'
        command = [str(interpreter), str(self.app / 'portable_runtime.py'), 'serve',
                   '--state-dir', str(self.state), '--port', '0', '--wait', '120']
        if self.offline:
            command += ['--offline']
        if self.model:
            command += ['--model-config', str(self.model)]
        if self.workspace is not None:
            command += ['--workspace-dir', str(self.workspace)]
        # No inherited developer PATH, model keys, proxy or Python installation.
        allowed = ('SystemRoot', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP',
                   'LOCALAPPDATA', 'APPDATA', 'USERPROFILE', 'PATHEXT')
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        env['PATH'] = ''
        self.process = subprocess.Popen(command, cwd=self.app, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace', bufsize=1)
        events = queue.Queue()
        def read_events():
            for line in self.process.stdout:
                try:
                    value = json.loads(line)
                except ValueError:
                    self.lines.append(line.rstrip())
                    continue
                if value.get('event') == 'ready':
                    # Sensitive ready IPC is consumed here, never logged.
                    events.put(value)
                elif value.get('event') == 'error':
                    events.put(value)
                else:
                    self.lines.append('Backend emitted a non-readiness event.')
        def read_errors():
            for line in self.process.stderr:
                self.lines.append(line.rstrip())
        threading.Thread(target=read_events, daemon=True).start()
        threading.Thread(target=read_errors, daemon=True).start()
        deadline = time.monotonic() + 135
        while time.monotonic() < deadline:
            try:
                event = events.get(timeout=.25)
            except queue.Empty:
                if self.process.poll() is not None:
                    raise RuntimeError('Backend exited before readiness.')
                continue
            if event.get('event') == 'error':
                raise RuntimeError(event.get('message', 'Backend startup failed.'))
            self.url, self.token = event['url'], event['token']
            assert self.url.startswith('http://127.0.0.1:')
            assert len(self.token) >= 24
            return self
        raise TimeoutError('Backend did not report readiness.')

    def call(self, method, path, value=None, *, data=None, headers=None, auth=True, token=None):
        sent_headers = dict(headers or {})
        if auth:
            sent_headers['Authorization'] = 'Bearer ' + (token or self.token)
        if value is not None:
            data = json.dumps(value).encode('utf-8')
            sent_headers['Content-Type'] = 'application/json'
        request = Request(self.url + path, method=method, data=data, headers=sent_headers)
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=20) as response:
                body = response.read()
                status = response.status
                content_type = response.headers.get('Content-Type', '')
        except HTTPError as error:
            status, body = error.code, error.read()
            content_type = error.headers.get('Content-Type', '')
        if 'json' in content_type:
            body = json.loads(body)
        return status, body

    def json(self, method, path, value=None):
        status, body = self.call(method, path, value)
        if status != 200:
            raise AssertionError(f'{method} {path} returned HTTP {status}')
        return body

    def upload(self, project, payload=CSV):
        boundary = 'atlas-boundary-' + uuid.uuid4().hex
        parts = []
        for name, value in [('request_id', identity()), ('local_processing', 'true')]:
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                          f'\r\n\r\n{value}\r\n').encode())
        parts.extend([(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                       'filename="offline-orders.csv"\r\nContent-Type: text/csv\r\n\r\n').encode(),
                      payload, f'\r\n--{boundary}--\r\n'.encode()])
        return self.call('POST', f'/api/projects/{project}/attachments', data=b''.join(parts),
                         headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})

    def stop(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write('stop\n')
                self.process.stdin.flush()
                result = self.process.wait(timeout=25)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=10)
                raise RuntimeError('Backend did not stop cleanly.')
            if result:
                raise RuntimeError('Backend stopped with a nonzero status.')

    def diagnostic(self):
        text = '\n'.join(self.lines)
        log = self.state / 'atlas.log'
        if log.exists():
            text += '\n' + log.read_text(encoding='utf-8', errors='replace')[-8000:]
        run_logs = sorted((self.state / 'data/runs').glob('*/attempt-*/execution.log'),
                          key=lambda path: path.stat().st_mtime, reverse=True)
        for path in run_logs[:3]:
            text += '\nSynthetic worker log:\n' + path.read_text(encoding='utf-8', errors='replace')[-12000:]
        if self.token:
            text = text.replace(self.token, '[REDACTED_ACCESS_TOKEN]')
        return text


def wait(host, callback, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if host.process.poll() is not None:
            raise RuntimeError('Backend stopped during the data workflow.')
        result = callback()
        if result:
            return result
        time.sleep(1)
    raise TimeoutError('Synthetic data workflow timed out.')


def workflow(host):
    project = host.json('POST', '/api/projects',
                        {'name': '离线验收', 'request_id': identity()})
    project_id = project['id']
    assert host.call('GET', '/api/status', auth=False)[0] == 401
    assert host.call('GET', '/api/status', token='invalid-test-token')[0] == 401
    assert host.call('POST', '/api/projects', {'name': '', 'request_id': identity()})[0] in (400, 422)
    assert host.call('POST', '/api/projects', {'name': 'x' * 81, 'request_id': identity()})[0] in (400, 422)
    assert host.upload(project_id, b'')[0] == 400
    assert host.call('POST', f'/api/projects/{project_id}/messages',
                     {'text': 'x' * 12001, 'request_id': identity()})[0] in (400, 422)
    assert host.call('POST', f'/api/projects/{project_id}/messages',
                     {'text': '', 'request_id': identity()})[0] in (400, 422)
    upload_status, attachment = host.upload(project_id)
    assert upload_status == 200
    def ready_source():
        source = next((source for source in host.json('GET', '/api/sources')
                       if source['id'] == attachment['id']), None)
        if source and source.get('status') == 'failed':
            raise AssertionError('Local source profiling failed.')
        return source if source and source.get('status') == 'ready' else None
    source = wait(host, ready_source)
    spec = dict(source['proposed_spec'], primary_key='c_1', group_by='c_2', amount_field='m_net',
                computations=[{'name': 'm_net', 'label': '净金额', 'operation': 'subtract',
                               'left_field': 'c_3', 'right_field': 'c_4'}],
                filters=[{'field': 'c_5', 'operator': 'eq', 'data_type': 'text', 'value': 'paid'}])
    pipeline = host.json('POST', '/api/pipelines', {'source_id': source['id'],
                         'name': '离线确定性加工', 'spec': spec, 'release_mode': 'manual'})
    pipeline_id = pipeline['id']
    def candidate():
        current = next(p for p in host.json('GET', '/api/pipelines') if p['id'] == pipeline_id)
        runs = [run for run in host.json('GET', '/api/runs') if run.get('target') == pipeline_id]
        if runs and all(run.get('status') == 'failed' for run in runs):
            for run in runs:
                details = host.json('GET', '/api/runs/' + run['id'])
                host.lines.append(json.dumps({'synthetic_pipeline_failure': details}, ensure_ascii=False))
            raise AssertionError('Local data pipeline failed.')
        return current if current.get('candidate_run') else None
    current = wait(host, candidate)
    assert not current.get('published_run')
    assert host.call('GET', f'/api/pipelines/{pipeline_id}/result')[0] == 409
    preview = host.json('GET', f'/api/pipelines/{pipeline_id}/preview')
    assert preview['manifest']['records'] == 2
    host.json('POST', f'/api/pipelines/{pipeline_id}/publish', {'run_id': current['candidate_run']})
    result = host.json('GET', f'/api/pipelines/{pipeline_id}/result')
    assert result['total'] == 2
    total = Decimal(result['manifest']['summary'][0]['amount_total'])
    assert total == Decimal('118.15')
    export_status, exported = host.call('GET', f'/api/pipelines/{pipeline_id}/export')
    assert export_status == 200
    rows = list(csv.DictReader(io.StringIO(exported.decode('utf-8-sig'))))
    net_label = next(field['label'] for field in result['manifest']['fields'] if field['name'] == 'm_net')
    assert len(rows) == 2
    assert sum((Decimal(row[net_label]) for row in rows), Decimal('0')) == Decimal('118.15')
    assert host.json('GET', '/api/audit/verify')['ok']
    return {'project': project_id, 'pipeline': pipeline_id, 'rows': 2, 'exact_total': str(total),
            'csv_sha256': hashlib.sha256(exported).hexdigest(),
            'auth_required': True, 'empty_input_rejected': True, 'long_input_rejected': True,
            'empty_file_rejected': True, 'manual_publish_required': True, 'audit_verified': True}


def missing_runtime(app, temporary):
    spec = importlib.util.spec_from_file_location('offline_portable_runtime', app / 'portable_runtime.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = temporary / 'missing-runtime-fixture'
    try:
        module.environment(temporary, 12345, {}, None, True)
    except ValueError as error:
        message = str(error)
        assert 'Git' in message and '交付包' in message and 'Traceback' not in message
        return {'ok': True, 'method': 'imported startup validation with an empty fixture root',
                'human_message': message, 'real_runtime_modified': False}
    raise AssertionError('Missing runtime did not fail clearly.')


@contextmanager
def test_workspace(hosts):
    # Stop live Windows processes before tempfile tries to remove SQLite files.
    # Preserve synthetic diagnostics in memory before the temporary tree is gone.
    with tempfile.TemporaryDirectory(prefix='atlas-offline-acceptance-', ignore_cleanup_errors=True) as temp:
        try:
            yield temp
        finally:
            for host in hosts:
                host.lines.append(host.diagnostic())
            for host in reversed(hosts):
                try: host.stop()
                except Exception: pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root', type=Path, default=ROOT)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    app, evidence = args.app_root.resolve(), args.evidence_dir.resolve()
    if evidence.exists() and any(evidence.iterdir()):
        parser.error('--evidence-dir must be new or empty')
    evidence.mkdir(parents=True, exist_ok=True)
    if sys.platform != 'win32':
        parser.error('This acceptance harness targets native Windows only.')
    result = {'ok': False, 'platform': 'native Windows', 'app_root': str(app),
              'synthetic_data_only': True, 'real_ai_evaluated': False,
              'os_firewall_or_disconnected_vm_evaluated': False, 'cases': {}}
    hosts = []
    step = 'initialize'
    try:
        with test_workspace(hosts) as temp:
            temporary = Path(temp)
            step = 'fresh_state_boot_and_local_workflow'
            host = Host(app, temporary / 'fresh'); hosts.append(host)
            host.start()
            assert host.json('GET', '/ready')['ready']
            first_url, first_token = host.url, host.token
            first = workflow(host)
            host.stop()
            step = 'restart_preserves_data_credentials_and_port'
            host.start()
            assert host.url == first_url and host.token == first_token
            restored = host.json('GET', f'/api/pipelines/{first["pipeline"]}/result')
            assert restored['total'] == 2
            assert Decimal(restored['manifest']['summary'][0]['amount_total']) == Decimal('118.15')
            assert host.json('GET', '/api/audit/verify')['ok']
            host.stop()
            result['cases']['fresh_local_workflow'] = first
            result['cases']['restart'] = {'data_preserved': True, 'credentials_preserved': True,
                                         'port_preserved': True, 'audit_verified': True}
            step = 'bad_model_does_not_block_boot_or_local_workflow'
            config = temporary / 'invalid-model.json'
            config.write_text(json.dumps({'default_model_id': 'unreachable', 'models': [{
                'id': 'unreachable', 'name': 'Unreachable synthetic model',
                'base_url': 'https://127.0.0.1:9/v1', 'model': 'invalid-model',
                'api_key': 'invalid-test-key-not-a-real-credential', 'share_data': False}]}), encoding='utf-8')
            bad = Host(app, temporary / 'invalid-model-state', config); hosts.append(bad)
            bad.start()
            result['cases']['bad_model_local_workflow'] = workflow(bad)
            bad.stop()
            step = 'missing_runtime_human_error'
            result['cases']['missing_runtime'] = missing_runtime(app, temporary)
            result.update(ok=True, offline_policy='local_only', inherited_path_empty=True,
                          app_directory_used_for_state=False,
                          startup_does_not_require_reachable_model=True)
    except BaseException as error:
        frames = traceback.extract_tb(error.__traceback__)
        result['failure'] = {'step': step, 'type': type(error).__name__,
                             'location': {'file': Path(frames[-1].filename).name,
                                          'line': frames[-1].lineno} if frames else None}
        for index, host in enumerate(hosts):
            try:
                diagnostic = host.diagnostic()
                (evidence / f'diagnostic-{index}.log').write_text(diagnostic, encoding='utf-8')
            except OSError:
                pass
    finally:
        for host in reversed(hosts):
            try: host.stop()
            except Exception: pass
    (evidence / 'backend-verification.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
