"""Local readiness diagnostics never replace integrity checks or leak secrets."""
import copy
import json
from pathlib import Path

import httpx
import pytest

from agent_service import opencode_runtime as runtime
from test_opencode_runtime import _prepare


def healthy_runtime(monkeypatch, tmp_path):
    root, _, _ = _prepare(monkeypatch, tmp_path)
    config = json.loads((root / 'config/opencode/opencode.json').read_bytes().decode('utf-8'))
    plugins = [{'id': name, 'state': {'status': 'active'}, 'source': {'type': 'builtin'}}
               for name in runtime.BUILTIN_PLUGINS]
    plugins.append({'id': 'atlas.business-tools', 'state': {'status': 'active'},
                    'source': {'type': 'local', 'path': str(root / 'plugin/index.js')}})
    responses = {'/api/info': {'version': runtime.VERSION}, '/api/plugin': {'data': plugins},
                 '/api/config': [{'type': 'document', 'path': str(root / 'config/opencode/opencode.json'),
                                  'info': copy.deepcopy(config)}]}
    calls = []
    def request(value, method, path, *args, **kwargs):
        calls.append(path)
        return responses[path]
    monkeypatch.setattr(runtime, '_request', request)
    return root, responses, calls, request


def test_ready_bool_and_bounded_success_diagnostic(monkeypatch, tmp_path):
    healthy_runtime(monkeypatch, tmp_path)
    assert runtime.readiness_diagnostic() == {'ready': True, 'stage': 'complete', 'code': 'ready', 'retryable': False}
    assert runtime.ready() is True


@pytest.mark.parametrize('error_class', [httpx.ConnectError, httpx.ReadTimeout])
def test_one_local_transport_interruption_recovers_without_model_call(monkeypatch, tmp_path, error_class):
    _, _, calls, request = healthy_runtime(monkeypatch, tmp_path)
    attempts = [];sleeps = []
    def transient(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:raise error_class('PRIVATE-EXCEPTION-TEXT')
        return request(*args, **kwargs)
    monkeypatch.setattr(runtime, '_request', transient)
    monkeypatch.setattr(runtime.time, 'sleep', sleeps.append)
    assert runtime.require_ready()['ready']
    assert sleeps == [.2] and len(attempts) == 4 and len(calls) == 3


@pytest.mark.parametrize('status', [429, 502, 503, 504])
def test_only_allowed_http_statuses_get_one_short_retry(monkeypatch, tmp_path, status):
    _, _, _, request = healthy_runtime(monkeypatch, tmp_path)
    attempts = [];sleeps = []
    def transient(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            req = httpx.Request('GET', 'http://127.0.0.1:12345/private')
            raise httpx.HTTPStatusError('PRIVATE-ERROR', request=req, response=httpx.Response(status, request=req))
        return request(*args, **kwargs)
    monkeypatch.setattr(runtime, '_request', transient)
    monkeypatch.setattr(runtime.time, 'sleep', sleeps.append)
    assert runtime.require_ready()['ready'] and sleeps == [.2]
    assert len(attempts) == 4


def test_retry_is_bounded_and_logs_only_enums(monkeypatch, tmp_path, caplog):
    healthy_runtime(monkeypatch, tmp_path)
    attempts = [];sleeps = [];callbacks = []
    def unavailable(*args, **kwargs):
        attempts.append(True)
        raise httpx.ConnectError('PRIVATE-EXCEPTION-TEXT owner-only-model-key must-not-reach-opencode')
    monkeypatch.setattr(runtime, '_request', unavailable)
    monkeypatch.setattr(runtime.time, 'sleep', sleeps.append)
    with pytest.raises(ValueError, match='AI运行服务暂不可用') as caught:
        runtime.run_session({}, 'unsubmitted task', {}, lambda *args: callbacks.append(args))
    assert len(attempts) == 2 and sleeps == [.2] and not callbacks
    assert 'local_connection_error' in caplog.text and 'stage=version' in caplog.text
    for text in ('PRIVATE-', 'owner-only-model-key', 'must-not-reach-opencode', 'http://'):
        assert text not in caplog.text and text not in str(caught.value)


@pytest.mark.parametrize('status', [401, 403, 408, 500])
def test_auth_or_unlisted_http_failure_is_not_retried(monkeypatch, tmp_path, status, caplog):
    healthy_runtime(monkeypatch, tmp_path)
    attempts = [];sleeps = []
    def unavailable(*args, **kwargs):
        attempts.append(True)
        req = httpx.Request('GET', 'http://127.0.0.1:12345/?secret=PRIVATE-URL')
        raise httpx.HTTPStatusError('PRIVATE-STATUS-BODY', request=req, response=httpx.Response(status, request=req))
    monkeypatch.setattr(runtime, '_request', unavailable)
    monkeypatch.setattr(runtime.time, 'sleep', sleeps.append)
    diagnostic = runtime.readiness_diagnostic()
    assert diagnostic['http_status'] == status and diagnostic['error_type'] == 'http_status_error'
    assert diagnostic['retryable'] is False
    attempts.clear()
    with pytest.raises(ValueError, match='连接校验未通过'):runtime.require_ready()
    assert len(attempts) == 1 and not sleeps and 'PRIVATE-' not in caplog.text


@pytest.mark.parametrize('tamper,code,stage', [
    ('version', 'version_mismatch', 'version'),
    ('plugin', 'plugin_set_mismatch', 'plugin_catalog'),
    ('plugin_file', 'plugin_integrity_mismatch', 'plugin_integrity'),
    ('local_config', 'configuration_integrity_mismatch', 'configuration_integrity'),
    ('effective_config', 'effective_configuration_mismatch', 'effective_configuration'),
])
def test_configuration_and_integrity_fail_closed_without_retry_or_rewrite(monkeypatch, tmp_path, tamper, code, stage):
    root, responses, calls, _ = healthy_runtime(monkeypatch, tmp_path)
    config_path = root / 'config/opencode/opencode.json'
    if tamper == 'version':responses['/api/info']['version'] = 'unexpected-version'
    elif tamper == 'plugin':responses['/api/plugin']['data'].append({'id': 'unexpected-plugin'})
    elif tamper == 'plugin_file':(root / 'plugin/index.js').write_bytes(b'PRIVATE-CHANGED-PLUGIN')
    elif tamper == 'local_config':config_path.write_bytes(b'{}')
    else:responses['/api/config'][0]['info']['instructions'] = ['PRIVATE-INSTRUCTIONS']
    before_config = config_path.read_bytes();before_plugin = (root / 'plugin/index.js').read_bytes()
    diagnostic = runtime.readiness_diagnostic()
    assert diagnostic == {'ready':False, 'stage':stage, 'code':code, 'retryable':False}
    calls.clear();sleeps = []
    monkeypatch.setattr(runtime.time, 'sleep', sleeps.append)
    with pytest.raises(ValueError, match='连接校验未通过'):runtime.require_ready()
    assert calls.count('/api/info') == 1 and not sleeps
    assert config_path.read_bytes() == before_config and (root / 'plugin/index.js').read_bytes() == before_plugin


def test_protocol_json_uses_utf8_for_unicode_paths_regardless_default_encoding(monkeypatch, tmp_path):
    unicode_root = tmp_path / '中文 运行路径';unicode_root.mkdir()
    root, _, _, _ = healthy_runtime(monkeypatch, unicode_root)
    # A real Unicode plugin path is present in the UTF-8 configuration bytes.
    config = json.loads((root / 'config/opencode/opencode.json').read_bytes().decode('utf-8'))
    assert '中文 运行路径' in config['plugins'][-2]['package']
    original = Path.read_text;reads = []
    def checked_read(path, *args, **kwargs):
        if path.is_relative_to(root) and path.suffix == '.json':
            reads.append(path.name)
            assert kwargs.get('encoding') == 'utf-8'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', checked_read)
    assert runtime.ready() is True
    assert set(reads) == {'connection.json', 'opencode.json'}
