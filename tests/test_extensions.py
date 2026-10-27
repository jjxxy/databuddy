"""Actual Skill versions and personal MCP connections retain ownership and gates."""
import io,json,os,stat,threading,zipfile,shutil
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from agent_service import extensions as e,work_tools as w,provider
from agent_service.actor_context import actor_scope
from agent_service.config import DATA,TOKEN
from agent_service.store import all_items,get,put,patch,delete,now,uid,unseal
from agent_service.jobs import jobs,engine

A='u_'+'a'*64;B='u_'+'b'*64

@pytest.fixture(autouse=True)
def clean():
    kinds=('installed_skill','extension_setting','mcp_preset','work_connector','work_action','turn','conversation','project')
    previous={k:{v['id'] for v in all_items(k)} for k in kinds}
    with engine.connect() as connection:previous_jobs=set(connection.execute(select(jobs.c.id)).scalars())
    root=DATA/'extensions'/'packages';folders=set(root.iterdir()) if root.exists() else set()
    yield
    # decide() intentionally enqueues a real durable work_action job even when
    # this test directly calls perform(). Remove those test-owned queue rows
    # before deleting their action/turn records, so later process_once() tests
    # cannot claim orphan jobs belonging to this fixture.
    with engine.begin() as connection:
        query=jobs.delete()
        if previous_jobs:query=query.where(jobs.c.id.not_in(previous_jobs))
        connection.execute(query)
    for kind in kinds:
        for value in all_items(kind):
            if value['id'] not in previous[kind]:delete(kind,value['id'])
    if root.exists():
        for folder in root.iterdir():
            if folder not in folders:shutil.rmtree(folder)


def package(instruction='First verify exact source totals.',extra=None):
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:
        z.writestr('finance/SKILL.md','---\nname: monthly-finance\ndescription: Repeatable monthly close\nversion: 1\n---\n'+instruction)
        z.writestr('finance/references/rules.md','Never guess missing currency.')
        for name,data in (extra or {}).items():z.writestr(name,data)
    return stream.getvalue()

def turn(pid=None,actor='owner'):
    pid=pid or put('project',{'name':'Extensions acceptance','created_at':now()},'p_'+uid())['id']
    sid='c_'+uid();put('conversation',{'project_id':pid,'name':'Extension task','created_at':now()},sid)
    return put('turn',{'project_id':pid,'actor_id':actor,'session_id':sid,'text':'请生成并保存工作汇报','state':'agent','interaction_mode':'work','created_at':now(),'agent_trace':[{'id':uid(),'tool':'prepare_work_action','status':'started','arguments':{}}]},'t_'+uid())


def test_skill_install_load_references_and_content_provenance(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    installed=e.install_skill('finance.zip',package())
    assert installed['files_count']==2 and installed['origin']=='personal' and installed['enabled']
    t=turn();loaded=e.load_skill(t,installed['id'])
    assert loaded['digest']==installed['digest'] and loaded['untrusted_instructions']
    assert 'First verify exact source totals.' in loaded['instructions']
    observed=e.read_skill_file(t,e.SkillFile(skill_id=installed['id'],path='references/rules.md'))
    assert observed['content']=='Never guess missing currency.'
    assert get('turn',t['id'])['loaded_skills'][0]['digest']==installed['digest']


def test_actor_scope_not_merely_frontend_filter(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    with actor_scope(A):
        skill=e.install_skill('SKILL.md',b'A private business rule')
        origin=turn(actor=A);e.load_skill(origin,skill['id'])
    with actor_scope(B):
        assert not e.list_skills()['items']
        with pytest.raises(ValueError):e.toggle_skill(skill['id'],e.Toggle(enabled=False,expected_digest=skill['digest']))
        with pytest.raises(ValueError):e.load_skill(turn(actor=B),skill['id'])
        with pytest.raises(ValueError):e.read_skill_file(origin,e.SkillFile(skill_id=skill['id'],path='SKILL.md'))


def test_preset_update_does_not_change_inflight_skill_or_silently_enable_new_version(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    original=e.install_skill('SKILL.md',b'original trusted procedure',preset=True)
    t=turn();e.load_skill(t,original['id'])
    changed=e.install_skill('SKILL.md',b'new procedure',preset=True,identity=original['id'])
    assert changed['digest']!=original['digest'] and not changed['enabled']
    assert e.load_skill(t,original['id'])['instructions']=='original trusted procedure'
    with pytest.raises(ValueError):e.load_skill(turn(),original['id'])
    e.toggle_skill(original['id'],e.Toggle(expected_digest=changed['digest'],enabled=True))
    assert e.load_skill(turn(),original['id'])['instructions']=='new procedure'
    with pytest.raises(ValueError):e.remove_skill(original['id'],changed['digest'])


def test_skill_disable_and_tamper_reject_reads(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    item=e.install_skill('finance.zip',package());t=turn();e.load_skill(t,item['id'])
    e.toggle_skill(item['id'],e.Toggle(expected_digest=item['digest'],enabled=False))
    with pytest.raises(ValueError):e.read_skill_file(t,e.SkillFile(skill_id=item['id'],path='references/rules.md'))
    e.toggle_skill(item['id'],e.Toggle(expected_digest=item['digest'],enabled=True))
    (DATA/'extensions/packages'/item['digest']/'references/rules.md').write_text('altered')
    with pytest.raises(ValueError,match='摘要'):e.read_skill_file(t,e.SkillFile(skill_id=item['id'],path='references/rules.md'))


@pytest.mark.parametrize('entry',['../escape.txt','/outside.txt','C:/secret.txt','finance/../../outside.txt','finance/.env'])
def test_archive_paths_cannot_escape_or_include_secret_hidden_files(entry):
    with pytest.raises(ValueError):e.install_skill('skill.zip',package(extra={entry:'untrusted'}))


def test_zip_link_duplicate_and_decompression_limits():
    for kind in ('link','duplicate','oversized'):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as z:
            z.writestr('SKILL.md','allowed')
            if kind=='link':
                item=zipfile.ZipInfo('link');item.external_attr=(stat.S_IFLNK|0o777)<<16;z.writestr(item,'/etc/passwd')
            elif kind=='duplicate':z.writestr('SKILL.md','different')
            else:z.writestr('large.txt',b'x'*(e.MAX_EXPANDED+1))
        with pytest.raises(ValueError):e.install_skill('skill.zip',stream.getvalue())


@pytest.fixture
def remote(monkeypatch):
    calls=[]
    class Server(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.headers.get('Authorization')=='Bearer fixture-private-mcp-key'
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(request)
            method=request['method']
            if method=='initialize':result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}}}
            elif method=='notifications/initialized':self.send_response(202);self.end_headers();return
            elif method=='tools/list':result={'tools':[{'name':name,'description':name,'inputSchema':{'type':'object'}} for name in ('search','write_report')]}
            else:result={'content':[{'type':'text','text':'actual tool result'}]}
            data=json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(data)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Server);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setenv('ATLAS_LOCAL_MODE','1');monkeypatch.setenv('ATLAS_LOCAL_WORKSPACE',str(DATA));monkeypatch.setenv('API_ALLOWED_HOSTS','127.0.0.1')
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True,'base_url':'https://model.test/v1','model':'fixture','api_key':'not-user-visible'})
    value=e.MCPInput(name='Local finance MCP',url=f'http://127.0.0.1:{server.server_port}/mcp',credential='Bearer fixture-private-mcp-key',read_tools=['search'],write_tools=['write_report'],storage_approved=True,model_approved=True)
    yield value,calls
    server.shutdown();server.server_close();thread.join(timeout=2)


def test_real_http_personal_mcp_calls_and_external_approval(remote):
    value,calls=remote;t=turn()
    conn=e.configure_mcp(t['project_id'],value)
    assert 'fixture-private-mcp-key' not in json.dumps(conn)
    assert 'fixture-private-mcp-key' not in json.dumps(get('work_connector',conn['id']))
    observed=w.read_tool(t,w.ToolCall(connector_id=conn['id'],tool='search',arguments={}))
    assert observed['result']['content'][0]['text']=='actual tool result'
    with pytest.raises(ValueError):w.read_tool(t,w.ToolCall(connector_id=conn['id'],tool='write_report',arguments={}))
    action=w.propose(t,w.ToolCall(connector_id=conn['id'],tool='write_report',arguments={}))
    assert action['state']=='pending'
    before=len([c for c in calls if c['method']=='tools/call'])
    w.decide(t['project_id'],action['id'],w.ActionDecision(digest=action['digest'],approve=True))
    assert w.perform(action['id'])['state']=='succeeded'
    assert w.perform(action['id'])['state']=='succeeded'
    assert len([c for c in calls if c['method']=='tools/call'])==before+1


def test_private_mcp_cannot_be_found_called_or_approved_by_other_actor(remote):
    value,_=remote
    with actor_scope(A):
        t=turn(actor=A);conn=e.configure_mcp(t['project_id'],value)
        action=w.propose(t,w.ToolCall(connector_id=conn['id'],tool='write_report'))
    with actor_scope(B):
        assert not w.list_connectors(t['project_id']) and not e.catalog(t['project_id'])['mcp']
        with pytest.raises(ValueError):w.read_tool(t,w.ToolCall(connector_id=conn['id'],tool='search'))
        with pytest.raises(ValueError):w.decide(t['project_id'],action['id'],w.ActionDecision(digest=action['digest'],approve=True))


def test_preset_credentials_remain_managed_and_updates_invalidate_prepared_actions(remote):
    value,_=remote;t=turn()
    preset=e.preset_mcp(value)
    assert 'fixture-private' not in json.dumps(e.catalog(t['project_id']))
    conn=e.enable_preset(t['project_id'],preset['id'],e.Toggle(enabled=True,expected_digest=preset['digest']))
    assert conn['origin']=='preset' and not conn['can_remove']
    action=w.propose(t,w.ToolCall(connector_id=conn['id'],tool='write_report'))
    w.decide(t['project_id'],action['id'],w.ActionDecision(digest=action['digest'],approve=True))
    updated=e.preset_mcp(value,preset['id'])
    e.enable_preset(t['project_id'],preset['id'],e.Toggle(enabled=True,expected_digest=updated['digest']))
    assert w.perform(action['id'])['state']=='failed'
    with pytest.raises(ValueError):e.configure_mcp(t['project_id'],value,conn['id'])


def test_main_api_requires_login_and_distinct_operator_for_presets(monkeypatch):
    from agent_service.api import app
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    assert client.get('/api/extensions').status_code==401
    response=client.post('/api/extensions/skills',headers=headers,files={'file':('SKILL.md',b'test API procedure','text/markdown')})
    assert response.status_code==200,response.text
    assert response.json()['origin']=='personal'
    assert client.post('/api/operator/extensions/skills',headers=headers,files={'file':('SKILL.md',b'admin preset')}).status_code==403
    monkeypatch.setenv('ATLAS_OPERATOR_TOKEN','fixture-only-operator-secret-long')
    operator={**headers,'X-Atlas-Operator-Token':'fixture-only-operator-secret-long'}
    assert client.post('/api/operator/extensions/skills',headers=operator,files={'file':('SKILL.md',b'admin preset')}).status_code==200


def test_trusted_preset_bootstrap_is_idempotent_and_does_not_contact_optional_servers(monkeypatch):
    monkeypatch.setattr(e,'probe_mcp',lambda *args:pytest.fail('startup must not contact optional servers'))
    value={'skills':[{'id':'department-rule','content':'---\nname: department-guidance\ndescription: Effective business procedure\n---\nRead actual sources.'}],
           'mcp':[{'id':'department-mcp','configuration':{'name':'Managed system','url':'https://company.test/mcp','credential':'Bearer fixture-only-key','read_tools':['search'],'storage_approved':True}}]}
    first=e.provision_presets(value);revision=get('mcp_preset',first['mcp'][0]['id'])['digest']
    second=e.provision_presets(value)
    assert second['mcp'][0]['unchanged'] is True
    assert get('mcp_preset',first['mcp'][0]['id'])['digest']==revision
    assert len([v for v in e.list_skills()['items'] if v['origin']=='preset'])==2
    assert 'fixture-only-key' not in json.dumps(e.catalog())


def test_default_local_stdio_template_is_operational_without_custom_install(tmp_path,monkeypatch):
    monkeypatch.setenv('ATLAS_LOCAL_MODE','1');monkeypatch.setenv('ATLAS_LOCAL_WORKSPACE',str(tmp_path))
    monkeypatch.delenv('ATLAS_STDIO_TEMPLATES',raising=False)
    assert 'atlas-local-clock' in e.catalog()['policy']['stdio_allowed_commands']
    value=e.MCPInput(name='本机时间',transport='stdio',command='atlas-local-clock',command_approved=True,read_tools=['get_local_time'],storage_approved=True,model_approved=True)
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True,'base_url':'https://model.test','model':'fixture'})
    t=turn();connector=e.configure_mcp(t['project_id'],value)
    result=w.read_tool(t,w.ToolCall(connector_id=connector['id'],tool='get_local_time',arguments={'timezone':'Asia/Shanghai'}))
    assert json.loads(result['result']['content'][0]['text'])['source']=='local_system_clock'


def test_disabled_personal_connection_can_be_reverified_without_reentering_secret(remote):
    value,calls=remote;t=turn();record=e.configure_mcp(t['project_id'],value)
    e.disconnect_mcp(t['project_id'],record['id'])
    cleared=value.model_copy(update={'credential':e.SecretStr('')})
    reconnected=e.configure_mcp(t['project_id'],cleared,record['id'])
    assert reconnected['enabled'] and reconnected['id']==record['id']
    assert reconnected['storage_approved'] is True
    assert unseal(get('work_connector',record['id'])['secret_config'])['credential']=='Bearer fixture-private-mcp-key'


def test_preset_model_approval_does_not_follow_changed_destination(remote,monkeypatch):
    value,_=remote;t=turn();preset=e.preset_mcp(value)
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True,'base_url':'https://other-model.test','model':'fixture'})
    with pytest.raises(ValueError,match='模型已变化'):e.enable_preset(t['project_id'],preset['id'],e.Toggle(enabled=True,expected_digest=preset['digest']))
    assert e.catalog(t['project_id'])['presets'][0]['permissions']['read_tools']==['search']


def test_operator_legacy_update_cannot_drop_personal_ownership(remote):
    value,_=remote
    with actor_scope(A):
        t=turn(actor=A);record=e.configure_mcp(t['project_id'],value)
        updated=w.configure(t['project_id'],e._http_input(value),record['id'])
        assert updated['owner_id']==A and updated['extension_origin']=='personal'
    with actor_scope(B):assert w.list_connectors(t['project_id'])==[]


def test_preset_manifest_reader_is_bounded_redacted_and_rejects_duplicate_keys(tmp_path):
    from agent_service.preset_configuration import load_presets_file,MAX_PRESET_BYTES
    path=tmp_path/'presets.json';path.write_text('{"skills":[],"mcp":[]}')
    assert load_presets_file(path)=={'skills':[],'mcp':[]}
    for raw in ('{"skills":[],"skills":[],"secret":"private-value"}', '[1]', '{"mcp":{}}', 'x'*(MAX_PRESET_BYTES+1)):
        path.write_text(raw)
        with pytest.raises(ValueError) as failure:load_presets_file(path)
        assert 'private-value' not in str(failure.value)
def test_preset_manifest_reader_rejects_symlinks_when_os_can_create_them(tmp_path):
    from agent_service.preset_configuration import load_presets_file
    path=tmp_path/'presets.json';path.write_text('{"skills":[],"mcp":[]}')
    link=tmp_path/'link.json'
    try:link.symlink_to(path)
    except OSError as error:
        if os.name=='nt' and getattr(error,'winerror',None)==1314:
            pytest.skip('Windows account lacks permission to create symbolic links')
        raise
    with pytest.raises(ValueError):load_presets_file(link)


def test_preset_manifest_reader_rejects_real_hardlinks(tmp_path):
    from agent_service.preset_configuration import load_presets_file
    path=tmp_path/'presets.json';path.write_text('{"skills":[],"mcp":[]}')
    link=tmp_path/'hardlink.json';os.link(path,link)
    with pytest.raises(ValueError):load_presets_file(link)
