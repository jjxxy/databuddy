import copy,json
import httpx,pytest
from agent_service import opencode,provider,company_search
from agent_service.store import put,get,delete,unseal,uid


def value():return {'engine':'opencode','base_url':'https://oc.test','model':'atlas/deepseek-v4-pro','api_key':'private-service-password','upstream_base_url':'https://api.deepseek.com','tool_protocol':'json','share_data':True}

def server(monkeypatch,change=None):
    model={'id':'deepseek-v4-pro','providerID':'atlas','modelID':'deepseek-v4-pro','enabled':True,'settings':{'baseURL':'https://api.deepseek.com'}}
    data={'/api/info':{'version':opencode.VERSION},'/api/config':[{'type':'document','info':{'permissions':[{'action':'*','resource':'*','effect':'deny'}]}}],'/api/provider/atlas':{'data':{'package':'@opencode/ai/providers/openai-compatible'}},'/api/model/default':{'data':model},'/api/experimental/generate':{'data':{'text':'{"kind":"final","answer":"已完成"}'}}}
    if change:change(data)
    calls=[]
    def request(v,method,path,body=None,**kwargs):calls.append((method,path,body));return copy.deepcopy(data[path])
    monkeypatch.setattr(opencode,'request',request)
    return data,calls


def test_v2_decisions_use_v2_api_and_not_v1_tool_protocol(monkeypatch):
    data,calls=server(monkeypatch);v=value();v.update(opencode.validate_configuration(v))
    content,tokens,receipt=opencode.complete([{'role':'user','content':'你好'}],v,None,128)
    assert json.loads(content)['kind']=='final' and tokens=={} and receipt['upstream_token_limit_enforced'] is False
    method,path,body=calls[-1]
    assert path=='/api/experimental/generate' and body['model']=={'providerID':'atlas','id':'deepseek-v4-pro'}
    assert set(body)=={'prompt','model'} and 'private-service-password' not in json.dumps(body)

@pytest.mark.parametrize('change',[
    lambda d:d['/api/info'].update(version='1.19.3'),
    lambda d:d['/api/info'].update(version='2.0.33'),
    lambda d:d['/api/model/default']['data']['settings'].update(baseURL='https://unapproved.test'),
    lambda d:d['/api/model/default']['data'].update(enabled=False),
    lambda d:d['/api/provider/atlas']['data'].update(package='untrusted-npm-package'),
])
def test_unverified_runtime_and_changed_destination_fail_before_prompt(monkeypatch,change):
    data,calls=server(monkeypatch,change)
    with pytest.raises(ValueError):opencode.validate_configuration(value())
    assert not any(method=='POST' for method,_,_ in calls)


def test_config_drift_blocks_export_and_native_calls_are_not_faked(monkeypatch):
    data,calls=server(monkeypatch);v=value();v.update(opencode.inspect(v))
    data['/api/config'][0]['info']['model']='other/model'
    with pytest.raises(ValueError,match='变化'):opencode.complete([{'role':'user','content':'internal content'}],v,None,128)
    assert not any(method=='POST' for method,_,_ in calls)
    v.update(opencode.inspect(v))
    with pytest.raises(provider.ModelOutputError):opencode.complete([],v,[{}],128)


def test_service_auth_pinned_network_and_response_limit(monkeypatch):
    monkeypatch.setattr('agent_service.connectors.safe_api_url',lambda u:'203.0.113.25');original=httpx.Client;seen=[]
    def handler(req):seen.append(req);return httpx.Response(200,json={'version':opencode.VERSION})
    def client(**kw):
        assert kw['trust_env'] is False and kw['follow_redirects'] is False
        return original(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(opencode.httpx,'Client',client)
    assert opencode.request(value(),'GET','/api/info')['version']==opencode.VERSION
    assert seen[0].url.host=='203.0.113.25' and seen[0].headers['host']=='oc.test'
    assert seen[0].headers['authorization'].startswith('Basic ')
    with pytest.raises(ValueError,match='超过'):opencode.request(value(),'GET','/api/info',limit=1)


def test_private_approval_changes_when_runtime_changes(monkeypatch):
    v=value();v.update(opencode_version=opencode.VERSION,opencode_fingerprint='first')
    monkeypatch.setattr(provider,'settings',lambda:v)
    before=company_search.model_fingerprint();v['opencode_fingerprint']='second'
    assert company_search.model_fingerprint()!=before
    v['engine']='atlas';assert company_search.model_fingerprint()!=before


def test_configure_encrypts_service_password_and_uses_json(monkeypatch):
    server(monkeypatch);monkeypatch.setattr('agent_service.connectors.safe_api_url',lambda u:'203.0.113.25')
    monkeypatch.setattr(provider,'complete',lambda *a,**kw:'OK')
    # This test does not require other test-created tasks to be terminal.
    original=__import__('agent_service.store',fromlist=['all_items']).all_items
    monkeypatch.setattr('agent_service.store.all_items',lambda kind:[] if kind=='turn' else original(kind))
    try:
        result=provider.configure(provider.ProviderInput(**dict(value(),tool_protocol='native')))
        assert result['engine']=='opencode' and result['tool_protocol']=='json'
        assert 'api_key' not in result and 'private-service-password' not in json.dumps(get('system','model-provider'))
        assert unseal(get('system','model-provider')['secret_config'])['api_key']=='private-service-password'
    finally:delete('system','model-provider')
