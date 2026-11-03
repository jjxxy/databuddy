import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_service.business_flow_client import FlowCallError, submit


def test_published_version_request_is_reusable_and_never_follows_redirect():
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            received.append((self.path, self.headers['Authorization'], json.loads(body)))
            if len(received) == 3:
                self.send_response(302);self.send_header('Location', '/leak');self.end_headers();return
            data = json.dumps({'run_id': 'r_one', 'plan_digest': 'a' * 64, 'status': 'queued'}).encode()
            self.send_response(200);self.send_header('Content-Length', str(len(data)));self.end_headers();self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    try:
        args = (f'http://127.0.0.1:{server.server_port}', 'synthetic-token', 'project', 'flow', 'a'*64, 'stable-1', {'amount': '118.1500'})
        assert submit(*args)['run_id'] == submit(*args)['run_id'] == 'r_one'
        assert received[0] == received[1]
        assert received[0][2] == {'request_id':'stable-1', 'expected_digest':'a'*64,
                                   'inputs':{'amount':'118.1500'}, 'use_published':True}
        with pytest.raises(FlowCallError):submit(*args)
        assert len(received) == 3
    finally:
        server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('url', ['http://example.com', 'https://user:password@example.com',
                                  'https://example.com/base', 'https://example.com?token=x'])
def test_call_refuses_unsafe_service_origins(url):
    with pytest.raises(FlowCallError):
        submit(url, 'token', 'project', 'flow', 'a'*64, 'stable', {})
