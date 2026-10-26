import copy
import json
import os
from pathlib import Path
import pytest
from agent_service import opencode_runtime as runtime


def _prepare(monkeypatch, tmp_path, turns=None):
    root = tmp_path / 'private-runtime'
    monkeypatch.setattr(runtime, '_root', lambda: root)
    binary = tmp_path / 'opencode'
    binary.write_bytes(b'test-only-not-executed')
    monkeypatch.setenv('ATLAS_OPENCODE_BINARY', str(binary))
    monkeypatch.setenv('ATLAS_OPENCODE_RUNTIME', '1')
    monkeypatch.setenv('APP_TOKEN', 'must-not-reach-opencode')
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-reach-opencode')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'must-not-reach-opencode')
    monkeypatch.setenv('OPENCODE_CONFIG_CONTENT', '{"plugins":["untrusted"]}')
    monkeypatch.setattr('agent_service.store.all_items', lambda kind: turns or [])
    entry = {'id': 'fast', 'name': '业务模型', 'base_url': 'https://model.example/v1',
             'model': 'business-model', 'api_key': 'owner-only-model-key', 'engine': 'atlas'}
    spec = runtime.prepare({'models': [entry], 'default_model_id': 'fast'}, {'type': 'object'})
    return root, entry, spec


def test_supervisor_uses_private_runtime_and_no_ambient_credentials(monkeypatch, tmp_path):
    root, entry, spec = _prepare(monkeypatch, tmp_path)
    assert spec['cwd'] == str(root / 'work')
    assert spec['command'][-4:] == ['--hostname', '127.0.0.1', '--port', runtime.connection()['base_url'].rsplit(':', 1)[1]]
    assert not {'APP_TOKEN', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'OPENCODE_CONFIG_CONTENT'} & spec['env'].keys()
    assert spec['env']['HOME'].startswith(str(root))
    assert spec['env']['OPENCODE_DISABLE_FREE_MODELS'] == 'true'
    config = json.loads((root / 'config/opencode/opencode.json').read_text())
    assert config['plugins'][0] == '-*' and '*' not in config['plugins']
    assert config['permissions'] == [{'action': '*', 'resource': '*', 'effect': 'deny'},
                                     {'action': 'atlas_step', 'resource': '*', 'effect': 'allow'}]
    assert config['mcp'] == {'servers': {}}
    if os.name!='nt':
        assert (root / 'connection.json').stat().st_mode & 0o777 == 0o600
    # Windows confidentiality is verified through the actual private-directory
    # DACL test, not through POSIX mode bits that Windows does not implement.
    assert 'owner-only-model-key' not in json.dumps(runtime.connection())
    with pytest.raises(ValueError, match='未加载'):
        runtime.connection(dict(entry, api_key='rotated-key'))


def test_prepare_keeps_old_pinned_model_route_without_changing_current_model(monkeypatch, tmp_path):
    old = {'base_url': 'https://old.example/v1', 'model': 'old-model', 'api_key': 'old-owner-key'}
    monkeypatch.setattr('agent_service.model_catalog.settings_for_turn', lambda turn: old)
    root, entry, spec = _prepare(monkeypatch, tmp_path,
        [{'state': 'done', 'resume_available': True, 'model_catalog_revision': 'old-revision'}])
    assert len(runtime.connection()['providers']) == 2
    assert runtime.connection(old)['model_ref']['id'] == 'old-model'
    assert runtime.connection(entry)['model_ref']['id'] == 'business-model'


@pytest.mark.parametrize('change', [
    {'target_url': 'https://fallback.example/v1/chat/completions'},
    {'target_url': 'https://model.example/v1/chat/completions?copy=1'},
    {'target_url': 'https://model.example/v2/chat/completions'},
    {'model_id': 'unselected-model'},
])
def test_every_upstream_request_must_match_pinned_model_and_exact_destination(change):
    selected = {'base_url': 'https://model.example/v1', 'model': 'selected-model'}
    payload = {'target_url': 'https://model.example/v1/chat/completions', 'model_id': 'selected-model'}
    runtime._check_upstream(payload, selected)
    with pytest.raises(ValueError, match='不匹配'):
        runtime._check_upstream(dict(payload, **change), selected)


@pytest.mark.parametrize('tamper', ['foreign_plugin', 'extra_document', 'mcp', 'instructions', 'permissions', 'plugin_source', 'local_plugin', 'compaction'])
def test_readiness_rejects_effective_configuration_and_plugin_drift(monkeypatch, tmp_path, tamper):
    root, entry, spec = _prepare(monkeypatch, tmp_path)
    config = json.loads((root / 'config/opencode/opencode.json').read_text())
    plugins = [{'id': name, 'state': {'status': 'active'}, 'source': {'type': 'builtin'}} for name in runtime.BUILTIN_PLUGINS]
    plugins.append({'id': 'atlas.business-tools', 'state': {'status': 'active'},
                    'source': {'type': 'local', 'path': str(root / 'plugin/index.js')}})
    loaded = [{'type': 'document', 'path': str(root / 'config/opencode/opencode.json'), 'info': copy.deepcopy(config)}]
    def request(value, method, path, *args, **kwargs):
        return {'/api/info': {'version': runtime.VERSION}, '/api/plugin': {'data': plugins}, '/api/config': loaded}[path]
    monkeypatch.setattr(runtime, '_request', request)
    assert runtime.ready()
    if tamper == 'foreign_plugin':plugins.append({'id': 'untrusted.plugin'})
    elif tamper == 'extra_document':loaded.append({'type': 'document', 'path': '/untrusted/opencode.json', 'info': {}})
    elif tamper == 'plugin_source':plugins[-1]['source']['path'] = '/untrusted/index.js'
    elif tamper == 'local_plugin':(root / 'plugin/index.js').write_text('untrusted')
    elif tamper == 'mcp':loaded[0]['info']['mcp'] = {'servers': {'bad': {'url': 'https://bad.example'}}}
    elif tamper == 'instructions':loaded[0]['info']['instructions'] = ['untrusted.md']
    elif tamper == 'permissions':loaded[0]['info']['permissions'].append({'action': '*', 'resource': '*', 'effect': 'allow'})
    elif tamper == 'compaction':loaded[0]['info']['compaction']['auto']=False
    assert runtime.ready() is False


def test_compaction_requires_completed_matching_message_not_admission(monkeypatch):
    identity=runtime._compaction_identity('turn-1',[])
    messages=[[],[{'type':'compaction','id':identity,'status':'pending'}],
              [{'type':'compaction','id':'unrelated','status':'completed','summary':'wrong'}],
              [{'type':'compaction','id':identity,'status':'completed','summary':'Preserved user constraints'}]]
    requests=[]
    def request(value,method,path,body=None):
        requests.append((method,path,body))
        return {'data':messages.pop(0)} if method=='GET' else {'data':{'id':identity,'status':'pending'}}
    monkeypatch.setattr(runtime,'_request',request)
    monkeypatch.setattr(runtime.time,'sleep',lambda _:None)
    result=runtime.compact_session({},'ses-1','turn-1',timeout=1)
    assert result['completed'] and result['summary_message_id']==identity
    assert requests[1]==('POST','/api/session/ses-1/compact',{'id':identity})


def test_compaction_failure_preserves_originals_and_does_not_report_success(monkeypatch):
    before=[{'type':'user','id':'original'}]
    identity=runtime._compaction_identity('turn-1',before)
    reads=[before,[{'type':'compaction','id':identity,'status':'failed','error':{'message':'No summary'}}]]
    def request(value,method,path,body=None):
        return {'data':reads.pop(0)} if method=='GET' else {'data':{'id':identity}}
    monkeypatch.setattr(runtime,'_request',request)
    with pytest.raises(ValueError,match='原生压缩失败'):runtime.compact_session({},'ses-1','turn-1',timeout=1)


def test_compaction_request_id_changes_after_more_native_history():
    old=[{'id':'summary-1','type':'compaction'}]
    new=old+[{'id':'user-2','type':'user'},{'id':'assistant-2','type':'assistant'}]
    assert runtime._compaction_identity('turn',old)!=runtime._compaction_identity('turn',new)


def test_compaction_admission_without_completed_receipt_times_out(monkeypatch):
    monkeypatch.setattr(runtime,'_request',lambda *args,**kwargs:{'data':[]})
    monkeypatch.setattr(runtime.time,'sleep',lambda _:None)
    moments=iter([0,0,2])
    monkeypatch.setattr(runtime.time,'monotonic',lambda:next(moments))
    with pytest.raises(ValueError,match='未获得完成回执'):runtime.compact_session({},'ses-1','turn-1',timeout=1)


def test_local_only_rejects_old_cloud_route_before_runtime_prepare(monkeypatch,tmp_path):
    monkeypatch.setenv('ATLAS_NETWORK_MODE','local_only')
    with pytest.raises(ValueError,match='仅本地连接模式'):_prepare(monkeypatch,tmp_path)

