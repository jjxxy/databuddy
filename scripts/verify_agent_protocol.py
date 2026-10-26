"""Real HTTP/API/worker protocol acceptance with an explicitly simulated model.

This checks integration, tool loops and persistence, not a real model's intelligence.
No credential or connection is retained outside the isolated temporary directory.
"""
import argparse,importlib.util,json,os,re,secrets,socket,subprocess,sys,tempfile,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import httpx
ROOT=Path(__file__).resolve().parents[1]

class Model(BaseHTTPRequestHandler):
    transient_sent=False
    native=False
    opencode=False
    native_calls=0
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        messages=body['messages']
        if Model.opencode:
            raw=messages[-1]['content']
            if isinstance(raw,list):raw=''.join(x.get('text','') for x in raw)
            messages=json.loads(raw[raw.index('\n[')+1:])
        if len(messages)==1:content='OK'
        else:
            info=json.loads(messages[1]['content']);step=len(info['completed_steps'])
            evaluation=bool(re.search(r'reports/check\.md|不要修改或删除任何文件',info['request']))
            if info['mode']=='work' and step==1 and not Model.transient_sent:
                Model.transient_sent=True;self.send_response(503);self.end_headers();return
            if evaluation:
                if '内容逐字为' in info['request'] and step==0:decision={'kind':'tool','tool':'write_file','arguments':{'path':'reports/check.md','content':'评测标记 ATLAS-VERIFIED-731。'}}
                else:decision={'kind':'final','answer':'文件已按要求保存。' if step else '本轮保持项目文件不变。'}
                content=json.dumps(decision,ensure_ascii=False)
            elif info['mode']=='work' and Model.native:
                if step==4:
                    checksum=info['completed_steps'][-1]['observation']['sha256']
                    current={'kind':'tool','tool':'edit_file','arguments':{'path':'reports/plan.md','expected_sha256':checksum,'old_text':'只使用已付款订单。','new_text':'只统计已付款订单，并保留历史。'}}
                else:
                    sequence=[{'kind':'tool','tool':'discover_tools','arguments':{'query':'write_file read_file edit_file search_files'}},{'kind':'tool','tool':'write_file','arguments':{'path':'reports/plan.md','content':'# 验收计划\n只使用已付款订单。\n'}},{'kind':'tool','tool':'search_files','arguments':{'query':'已付款','path_glob':'reports/*.md'}},{'kind':'tool','tool':'read_file','arguments':{'path':'reports/plan.md'}},None,{'kind':'tool','tool':'read_file','arguments':{'path':'reports/plan.md'}},{'kind':'final','answer':'报告已生成、搜索并精确修改，约定只统计已付款订单。'}]
                    current=sequence[min(step,len(sequence)-1)]
                content=json.dumps(current,ensure_ascii=False)
            elif info['mode']=='work':
                steps=[{'kind':'tool','tool':'project_context'}, {'kind':'tool','tool':'write_file','arguments':{'path':'reports/plan.md','content':'# 验收计划\n只使用已付款订单。\n'}}, {'kind':'tool','tool':'read_file','arguments':{'path':'reports/plan.md'}}, {'kind':'final','answer':'报告已生成并检查，约定只使用已付款订单。'}]
                content=json.dumps(steps[min(step,len(steps)-1)],ensure_ascii=False)
            else:
                steps=[{'kind':'tool','tool':'search_project_history','arguments':{'query':'已付款'}},{'kind':'final','answer':'本项目之前约定只使用已付款订单。'}]
                content=json.dumps(steps[min(step,len(steps)-1)],ensure_ascii=False)
        message={'content':content}
        if body.get('tools'):
            Model.native_calls+=1
            if len(messages)==1:name='connection_check';arguments={'ok':True}
            else:
                decision=json.loads(content);name='finish_task' if decision['kind']=='final' else decision['tool'];arguments={k:v for k,v in decision.items() if k in ('answer','status','citations')} if name=='finish_task' else decision.get('arguments',{})
            assert name in {x['function']['name'] for x in body['tools']}
            message={'content':None,'tool_calls':[{'id':'protocol-call','type':'function','function':{'name':name,'arguments':json.dumps(arguments,ensure_ascii=False)}}]}
        encoded=json.dumps({'id':'simulated-only','object':'chat.completion','model':body.get('model'),'choices':[{'index':0,'message':message,'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}},ensure_ascii=False).encode()
        if Model.opencode and body.get('stream'):
            chunk={'id':'simulated-only','object':'chat.completion.chunk','model':body.get('model'),'choices':[{'index':0,'delta':{'role':'assistant','content':content},'finish_reason':None}]}
            end={'id':'simulated-only','object':'chat.completion.chunk','choices':[{'index':0,'delta':{},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}}
            encoded=('data: '+json.dumps(chunk,ensure_ascii=False)+'\n\ndata: '+json.dumps(end)+'\n\ndata: [DONE]\n\n').encode()
        self.send_response(200);self.send_header('Content-Type','text/event-stream' if Model.opencode and body.get('stream') else 'application/json');self.end_headers();self.wfile.write(encoded)
    def log_message(self,*args):pass

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--opencode-binary',type=Path);parser.add_argument('--native',action='store_true');parser.add_argument('--check-evaluator',action='store_true');args=parser.parse_args();Model.native=args.native;Model.opencode=bool(args.opencode_binary)
    if Model.opencode and args.native:parser.error('V2 decision backend is JSON validated, not V1 native tools')
    model=ThreadingHTTPServer(('127.0.0.1',0),Model);thread=threading.Thread(target=model.serve_forever,daemon=True);thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='atlas-agent-protocol-') as temp:
            temp=Path(temp)
            oc_process=None
            oc_config={}
            if args.opencode_binary:
                spec=importlib.util.spec_from_file_location('oc_bootstrap',ROOT/'deploy/opencode_bootstrap.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
                oc_dir=temp/'opencode';oc_dir.mkdir();upstream=f'http://127.0.0.1:{model.server_port}/v1'
                (oc_dir/'config/opencode').mkdir(parents=True);(oc_dir/'config/opencode/opencode.json').write_text(json.dumps(module.config(upstream)))
                with socket.socket() as sock:sock.bind(('127.0.0.1',0));oc_port=sock.getsockname()[1]
                password=secrets.token_urlsafe(32)
                oc_env={k:v for k,v in os.environ.items() if k not in ('APP_TOKEN','DATABASE_URL','LLM_API_KEY')}
                oc_env.update(XDG_DATA_HOME=str(oc_dir/'data'),XDG_CONFIG_HOME=str(oc_dir/'config'),OPENCODE_SERVER_PASSWORD=password,ATLAS_MODEL_KEY='simulated-only',OPENCODE_DISABLE_MODELS_FETCH='true')
                oc_log=(temp/'opencode.log').open('w');oc_process=subprocess.Popen([str(args.opencode_binary.resolve()),'serve','--hostname','127.0.0.1','--port',str(oc_port)],cwd=oc_dir,env=oc_env,stdout=oc_log,stderr=oc_log)
                oc_config={'engine':'opencode','base_url':f'http://127.0.0.1:{oc_port}','model':'atlas/deepseek-v4-pro','api_key':password,'upstream_base_url':upstream,'share_data':True,'tool_protocol':'json'}
                with httpx.Client(timeout=1,trust_env=False,auth=('opencode',password)) as check:
                    for _ in range(80):
                        try:
                            r=check.get(oc_config['base_url']+'/api/info')
                            if r.status_code==200:assert r.json()['version']=='2.0.32';break
                        except httpx.TransportError:pass
                        time.sleep(.25)
                    else:raise RuntimeError('OpenCode startup failed')
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            env=dict(os.environ,DATA_DIR=str(temp/'data'),DATABASE_URL='sqlite:///'+str(temp/'data/metadata.sqlite'),APP_TOKEN=secrets.token_urlsafe(32),PORT=str(port),PYTHONPATH=str(ROOT),LLM_BASE_URL='',LLM_MODEL='',API_ALLOWED_HOSTS='127.0.0.1',ATLAS_CLOUD_VERIFY='0')
            with (temp/'service.log').open('w') as log:
                process=None
                try:
                    with httpx.Client(base_url=f'http://127.0.0.1:{port}',headers={'Authorization':'Bearer '+env['APP_TOKEN']},trust_env=False,timeout=15) as client:
                        def request(method,path,**kwargs):
                            response=client.request(method,path,**kwargs);response.raise_for_status();return response.json()
                        def wait(check):
                            until=time.monotonic()+90
                            while time.monotonic()<until:
                                if process.poll() is not None:raise RuntimeError('service exited')
                                try:
                                    value=check()
                                    if value:return value
                                except (httpx.ConnectError,httpx.ReadError):pass
                                time.sleep(.2)
                            raise TimeoutError('agent protocol acceptance timed out')
                        def start():return subprocess.Popen([sys.executable,'-m','agent_service.cloud'],cwd=ROOT,env=env,stdout=log,stderr=log)
                        process=start();wait(lambda:request('GET','/health'))
                        configured=request('PUT','/api/model',json={'base_url':f'http://127.0.0.1:{model.server_port}/v1','model':'simulated-protocol-model','api_key':'disposable-protocol-key','share_data':True,'tool_protocol':'native' if args.native else 'json',**oc_config})
                        assert configured['configured'] and 'api_key' not in configured
                        project=request('POST','/api/projects',json={'name':'协议验收','request_id':'protocol-project-1'});path='/api/projects/'+project['id']
                        initial=request('GET',path+'/versions')[0]['commit']
                        def say(text,key,mode='work',session=None):
                            value=request('POST',path+'/messages',json={'text':text,'request_id':key,'interaction_mode':mode,'session_id':session})
                            def done():
                                data=request('GET',path+'/conversation'+('?session_id='+session if session else ''))
                                turn=next(t for t in data['turns'] if t['id']==value['id'])
                                if turn['state']=='failed':raise AssertionError(turn.get('answer'))
                                return turn if turn['state']=='done' and (mode=='chat' or turn.get('work_version')) else None
                            return wait(done)
                        work=say('写一份项目报告','protocol-write-1')
                        assert work['work_version']['changed'] and len(work['agent_trace'])==(6 if args.native else 3)
                        file=client.get(path+'/file',params={'path':'reports/plan.md'});assert '已付款' in file.text
                        process.terminate();assert process.wait(timeout=15)==0
                        process=start();wait(lambda:request('GET','/health'))
                        assert request('GET','/api/model')['configured']
                        session=request('POST',path+'/sessions',json={'name':'另一段对话','request_id':'protocol-session-1'})
                        chat=say('之前约定什么口径','protocol-chat-1','chat',session['id'])
                        assert not chat.get('work_version') and chat['agent_trace'][0]['observation']['matches']
                        restored=say('回退到 '+initial,'protocol-restore-1','work',session['id'])
                        assert restored['work_version']['changed']
                        assert client.get(path+'/file',params={'path':'reports/plan.md'}).status_code==404
                        assert request('GET','/api/audit/verify')['ok']
                        assert Model.transient_sent
                        if args.check_evaluator:
                            report_file=temp/'evaluator-protocol.json'
                            evaluation=subprocess.run([sys.executable,str(ROOT/'scripts/evaluate_agent.py'),'--execute','--protocol-test','--limit','3','--output',str(report_file)],env=dict(env,ATLAS_URL=f'http://127.0.0.1:{port}'),capture_output=True,text=True,timeout=120)
                            assert evaluation.returncode==0,evaluation.stdout[-2000:]+evaluation.stderr[-1000:]
                            result=json.loads(report_file.read_text());assert result['passed']==3 and result['real_model_evaluated'] is False and result['success_rate'] is None
                        print(json.dumps({'ok':True,'simulated_model':True,'real_api_worker':True,'real_opencode_binary':bool(args.opencode_binary),'opencode_version':'2.0.32' if args.opencode_binary else None,'model_steps':6 if args.native else 3,'native_tool_calling':bool(args.native and Model.native_calls),'tool_discovery_search_exact_edit':bool(args.native),'evaluation_runner_protocol_test':args.check_evaluator,'transient_model_retry':True,'credentials_survive_restart':True,'cross_session_retrieval':True,'files_versioned_and_restored':True}))
                except Exception:
                    print((temp/'service.log').read_text()[-4000:]);raise
                finally:
                    if process and process.poll() is None:process.terminate();process.wait(timeout=15)
                    if oc_process and oc_process.poll() is None:oc_process.terminate();oc_process.wait(timeout=15)
                    if args.opencode_binary:oc_log.close()
    finally:model.shutdown();model.server_close();thread.join(timeout=2)

if __name__=='__main__':main()
