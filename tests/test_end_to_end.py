import json
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from openpyxl import Workbook
from agent_service.api import app
from agent_service.store import get,all_items,unseal,put
from agent_service.config import DATA,TOKEN
from agent_service.execution import process_once,queue_due
from agent_service.dagster_defs import defs

client=TestClient(app)
headers={'Authorization':'Bearer '+TOKEN}

def upload(text,name='orders.csv'):
    r=client.post('/api/upload',headers=headers,files={'file':(name,text,'application/octet-stream')})
    assert r.status_code==200,r.text
    s=r.json();assert process_once()
    s=get('source',s['id']);assert s['status']=='ready',s
    return s

def create(s,spec=None):
    r=client.post('/api/pipelines',headers=headers,json={'source_id':s['id'],'name':'客户分析','spec':spec or s['proposed_spec'],'release_mode':'auto'})
    assert r.status_code==200,r.text
    p=r.json();assert process_once()
    return get('pipeline',p['id'])

def result(p):return client.get('/api/pipelines/'+p['id']+'/result',headers=headers)

def test_complete_csv_git_dlt_dbt_notes_queries_and_refresh():
    s=upload('订单号,客户,金额\nO1,甲公司,100.25\nO2,乙公司,200.00\nO3,甲公司,50.25\n')
    assert s['proposed_spec']['primary_key']=='c_1'
    p=create(s);r=result(p);assert r.status_code==200,r.text
    obj=r.json();assert obj['total']==3
    from decimal import Decimal
    sums={x['category']:Decimal(x['amount_total']) for x in obj['manifest']['summary']}
    assert sums=={'乙公司':Decimal('200.00'),'甲公司':Decimal('150.50')}
    code=client.get('/api/pipelines/'+p['id']+'/code',headers=headers).json()
    assert len(code['commit'])==40
    assert any(x['path'].endswith('business_objects.sql') for x in code['files'])
    assert 'secret_config' not in json.dumps(code)
    oid=obj['rows'][0]['object_id']
    assert client.put(f'/api/pipelines/{p["id"]}/objects/{oid}/note',headers=headers,json={'note':'需要跟进','tags':['重点']}).status_code==200
    assert client.post(f'/api/pipelines/{p["id"]}/run',headers=headers).status_code==200
    assert process_once()
    again=result(p).json();assert again['total']==3
    assert again['notes'][oid]['note']=='需要跟进'
    q=client.post(f'/api/pipelines/{p["id"]}/query',headers=headers,json={'sql':'select count(*) as n from business.business_objects'})
    assert q.status_code==200,q.text
    assert q.json()['rows']==[[3]]
    for sql in ["select * from read_csv('/etc/passwd')",'drop table business.business_objects','select 1; select 2',"select * into 'x.csv' from business.business_objects"]:
        assert client.post(f'/api/pipelines/{p["id"]}/query',headers=headers,json={'sql':sql}).status_code==400
    assert client.get(f'/api/pipelines/{p["id"]}/code.zip',headers=headers).status_code==200
    assert client.get(f'/api/pipelines/{p["id"]}/export',headers=headers).content.startswith(b'\xef\xbb\xbf')
    # A complete validation failure must not replace the previous published snapshot.
    stored=DATA/'uploads'/unseal(s['secret_config'])['stored_name']
    stored.write_text('订单号,客户,金额\nO1,甲公司,100.25\nO1,甲公司,20.00\n')
    previous=get('pipeline',p['id'])['published_run']
    assert client.post(f'/api/pipelines/{p["id"]}/run',headers=headers).status_code==200
    process_once()
    assert get('pipeline',p['id'])['published_run']==previous
    assert result(p).json()['total']==3
    assert [r for r in all_items('run') if r['target']==p['id']][-1]['status']=='failed'


def test_dedup_without_primary_and_schema_failure():
    s=upload('客户,金额\n甲,10\n甲,10\n乙,20\n')
    spec=dict(s['proposed_spec'],primary_key=None)
    p=create(s,spec);assert result(p).json()['total']==2
    old=p['published_run']
    path=DATA/'uploads'/unseal(s['secret_config'])['stored_name']
    path.write_text('其他字段,金额\n甲,10\n')
    client.post(f'/api/pipelines/{p["id"]}/run',headers=headers);process_once()
    assert get('pipeline',p['id'])['published_run']==old


def test_xlsx_sheet_selection_and_api_page_collection(monkeypatch,tmp_path):
    wb=Workbook();wb.active.title='第一页';wb.active.append(['客户','金额']);wb.active.append(['甲',10]);ws=wb.create_sheet('第二页');ws.append(['客户','金额']);ws.append(['乙',20]);file=tmp_path/'test.xlsx';wb.save(file)
    s=upload(file.read_bytes(),'test.xlsx')
    assert s['profile']['evidence']['sheets']==['第一页','第二页']
    assert client.post(f'/api/sources/{s["id"]}/sheet',headers=headers,json={'sheet':'第二页'}).status_code==200
    process_once();assert get('source',s['id'])['profile']['evidence']['sheet']=='第二页'
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import parse_qs,urlsplit
            page=int(parse_qs(urlsplit(self.path).query)['page'][0]);rows=[{'订单号':f'O{page}','客户':'甲','金额':str(page*10)}] if page<=2 else []
            data=json.dumps({'data':{'items':rows}},ensure_ascii=False).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(data)
        def log_message(self,*args):pass
    server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setenv('API_ALLOWED_HOSTS','127.0.0.1')
    try:
        res=client.post('/api/sources',headers=headers,json={'name':'分页订单','kind':'api','config':{'url':f'http://127.0.0.1:{server.server_port}/orders','records_path':'data.items','page_param':'page','max_pages':5}})
        assert res.status_code==200,res.text
        process_once();source=get('source',res.json()['id']);assert source['status']=='ready'
        p=create(source);assert result(p).json()['total']==2
    finally:server.shutdown()


def test_auth_schedule_fixed_commit_and_encrypted_credentials():
    assert client.get('/api/sources').status_code==401
    assert client.get('/').status_code==200
    s=upload('订单号,客户,金额\nT1,甲,12\n');p=create(s)
    r=client.post(f'/api/pipelines/{p["id"]}/schedule',headers=headers,json={'cron':'0 9 * * *'});assert r.status_code==200
    assert r.json()['next_due']
    task=get('pipeline',p['id']);task['next_due']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat();put('pipeline',task)
    queue_due();queue_due()
    due=[r for r in all_items('run') if r['target']==p['id'] and r.get('scheduled_key')]
    assert len(due)==1 and due[0]['commit']==p['commit']
    assert process_once()
    assert get('source',s['id'])['secret_config'].startswith('gAAAA')
    assert 'secret_config' not in client.get('/api/sources',headers=headers).text
    # Dagster definitions are importable and schedulable without commercial cloud services.
    assert defs.get_job_def('personal_data_schedule') is not None

def test_pdf_provenance_and_stable_special_primary_key(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
    writer=PdfWriter();page=writer.add_blank_page(width=612,height=792)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
    stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 72 720 Td (Contract example) Tj ET');page[NameObject('/Contents')]=stream
    path=tmp_path/'contract.pdf'
    with path.open('wb') as f:writer.write(f)
    s=upload(path.read_bytes(),'contract.pdf')
    assert s['profile']['evidence']['parser']=='pypdf'
    assert any(f['label']=='页码' for f in s['profile']['fields'])
    assert client.get(f'/api/sources/{s["id"]}/original',headers=headers).content==path.read_bytes()
    p=create(s)
    assert not p.get('published_run') and p.get('candidate_run')
    candidate=client.get(f'/api/pipelines/{p["id"]}/preview',headers=headers)
    assert candidate.status_code==200 and candidate.json()['requires_source_review'] is True
    preview=candidate.json();confirmation={'run_id':preview['run_id'],'confirm_source_review':True,
        'expected_commit':preview['manifest']['commit'],'expected_parquet_sha256':preview['manifest']['parquet_hash']}
    published=client.post(f'/api/pipelines/{p["id"]}/publish',headers=headers,json=confirmation)
    assert published.status_code==200,published.text
    r=result(p).json();assert r['total']==1
    row=r['rows'][0];labels={f['label']:f['name'] for f in r['manifest']['fields']}
    assert row[labels['页码']]==1 and 'Contract example' in row[labels['内容']]
    s2=upload('订单号,客户,金额\n订单/甲,甲公司,123.45\n')
    p2=create(s2);oid=result(p2).json()['rows'][0]['object_id']
    assert '/' not in oid and len(oid)==64
    assert client.put(f'/api/pipelines/{p2["id"]}/objects/{oid}/note',headers=headers,json={'note':'可用的稳定 ID'}).status_code==200

def test_task_revision_uses_same_git_repo_and_preserves_previous_result():
    s=upload('订单号,客户,金额\nR1,甲,10\nR2,乙,20\n')
    p=create(s);old=result(p).json();oid=old['rows'][0]['object_id']
    client.put(f'/api/pipelines/{p["id"]}/objects/{oid}/note',headers=headers,json={'note':'版本之间保留'})
    spec=dict(p['spec'],object_label='订单',group_by=None,amount_field=None)
    r=client.put(f'/api/pipelines/{p["id"]}',headers=headers,json={'source_id':s['id'],'name':'订单明细','spec':spec,'release_mode':'auto'})
    assert r.status_code==200,r.text
    revised=r.json();assert revised['repository']==p['repository'] and revised['commit']!=p['commit']
    assert result(p).json()['manifest']['commit']==p['commit']
    assert process_once()
    newer=result(p).json();assert newer['manifest']['commit']==revised['commit']
    assert newer['manifest']['summary']==[]
    assert newer['notes'][oid]['note']=='版本之间保留'
    import subprocess
    count=subprocess.check_output(['git','-C',p['repository'],'rev-list','--count','HEAD'],text=True).strip()
    assert count=='2'

    historical=client.get(f'/api/pipelines/{p["id"]}/code?commit={p["commit"]}',headers=headers)
    assert historical.status_code==200 and historical.json()['commit']==p['commit']
    assert any(f['path'].endswith('summary.sql') for f in historical.json()['files'])
    assert client.get(f'/api/pipelines/{p["id"]}/code?commit=unknown',headers=headers).status_code==400
