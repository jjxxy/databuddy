from decimal import Decimal
"""Run an isolated, real API + worker acceptance check without touching deployment data."""
import argparse,json,os,secrets,signal,socket,subprocess,sys,tempfile,time
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--cloud',action='store_true');parser.add_argument('--drop-privileges',action='store_true');options=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='atlas-service-check-') as directory:
        directory=Path(directory)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        token=secrets.token_urlsafe(32)
        env=dict(os.environ,DATA_DIR=str(directory/'data'),DATABASE_URL='sqlite:///'+str(directory/'data/metadata.sqlite'),APP_TOKEN=token,PYTHONPATH=str(ROOT),SCHEDULER_MODE='worker',LLM_BASE_URL='',LLM_MODEL='',DO_NOT_TRACK='1',DLT_TELEMETRY='false')
        if options.cloud:env.update(PORT=str(port),RAILWAY_ENVIRONMENT_ID='acceptance-only',RAILWAY_VOLUME_MOUNT_PATH=str(directory/'data'))
        if options.drop_privileges:
            if not options.cloud or not hasattr(os,'geteuid') or os.geteuid()!=0:raise ValueError('--drop-privileges requires Linux root and --cloud')
            directory.chmod(0o755);env['ATLAS_DROP_PRIVILEGES']='1'
        processes=[]
        with (directory/'api.log').open('w') as api_log,(directory/'worker.log').open('w') as worker_log:
            try:
                command=[sys.executable,'-m','agent_service.cloud'] if options.cloud else [sys.executable,'-m','uvicorn','agent_service.api:app','--host','127.0.0.1','--port',str(port)]
                processes.append(subprocess.Popen(command,cwd=ROOT,env=env,stdout=api_log,stderr=api_log))
                # API imports initialize metadata before the worker starts.
                with httpx.Client(base_url=f'http://127.0.0.1:{port}',headers={'Authorization':'Bearer '+token},trust_env=False,timeout=10) as client:
                    def wait(check,seconds=90):
                        end=time.monotonic()+seconds
                        while time.monotonic()<end:
                            if any(p.poll() is not None for p in processes):raise RuntimeError('service exited during acceptance check')
                            try:
                                value=check()
                                if value:return value
                            except (httpx.ConnectError,httpx.ReadError):pass
                            time.sleep(.25)
                        raise TimeoutError('acceptance check timed out')
                    def request(method,path,**kwargs):
                        response=client.request(method,path,**kwargs);response.raise_for_status();return response.json()
                    wait(lambda:request('GET','/health'))
                    if not options.cloud:processes.append(subprocess.Popen([sys.executable,'-m','agent_service.worker'],cwd=ROOT,env=env,stdout=worker_log,stderr=worker_log))
                    s=request('POST','/api/upload',files={'file':('acceptance.csv',b'order,customer,amount,refund,status\nA1,Acme,100.25,10.10,paid\nA2,Acme,20,0,pending\nA3,Acme,30,2,paid\n','text/csv')},data={'goal':'paid orders minus refund'})
                    source=wait(lambda:next((x for x in request('GET','/api/sources') if x['id']==s['id'] and x['status']=='ready'),None))
                    spec=dict(source['proposed_spec'],primary_key='c_1',group_by='c_2',amount_field='m_net',computations=[{'name':'m_net','label':'净金额','operation':'subtract','left_field':'c_3','right_field':'c_4'}],filters=[{'field':'c_5','operator':'eq','data_type':'text','value':'paid'}])
                    p=request('POST','/api/pipelines',json={'source_id':s['id'],'name':'验收任务','spec':spec,'release_mode':'manual'})
                    candidate=wait(lambda:next((x for x in request('GET','/api/pipelines') if x['id']==p['id'] and x.get('candidate_run')),None))
                    assert not candidate.get('published_run')
                    preview=request('GET',f'/api/pipelines/{p["id"]}/preview');assert preview['manifest']['records']==2
                    request('POST',f'/api/pipelines/{p["id"]}/publish',json={'run_id':candidate['candidate_run']})
                    result=request('GET',f'/api/pipelines/{p["id"]}/result');assert result['total']==2
                    assert Decimal(result['manifest']['summary'][0]['amount_total'])==Decimal('118.15')
                    assert request('GET','/api/status')['worker']['healthy']
                    assert request('GET','/api/audit/verify')['ok']
                    if options.cloud:
                        first=processes.pop();first.terminate();assert first.wait(timeout=12)==0
                        processes.append(subprocess.Popen(command,cwd=ROOT,env=env,stdout=api_log,stderr=api_log))
                        wait(lambda:request('GET','/health'))
                        wait(lambda:request('GET','/api/status')['worker']['healthy'])
                        restored=request('GET',f'/api/pipelines/{p["id"]}/result')
                        assert restored['total']==2 and Decimal(restored['manifest']['summary'][0]['amount_total'])==Decimal('118.15')
                        assert request('GET','/api/audit/verify')['ok']
                        # Linux integration: a dead worker must take down its paired API so the host restarts both.
                        if sys.platform=='linux':
                            victim=request('GET','/api/status')['worker']['pid']
                            assert victim>1 and victim not in (os.getpid(),processes[0].pid)
                            os.kill(victim,signal.SIGKILL);assert processes[0].wait(timeout=12)==1
                            try:client.get('/health');raise AssertionError('API outlived failed worker')
                            except (httpx.ConnectError,httpx.ReadError):pass
                    print(json.dumps({'cloud_supervisor':options.cloud,'restart_persistence':options.cloud,'ok':True,'version':'0.20.0','records':2,'net_amount':'118.1500','services':['uvicorn','worker'],'checks':['goal','rules','git','dlt','dbt','manual_release','heartbeat','audit']},ensure_ascii=False))
            except BaseException:
                api_log.flush();worker_log.flush()
                # Logs stay in the isolated temporary workspace; no user source credentials are used.
                for name in ('api.log','worker.log'):print((directory/name).read_text()[-5000:],file=sys.stderr)
                raise
            finally:
                for p in reversed(processes):
                    if p.poll() is None:p.terminate()
                for p in processes:
                    try:p.wait(timeout=8)
                    except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)

if __name__=='__main__':main()
