"""The generated entry targets the frozen adopted version and retry key."""
import json
import sys
import urllib.request
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_service.business_flows import _entry


def test_entry_uses_adopted_digest_explicit_parameters_and_retry_id(tmp_path, monkeypatch, capsys):
    params = tmp_path / 'inputs.json'
    params.write_text('{"amount":"118.150000000001","region":"西部"}', encoding='utf-8')
    monkeypatch.setenv('NEXUS_URL', 'http://127.0.0.1:8080')
    monkeypatch.setenv('NEXUS_TOKEN', 'synthetic-token')
    monkeypatch.setenv('NEXUS_REQUEST_ID', 'same-retry-id')
    monkeypatch.setattr(sys, 'argv', ['run.py', str(params)])
    received = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return json.dumps({'run_id':'run-1','plan_digest':'a' * 64}).encode()

    class Opener:
        def open(self, request, timeout):
            received.append((request, timeout))
            return Response()

    monkeypatch.setattr(urllib.request, 'build_opener', lambda *_: Opener())
    code = _entry('project', 'flow', 'a' * 64)
    exec(compile(code, 'run.py', 'exec'), {'__name__': '__main__'})
    request, timeout = received[0]
    assert timeout == 30
    assert request.full_url == 'http://127.0.0.1:8080/api/projects/project/business-flows/flow/runs'
    assert json.loads(request.data) == {'request_id': 'same-retry-id', 'expected_digest': 'a' * 64,
                                       'inputs': {'amount': '118.150000000001', 'region': '西部'},
                                       'use_published': True}
    out = capsys.readouterr()
    assert 'same-retry-id' in out.err and 'synthetic-token' not in out.err + out.out


def test_entry_does_not_forward_credentials_to_redirect(tmp_path, monkeypatch):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            requests.append(self.path)
            self.send_response(302)
            self.send_header('Location', '/unexpected-destination')
            self.end_headers()
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('NEXUS_URL', f'http://127.0.0.1:{server.server_port}')
    monkeypatch.setenv('NEXUS_TOKEN', 'synthetic-token')
    monkeypatch.setenv('NEXUS_REQUEST_ID', 'retry-id')
    monkeypatch.setattr(sys, 'argv', ['run.py'])
    try:
        with pytest.raises(urllib.error.HTTPError):
            exec(compile(_entry('project', 'flow', 'a' * 64), 'run.py', 'exec'), {'__name__': '__main__'})
        assert requests == ['/api/projects/project/business-flows/flow/runs']
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
