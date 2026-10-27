import json,time,threading,subprocess,sys,os
from pathlib import Path
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import DATA,TOKEN
from agent_service.store import get,put,all_items,patch,engine
from agent_service.execution import process_once,enqueue,queue_due
from agent_service import jobs as q
from agent_service.audit import verify,events
from agent_service.maintenance import backup
from sqlalchemy import update

client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}

def source(text,name):
    r=client.post('/api/upload',headers=headers,files={'file':(name,text.encode(),'text/csv')});assert r.status_code==200,r.text
    s=r.json()
    if not s.get('duplicate'):assert process_once()
    return get('source',s['id'])

def create(s,spec=None,mode='manual'):
    r=client.post('/api/pipelines',headers=headers,json={'source_id':s['id'],'name':'正式业务任务','spec':spec or s['proposed_spec'],'release_mode':mode});assert r.status_code==200,r.text
    assert process_once();return get('pipeline',r.json()['id'])

def test_manual_release_rollback_and_schedule_uses_active_code():
    s=source('订单号,客户,金额\nM1,甲,10\nM2,乙,20\n','manual.csv');p=create(s)
    assert p['published_run'] is None and p['candidate_run']
    r=client.get(f'/api/pipelines/{p["id"]}/preview',headers=headers);assert r.status_code==200 and r.json()['manifest']['records']==2
    old=p['candidate_run']
    assert client.post(f'/api/pipelines/{p["id"]}/publish',headers=headers,json={'run_id':old}).status_code==200
    current=get('pipeline',p['id']);assert current['active_commit']==p['commit']
    spec=dict(p['spec'],object_label='订单明细',metric_status='confirmed',metric_definition='当前订单金额字段，不扣除退款',currency='CNY')
    r=client.put(f'/api/pipelines/{p["id"]}',headers=headers,json={'source_id':s['id'],'name':'正式订单','spec':spec,'release_mode':'manual'});assert r.status_code==200
    assert process_once();new=get('pipeline',p['id'])
    assert new['active_commit']==p['commit'] and new['commit']!=p['commit']
    assert client.get(f'/api/pipelines/{p["id"]}/result',headers=headers).json()['manifest']['spec']['object_label']!=spec['object_label']
    client.post(f'/api/pipelines/{p["id"]}/schedule',headers=headers,json={'cron':'0 9 * * *'})
    patch('pipeline',p['id'],next_due='2000-01-01T00:00:00+00:00')
    queue_due();queued=q.list_jobs(target=p['id'],status='queued');assert queued[0]['commit']==p['commit']
    assert process_once()
    latest=get('pipeline',p['id']);assert latest['candidate_run']==new['candidate_run']
    assert client.post(f'/api/pipelines/{p["id"]}/publish',headers=headers,json={'run_id':new['candidate_run']}).status_code==200
    assert client.post(f'/api/pipelines/{p["id"]}/rollback',headers=headers,json={'run_id':old}).status_code==200
    assert get('pipeline',p['id'])['active_commit']==p['commit']


def test_multi_source_join_quality_search_views_and_raw_versions():
    customers=source('客户编号,行业\nC1,制造\nC2,服务\n','customers.csv')
    orders=source('订单号,客户编号,金额\nJ1,C1,10.25\nJ2,C2,20.50\nJ3,C1,1.25\n','join-orders.csv')
    spec=dict(orders['proposed_spec'],primary_key='c_1',group_by='j1_c_2',amount_field='c_3',joins=[{'source_id':customers['id'],'left_field':'c_2','right_field':'c_1','label':'客户','required_match':True}],metric_status='confirmed',metric_definition='订单金额字段合计',currency='CNY',required_fields=['c_1','c_2'],nonnegative_fields=['c_3'])
    p=create(orders,spec,'auto');assert p.get('published_run'),q.list_jobs(target=p['id'])
    result=client.get(f'/api/pipelines/{p["id"]}/result',headers=headers).json();assert result['total']==3
    from decimal import Decimal
    sums={x['category']:Decimal(x['amount_total']) for x in result['manifest']['summary']};assert sums=={'制造':Decimal('11.50'),'服务':Decimal('20.50')}
    search=client.get(f'/api/pipelines/{p["id"]}/result?search=制造&sort=c_3',headers=headers).json();assert search['total']==2
    r=client.post('/api/views',headers=headers,json={'name':'行业收入','pipeline_id':p['id'],'sql':'select * from business.summary','chart':'bar'});assert r.status_code==200
    view=client.post(f'/api/views/{r.json()["id"]}/run',headers=headers);assert view.status_code==200 and len(view.json()['rows'])==2
    lineage=client.get(f'/api/pipelines/{p["id"]}/lineage',headers=headers).json();assert len(lineage['inputs'])==2 and lineage['relationships'][0]['unmatched']==0
    old=p['published_run']
    r=client.post(f'/api/sources/{customers["id"]}/replace-file',headers=headers,files={'file':('customers-new.csv',b'customer_id,industry\nC1,A\nC1,B\n','text/csv')});assert r.status_code==200
    assert process_once()
    assert get('source',customers['id'])['schema_diff']['review_required']
    assert client.get(f'/api/sources/{customers["id"]}/original?revision=1',headers=headers).content.decode().startswith('客户编号')
    # Regenerate against the new labels; duplicate join keys must prevent publication and sums inflation.
    r=client.put(f'/api/pipelines/{p["id"]}',headers=headers,json={'source_id':orders['id'],'name':'关联验证','spec':spec,'release_mode':'auto'});assert r.status_code==200
    process_once();assert get('pipeline',p['id'])['published_run']==old
    failed=q.list_jobs(target=p['id'])[0];assert failed['status']=='failed' and failed['error_code']=='QUALITY_FAILED'
    assert any(x['status']=='fail' for x in failed['quality'])


def test_unique_enqueue_lease_recovery_and_cancellation():
    s=put('source',{'name':'队列契约','kind':'file'})
    first=q.enqueue('probe',s['id'],scheduled_key='unique-contract');again=q.enqueue('probe',s['id'],scheduled_key='unique-contract');assert first['id']==again['id']
    run=q.claim(lease_seconds=-1);assert run['id']==first['id']
    q.recover_expired();recovered=q.get_job(run['id']);assert recovered['status']=='retrying'
    try:q.fenced_update(run['id'],run['lease_token'],status='succeeded');assert False
    except RuntimeError:pass
    second=q.claim();assert second['id']==first['id'] and second['attempts']==2 and second['lease_token']!=run['lease_token']
    q.fenced_update(second['id'],second['lease_token'],status='failed')
    s2=put('source',{'name':'取消契约','kind':'file'})
    job=q.enqueue('probe',s2['id']);q.request_cancel(job['id']);assert q.get_job(job['id'])['status']=='cancelled'
    public=client.get('/api/runs',headers=headers).text;assert 'source_snapshot' not in public and 'lease_token' not in public and 'secret_config' not in public


def test_backup_restore_audit_integrity_and_security_headers(tmp_path):
    assert verify()['ok']
    archive=tmp_path/'workspace.atlas';saved=backup(archive);assert saved['files']>0
    target=tmp_path/'restored';env=dict(os.environ,DATA_DIR=str(target),DATABASE_URL='sqlite:///'+str(target/'metadata.sqlite'),PYTHONPATH=str(Path('personal-data-agent').resolve()))
    # Full-session backup has >9k files; measured Windows restore is 72s.
    r=subprocess.run([sys.executable,'-m','agent_service.maintenance','restore',str(archive)],env=env,capture_output=True,text=True,encoding='utf-8',timeout=180);assert r.returncode==0,r.stderr
    report=json.loads(r.stdout);assert report['ok']
    r=subprocess.run([sys.executable,'-c','from agent_service.audit import verify; import json; print(json.dumps(verify()))'],env=env,capture_output=True,text=True,timeout=10);assert json.loads(r.stdout)['ok']
    assert client.get('/',headers=headers).headers['X-Content-Type-Options']=='nosniff'
    assert "frame-ancestors 'none'" in client.get('/').headers['Content-Security-Policy']
    # Chain verification detects a modified stored event.
    with engine.begin() as c:
        row=c.execute(events.select().limit(1)).mappings().first()
        original=row['body'];c.execute(update(events).where(events.c.sequence==row['sequence']).values(body='{} modified'))
    assert not verify()['ok']
    with engine.begin() as c:c.execute(update(events).where(events.c.sequence==row['sequence']).values(body=original))
    assert verify()['ok']

def test_transient_api_failure_retries_and_long_subprocess_cancels(monkeypatch,tmp_path):
    from http.server import HTTPServer,BaseHTTPRequestHandler
    calls=[0]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls[0]+=1
            if calls[0]==1:self.send_response(503);self.end_headers();return
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b'[{"id":"A1","amount":10}]')
        def log_message(self,*args):pass
    server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();monkeypatch.setenv('API_ALLOWED_HOSTS','127.0.0.1')
    try:
        r=client.post('/api/sources',headers=headers,json={'name':'暂时不可用API','kind':'api','config':{'url':f'http://127.0.0.1:{server.server_port}/orders'}});assert r.status_code==200
        sid=r.json()['id'];assert process_once()
        job=q.list_jobs(target=sid)[0];assert job['status']=='retrying' and job['error_code']=='API_HTTP_FAILED'
        with engine.begin() as c:c.execute(update(q.jobs).where(q.jobs.c.id==job['id']).values(available_at=time.time()-1))
        assert process_once();assert q.get_job(job['id'])['status']=='succeeded' and q.get_job(job['id'])['attempts']==2
    finally:server.shutdown()
    target=put('source',{'name':'长进程取消','kind':'file'})
    job=q.enqueue('probe',target['id']);run=q.claim();assert run['id']==job['id']
    folder=tmp_path/'cancel';folder.mkdir();errors=[]
    from agent_service.execution import invoke
    def work():
        try:invoke([sys.executable,'-c','import time; time.sleep(60)'],run,folder,dict(os.environ))
        except Exception as e:errors.append(e)
    worker=threading.Thread(target=work);worker.start();time.sleep(.3);q.request_cancel(job['id']);worker.join(timeout=8)
    assert not worker.is_alive() and errors and getattr(errors[0],'code',None)=='CANCELLED'
    q.fenced_update(run['id'],run['lease_token'],status='cancelled')

def test_legacy_queue_upgrade_and_retention_protects_releases(tmp_path):
    import sqlite3,hashlib
    root=tmp_path/'legacy';root.mkdir()
    db=sqlite3.connect(root/'metadata.sqlite')
    db.execute('create table items (id varchar(64) primary key, kind varchar(32), body text not null)')
    job={'id':'old-job','kind':'pipeline','target':'old-pipeline','commit':'a'*40,'status':'succeeded','created_at':'2000-01-01T00:00:00+00:00','records':2}
    pipeline={'id':'old-pipeline','published_run':'old-job','commit':'a'*40,'source_id':'old-source','name':'历史结果'}
    db.execute('insert into items values (?,?,?)',('old-job','run',json.dumps(job)))
    db.execute('insert into items values (?,?,?)',('old-pipeline','pipeline',json.dumps(pipeline)))
    db.commit();db.close()
    folder=root/'runs'/'old-job';folder.mkdir(parents=True);original=json.dumps({'commit':'a'*40,'records':2}).encode();(folder/'output.json').write_bytes(original)
    env=dict(os.environ,DATA_DIR=str(root),DATABASE_URL='sqlite:///'+str(root/'metadata.sqlite'),PYTHONPATH=str(Path('personal-data-agent').resolve()))
    script='from agent_service.jobs import get_job; from agent_service.store import get; import json; print(json.dumps({"run":get_job("old-job"),"pipeline":get("pipeline","old-pipeline")}))'
    result=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True,timeout=10);assert result.returncode==0,result.stderr
    state=json.loads(result.stdout);assert state['run']['validated'] and state['pipeline']['published_run']=='old-job'
    assert (folder/'output.json').read_bytes()==original
    second=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True,timeout=10);assert second.returncode==0
    from agent_service.maintenance import retention
    candidates=retention(days=0,keep_success=1,apply=False)['candidates'];ids={v['run_id'] for v in candidates}
    for p in all_items('pipeline'):
        assert p.get('published_run') not in ids and p.get('candidate_run') not in ids
