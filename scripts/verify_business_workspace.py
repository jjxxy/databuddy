"""Isolated real API/worker/restart acceptance for the personal finance path.

Uses synthetic data and explicitly authored plans, not a real-model evaluation.
No deployment or existing workspace data is touched.
"""
import hashlib,json,os,secrets,socket,subprocess,sys,tempfile,time
from pathlib import Path
from decimal import Decimal
import httpx

ROOT=Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='atlas-business-check-') as name:
        folder=Path(name);data=folder/'data'
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        env=dict(os.environ,DATA_DIR=str(data),DATABASE_URL='sqlite:///'+str(data/'metadata.sqlite'),
                 APP_TOKEN=secrets.token_urlsafe(32),PYTHONPATH=str(ROOT),LLM_BASE_URL='',LLM_MODEL='',
                 SCHEDULER_MODE='worker',DO_NOT_TRACK='1',DLT_TELEMETRY='false')
        processes=[]
        with (folder/'service.log').open('w') as log,httpx.Client(
                base_url=f'http://127.0.0.1:{port}',headers={'Authorization':'Bearer '+env['APP_TOKEN']},
                timeout=20,trust_env=False) as client:
            def request(method,path,**kwargs):
                response=client.request(method,path,**kwargs);response.raise_for_status();return response
            def call(method,path,**kwargs):return request(method,path,**kwargs).json()
            def wait(check):
                deadline=time.monotonic()+90
                while time.monotonic()<deadline:
                    if any(p.poll() is not None for p in processes):raise RuntimeError('acceptance service stopped')
                    try:
                        value=check()
                        if value:return value
                    except (httpx.ConnectError,httpx.ReadError):pass
                    time.sleep(.2)
                raise TimeoutError('business acceptance timeout')
            def start():
                processes.append(subprocess.Popen([sys.executable,'-m','uvicorn','agent_service.api:app',
                    '--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log))
                wait(lambda:call('GET','/health'))
                processes.append(subprocess.Popen([sys.executable,'-m','agent_service.worker'],
                    cwd=ROOT,env=env,stdout=log,stderr=log))
            def stop():
                for process in processes:process.terminate()
                for process in processes:process.wait(timeout=15)
                processes.clear()
            try:
                start()
                pid=call('POST','/api/projects',json={'name':'个人经营月报验收','request_id':'finance-project'})['id']
                root='/api/projects/'+pid
                raw=b'office,actual,budget\nEast,100.000000000001,100\nWest,200.000000000001,200\n'
                source=call('POST',root+'/attachments',files={'file':('month.csv',raw,'text/csv')},
                            data={'request_id':'finance-source'})
                wait(lambda:any(s['id']==source['id'] and s['status']=='ready' for s in call('GET','/api/sources')))
                columns=[{'name':'office','data_type':'text','meaning':'办事处'}]+[
                    {'name':field,'data_type':'decimal','meaning':meaning,'unit':'CNY',
                     'period':'2026-09','aggregation':'additive'}
                    for field,meaning in [('actual','本月净收入'),('budget','本月净收入预算')]]
                schema={'columns':columns,'grain':['office'],'meaning':'办事处月末实际和预算'}
                document={'version':1,'plan':{'version':1,'name':'经营月报底表','inputs':{'detail':schema},
                    'stages':[{'id':'checked','kind':'select','description':'保留完整月报底表',
                               'input':'detail','columns':['office','actual','budget']}],'output':'checked'},
                    'bindings':{'detail':{'source_id':source['id'],
                                         'column_map':{'c_1':'office','c_2':'actual','c_3':'budget'}}}}
                # Represents the Agent-authored workspace file; numbers remain in source storage.
                files=data/'workspaces'/pid/'files';(files/'workflows').mkdir(parents=True)
                (files/'workflows/month.json').write_text(json.dumps(document,ensure_ascii=False))
                workflow=call('POST',root+'/data-workflows',json={'path':'workflows/month.json'})
                wid=workflow['workflow_id'];job=call('POST',root+'/data-workflows/'+wid+'/run')['id']
                result=wait(lambda:next((r for r in call('GET','/api/runs',params={'target':wid})
                                        if r['id']==job and r['status'] in ('validated','succeeded')),None))
                page=call('GET',root+'/data-results/'+job)
                assert page['total']==2 and sum((Decimal(r['actual']) for r in page['rows']),Decimal(0))==Decimal('300.000000000002')
                call('POST',root+'/data-results/'+job+'/publish',json={'expected_plan_sha256':result['commit']})
                xlsx=request('GET',root+'/data-results/'+job+'/download',params={'format':'xlsx'}).content
                assert xlsx.startswith(b'PK')
                info=call('GET',root+'/business-report-inputs/'+job)
                recipe={'version':1,'title':'经营月报','period':'2026-09','expected_schema_sha256':info['schema_sha256'],
                    'metrics':[{'id':'actual','label':'净收入','kind':'sum','column':'actual'},
                               {'id':'delta','label':'预算差异','kind':'variance','column':'actual',
                                'baseline_column':'budget','rounding_scale':12}]}
                (files/'reports').mkdir();(files/'reports/month.json').write_text(json.dumps(recipe,ensure_ascii=False))
                brief=call('POST',root+'/business-reports',json={'run_id':job,'recipe_path':'reports/month.json'})
                assert brief['assertions'][0]['value']=='300.000000000002'
                downloads={}
                for format in ('docx','pptx'):
                    content=request('GET',brief['download_url'],params={'format':format}).content
                    assert content.startswith(b'PK');downloads[format]=hashlib.sha256(content).hexdigest()
                # A bad new draft must show its exact failing stage and retain the adopted result.
                info=call('GET',root+'/data-workflows/'+wid)
                document['plan']['stages'][0]['columns']=['does_not_exist']
                call('PUT',root+'/data-workflows/'+wid,json={'expected_sha256':info['draft_sha256'],'document':document})
                bad=call('POST',root+'/data-workflows/'+wid+'/run')['id']
                failure=wait(lambda:next((r for r in call('GET','/api/runs',params={'target':wid})
                                         if r['id']==bad and r['status']=='failed'),None))
                assert failure['workflow_failure']['stage_id']=='checked'
                assert failure['workflow_failure']['code']=='MISSING_COLUMN'
                assert call('GET',root+'/data-workflows/'+wid)['published_run']==job
                stop();start()
                assert call('GET',root+'/data-workflows/'+wid)['published_run']==job
                for format,digest in downloads.items():
                    assert hashlib.sha256(request('GET',brief['download_url'],params={'format':format}).content).hexdigest()==digest
                print(json.dumps({'ok':True,'version':'0.20.0','real_api_worker':True,
                    'restart_persistence':True,'exact_total':'300.000000000002','full_rows':2,
                    'complete_excel':True,'docx_pptx_reusable':True,'specific_failure_retains_adopted':True,
                    'real_model_evaluated':False,'browser_layout_verified':False},ensure_ascii=False))
            except Exception:
                print((folder/'service.log').read_text()[-3000:]);raise
            finally:stop()


if __name__=='__main__':main()
