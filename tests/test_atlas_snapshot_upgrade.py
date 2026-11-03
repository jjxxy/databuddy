"""Real old-format crypto fixtures, read by the upgraded runtime.

Construct the wire format as written by the old Atlas revision: identical
master key, scope, version and filename, but historical HKDF domains and AAD.
"""
import hashlib
import hmac
import json
import secrets
import shutil
import uuid
import base64

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from agent_service import business_flow_data as flow
from agent_service.data_workflows import canonical


def context(actor='owner',project=None):
    tag=uuid.uuid4().hex
    return {'project_id':project or 'p_'+tag,'actor_id':actor,'turn_id':'t_'+tag,'run_id':'r_'+tag}


def old_snapshot(ctx,rows,*,version):
    folder,scope=flow._table_folder(ctx)
    from agent_service.config import DATA
    from agent_service.store import cipher
    cipher()
    master=base64.urlsafe_b64decode((DATA/'connector.key').read_bytes())
    old_key=HKDF(algorithm=hashes.SHA256(),length=32,salt=bytes.fromhex(scope),
                 info=b'atlas-business-flow-table-v1').derive(master)
    old_index=HKDF(algorithm=hashes.SHA256(),length=32,salt=None,
                   info=b'atlas-business-flow-owner-index-v1').derive(master)
    old_claim=flow._owner_claim(ctx,scope,old_index)
    (folder/'.owner.json').write_bytes(canonical(old_claim))
    raw=canonical(rows);sha=hashlib.sha256(raw).hexdigest()
    path=folder/(sha+'.enc')
    if version==1:
        nonce=secrets.token_bytes(12)
        path.write_bytes(nonce+AESGCM(old_key).encrypt(nonce,raw,scope.encode()))
        return {'version':1,'sha256':sha,'bytes':len(raw),'row_count':len(rows),'scope':scope}
    entries=[]
    with path.open('wb') as stream:
        stream.write(flow._PAGE_MAGIC)
        for start in range(0,len(rows),flow._PAGE_ROWS):
            page=rows[start:start+flow._PAGE_ROWS];nonce=secrets.token_bytes(12)
            aad=canonical(['atlas-table-page-v2',scope,sha,len(entries),start,len(page)])
            part=nonce+AESGCM(old_key).encrypt(nonce,canonical(page),aad)
            entries.append({'start':start,'count':len(page),'offset':stream.tell(),'length':len(part),'sha256':hashlib.sha256(part).hexdigest()})
            stream.write(part)
        metadata={'version':2,'scope':scope,'sha256':sha,'bytes':len(raw),'row_count':len(rows),'chunks':entries}
        encoded=canonical(metadata);stream.write(encoded)
        stream.write(hmac.new(old_key,encoded,hashlib.sha256).digest());stream.write(len(encoded).to_bytes(8,'big'))
    return {'version':2,'sha256':sha,'bytes':len(raw),'row_count':len(rows),'scope':scope,
            'cipher_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'table_sha256':None}


@pytest.mark.parametrize('version',[1,2])
def test_old_cipher_snapshot_read_page_usage_and_new_writes(version):
    ctx=context();rows=[{'n':str(n),'value':'历史金额'} for n in range(550)]
    old=old_snapshot(ctx,rows,version=version)
    folder,_=flow._table_folder(ctx);marker=(folder/'.owner.json').read_bytes()
    assert flow._read_rows(ctx,old)==rows
    assert flow.get_table_page(ctx,{'rows_ref':old},245,30)==rows[245:275]
    flow.verify_table_snapshot(ctx,{'rows_ref':old})
    usage=flow.snapshot_storage_usage(ctx['project_id'],ctx['actor_id'])
    assert usage['project_bytes']>=sum(p.stat().st_size for p in folder.iterdir())
    # Reusing old ciphertext and publishing different new ciphertext both work
    # without replacing the historical authenticated owner marker.
    assert flow._save_rows_paged(ctx,rows,canonical(rows))['version']==version
    new_rows=[{'n':'new','value':'新增金额'}]
    newer=flow._save_rows_paged(ctx,new_rows,canonical(new_rows))
    assert flow._read_rows(ctx,newer)==new_rows and (folder/'.owner.json').read_bytes()==marker
    assert flow._read_rows(ctx,old)==rows
    other=flow.snapshot_storage_usage('foreign','u_'+'a'*64)
    assert other['project_bytes']==0


@pytest.mark.parametrize('version',[1,2])
def test_old_snapshot_tamper_and_foreign_scope_fail_closed(version):
    ctx=context();rows=[{'n':str(n)} for n in range(300)]
    ref=old_snapshot(ctx,rows,version=version)
    with pytest.raises(ValueError):flow._read_rows({**ctx,'project_id':'foreign'},ref)
    folder,_=flow._table_folder(ctx);path=folder/(ref['sha256']+'.enc')
    payload=bytearray(path.read_bytes());payload[20]^=1;path.write_bytes(payload)
    # Even updating the public ciphertext digest cannot bypass authenticated
    # encryption or the signed index.
    changed={**ref,'cipher_sha256':hashlib.sha256(payload).hexdigest()} if version==2 else ref
    with pytest.raises(ValueError):flow._read_rows(ctx,changed)


def test_legacy_owner_index_tampering_never_downgrades_to_unindexed():
    ctx=context();old_snapshot(ctx,[{'v':'1'}],version=1)
    folder,_=flow._table_folder(ctx);marker=folder/'.owner.json'
    data=json.loads(marker.read_bytes());data['project']='0'*64;marker.write_bytes(canonical(data))
    try:
        with pytest.raises(ValueError,match='索引损坏'):flow.snapshot_storage_usage(ctx['project_id'],ctx['actor_id'])
    finally:shutil.rmtree(folder)


def test_frozen_old_flow_uses_original_ordinal_and_preserves_duplicates():
    from decimal import Decimal
    from fastapi.testclient import TestClient
    from agent_service.api import app
    from agent_service.config import TOKEN
    from agent_service.execution import process_once
    from agent_service.store import get,uid
    from agent_service.business_flow_engine import Handler,run
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    def req(method,path,**kwargs):
        response=client.request(method,path,headers=headers,**kwargs)
        assert response.status_code==200,response.text
        return response.json()
    pid=req('POST','/api/projects',json={'name':'升级测试','request_id':uid()})['id'];base='/api/projects/'+pid
    source=req('POST',base+'/attachments',files={'file':('income.csv','办事处,收入\n东部,9.00\n东部,9.00\n'.encode(),'text/csv')},data={'request_id':uid(),'local_processing':'true'})
    for _ in range(100):
        if get('source',source['id'])['status'] in ('ready','failed'):break
        assert process_once()
    source=get('source',source['id']);assert source['status']=='ready'
    turn=req('POST',base+'/messages',json={'text':'按办事处汇总','request_id':uid(),'source_id':source['id']})
    ctx={'project_id':pid,'turn_id':turn['id'],'actor_id':'owner'}
    plan={'version':1,'name':'旧版收入汇总','inputs':{'table':{'meaning':'收入','grain':['atlas_row'],
        'columns':[{'name':'atlas_row','data_type':'text','meaning':'本次资料行号'},
                   {'name':'c_1','data_type':'text','meaning':'办事处'},
                   {'name':'c_2','data_type':'decimal','meaning':'收入','unit':'CNY','aggregation':'additive'}]}},
        'stages':[{'id':'total','kind':'aggregate','description':'汇总','input':'table','by':['c_1'],'meaning':'收入',
                   'aggregations':[{'function':'sum','column':'c_2','output':{'name':'amount','data_type':'decimal','meaning':'金额','unit':'CNY'}}]}], 'output':'total'}
    pin={'source_id':source['id'],'revision':source['revision'],'content_hash':source['content_hash']}
    doc={'name':'旧业务流程','nodes':[{'id':'read','kind':'source_table','title':'读取','config':pin},
        {'id':'sum','kind':'table_workflow','title':'汇总','depends_on':['read'],
         'inputs':{'table':{'source':'node','name':'read'}},'config':{'plan':plan}}],'outputs':['sum']}
    handlers={'source_table':Handler(flow.source_table,True,True,flow.validate_source),
              'table_workflow':Handler(flow.table_workflow,True,True,flow.validate_table)}
    result=run(doc,{},handlers,ctx,persist=lambda state:None)
    assert result['status']=='succeeded',result
    out=result['outputs']['sum'];assert out['row_count']==1 and Decimal(out['rows'][0]['amount'])==Decimal('18.00')
    assert out['source_refs'][0]['content_hash']==source['content_hash']
    current=flow.source_table(ctx,pin,{})
    assert [r['nexus_row'] for r in current['rows']]==['1','2']
    # In-flight old runs can already have a saved source-table checkpoint.
    # Do not force a second migration or accidentally reject that old table.
    old_table={**current,
               'fields':[{**f,'name':'atlas_row'} if f['name']=='nexus_row' else f for f in current['fields']],
               'rows':[{('atlas_row' if k=='nexus_row' else k):v for k,v in r.items()} for r in current['rows']]}
    restored=flow.table_workflow(ctx,{'plan':plan},{'table':old_table})
    assert Decimal(restored['rows'][0]['amount'])==Decimal('18.00')
    # A genuine source column named atlas_row must never be silently replaced
    # with a fabricated ordinal just because an old plan happens to name it.
    conflicting={**current,'fields':[*current['fields'],{'name':'atlas_row'}],
                 'rows':[{**r,'atlas_row':'user-data'} for r in current['rows']]}
    with pytest.raises(ValueError,match='行标识与来源字段冲突'):
        flow.table_workflow(ctx,{'plan':plan},{'table':conflicting})


def test_cleanup_accepts_valid_legacy_owner_marker():
    from test_flow_snapshot_cleanup import fixture
    from agent_service import flow_snapshot_cleanup as cleanup
    ctx,job,state,flow_id,folder,keep,orphan=fixture()
    scope=flow._table_scope(ctx)
    old_claim=flow._owner_claim(ctx,scope,flow._index_key(legacy=True))
    (folder/'.owner.json').write_bytes(canonical(old_claim))
    preview=cleanup.cleanup_failed_run(job['id'])
    assert preview['files']==1 and preview['applied'] is False
    result=cleanup.cleanup_failed_run(job['id'],apply=True)
    assert result['files']==1 and (folder/(keep['sha256']+'.enc')).exists()
    assert flow._read_rows(ctx,keep)


@pytest.mark.parametrize('suffix,default',[('TOTAL',2048),('ACTOR',1024),('PROJECT',512)])
def test_old_atlas_quota_survives_upgrade_and_conflicts_fail_closed(monkeypatch,suffix,default):
    new='NEXUS_FLOW_SNAPSHOT_'+suffix+'_MB'
    old='ATLAS_FLOW_SNAPSHOT_'+suffix+'_MB'
    monkeypatch.delenv(new,raising=False);monkeypatch.delenv(old,raising=False)
    assert flow._quota_mb(new,default)==default
    monkeypatch.setenv(old,'3')
    assert flow._quota_mb(new,default)==3
    monkeypatch.setenv(new,'03')
    assert flow._quota_mb(new,default)==3
    monkeypatch.setenv(new,'4')
    with pytest.raises(ValueError,match='新旧配额配置冲突'):flow._quota_mb(new,default)
    monkeypatch.delenv(new);monkeypatch.setenv(old,'0')
    with pytest.raises(ValueError,match='配额配置无效'):flow._quota_mb(new,default)


def test_snapshot_usage_reports_legacy_quota_limits(monkeypatch):
    for key in ('TOTAL','ACTOR','PROJECT'):
        monkeypatch.delenv('NEXUS_FLOW_SNAPSHOT_'+key+'_MB',raising=False)
        monkeypatch.setenv('ATLAS_FLOW_SNAPSHOT_'+key+'_MB','2')
    ctx=context();usage=flow.snapshot_storage_usage(ctx['project_id'],ctx['actor_id'])
    assert [usage[k] for k in ('global_limit_bytes','actor_limit_bytes','project_limit_bytes')]==[2*1024*1024]*3
    monkeypatch.setenv('NEXUS_FLOW_SNAPSHOT_PROJECT_MB','3')
    with pytest.raises(ValueError,match='新旧配额配置冲突'):
        flow._save_rows_paged(ctx,[{'x':'1'}],canonical([{'x':'1'}]))
