"""Real HTTP service/restart test with simulated company API and model.

This validates integration and persistence, not real company access or intelligence.
All credentials and data are disposable and confined to a temporary directory.
"""
import json,os,secrets,socket,subprocess,sys,tempfile,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import httpx
ROOT=Path(__file__).resolve().parents[1]
TEXT='协议专用内部制度：员工差旅报销必须提交审批单。'

class Upstream(BaseHTTPRequestHandler):
    code=200
    model_inputs=[]
    def send(self,body,code=200):
        self.send_response(code);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(body,ensure_ascii=False).encode())
    def do_GET(self):
        if self.headers.get('Authorization')!='Bearer disposable-company-key':return self.send({},403)
        if self.code!=200:return self.send({'error':'private-upstream-error'},self.code)
        doc={'id':'doc1','title':'差旅管理办法','content':TEXT,'snippet':'差旅报销审批要求','published_at':'2026-10-01','status':'有效','version':'1'}
        self.send(doc if self.path.startswith('/document/') else {'results':[doc],'total':1})
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        messages=body['messages']
        if len(messages)==1:content='OK'
        else:
            info=json.loads(messages[1]['content']);Upstream.model_inputs.append(info)
            assert 'disposable-company-key' not in json.dumps(info)
            n=len(info['completed_steps'])
            if n==0:step={'kind':'tool','tool':'search_company_documents','arguments':{'query':'差旅报销'}}
            elif n==1:step={'kind':'tool','tool':'read_company_document','arguments':{'evidence_id':info['company_candidates'][0]['evidence_id']}}
            elif n==2 and info['mode']=='work':step={'kind':'tool','tool':'write_file','arguments':{'path':'reports/policy.md','content':'# 差旅依据\n'+TEXT+'\n'}}
            else:step={'kind':'final','answer':'公司规定报销需要审批单。'+('依据报告已保存。' if info['mode']=='work' else ''),'citations':[{'evidence_id':info['company_evidence'][0]['evidence_id'],'claim':'公司规定报销需要审批单','quote':'员工差旅报销必须提交审批单。'}]}
            content=json.dumps(step,ensure_ascii=False)
        self.send({'choices':[{'message':{'content':content}}]})
    def log_message(self,*args):pass

def main():
    server=ThreadingHTTPServer(('127.0.0.1',0),Upstream);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='atlas-company-protocol-') as tmp:
            tmp=Path(tmp)
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            env=dict(os.environ,DATA_DIR=str(tmp/'data'),DATABASE_URL='sqlite:///'+str(tmp/'data/metadata.sqlite'),APP_TOKEN=secrets.token_urlsafe(32),PORT=str(port),PYTHONPATH=str(ROOT),LLM_BASE_URL='',LLM_MODEL='',API_ALLOWED_HOSTS='127.0.0.1',ATLAS_CLOUD_VERIFY='0')
            process=None
            with (tmp/'service.log').open('w') as log:
                try:
                    with httpx.Client(base_url=f'http://127.0.0.1:{port}',headers={'Authorization':'Bearer '+env['APP_TOKEN']},trust_env=False,timeout=20) as client:
                        def request(method,path,**kwargs):
                            result=client.request(method,path,**kwargs);result.raise_for_status();return result.json()
                        def wait(check):
                            deadline=time.monotonic()+90
                            while time.monotonic()<deadline:
                                if process.poll() is not None:raise RuntimeError('service exited')
                                try:
                                    result=check()
                                    if result:return result
                                except (httpx.ConnectError,httpx.ReadError):pass
                                time.sleep(.2)
                            raise TimeoutError('company protocol acceptance timed out')
                        def start():return subprocess.Popen([sys.executable,'-m','agent_service.cloud'],cwd=ROOT,env=env,stdout=log,stderr=log)
                        process=start();wait(lambda:request('GET','/health'))
                        request('PUT','/api/model',json={'base_url':f'http://127.0.0.1:{server.server_port}/v1','model':'simulated-company-model','api_key':'disposable-model-key','share_data':True})
                        project=request('POST','/api/projects',json={'name':'内部检索协议验收','request_id':'company-protocol-project'});path='/api/projects/'+project['id']
                        config={'search_url':f'http://127.0.0.1:{server.server_port}/search','detail_url':f'http://127.0.0.1:{server.server_port}/document/{{id}}','headers':{'Authorization':'Bearer disposable-company-key'},'principal_label':'disposable-employee','user_scoped_ack':True,'storage_approved':True,'model_approved':False}
                        request('PUT',path+'/company-search',json=config)
                        def say(text,key,mode):
                            value=request('POST',path+'/messages',json={'text':text,'request_id':key,'interaction_mode':mode})
                            def done():
                                turn=next(t for t in request('GET',path+'/conversation')['turns'] if t['id']==value['id'])
                                if turn['state']=='failed':raise AssertionError(turn.get('answer'))
                                return turn if turn['state']=='done' and (mode=='chat' or turn.get('work_version')) else None
                            return wait(done)
                        blocked=say('根据公司发文解释差旅报销','unapproved','chat')
                        assert not Upstream.model_inputs and '批准' in blocked['answer']
                        config['model_approved']=True;request('PUT',path+'/company-search',json=config)
                        chat=say('根据公司发文解释差旅报销','approved-chat','chat')
                        assert chat['outcome']=='answered' and chat['knowledge_sources'] and not chat.get('work_version')
                        work=say('根据公司发文写一份报告并说明依据','approved-work','work')
                        assert work['outcome']=='verified_complete' and work['work_version']['changed']
                        eid=work['knowledge_sources'][0]['evidence_id']
                        process.terminate();assert process.wait(timeout=15)==0
                        process=start();wait(lambda:request('GET','/health'))
                        settings=request('GET',path+'/company-search')
                        assert settings['status']['model_allowed'] and settings['configuration']['headers']=={}
                        assert request('GET',path+'/company-search/evidence/'+eid)['content']==TEXT
                        assert TEXT not in (tmp/'data/metadata.sqlite').read_bytes().decode(errors='ignore')
                        Upstream.code=403
                        denied=client.get(path+'/company-search/evidence/'+eid)
                        assert denied.status_code==400 and 'private-upstream-error' not in denied.text
                        hidden=next(t for t in request('GET',path+'/conversation')['turns'] if t['id']==work['id'])
                        assert '已隐藏' in hidden['answer']
                        assert request('GET','/api/audit/verify')['ok']
                        print(json.dumps({'ok':True,'simulated_company_api':True,'simulated_model':True,'real_service_processes':True,'separate_model_approval':True,'search_read_citations':True,'report_versioned':True,'restart_credentials_and_evidence':True,'encrypted_conversations':True,'permission_denial_hides_answer':True}))
                except Exception:
                    print((tmp/'service.log').read_text()[-2500:]);raise
                finally:
                    if process and process.poll() is None:process.terminate();process.wait(timeout=15)
    finally:server.shutdown();server.server_close();thread.join(timeout=2)

if __name__=='__main__':main()
