"""Actual legacy Agent, Git workspace and HTTP fixture for model recovery.

Runs with an isolated temporary data directory and a local simulated model.
No external model, business content, authentication credentials or billing.
"""
import json,os,shutil,sys,tempfile,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

repo=Path(__file__).resolve().parents[1];sys.path.insert(0,str(repo))
git=shutil.which('git')
if not git:raise RuntimeError('Git is required for this isolated workspace validation')
root=Path(tempfile.mkdtemp(prefix='atlas-legacy-model-'))
os.environ.update(DATA_DIR=str(root/'data'),DATABASE_URL='sqlite:///'+str(root/'data/metadata.sqlite'),
    APP_TOKEN='local-synthetic-access-token-only',ATLAS_DESKTOP_MODE='1',ATLAS_OPENCODE_RUNTIME='0',
    ATLAS_AUTH_MODE='token',API_ALLOWED_HOSTS='127.0.0.1',ATLAS_GIT_BINARY=git)
from agent_service import agent,conversation,model_catalog,workspace
from agent_service.store import get,patch

calls=[];mode='work';json_steps=0;rate_count=0
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global json_steps,rate_count
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        native='tools' in body;calls.append({'mode':mode,'native_tools':native})
        status=200
        if mode=='auth':
            status=401;payload={'error':{'message':'synthetic-sensitive-provider-detail'}}
        elif mode=='rate' and rate_count==0:
            rate_count+=1;status=429;payload={'error':{'message':'temporary provider rate limit'}}
        elif mode=='work' and native:
            status=400;payload={'error':{'code':'unsupported_parameter','param':'tools','message':'This model does not support tools'}}
        else:
            if mode=='work':
                json_steps+=1
                step=({'kind':'tool','tool':'write_file','arguments':{'path':'reports/live.md','content':'actual controlled report'}}
                      if json_steps==1 else {'kind':'final','answer':'报告文件已保存。','status':'complete'})
                message={'content':json.dumps(step,ensure_ascii=False)}
            else:
                message={'content':'','tool_calls':[{'type':'function','id':'finish',
                    'function':{'name':'finish_task','arguments':json.dumps({'answer':'继续正常回答。','status':'complete'},ensure_ascii=False)}}]}
            payload={'choices':[{'message':message,'finish_reason':'stop'}],
                     'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}}
        data=json.dumps(payload,ensure_ascii=False).encode();self.send_response(status)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)))
        self.end_headers();self.wfile.write(data)
    def log_message(self,*args):pass
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
try:
    entry={'id':'fixture','name':'Fixture','engine':'atlas','base_url':f'http://127.0.0.1:{server.server_port}/v1',
        'model':'synthetic','api_key':'fixture-only','share_data':True,'tool_protocol':'native'}
    model_catalog.provision({'default_model_id':'fixture','models':[entry]},verify=False)
    pid=conversation.create_project(conversation.ProjectInput(name='Legacy model fixture',request_id='legacy-fixture-project'))['id']
    work=conversation.submit(pid,conversation.TurnInput(text='创建报告文件 reports/live.md',request_id='legacy-fixture-work',interaction_mode='work'))
    patch('turn',work['id'],state='agent');agent.advance(get('turn',work['id']));agent.advance(get('turn',work['id']))
    result=get('turn',work['id'])
    assert result['state']=='done' and result['outcome']=='verified_complete',{'state':result['state'],'answer':result.get('answer')}
    assert workspace.read_file(pid,'reports/live.md')=='actual controlled report'
    assert len([step for step in result['agent_trace'] if step['tool']=='write_file' and step['status']=='done'])==1
    mode='auth';before=len(calls)
    auth=conversation.submit(pid,conversation.TurnInput(text='你好',request_id='legacy-fixture-auth',interaction_mode='chat'))
    patch('turn',auth['id'],state='agent');agent.advance(get('turn',auth['id']))
    failed=get('turn',auth['id']);assert len(calls)-before==1 and failed['state']=='failed' and failed['resume_available']
    assert '密钥' in failed['answer'] and 'synthetic-sensitive' not in failed['answer']
    mode='rate';before=len(calls)
    rate=conversation.submit(pid,conversation.TurnInput(text='你好',request_id='legacy-fixture-rate',interaction_mode='chat'))
    patch('turn',rate['id'],state='agent');agent.advance(get('turn',rate['id']))
    waiting=get('turn',rate['id']);assert waiting['state']=='agent' and waiting.get('model_retry_count')==1
    patch('turn',rate['id'],model_retry_at=0);agent.advance(get('turn',rate['id']))
    answered=get('turn',rate['id']);assert answered['state']=='done' and answered['outcome']=='answered' and len(calls)-before==2
    print(json.dumps({'actual_legacy_agent':True,'actual_model_http':True,'model_is_local_fixture':True,
        'unsupported_native_automatically_falls_back':True,'actual_git_workspace_file_written_once':True,
        'auth_401_reports_real_issue_without_sensitive_body':True,'rate_429_resumes_original_task':True,
        'state':'passed','calls':calls,'evidence_dir':str(root)},ensure_ascii=False))
finally:server.shutdown();server.server_close();thread.join(timeout=2)
