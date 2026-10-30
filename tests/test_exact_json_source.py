import hashlib
import pytest
from agent_service.connectors import exact_json,read_source,SourceError
from agent_service.config import DATA
from agent_service.store import seal,uid


@pytest.mark.parametrize('extension,raw',[
    ('.json',b'[{"id":"A","amount":9007199254740.000001}]'),
    ('.jsonl',b'{"id":"A","amount":9007199254740.000001}\n'),
])
def test_source_json_decimal_token_survives_full_read_without_float(extension,raw):
    name=uid()+extension;(DATA/'uploads').mkdir(exist_ok=True);(DATA/'uploads'/name).write_bytes(raw)
    rows,fields,_,truncated=read_source({'id':'precision-test','kind':'file','name':'values'+extension,
                                       'secret_config':seal({'stored_name':name}),
                                       'content_hash':hashlib.sha256(raw).hexdigest()})
    assert rows[0]['c_2']=='9007199254740.000001' and not truncated


@pytest.mark.parametrize('raw,code',[
    ('{"amount":1,"amount":2}','DUPLICATE_JSON_FIELD'),
    ('{"amount":NaN}','INVALID_JSON_NUMBER'),
    ('{"amount":Infinity}','INVALID_JSON_NUMBER'),
])
def test_ambiguous_or_nonfinite_json_never_silently_changes_a_value(raw,code):
    with pytest.raises(SourceError) as caught:exact_json(raw)
    assert caught.value.code==code


def _file_source(raw,extension='.json',**config):
    name=uid()+extension;(DATA/'uploads').mkdir(exist_ok=True);(DATA/'uploads'/name).write_bytes(raw)
    return {'id':'s_'+uid(),'kind':'file','name':'document'+extension,
            'secret_config':seal({'stored_name':name,**config}),
            'content_hash':hashlib.sha256(raw).hexdigest()}


@pytest.mark.parametrize('raw',[
    b'{"meta":{"currency":"CNY","amount":9007199254740.000001},"data":[{"id":1}],"other":[{"id":2}]}',
    b'{}',b'[]',b'[1,2,{"x":3}]',b'"plain text"',b'null',
])
def test_valid_non_tabular_json_is_kept_as_complete_original_document(raw):
    source=_file_source(raw)
    for limit in (1,100):
        rows,fields,evidence,truncated=read_source(source,limit=limit)
        assert fields[0]['label']=='JSON原文'
        assert rows[0]['c_1']==raw.decode('utf-8')
        assert evidence['document_only'] is True and evidence['semantic_review_required'] is True
        assert evidence['warnings'][0]['code']=='JSON_DOCUMENT_NOT_TABLE'
        assert evidence['source_integrity']=='verified' and not truncated


def test_json_array_nested_decimals_remain_json_numbers_and_keep_all_subtrees():
    from decimal import Decimal
    raw=b'[{"id":"A","data":{"amount":9007199254740.000001,"items":[{"v":0.1234567890123456789}]},"another":[1,2]}]'
    rows,fields,evidence,_=read_source(_file_source(raw))
    by_label={field['label']:field['name'] for field in fields}
    nested=exact_json(rows[0][by_label['data']])
    assert nested=={'amount':Decimal('9007199254740.000001'),'items':[{'v':Decimal('0.1234567890123456789')}]}
    assert exact_json(rows[0][by_label['another']])==[1,2]
    assert evidence['parser']=='json'


def test_json_bom_and_errors_have_clear_locations_without_exposing_contents():
    rows,fields,_,_=read_source(_file_source(b'\xef\xbb\xbf{"name":"value"}'))
    assert fields[0]['label']=='JSON原文' and rows[0]['c_1']=='{"name":"value"}'
    for raw,extension,code in [(b'{"private":}', '.json','JSON_SYNTAX_INVALID'),(b'{"x":1}\n42\n','.jsonl','JSONL_RECORD_INVALID')]:
        with pytest.raises(SourceError) as caught:read_source(_file_source(raw,extension))
        assert caught.value.code==code and 'private' not in str(caught.value)
        assert ('第 1 行' if extension=='.json' else '第 2 行') in str(caught.value)
    with pytest.raises(SourceError) as caught:read_source(_file_source(b'{"data":{}}',records_path='data'))
    assert caught.value.code=='JSON_RECORDS_PATH_INVALID'


def test_actual_upload_and_probe_preserve_nested_json_document():
    from test_end_to_end import upload
    from agent_service.connectors import verified_file_source
    raw=b'{"meta":{"currency":"CNY"},"data":[{"amount":9007199254740.000001}],"other":[1,2]}'
    source=upload(raw,'record-document.json')
    assert source['status']=='ready' and source['profile']['evidence']['document_only']
    assert verified_file_source(source)[1]==raw
    rows,fields,_,_=read_source(source)
    assert fields[0]['label']=='JSON原文' and rows[0]['c_1'].encode()==raw


def test_legacy_table_ingest_refuses_document_projection_before_processing(tmp_path,monkeypatch):
    import json
    from agent_service import rules,runner
    from agent_service.errors import TaskError
    source=_file_source(b'{"data":[{"amount":12.50}],"other":[{"amount":100}]}')
    run=tmp_path/'run';code=tmp_path/'code';run.mkdir();code.mkdir()
    (run/'sources.json').write_text(json.dumps({source['id']:source}),encoding='utf-8')
    (code/'plan.json').write_text(json.dumps({'source_id':source['id'],'spec':{},'schema_labels':['JSON原文'],'schema_names':['c_1']}),encoding='utf-8')
    monkeypatch.setattr(rules,'validate_legacy_numeric',lambda *args,**kwargs:None)
    with pytest.raises(TaskError) as caught:runner.ingest(run,code)
    assert caught.value.code=='JSON_DOCUMENT_NOT_TABLE'
    assert not (run/'warehouse.duckdb').exists()


def test_explicit_json_record_path_is_recorded_and_only_explicitly_selected():
    raw=b'{"data":{"items":[{"amount":123.450001}]},"other":[{"amount":999}],"total":1}'
    rows,fields,evidence,_=read_source(_file_source(raw,records_path='data.items'))
    assert rows[0]['c_1']=='123.450001' and fields[0]['label']=='amount'
    assert evidence['records_path']=='data.items' and not evidence.get('document_only')


def test_real_multi_attachment_message_probes_all_files_including_nested_json(monkeypatch):
    from fastapi.testclient import TestClient
    from agent_service import api,conversation,execution,model_catalog
    from agent_service.config import TOKEN
    from agent_service.store import get
    from agent_service.connectors import verified_file_source
    monkeypatch.setattr(model_catalog,'select',lambda *a,**k:{})
    headers={'Authorization':'Bearer '+TOKEN}
    inputs=[('record.json',b'{"metadata":{"unit":"CNY"},"data":[{"amount":9007199254740.000001}],"other":[1,2]}'),
            ('orders.csv',b'id,amount\nA,12.50\n'),('notes.txt','两份资料共同核对。\n'.encode())]
    with TestClient(api.app) as client:
        result=client.post('/api/projects',headers=headers,json={'name':'JSON mixed multi-upload','request_id':'project-'+uid()})
        assert result.status_code==200,result.text
        pid=result.json()['id'];ids=[]
        for name,content in inputs:
            result=client.post('/api/projects/'+pid+'/attachments',headers=headers,
                files={'file':(name,content,'application/octet-stream')},data={'request_id':'upload-'+uid()})
            assert result.status_code==200,result.text
            ids.append(result.json()['id'])
        result=client.post('/api/projects/'+pid+'/messages',headers=headers,json={
            'text':'结合三份资料核对','source_ids':ids,'interaction_mode':'work','request_id':'message-'+uid()})
        assert result.status_code==200,result.text
        turn=get('turn',result.json()['id'])
        assert turn['source_ids']==ids and not conversation.prepare_attachments(turn)
        for _ in range(20):
            if all(get('source',sid)['status']=='ready' for sid in ids):break
            assert execution.process_once()
        sources=[get('source',sid) for sid in ids]
        assert [source['status'] for source in sources]==['ready']*3
        assert sources[0]['profile']['evidence']['document_only'] is True
        assert conversation.prepare_attachments(get('turn',turn['id']))
        assert [item['id'] for item in get('turn',turn['id'])['attached_sources']]==ids
        assert [verified_file_source(source)[1] for source in sources]==[content for _,content in inputs]
