from decimal import Decimal
"""Regression contracts for business rules, pagination and recovery boundaries."""
import hashlib,json,os,subprocess,sys,threading,time,zipfile
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlsplit,parse_qs
import duckdb,pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from agent_service.api import app
from agent_service.config import DATA,TOKEN,MAX_ROWS
from agent_service.store import get,put,seal
from agent_service import jobs as q
from agent_service.execution import process_once,checkpoint,invoke
from agent_service.connectors import api_rows,SourceError
from agent_service.models import FilterSpec
from agent_service.maintenance import backup

client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}

def upload(text,name):
    r=client.post('/api/upload',headers=headers,files={'file':(name,text.encode(),'text/csv')},data={'goal':'仅统计已付款订单，扣除退款'});assert r.status_code==200,r.text
    assert process_once();return get('source',r.json()['id'])

def pipeline(s,spec,runtime=None):
    body={'source_id':s['id'],'name':'业务规则验证','release_mode':'auto','spec':spec}
    if runtime:body['runtime']=runtime
    r=client.post('/api/pipelines',headers=headers,json=body);assert r.status_code==200,r.text
    assert process_once();p=get('pipeline',r.json()['id']);assert p['published_run'],q.list_jobs(target=p['id'])
    return p

def replace(s,text):
    r=client.post('/api/sources/'+s['id']+'/replace-file',headers=headers,files={'file':('rules-new.csv',text.encode(),'text/csv')});assert r.status_code==200,r.text
    assert process_once()

def test_business_rules_actual_net_revenue_runtime_and_freshness():
    s=upload('order,customer,amount,refund,status,day\nR1,A,100.25,10.10,paid,2026-10-10\nR2,A,20,0,pending,2026-10-11\nR3,B,50,5,paid,2026-09-01\nR4,A,30,2,paid,2026-10-12\n','rules-paid.csv')
    assert s['goal']=='仅统计已付款订单，扣除退款'
    spec=dict(s['proposed_spec'],primary_key='c_1',group_by='c_2',amount_field='m_net',computations=[{'name':'m_net','label':'退款后金额','operation':'subtract','left_field':'c_3','right_field':'c_4'}],filters=[{'field':'c_5','operator':'eq','data_type':'text','value':'paid'},{'field':'c_6','operator':'gte','data_type':'date','value':'2026-10-01'}],metric_status='confirmed',metric_definition='已付款且日期自2026-10-01起，订单金额扣除退款',currency='CNY')
    p=pipeline(s,spec,{'timeout_seconds':180,'max_attempts':1,'freshness_minutes':30})
    r=client.get('/api/pipelines/'+p['id']+'/result',headers=headers).json();m=r['manifest']
    assert r['total']==2 and Decimal(m['summary'][0]['amount_total'])==Decimal('118.15')
    assert {x['m_net'] for x in r['rows']}=={'90.150000000000','28.000000000000'}
    assert m['inputs'][0]['records']==4 and m['runtime']['max_attempts']==1
    job=q.get_job(p['published_run']);assert job['max_attempts']==1 and job['runtime_snapshot']['timeout_seconds']==180
    folder=DATA/job['artifact_dir']
    with duckdb.connect(str(folder/'warehouse.duckdb'),read_only=True) as con:assert con.execute('select count(*) from raw.records').fetchone()[0]==4
    assert "'paid'" in (folder/'code/dbt/models/business_objects.sql').read_text()
    assert (folder/'code/dbt/models/prepared_objects.sql').exists()
    lineage=client.get('/api/pipelines/'+p['id']+'/lineage',headers=headers).json();assert len(lineage['rules']['filters'])==2
    # Rollback/publish must retain collection age, rather than appear fresh because of the operation.
    m['data_as_of']='2000-01-01T00:00:00+00:00';(folder/'output.json').write_text(json.dumps(m))
    assert client.post('/api/pipelines/'+p['id']+'/rollback',headers=headers,json={'run_id':job['id']}).status_code==200
    item=next(x for x in client.get('/api/pipelines',headers=headers).json() if x['id']==p['id'])
    assert item['freshness']['stale'] and item['data_as_of'].startswith('2000')


def test_empty_snapshot_requires_opt_in_and_preserves_schema_on_recovery():
    text='order,customer,amount\nE1,A,12.5\nE2,B,5.0\n'
    s=upload(text,'rules-empty.csv')
    spec=dict(s['proposed_spec'],primary_key='c_1',group_by='c_2',amount_field='c_3',allow_empty_source=True,min_rows=0)
    p=pipeline(s,spec);previous=p['published_run']
    replace(s,'order,customer,amount\n')
    assert client.post('/api/pipelines/'+p['id']+'/run',headers=headers).status_code==200
    assert process_once();p=get('pipeline',p['id']);assert p['published_run']!=previous,q.list_jobs(target=p['id'])
    r=client.get('/api/pipelines/'+p['id']+'/result',headers=headers).json();assert r['total']==0 and r['manifest']['inputs'][0]['evidence']['empty']
    replace(s,text)
    assert client.post('/api/pipelines/'+p['id']+'/run',headers=headers).status_code==200
    assert process_once();assert client.get('/api/pipelines/'+p['id']+'/result',headers=headers).json()['total']==2
    # A default task keeps its published snapshot if the upstream unexpectedly empties.
    strict=pipeline(get('source',s['id']),dict(spec,allow_empty_source=False,min_rows=1));old=strict['published_run']
    replace(s,'order,customer,amount\n')
    client.post('/api/pipelines/'+strict['id']+'/run',headers=headers);assert process_once()
    assert get('pipeline',strict['id'])['published_run']==old
    job=q.list_jobs(target=strict['id'])[0];assert job['status']=='failed' and job['error_code']=='SOURCE_EMPTY'


def test_divide_zero_is_quality_failure_and_sql_values_are_literals():
    s=upload('order,customer,amount,divisor\nD1,A,10,0\nD2,B,20,2\n','rules-divide.csv')
    spec=dict(s['proposed_spec'],primary_key='c_1',group_by='c_2',amount_field='m_ratio',computations=[{'name':'m_ratio','label':'比率','operation':'divide','left_field':'c_3','right_field':'c_4'}])
    r=client.post('/api/pipelines',headers=headers,json={'source_id':s['id'],'name':'除零防护','spec':spec,'release_mode':'auto'});assert r.status_code==200,r.text
    assert process_once();p=get('pipeline',r.json()['id']);assert p['published_run'] is None
    assert q.list_jobs(target=p['id'])[0]['error_code']=='QUALITY_FAILED'
    from agent_service.rules import filter_sql
    rule=FilterSpec(field='c_2',value="paid' OR 1=1 --").model_dump()
    with duckdb.connect() as con:
        con.execute("create table records(c_2 varchar); insert into records values ('paid'), ('other')")
        assert con.execute('select count(*) from records where '+filter_sql(rule)).fetchone()[0]==0
    for invalid in [{'data_type':'number','value':'NaN'},{'data_type':'date','value':'2026-02-30'},{'operator':'in','value':[]},{'data_type':'number','operator':'contains','value':1}]:
        with pytest.raises(ValidationError):FilterSpec(field='c_2',**invalid)


def test_cursor_offset_pagination_empty_intermediate_loop_and_total(monkeypatch):
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            u=urlsplit(self.path);params=parse_qs(u.query);calls.append((u.path,params))
            if u.path=='/cursor':
                token=params.get('after',[''])[0]
                payload={'':{'data':[{'id':'A'}],'next':'mid'},'mid':{'data':[],'next':'end'},'end':{'data':[{'id':'B'}],'next':None}}[token]
            elif u.path=='/offset':
                offset=int(params.get('offset',['0'])[0]);payload={'data':[{'id':i} for i in range(offset,min(3,offset+2))],'total':3}
            elif u.path=='/loop':payload={'data':[{'id':'A'}],'next':'again'}
            elif u.path=='/missing':payload={'data':[{'id':'A'}],'total':2}
            elif u.path=='/bounded':payload={'data':[{'id':len(calls)}],'next':'cursor-'+str(len(calls))}
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(payload).encode())
        def log_message(self,*args):pass
    server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();monkeypatch.setenv('API_ALLOWED_HOSTS','127.0.0.1')
    def read(path,**config):return api_rows({'secret_config':seal(dict(url=f'http://127.0.0.1:{server.server_port}'+path,records_path='data',max_pages=5,**config))},MAX_ROWS)
    try:
        rows,e=read('/cursor',pagination='cursor',cursor_param='after',next_cursor_path='next');assert rows==[{'id':'A'},{'id':'B'}] and e['pages']==3 and e['complete']
        rows,e=read('/offset',pagination='offset',offset_param='offset',page_size=2,total_path='total');assert len(rows)==3 and e['pages']==2
        with pytest.raises(SourceError) as error:read('/loop',pagination='cursor',cursor_param='after',next_cursor_path='next')
        assert error.value.code=='PAGINATION_LOOP'
        with pytest.raises(SourceError) as error:read('/missing',pagination='none',total_path='total')
        assert error.value.code=='TOTAL_MISMATCH'
        with pytest.raises(SourceError) as error:read('/bounded',pagination='cursor',cursor_param='after',next_cursor_path='next')
        assert error.value.code=='PAGINATION_INCOMPLETE'
    finally:server.shutdown();server.server_close()


def test_restore_invalid_audit_is_rejected_before_writing(tmp_path):
    original=tmp_path/'good.atlas';backup(original);bad=tmp_path/'bad.atlas'
    with zipfile.ZipFile(original) as z:contents={name:z.read(name) for name in z.namelist()}
    rows=[json.loads(line) for line in contents['metadata.jsonl'].splitlines()]
    event=next(x for x in rows if x['table']=='audit_events');event['row']['body']='{"tampered":true}'
    contents['metadata.jsonl']=('\n'.join(json.dumps(x) for x in rows)+'\n').encode()
    manifest=json.loads(contents['manifest.json']);manifest['files']['metadata.jsonl']=hashlib.sha256(contents['metadata.jsonl']).hexdigest();contents['manifest.json']=json.dumps(manifest).encode()
    with zipfile.ZipFile(bad,'w') as z:
        for name,data in contents.items():z.writestr(name,data)
    target=tmp_path/'target';env=dict(os.environ,DATA_DIR=str(target),DATABASE_URL='sqlite:///'+str(target/'metadata.sqlite'))
    result=subprocess.run([sys.executable,'-m','agent_service.maintenance','restore',str(bad)],env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode!=0 and '审计链无效' in result.stderr
    assert not (target/'connector.key').exists() and not any((target/'uploads').glob('*'))
    script='from agent_service.store import engine,items; from agent_service.jobs import jobs; from agent_service.audit import events; from sqlalchemy import select,func; print([engine.connect().execute(select(func.count()).select_from(t)).scalar() for t in (items,jobs,events)])'
    state=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True,timeout=10);assert state.returncode==0,state.stderr
    assert json.loads(state.stdout)==[1,0,0]


def test_deadline_and_late_cancellation_checkpoint(tmp_path):
    target=put('source',{'name':'运行预算边界','kind':'file'});job=q.enqueue('probe',target['id']);run=q.claim();assert run['id']==job['id']
    run['_deadline']=time.monotonic()-1
    try:
        with pytest.raises(Exception) as error:invoke([sys.executable,'-c','import time;time.sleep(30)'],run,tmp_path,dict(os.environ))
        assert getattr(error.value,'code',None)=='TIMEOUT'
        q.request_cancel(run['id'])
        with pytest.raises(Exception) as error:checkpoint(run)
        assert getattr(error.value,'code',None)=='CANCELLED'
    finally:q.fenced_update(run['id'],run['lease_token'],status='cancelled')


def test_nonnegative_quality_checks_small_negatives_before_money_rounding():
    from agent_service.codegen import generate
    s=upload('order,customer,amount\nN1,A,-0.00001\nN2,B,1.23456\n','rules-small-negative.csv')
    p=generate(s,'精度质量检查',dict(s['proposed_spec'],nonnegative_fields=['c_3']))
    sql=(Path(p['repository'])/'dbt/tests/generated_nonnegative_c_3.sql').read_text().replace("{{ ref('business_objects') }}",'business_objects')
    with duckdb.connect() as con:
        con.execute("create table business_objects(c_3 varchar); insert into business_objects values ('-0.00001'), ('1.23456'), ('1,000.12'), ('bad')")
        assert {x[0] for x in con.execute(sql).fetchall()}=={'-0.00001','bad'}


def test_retrying_pipeline_cannot_swallow_a_new_code_revision():
    from agent_service.codegen import generate
    s=upload('order,customer,amount\nQ1,A,12\nQ2,B,3\n','rules-retry-lock.csv')
    p=generate(s,'重试版本保护',s['proposed_spec'])
    from agent_service.execution import enqueue
    job=enqueue('pipeline',p['id'],p['commit']);run=q.claim();assert run['id']==job['id']
    q.fenced_update(run['id'],run['lease_token'],status='retrying',available_at=time.time()+3600)
    try:
        response=client.put('/api/pipelines/'+p['id'],headers=headers,json={'source_id':s['id'],'name':'不能丢掉此版本','spec':s['proposed_spec']})
        assert response.status_code==409 and get('pipeline',p['id'])['commit']==p['commit']
        assert client.post('/api/pipelines/'+p['id']+'/run',headers=headers).status_code==409
    finally:q.request_cancel(job['id'])
