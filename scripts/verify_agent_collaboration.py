"""Run two real Atlas instances against an explicit deterministic model fixture.

This checks execution, protocol and data integrity. It does not evaluate an LLM's
business intelligence, Docker, operating-system isolation or production HA.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from decimal import Decimal
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import zipfile

import httpx

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.verify_quickstart import (FIXTURE_KEY,FIXTURE_MODEL,FIXTURE_OPERATOR_TOKEN,
    ModelFixture,isolated_environment,local_model,prepare_quickstart,workflow_document)

TOTAL=Decimal('300.000000000002')
ROWS=40


def source_bytes():
    lines=['office,actual,budget']
    for index in range(1,ROWS+1):
        amount='7.500000000002' if index==ROWS else '7.500000000000'
        lines.append('Office'+str(index).zfill(2)+','+amount+',8.000000000000')
    return ('\n'.join(lines)+'\n').encode()


def objects(value):
    """Inspect synthetic structured messages, including JSON tool receipts."""
    if isinstance(value,dict):
        yield value
        for item in value.values():yield from objects(item)
    elif isinstance(value,list):
        for item in value:yield from objects(item)
    elif isinstance(value,str):
        decoder=json.JSONDecoder()
        for index,character in enumerate(value):
            if character!='{':continue
            try:item,_=decoder.raw_decode(value[index:])
            except json.JSONDecodeError:continue
            if isinstance(item,dict):yield from objects(item)


def action(name,**arguments):return {'kind':'tool','tool':name,'arguments':arguments}
def final(text='合成协议检查已完成，业务数值以完整结果为准。'):
    return {'kind':'final','answer':text}


class CollaborationFixture:
    """Return predefined tool choices; all actual work runs in Atlas processes."""
    def __init__(self,fixture):
        self.fixture=fixture;self.original_reply=fixture.reply
        self.original_message=fixture.message
        self.routes={};self.lock=threading.Lock();self.requests=[];self.active_children=0;self.max_children=0
        self.raw_requests=[]
        fixture.reply=self.reply
        fixture.message=self.message

    def message(self,body):
        with self.lock:self.raw_requests.append(json.dumps(body,ensure_ascii=False))
        return self.original_message(body)

    def configure(self,marker,**values):self.routes[marker]=values

    @staticmethod
    def _step(context,body):
        # A native compaction legitimately removes older tool calls. The latest
        # authoritative prompt supplies the durable baseline; count only calls
        # appended after that prompt, rather than guessing from all history.
        boundary=-1
        for index,message in enumerate(body['messages']):
            try:candidate=ModelFixture.context([message])
            except ValueError:continue
            if 'completed_steps' in candidate:boundary=index
        native=sum(1 for message in body['messages'][boundary+1:] for call in message.get('tool_calls',[])
                   if call.get('function',{}).get('name')=='atlas_step')
        return len(context.get('completed_steps',[]))+native

    @staticmethod
    def _observation(context,tool,body=None):
        values=[step.get('observation',{}) for step in context.get('completed_steps',[]) if step.get('tool')==tool]
        # OpenCode preserves the original prompt while tool receipts append to
        # the native history. Dynamic IDs must come from those real receipts.
        if body:
            values.extend(obj['observation'] for obj in objects(body['messages'])
                          if obj.get('tool')==tool and isinstance(obj.get('observation'),dict))
        return (values[-1] or {}) if values else {}

    def reply(self,body):
        try:context=ModelFixture.context(body['messages'])
        except ValueError:return self.original_reply(body)
        request=context.get('request','')
        route=next(((marker,value) for marker,value in self.routes.items() if marker in request),None)
        child='fixture-child-' in request
        with self.lock:
            self.requests.append({'request':request[:180],'model':body['model'],'child':child,
                                  'native':any(t.get('function',{}).get('name')=='atlas_step' for t in body.get('tools',[]))})
            if child:
                self.active_children+=1;self.max_children=max(self.max_children,self.active_children)
        try:
            if child:time.sleep(.45)
            if route is None:
                if child:
                    ids=context.get('attached_source_ids') or [context.get('attached_source_id')]
                    steps=[action('source_schema',source_id=ids[0]),final('只读专业检查已记录来源结构；未批准指标口径。')]
                else:return self.original_reply(body)
            else:
                marker,values=route;kind=values['kind']
                if kind=='finance':
                    ids=context.get('attached_source_ids') or [context.get('attached_source_id')]
                    source=next((item for item in ids if item),None)
                    if not source:raise AssertionError('Synthetic finance task has no imported source')
                    registered=self._observation(context,'register_data_workflow',body)
                    ran=self._observation(context,'run_data_workflow',body)
                    workflow=registered.get('id') or registered.get('workflow_id') or 'not-yet-registered'
                    run=ran.get('run_id') or ran.get('id') or context.get('current_run_id') or 'not-yet-run'
                    steps=[action('discover_tools',query='register_data_workflow run_data_workflow read_data_workflow_result write_file',limit=6),
                        action('write_file',path='workflows/agent-collaboration.json',content=json.dumps(workflow_document(source),ensure_ascii=False)),
                        action('register_data_workflow',path='workflows/agent-collaboration.json'),
                        action('run_data_workflow',workflow_id=workflow),
                        action('read_data_workflow_result',run_id=run,offset=0,limit=100),final()]
                elif kind=='delegate':
                    task=next((obj for obj in objects(body['messages']) if obj.get('peer_id')==values['peer_id'] and obj.get('id')),None)
                    steps=[action('list_agent_peers'),action('delegate_agents',assignments=[{
                        'peer_id':values['peer_id'],'goal':values['goal'],'source_ids':values.get('source_ids',[]),
                        'result_ids':[],'expected_delivery':values.get('expected_delivery','artifacts')}]),
                        action('get_agent_task',task_id=task['id'] if task else 'not-yet-delegated'),final()]
                elif kind=='team':
                    steps=[action('list_agent_peers'),action('delegate_agents',assignments=[
                        {'peer_id':'local_data_checker','goal':'只读核对资料结构。fixture-child-data','source_ids':values['source_ids'],'result_ids':[]},
                        {'peer_id':'local_finance_reviewer','goal':'只读核对单位期间粒度。fixture-child-finance','source_ids':values['source_ids'],'result_ids':[]}]),final()]
                elif kind=='readonly':
                    ids=context.get('attached_source_ids') or [context.get('attached_source_id')]
                    steps=[action('source_schema',source_id=next(i for i in ids if i)),final('只读来源检查完成，结论须核阅。')]
                elif kind=='needs_input':
                    steps=[{'kind':'final','answer':'需要明确办事处映射后才能继续。','status':'needs_input'}]
                elif kind=='slow':
                    time.sleep(2)
                    steps=[action('project_context'),final()]
                else:raise AssertionError('Unknown synthetic collaboration route')
            return json.dumps(steps[min(self._step(context,body),len(steps)-1)],ensure_ascii=False)
        finally:
            if child:
                with self.lock:self.active_children-=1


def free_port():
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


class Node:
    def __init__(self,path,endpoint,*,opencode_binary=None):
        self.path=Path(path);self.path.mkdir(parents=True)
        self.port=free_port();self.url='http://127.0.0.1:'+str(self.port)
        self.token=prepare_quickstart(self.path,{'default_model_id':'finance','models':[{
            'id':'finance','name':'Synthetic collaboration model','base_url':endpoint,'model':FIXTURE_MODEL,
            'api_key':FIXTURE_KEY,'share_data':True}]})
        self.environment=isolated_environment(self.path,self.port,self.token)
        selected=self.path/'selected';selected.mkdir()
        self.data=selected/'.atlas'
        self.environment.update(ATLAS_LOCAL_MODE='1',ATLAS_LOCAL_WORKSPACE=str(selected),
            DATA_DIR=str(self.data),DATABASE_URL='sqlite:///'+str(self.data/'metadata.sqlite'),
            ATLAS_AGENT_EFFORT='deep',API_ALLOWED_HOSTS='127.0.0.1',ATLAS_AGENT_PUBLIC_URL=self.url)
        if opencode_binary:self.environment.update(ATLAS_OPENCODE_RUNTIME='1',ATLAS_OPENCODE_BINARY=str(opencode_binary))
        self.process=None;self.log=None
        self.client=httpx.Client(base_url=self.url,headers={'Authorization':'Bearer '+self.token},trust_env=False,timeout=20)

    def call(self,method,path,**kwargs):
        response=self.client.request(method,path,**kwargs);response.raise_for_status();return response.json()

    def operator(self,method,path,**kwargs):
        return self.call(method,path,headers={'X-Atlas-Operator-Token':FIXTURE_OPERATOR_TOKEN},**kwargs)

    def wait(self,check,label,timeout=120):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if self.process is not None and self.process.poll() is not None:raise RuntimeError('Atlas stopped during '+label)
            try:value=check()
            except (httpx.ConnectError,httpx.ReadError):value=None
            if value:return value
            time.sleep(.3)
        raise TimeoutError('Timed out during '+label)

    def start(self):
        self.log=(self.path/'service.log').open('a')
        self.process=subprocess.Popen([sys.executable,'-m','agent_service.cloud'],cwd=ROOT,env=self.environment,stdout=self.log,stderr=self.log)
        self.wait(lambda:self.client.get('/health').status_code==200,'API startup')
        self.wait(lambda:(lambda s:s['agent']['healthy'] and s['worker']['healthy'])(self.call('GET','/api/status')),'service heartbeats')
        return self

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5);raise RuntimeError('Atlas supervisor did not stop')
        if self.log:self.log.close();self.log=None
        self.process=None

    def close(self):self.stop();self.client.close()

    def records(self,kind):
        """Read persisted receipts for evidence only; never mutate service state."""
        with sqlite3.connect('file:'+str(self.data/'metadata.sqlite')+'?mode=ro',uri=True) as db:
            return [json.loads(body) for (body,) in db.execute('SELECT body FROM items WHERE kind=?',(kind,))]

    def project(self,name):
        return self.call('POST','/api/projects',json={'name':name,'request_id':'fixture-project-'+hashlib.sha256(name.encode()).hexdigest()[:16]})['id']

    def source(self,pid):
        source=self.call('POST',f'/api/projects/{pid}/attachments',files={'file':('forty-rows.csv',source_bytes(),'text/csv')},data={'request_id':'fixture-forty-rows'})
        self.wait(lambda:next((s for s in self.call('GET','/api/sources') if s['id']==source['id'] and s['status']=='ready'),None),'source probe')
        return source['id']

    def grant(self,pid,mode='work'):
        return self.operator('POST','/api/operator/agent-clients',json={
            'name':'Synthetic independent agent','project_ids':[pid],'approved_model_ids':['finance'],
            'interaction_mode':mode,'max_model_calls_per_task':24,'max_output_tokens':64000,
            'max_input_characters':1000000,'task_timeout_seconds':300})

    def peer(self,pid,target,grant,*,transport='a2a',url=None):
        path='/a2a' if transport=='a2a' else '/mcp/atlas'
        return self.call('POST',f'/api/projects/{pid}/agent-peers',json={
            'name':'Synthetic '+transport+' peer','transport':transport,'protocol_version':'1.0',
            'url':url or target.url+path,'credential':grant['token'],
            'remote_project_id':target.project_id,'remote_model_id':'finance','data_sharing_approved':True,
            'submit_approved':True,'model_approved':True,'approved_model_ids':['finance']})

    def say(self,pid,marker,source_ids=()):
        queued=self.call('POST',f'/api/projects/{pid}/messages',json={'text':marker,'request_id':'fixture-turn-'+hashlib.sha256(marker.encode()).hexdigest()[:32],
            'interaction_mode':'work','model_id':'finance','source_ids':list(source_ids)})
        def completed():
            turn=self.call('GET',f'/api/projects/{pid}/turns/{queued["id"]}')
            if turn['state']=='failed':raise AssertionError('Synthetic parent task failed')
            return turn if turn['state']=='done' else None
        return self.wait(completed,'parent Agent task',180)


@contextmanager
def ambiguous_proxy(target_url):
    """Forward one real submit, then lose its reply without retrying it."""
    state={'submits':0,'accepted_task_id':None}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):self.forward()
        def do_POST(self):self.forward()
        def forward(self):
            raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
            body=json.loads(raw) if raw else {}
            submit=body.get('method') in ('message/send','SendMessage')
            if submit:state['submits']+=1
            headers={k:v for k,v in self.headers.items() if k.lower() not in ('host','content-length','connection')}
            with httpx.Client(trust_env=False,timeout=30) as client:
                response=client.request(self.command,target_url+self.path,headers=headers,content=raw)
            content=response.content
            if self.command=='GET' and self.path=='/.well-known/agent-card.json' and response.status_code==200:
                # This test reverse proxy owns its public origin, as a normal
                # deployment proxy would. Keep advertised endpoints on it.
                value=response.json();origin='http://127.0.0.1:'+str(server.server_port)
                if 'url' in value:value['url']=origin+'/a2a'
                for interface in value.get('supportedInterfaces',[]):interface['url']=origin+'/a2a'
                content=json.dumps(value).encode()
            if submit:
                value=response.json().get('result',{});value=value.get('task',value)
                state['accepted_task_id']=value.get('id')
                self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
            self.send_response(response.status_code)
            self.send_header('Content-Type',response.headers.get('content-type','application/json'))
            self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield state,'http://127.0.0.1:'+str(server.server_port)
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


def verify_output(bundle):
    """Validate actual transferred bytes and exact totals independently of prose."""
    if not bundle or bundle.get('complete') is not True:raise AssertionError('Missing complete output bundle')
    manifest={key:([{k:v for k,v in item.items() if k!='content_base64'} for item in value] if key=='items' else value)
              for key,value in bundle.items() if key!='manifest_sha256'}
    expected=hashlib.sha256(json.dumps(manifest,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    assert bundle.get('manifest_sha256')==expected,'Outer handoff manifest mismatch'
    found=[]
    for item in bundle.get('items',[]):
        raw=base64.b64decode(item['content_base64'],validate=True)
        assert len(raw)==item['bytes'] and hashlib.sha256(raw).hexdigest()==item['sha256']
        if item.get('kind')!='result':continue
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            assert len(archive.namelist())==len(set(archive.namelist()))
            assert set(archive.namelist())=={'manifest.json','rows.jsonl','schema.json','context.json','provenance.jsonl','stages.json'}
            manifest=json.loads(archive.read('manifest.json'))
            assert hashlib.sha256(archive.read('manifest.json')).hexdigest()==item['result_metadata']['handoff_manifest_sha256']
            for name,metadata in manifest['files'].items():
                content=archive.read(name)
                assert len(content)==metadata['bytes'] and hashlib.sha256(content).hexdigest()==metadata['sha256']
            def unpack(value):
                if isinstance(value,list):return [unpack(item) for item in value]
                if isinstance(value,dict):
                    if value.get('$atlas')=='object' and set(value)=={'$atlas','items'}:
                        return {key:unpack(item) for key,item in value['items']}
                    if value.get('$atlas')=='decimal' and set(value)=={'$atlas','value'}:return Decimal(value['value'])
                    raise AssertionError('Unexpected synthetic result type')
                return value
            rows=[unpack(json.loads(line)['values']) for line in archive.read('rows.jsonl').decode().splitlines()]
            actual=[Decimal(str(row['actual'])) for row in rows]
            assert len(rows)==ROWS==manifest['row_count'] and sum(actual)==TOTAL
            assert item['result_metadata']['records']==ROWS
            assert sum(Decimal(str(row['budget'])) for row in rows)==Decimal('320')
            found.append({'rows':len(rows),'exact_total':str(sum(actual)),'sha256':item['sha256']})
    if not found:raise AssertionError('No actual complete financial result was transferred')
    return found


def verify_shared_budget(ledger,parent,children):
    """Check accounting and explicit grants, including an unlimited owner root."""
    from agent_service.task_budget import effective_limits
    expected=effective_limits(parent)
    for key,limit in ledger.get('limit_overrides',{}).items():
        assert key in expected and type(limit) is int and limit>0,'Invalid shared budget override'
        assert expected[key] is None or limit<=expected[key],'Shared override enlarged a task grant'
        expected[key]=limit
    assert ledger['limits']==expected,'Shared ledger widened or changed the parent grant'
    counters={'model_calls':'calls','input_characters':'input_characters','output_tokens':'output_tokens_budgeted'}
    per_turn=ledger['per_turn']
    assert {child['id'] for child in children}.issubset(per_turn)
    for key,counter in counters.items():
        used=ledger['used'][counter];baseline=ledger['baseline_usage'][counter]
        assert type(used) is int and used>=0 and type(baseline) is int and baseline>=0
        assert all(type(item[counter]) is int and item[counter]>=0 for item in per_turn.values())
        assert used==baseline+sum(item[counter] for item in per_turn.values()),'Shared usage was reset or lost'
        if expected[key] is not None:assert used<=expected[key],'Shared budget exceeded its explicit grant'
    for child in children:
        child_limits=effective_limits(child)
        for key,counter in counters.items():
            cap=child_limits[key]
            if cap is not None:assert per_turn[child['id']][counter]<=cap,'Child exceeded its delegated grant'
    return True


def verify(folder,*,opencode_binary=None):
    folder=Path(folder);nodes=[];checks={}
    native_version=None
    if opencode_binary:
        version=subprocess.run([str(opencode_binary),'--version'],capture_output=True,text=True,timeout=15,check=True)
        native_version=version.stdout.strip().removeprefix('opencode v')
    with local_model() as (base_fixture,endpoint):
        fixture=CollaborationFixture(base_fixture)
        a=Node(folder/'A',endpoint,opencode_binary=opencode_binary);nodes.append(a)
        b=Node(folder/'B',endpoint,opencode_binary=opencode_binary);nodes.append(b)
        try:
            a.start();b.start();a.project_id=a.project('Synthetic A');b.project_id=b.project('Synthetic B')
            unauthorized_project=b.project('Unrelated existing project')
            source=a.source(a.project_id)
            grant_b=b.grant(b.project_id);peer_b=a.peer(a.project_id,b,grant_b)
            fixture.configure('fixture-remote-finance',kind='finance')
            fixture.configure('fixture-parent-outbound',kind='delegate',peer_id=peer_b['id'],source_ids=[source],
                goal='请生成完整数据工作流，保留原子值与全部行。fixture-remote-finance')
            parent=a.say(a.project_id,'请协作调用专业Agent并生成完整数据结果。fixture-parent-outbound',[source])
            tasks=a.call('GET',f'/api/projects/{a.project_id}/agent-tasks')
            tasks=tasks.get('items',[]) if isinstance(tasks,dict) else tasks
            task=next(t for t in tasks if t.get('parent_turn_id')==parent['id'])
            assert task['state']=='succeeded',task.get('error') or task['state']
            imported=next(s for s in b.records('source') if s.get('external_provenance',{}).get('source_id')==source)
            original=b.client.get('/api/sources/'+imported['id']+'/original');original.raise_for_status()
            assert original.content==source_bytes()
            assert imported['content_hash']==hashlib.sha256(source_bytes()).hexdigest()
            # Machine task retrieval uses only the independently issued token.
            with httpx.Client(base_url=b.url,headers={'Authorization':'Bearer '+grant_b['token']},trust_env=False,timeout=30) as machine:
                response=machine.get('/api/agent/v1/tasks/'+task['remote_task_id']);response.raise_for_status();remote=response.json()
                result=verify_output(remote['output_bundle'])
                assert machine.get('/api/projects').status_code in (401,403)
                assert machine.post('/api/agent/v1/tasks',json={'project_id':unauthorized_project,'request_id':'fixture-cross-project','goal':'拒绝未授权项目'}).status_code in (403,404)
            assert b.client.get('/api/agent/v1/tasks/'+task['remote_task_id']).status_code in (401,403)
            checks.update(real_A_to_B=True,complete_40_row_handoff=True,exact_result_and_manifest=True,
                          service_token_not_business_token=True,cross_project_denied=True,source_bytes_exact=True)
            other_grant=b.grant(b.project_id,mode='chat')
            with httpx.Client(base_url=b.url,headers={'Authorization':'Bearer '+other_grant['token']},trust_env=False) as other:
                assert other.get('/api/agent/v1/tasks/'+task['remote_task_id']).status_code in (403,404)
            checks['cross_client_task_denied']=True

            # Reverse direction exercises the MCP Agent adapter, not a callback loop.
            grant_a=a.grant(a.project_id);peer_a=b.peer(b.project_id,a,grant_a,transport='mcp')
            source_b=b.source(b.project_id)
            fixture.configure('fixture-reverse-finance',kind='finance')
            fixture.configure('fixture-parent-reverse',kind='delegate',peer_id=peer_a['id'],source_ids=[source_b],
                goal='请生成完整数据工作流，不截断。fixture-reverse-finance')
            reverse=b.say(b.project_id,'请协作调用另一Agent并生成完整结果。fixture-parent-reverse',[source_b])
            reverse_tasks=b.call('GET',f'/api/projects/{b.project_id}/agent-tasks')
            reverse_tasks=reverse_tasks.get('items',[]) if isinstance(reverse_tasks,dict) else reverse_tasks
            reverse_task=next(t for t in reverse_tasks if t.get('parent_turn_id')==reverse['id'])
            assert reverse_task['state']=='succeeded',reverse_task.get('error') or reverse_task['state']
            with httpx.Client(base_url=a.url,headers={'Authorization':'Bearer '+grant_a['token']},trust_env=False,timeout=30) as machine:
                response=machine.get('/api/agent/v1/tasks/'+reverse_task['remote_task_id']);response.raise_for_status()
                verify_output(response.json()['output_bundle'])
            checks.update(real_B_to_A=True,mcp_agent_adapter=True)

            fixture.configure('fixture-parent-team',kind='team',source_ids=[source])
            team=a.say(a.project_id,'请协作并行核对这份资料，仅作专业检查。fixture-parent-team',[source])
            team_tasks=a.call('GET',f'/api/projects/{a.project_id}/agent-tasks')
            team_tasks=team_tasks.get('items',[]) if isinstance(team_tasks,dict) else team_tasks
            children=[t for t in team_tasks if t.get('parent_turn_id')==team['id']]
            assert len(children)==2 and all(t['state']=='succeeded' for t in children)
            child_turns=[a.call('GET',f'/api/projects/{a.project_id}/turns/{t["child_turn_id"]}') for t in children]
            assert all(t['interaction_mode']=='chat' for t in child_turns)
            assert all(t.get('model_usage',{}).get('calls',0)>=2 for t in child_turns)
            assert fixture.max_children>=2,'Readonly specialists never ran concurrently'
            child_ids={t['id'] for t in child_turns}
            persisted=[t for t in a.records('turn') if t['id'] in child_ids]
            if opencode_binary:assert len({t['opencode_session_id'] for t in persisted})==2
            ledger=next(entry for entry in a.records('collab_budget') if entry['root_turn_id']==team['id'])
            persisted_parent=next(entry for entry in a.records('turn') if entry['id']==team['id'])
            verify_shared_budget(ledger,persisted_parent,persisted)
            assert all(t['model_id']==team['model_id'] for t in persisted)
            checks.update(real_readonly_child_agents=True,parallel_model_calls=True,shared_durable_model_budget=True,
                          child_model_pinned=True,distinct_opencode_child_sessions=bool(opencode_binary))
            # This proof is conditional rather than a failed requirement for the legacy harness.
            if not opencode_binary:checks.pop('distinct_opencode_child_sessions')

            # Duplicate inbound submissions use the same durable task; altered input conflicts.
            fixture.configure('fixture-idempotent',kind='needs_input')
            body={'project_id':b.project_id,'request_id':'fixture-idempotent-request','goal':'需要映射依据。fixture-idempotent','model_id':'finance'}
            with httpx.Client(base_url=b.url,headers={'Authorization':'Bearer '+grant_b['token']},trust_env=False,timeout=30) as machine:
                first=machine.post('/api/agent/v1/tasks',json=body);first.raise_for_status()
                repeated=machine.post('/api/agent/v1/tasks',json=body);repeated.raise_for_status()
                assert first.json()['id']==repeated.json()['id']
                conflict=machine.post('/api/agent/v1/tasks',json=dict(body,goal='更换输入'))
                assert conflict.status_code==409
                clarification=b.wait(lambda:(lambda value:value if value['status']=='input-required' else None)(machine.get('/api/agent/v1/tasks/'+first.json()['id']).json()),'clarification')
                assert clarification['outcome']=='needs_input'
            checks.update(idempotent_submit=True,changed_request_rejected=True,needs_input_not_success=True)

            # Cancel while a real model HTTP request is in flight. A late reply
            # must not change the durable cancellation or execute its tool.
            fixture.configure('fixture-cancel-inflight',kind='slow')
            with httpx.Client(base_url=b.url,headers={'Authorization':'Bearer '+grant_b['token']},trust_env=False,timeout=30) as machine:
                queued=machine.post('/api/agent/v1/tasks',json={'project_id':b.project_id,'request_id':'fixture-cancel-inflight',
                    'goal':'只读检查，等待后续取消。fixture-cancel-inflight','model_id':'finance'})
                queued.raise_for_status();cancel_id=queued.json()['id']
                b.wait(lambda:any('fixture-cancel-inflight' in row['request'] for row in fixture.requests),'inflight model request')
                response=machine.post('/api/agent/v1/tasks/'+cancel_id+'/cancel');response.raise_for_status()
                assert response.json()['status']=='cancelled'
                time.sleep(2.5)
                cancelled=machine.get('/api/agent/v1/tasks/'+cancel_id);cancelled.raise_for_status()
                assert cancelled.json()['status']=='cancelled' and cancelled.json()['outcome']=='cancelled'
                turn=next(t for t in b.records('turn') if t['id']==cancelled.json()['turn_id'])
                assert not turn.get('agent_trace'),'Late model reply executed a tool after cancellation'
            checks['cancel_inflight_late_reply_no_tool']=True

            with ambiguous_proxy(b.url) as (lost,proxy_url):
                uncertain=a.peer(a.project_id,b,grant_b,url=proxy_url+'/a2a')
                fixture.configure('fixture-uncertain-finance',kind='finance')
                fixture.configure('fixture-parent-uncertain',kind='delegate',peer_id=uncertain['id'],source_ids=[source],
                    goal='请生成完整工作流。fixture-uncertain-finance')
                # Parent is expected to stop with a truthful unresolved outcome.
                uncertain_parent=a.say(a.project_id,'请协作处理并生成完整结果。fixture-parent-uncertain',[source])
                def unknown():
                    entries=a.call('GET',f'/api/projects/{a.project_id}/agent-tasks')
                    entries=entries.get('items',[]) if isinstance(entries,dict) else entries
                    return next((t for t in entries if t.get('parent_turn_id')==uncertain_parent['id'] and t['state']=='unknown'),None)
                a.wait(unknown,'ambiguous submit')
                assert lost['submits']==1 and lost['accepted_task_id']
                a.stop();a.start();time.sleep(2)
                assert lost['submits']==1 and unknown()
                restored=next(t for t in a.call('GET',f'/api/projects/{a.project_id}/agent-tasks')['items'] if t['id']==task['id'])
                assert restored['state']=='succeeded' and restored['evidence']==task['evidence']
                checks.update(ambiguous_submit_no_replay=True,unknown_survives_restart=True,completed_result_receipt_survives_restart=True)

            b.operator('DELETE','/api/operator/agent-clients/'+grant_b['client']['id'],params={'expected_revision':grant_b['client']['revision']})
            with httpx.Client(base_url=b.url,headers={'Authorization':'Bearer '+grant_b['token']},trust_env=False) as machine:
                assert machine.get('/api/agent/v1/tasks/'+task['remote_task_id']).status_code in (401,403)
            checks['revoked_client_denied']=True
            payload='\n'.join(fixture.raw_requests)
            for secret in (a.token,b.token,grant_a['token'],grant_b['token'],other_grant['token'],FIXTURE_OPERATOR_TOKEN):assert secret not in payload
            assert grant_a['token'].encode() not in a.data.joinpath('metadata.sqlite').read_bytes()
            assert grant_b['token'].encode() not in b.data.joinpath('metadata.sqlite').read_bytes()
            checks['credentials_not_in_model_or_plain_db']=True
            return {'ok':True,'version':'0.20.0','checks':checks,'rows':ROWS,'exact_total':str(TOTAL),
                'result_receipts':result,'instances':2,'real_api_agent_worker':True,
                'opencode_runtime_executed':bool(opencode_binary),'opencode_version':native_version,
                'native_tool_loop_calls':base_fixture.native_loop_calls,'native_compaction_calls':base_fixture.native_compaction_calls,
                'model_requests':len(fixture.requests),
                'max_parallel_specialist_requests':fixture.max_children,'simulated_model':True,'real_model_evaluated':False,
                'docker_executed':False,'production_ha_evaluated':False,'offline_evaluated':False}
        finally:
            for node in reversed(nodes):node.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-dir',type=Path,required=True)
    parser.add_argument('--opencode-binary',type=Path)
    options=parser.parse_args(argv)
    folder=options.evidence_dir.resolve()
    if folder.exists() and any(folder.iterdir()):parser.error('--evidence-dir must be empty')
    folder.mkdir(parents=True,exist_ok=True);folder.chmod(0o700)
    try:
        result=verify(folder,opencode_binary=options.opencode_binary.resolve(strict=True) if options.opencode_binary else None)
        (folder/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(result,ensure_ascii=False));return 0
    except Exception as error:
        frame=traceback.extract_tb(error.__traceback__)[-1]
        diagnostic={'ok':False,'simulated_model':True,'error_type':type(error).__name__,
                    'location':{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}}
        (folder/'verification.failure.json').write_text(json.dumps(diagnostic,indent=2)+'\n')
        print('Agent collaboration integration failed: '+type(error).__name__,file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
