"""Actual OpenCode + local simulated 400-context-overflow recovery.

Use --lazy-office to verify late capability/spec discovery and a real DOCX.
No external LLM, real account, billing or business files. Requires the pinned
ATLAS_OPENCODE_BINARY. Creates an isolated temporary state directory.
"""
import json,os,sys,tempfile,subprocess,time,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

repo=Path(__file__).resolve().parents[1];sys.path.insert(0,str(repo))
root=Path(tempfile.mkdtemp(prefix='atlas-context-overflow-'))
os.environ.update(DATA_DIR=str(root/'data'),DATABASE_URL='sqlite:///'+str(root/'data/metadata.sqlite'),
    APP_TOKEN='context-overflow-fixture-only-token',ATLAS_OPENCODE_RUNTIME='1',API_ALLOWED_HOSTS='127.0.0.1',
    ATLAS_DESKTOP_MODE='0',ATLAS_AUTH_MODE='token')
from agent_service import agent,provider,opencode_runtime as runtime,model_catalog,conversation,workspace
from agent_service.store import get,put,now
calls=[];turn_id='';process=None;lazy_office='--lazy-office' in sys.argv
from agent_service import office,workspace_artifacts
office_spec={'kind':'document','title':'Weekly progress','sections':[{'body':'Actual controlled file, not a business claim.'}]}
lazy_steps=[{'kind':'tool','tool':'discover_tools','arguments':{'query':'render_office_artifact','limit':1}},
    {'kind':'tool','tool':'write_file','arguments':{'path':'specs/weekly.json','content':json.dumps(office_spec)}},
    {'kind':'tool','tool':'render_office_artifact','arguments':{'spec_path':'specs/weekly.json','name':'Weekly progress'}},
    {'kind':'final','answer':'进展小结已生成，可在本条对话查看和下载。','status':'complete'}]
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path!='/v1/chat/completions' or self.headers.get('Authorization')!='Bearer local-fixture':
            self.send_response(401);self.end_headers();return
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        current=get('turn',turn_id)
        written=any(item.get('tool')=='write_file' and item.get('status')=='done' for item in current.get('agent_trace',[]))
        recovered=current.get('context_window',{}).get('recoveries',0)>0
        calls.append({'input_characters':len(json.dumps(body,ensure_ascii=False)),'written':written,'recovered':recovered})
        if not lazy_office and written and not recovered:
            raw=json.dumps({'error':{'code':'context_length_exceeded','message':'maximum context length exceeded (controlled local fixture)'}}).encode()
            self.send_response(400);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
        step={'kind':'final','answer':'报告已生成，原有写入没有重复。','status':'complete'} if written else {
            'kind':'tool','tool':'write_file','arguments':{'path':'reports/native.md','content':'preserved exact report 9007199254740993.000000000001'}}
        if lazy_office:
            position=len([item for item in current.get('agent_trace',[]) if item.get('status')=='done'])
            actual_schema='office_spec_schema' in json.dumps(body,ensure_ascii=False)
            assert actual_schema==(position>0),'Office DTO must load only after explicit discovery'
            calls[-1]['office_spec_loaded']=actual_schema
            step=lazy_steps[min(position,len(lazy_steps)-1)]
        message={'role':'assistant','tool_calls':[{'id':'fixture_call_'+str(len(calls)),'type':'function',
            'function':{'name':'atlas_step','arguments':json.dumps(step,ensure_ascii=False)}}]}
        payload={'id':'fixture','object':'chat.completion','model':'context-fixture','choices':[{'index':0,'message':message,'finish_reason':'tool_calls'}],
            'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}}
        if body.get('stream'):
            chunks=[{'id':'fixture','object':'chat.completion.chunk','created':int(time.time()),'model':'context-fixture',
                'choices':[{'index':0,'delta':{'role':'assistant','tool_calls':[dict(message['tool_calls'][0],index=0)]},'finish_reason':None}]},
                {'id':'fixture','object':'chat.completion.chunk','created':int(time.time()),'model':'context-fixture',
                'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}],'usage':payload['usage']}]
            raw=(''.join('data: '+json.dumps(chunk,ensure_ascii=False)+'\n\n' for chunk in chunks)+'data: [DONE]\n\n').encode();kind='text/event-stream'
        else:raw=json.dumps(payload,ensure_ascii=False).encode();kind='application/json'
        self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def log_message(self,*args):pass
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
log=(root/'native.log').open('w')
try:
    selected={'id':'fixture','name':'Local fixture','base_url':f'http://127.0.0.1:{server.server_port}/v1',
        'model':'context-fixture','api_key':'local-fixture','share_data':True,'engine':'atlas','tool_protocol':'json'}
    model_catalog.provision({'models':[selected],'default_model_id':'fixture'},verify=False)
    spec=runtime.prepare({'models':[selected],'default_model_id':'fixture'},agent.Step.model_json_schema())
    process=subprocess.Popen(spec['command'],env=spec['env'],cwd=spec['cwd'],stdout=log,stderr=log);runtime.wait_ready(30)
    pid=conversation.create_project(conversation.ProjectInput(name='Controlled context recovery',request_id='fixture-project'))['id']
    session=conversation.default_session(pid)
    for i in range(10):put('turn',{'project_id':pid,'session_id':session,'state':'done','text':'Historical source '+('synthetic note '*650),
        'answer':'Saved historical response '+str(i),'created_at':'2025-01-%02d'%(i+1)},'historical_'+str(i))
    goal='帮我规划一份项目本周进展小结，由你选择交付方式。' if lazy_office else '请创建报告文件 reports/native.md，不要发邮件，不要发布。'
    current=conversation.submit(pid,conversation.TurnInput(text=goal,request_id='fixture-write-report',session_id=session))
    turn_id=current['id']
    from agent_service.store import patch
    current=patch('turn',turn_id,state='agent')
    agent.advance(current)
    final=get('turn',turn_id)
    assert final['state']=='done' and final['outcome'] in ('verified_complete','answered'),{'state':final['state'],'answer':final.get('answer')}
    writes=[item for item in final['agent_trace'] if item['tool']=='write_file']
    assert len(writes)==1
    if lazy_office:
        outputs=conversation.public_turn(final)['outputs']
        assert len(outputs)==1 and outputs[0]['format']=='docx'
        file,metadata=workspace_artifacts.file(pid,outputs[0]['id'])
        import hashlib
        assert hashlib.sha256(file.read_bytes()).hexdigest()==outputs[0]['sha256']
        assert not calls[0]['office_spec_loaded'] and all(call['office_spec_loaded'] for call in calls[1:])
        print(json.dumps({'ok':True,'actual_opencode_binary':True,'version':runtime.VERSION,'local_simulated_provider':True,
            'office_spec_absent_from_bootstrap':True,'office_spec_loaded_after_discovery':True,'actual_docx_generated':True,
            'inline_output_metadata':True,'file_sha256_verified':True,'write_count':len(writes),'calls':calls,'evidence_dir':str(root)},ensure_ascii=False))
    else:
        assert workspace.read_file(pid,'reports/native.md')=='preserved exact report 9007199254740993.000000000001'
        assert final['context_window']['recoveries']>=1
        old=final['opencode_retired_sessions'][-1]
        original=runtime._request(runtime.connection(),'GET','/api/session/'+old+'/message?limit=100&order=asc')
        assert 'Historical source' in json.dumps(original)
        print(json.dumps({'ok':True,'actual_opencode_binary':True,'version':runtime.VERSION,'local_simulated_provider':True,
            'real_provider_context_400_observed':any(v['written'] and not v['recovered'] for v in calls),
            'automatic_retry_without_user_continue':True,'originals_retained':True,'write_count':len(writes),
            'context_recoveries':final['context_window']['recoveries'],'state':final['state'],'calls':calls,'evidence_dir':str(root)},ensure_ascii=False))
except Exception:
    if turn_id:
        final=get('turn',turn_id);(root/'failed-turn.json').write_text(json.dumps(final,ensure_ascii=False,default=str,indent=2))
        if final.get('opencode_session_id'):
            try:(root/'failed-native-context.json').write_text(json.dumps(runtime._request(runtime.connection(),'GET','/api/session/'+final['opencode_session_id']+'/context'),ensure_ascii=False,default=str,indent=2))
            except Exception:pass
    print('EVIDENCE_DIR='+str(root),flush=True);raise
finally:
    if process:
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait()
    log.close();server.shutdown();server.server_close();thread.join(timeout=2)
