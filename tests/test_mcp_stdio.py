"""Real local MCP process framing, bounded execution and secret-minimal env."""
import json,os,sys
from pathlib import Path
import pytest
from agent_service import mcp_stdio as s

@pytest.fixture
def setup(tmp_path,monkeypatch):
    script=tmp_path/'server.py'
    script.write_text('''import sys,json,os,time
for line in sys.stdin:
 request=json.loads(line);method=request['method']
 if method=='notifications/initialized':continue
 if method=='initialize':result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}}}
 elif method=='tools/list':result={'tools':[{'name':'environment','inputSchema':{'type':'object'}}]}
 else:result={'content':[{'type':'text','text':json.dumps({'app_token':os.getenv('APP_TOKEN'),'model_key':os.getenv('LLM_API_KEY'),'personal_token':os.getenv('PERSONAL_TOKEN'),'cwd':os.getcwd()})}]}
 print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
''')
    monkeypatch.setenv('ATLAS_LOCAL_MODE','1');monkeypatch.setenv('ATLAS_LOCAL_WORKSPACE',str(tmp_path))
    monkeypatch.setenv('ATLAS_STDIO_TEMPLATES',json.dumps({'fixture':{'command':sys.executable,'args':[str(script)]}}))
    monkeypatch.setenv('APP_TOKEN','must-not-inherit');monkeypatch.setenv('LLM_API_KEY','must-not-inherit')
    return {'transport':'stdio','command':'fixture','args':[],'env':{'PERSONAL_TOKEN':'personal-only'}}


def test_actual_stdio_mcp_roundtrip_and_minimal_process_environment(setup):
    config,argv,root=s.validate(setup)
    with s.Session(config) as client:
        assert client.request('tools/list')['tools'][0]['name']=='environment'
        result=client.request('tools/call',{'name':'environment','arguments':{}})
        environment=json.loads(result['content'][0]['text'])
        assert environment=={'app_token':None,'model_key':None,'personal_token':'personal-only','cwd':str(root)}
        process=client.process
    assert process.poll() is not None


def test_cloud_cannot_start_local_process(setup,monkeypatch):
    monkeypatch.setenv('ATLAS_LOCAL_MODE','0')
    with pytest.raises(ValueError,match='本地电脑'):s.validate(setup)


@pytest.mark.parametrize('change',[{'command':'unapproved'},{'args':['--unexpected']},{'env':{'APP_TOKEN':'leak'}},{'env':{'NODE_OPTIONS':'--eval exploit'}},{'template_sha256':'0'*64}])
def test_unapproved_commands_args_ambient_credentials_and_template_drift_are_rejected(setup,change):
    with pytest.raises(ValueError):s.validate({**setup,**change})


def test_timeout_stops_child_and_does_not_reflect_stderr(setup,tmp_path,monkeypatch):
    script=tmp_path/'hang.py';script.write_text("import sys,time;sys.stderr.write('PRIVATE-SECRET');sys.stderr.flush();time.sleep(10)")
    monkeypatch.setenv('ATLAS_STDIO_TEMPLATES',json.dumps({'fixture':{'command':sys.executable,'args':[str(script)]}}))
    monkeypatch.setenv('ATLAS_STDIO_TIMEOUT_SECONDS','0.1')
    client=s.Session(setup)
    with pytest.raises(ValueError,match='超时') as error:
        with client:pass
    assert client.process.poll() is not None and 'PRIVATE-SECRET' not in str(error.value)


def test_preinstalled_script_changes_are_detected_before_execution(setup,tmp_path):
    frozen,_,_=s.validate(setup)
    (tmp_path/'server.py').write_text('raise RuntimeError("changed")')
    with pytest.raises(ValueError,match='已变更'):s.validate(frozen)


def test_local_only_policy_disables_arbitrary_stdio_even_if_template_preinstalled(setup,monkeypatch):
    monkeypatch.setenv('ATLAS_NETWORK_MODE','local_only')
    assert list(s.templates())==['atlas-local-clock']
    with pytest.raises(ValueError,match='未由本机管理员预置'):s.validate(setup)
