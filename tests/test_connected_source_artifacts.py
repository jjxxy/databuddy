"""Connected source views show real, bounded samples without leaking config."""
import json
import sqlite3

from agent_service import connectors, workspace_artifacts as assets
from agent_service.store import now, patch, put, seal
from test_agent_handoff import project
from test_workspace_artifacts import api, clean


def connected(pid, kind, config):
    return put('source', {'project_id':pid, 'name':'业务数据', 'kind':kind,
                          'revision':1, 'secret_config':seal(config), 'created_at':now()})


def test_database_real_sample_is_bounded_project_scoped_and_versioned(api,tmp_path):
    path=tmp_path/'business.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('create table orders (order_id integer, amount text)')
        db.executemany('insert into orders values (?,?)',[(i,'0.10') for i in range(65)])
    source=connected(project(),'database',{'connection_string':'sqlite:///'+str(path),
                                           'table':'orders','read_only_ack':True})
    pid=source['project_id'];client,headers=api
    listing=client.get(f'/api/projects/{pid}/artifacts?kind=source',headers=headers).json()
    item=listing['items'][0]
    assert item['format']=='database' and item['download_url'] is None and 'sha256' not in item
    assert str(path) not in json.dumps(listing)
    direct=client.get(f'/api/projects/{pid}/sources/{source["id"]}/artifact?revision=1',headers=headers)
    assert direct.status_code==200 and direct.json()['id']==item['id']
    historical=client.get(f'/api/projects/{pid}/sources/{source["id"]}/artifact?revision=1&expected_sha256='+('a'*64),headers=headers)
    assert historical.status_code==409
    result=client.get(item['preview_url'],headers=headers)
    assert result.status_code==200,result.text
    body=result.json()
    assert [v['label'] for v in body['table']['columns']]==['order_id','amount']
    assert len(body['table']['rows'])==50 and body['table']['rows'][0]['c_2']=='0.10'
    assert body['next_offset'] is None and '不代表完整数据' in body['fidelity_note']
    assert 'orders' in body['fidelity_note'] and str(path) not in result.text
    assert client.get(item['preview_url']+'?offset=50',headers=headers).status_code==409
    assert client.get(item['preview_url'].replace(pid,project()),headers=headers).status_code==404
    patch('source',source['id'],revision=2)
    assert client.get(item['preview_url'],headers=headers).status_code==409


def test_api_never_discloses_config_or_raw_error_and_catalog_does_not_fetch(api,monkeypatch):
    secret='secret-test-header'
    source=connected(project(),'api',{'url':'https://example.com/data',
                                      'headers':{'Authorization':secret},'records_path':'data'})
    calls=[]
    def read(value,limit,allow_empty,expected_labels):
        calls.append(limit)
        return ([{'c_1':'real value','record_hash':'internal'}],
                [{'name':'c_1','label':'业务字段'}],{'cursor':secret},True)
    monkeypatch.setattr(connectors,'read_source',read)
    client,headers=api
    item=assets.catalog(source['project_id'],'source')['items'][0]
    assert calls==[] and secret not in json.dumps(item)
    response=client.get(item['preview_url'],headers=headers)
    assert response.status_code==200 and response.json()['table']['rows']==[{'c_1':'real value'}]
    assert calls==[50] and secret not in response.text and 'cursor' not in response.text
    def fail(*args,**kwargs):raise ValueError(secret)
    monkeypatch.setattr(connectors,'read_source',fail)
    response=client.get(item['preview_url'],headers=headers)
    assert response.status_code==409 and secret not in response.text
