"""Offline Codex app-server protocol probe; never enables an Nexus WorkStudio runtime.

Uses a temporary Codex state directory and a local, synthetic Responses API.
No user credentials or business data are used.  A passed probe demonstrates
only this binary/protocol combination, not production isolation or recovery.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class Model(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        Model.calls.append(body)
        if len(Model.calls) == 1:
            item = {'type': 'function_call', 'id': 'fc_fixture', 'call_id': 'call_fixture',
                    'name': 'nexus_step', 'arguments': '{"text":"fixture"}', 'status': 'completed'}
        else:
            item = {'type': 'message', 'id': 'msg_fixture', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': 'fixture complete', 'annotations': []}]}
        response = {'id': 'resp_fixture_'+str(len(Model.calls)), 'object': 'response', 'created_at': 1,
                    'status': 'completed', 'output': [item],
                    'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110}}
        events = [
            {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
            {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
            {'type': 'response.completed', 'response': response},
        ]
        data = ''.join('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n' for event in events).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    binary = os.environ.get('NEXUS_CODEX_BINARY') or shutil.which('codex')
    if not binary:
        print(json.dumps({'status': 'unavailable', 'reason': 'Set NEXUS_CODEX_BINARY to a Codex executable.'}))
        return 2
    model = HTTPServer(('127.0.0.1', 0), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    state = {'initialized': False, 'thread_started': False, 'turn_started': False,
             'model_requests': 0, 'dynamic_tool_called': False, 'completed': False,
             'native_tools': [], 'production_security_verified': False,
             'crash_recovery_verified': False}
    try:
        with tempfile.TemporaryDirectory(prefix='nexus-codex-fixture-') as root:
            # Explicit allowlist: never inherit user credentials, model settings,
            # proxy configuration, or the active Codex session.
            env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'CODEX_HOME': root,
                   'NEXUS_FIXTURE_KEY': 'not-a-real-key', 'NO_PROXY': 'localhost,127.0.0.1'}
            config = ['model_provider="fixture"', 'model_providers.fixture.name="Fixture"',
                      f'model_providers.fixture.base_url="http://127.0.0.1:{model.server_port}/v1"',
                      'model_providers.fixture.env_key="NEXUS_FIXTURE_KEY"',
                      'model_providers.fixture.wire_api="responses"',
                      'model_providers.fixture.supports_websockets=false',
                      'features.shell_tool=false', 'features.multi_agent=false',
                      'features.goals=false', 'features.view_image=false',
                      'features.apps=false', 'features.plugins=false', 'web_search="disabled"']
            args = [binary, 'app-server', '--listen', 'stdio://']
            for value in config:
                args.extend(['-c', value])
            proc = subprocess.Popen(args, cwd=root, env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, bufsize=1)
            inbox = queue.Queue()

            def reader():
                for line in proc.stdout:
                    try:
                        inbox.put(json.loads(line))
                    except ValueError:
                        pass
            threading.Thread(target=reader, daemon=True).start()

            def send(value):
                proc.stdin.write(json.dumps(value)+'\n')
                proc.stdin.flush()

            def wait_for(predicate, seconds):
                deadline = time.monotonic()+seconds
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        raise RuntimeError('app-server exited before the expected response')
                    try:
                        value = inbox.get(timeout=min(1, max(.01, deadline-time.monotonic())))
                    except queue.Empty:
                        continue
                    if predicate(value):
                        return value
                raise TimeoutError('timed out waiting for app-server event')

            def request(identity, method, params):
                send({'id': identity, 'method': method, 'params': params})
                answer = wait_for(lambda value: value.get('id') == identity, 25)
                if 'error' in answer:
                    raise RuntimeError(method+' rejected: '+str(answer['error'].get('message', 'unknown')))
                return answer['result']

            try:
                request(1, 'initialize', {'clientInfo': {'name': 'nexus-probe', 'title': 'Nexus WorkStudio probe',
                                                         'version': '1'},
                                          'capabilities': {'experimentalApi': True}})
                state['initialized'] = True
                send({'method': 'initialized', 'params': {}})
                thread = request(2, 'thread/start', {
                    'model': 'fixture-model', 'cwd': root, 'sandbox': 'read-only',
                    'approvalPolicy': 'never', 'environments': [],
                    'dynamicTools': [{'type': 'function', 'name': 'nexus_step',
                                      'description': 'Synthetic local fixture',
                                      'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}},
                                                      'required': ['text'], 'additionalProperties': False}}],
                })['thread']['id']
                state['thread_started'] = True
                request(3, 'turn/start', {'threadId': thread,
                                          'input': [{'type': 'text', 'text': 'Call nexus_step with fixture.',
                                                     'text_elements': []}]})
                state['turn_started'] = True
                deadline = time.monotonic()+35
                while time.monotonic() < deadline:
                    event = wait_for(lambda _value: True, max(.01, deadline-time.monotonic()))
                    method = event.get('method')
                    if method == 'item/tool/call':
                        params = event.get('params', {})
                        if params.get('threadId') != thread or params.get('tool') != 'nexus_step':
                            raise RuntimeError('unexpected dynamic tool request')
                        state['dynamic_tool_called'] = True
                        send({'id': event['id'], 'result': {'contentItems': [{'type': 'inputText', 'text': 'fixture result'}],
                                                          'success': True}})
                    elif method == 'turn/completed':
                        state['completed'] = event.get('params', {}).get('turn', {}).get('status') == 'completed'
                        break
                state['reason'] = 'completed' if state['completed'] else 'no completed turn before timeout'
            except (RuntimeError, TimeoutError, BrokenPipeError) as error:
                state['reason'] = str(error)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            state['model_requests'] = len(Model.calls)
            if Model.calls:
                state['native_tools'] = [tool.get('name', tool.get('type', '?')) for tool in Model.calls[0].get('tools', [])]
            state['status'] = 'completed' if state['completed'] and state['dynamic_tool_called'] else 'inconclusive'
            print(json.dumps(state, ensure_ascii=False))
            return 0 if state['status'] == 'completed' else 1
    finally:
        model.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
