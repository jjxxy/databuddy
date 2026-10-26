"""Frozen source revisions must identify the bytes actually consumed."""
import hashlib

import pytest

from agent_service import connectors, project_data as data
from agent_service.execution import process_once
from agent_service.jobs import get_job
from agent_service.config import DATA
from agent_service.store import get, patch, seal, uid
from test_project_data import csv_bytes, document, project, registered, replace_source, run, source


def test_replaced_source_bytes_block_read_and_refresh_without_changing_formal_result():
    pid=project();original=source(pid);wid=registered(pid,document(original['id']))
    first=run(pid,wid);data.publish_result(pid,first['id'],first['commit'])
    old_page=data.read_result(pid,first['id'])
    refresh=data.enqueue_workflow(pid,wid,use_published=True)
    path=data.source_path(original)
    path.write_bytes(csv_bytes(amount_offset=1000))
    with pytest.raises(connectors.SourceError) as caught:data.preview_source(pid,{'source_id':original['id']})
    assert caught.value.code=='SOURCE_CONTENT_CHANGED'
    with pytest.raises(connectors.SourceError) as caught:connectors.read_source(original)
    assert caught.value.code=='SOURCE_CONTENT_CHANGED'
    assert process_once()
    failed=get_job(refresh['id'])
    assert failed['status']=='failed' and failed['error_code']=='SOURCE_CONTENT_CHANGED'
    assert get('data_workflow',wid)['published_run']==first['id']
    preserved=data.read_result(pid,first['id'])
    assert preserved['manifest_sha256']==old_page['manifest_sha256'] and preserved['rows']==old_page['rows']
    # A normal new immutable upload/revision remains usable with the same plan.
    updated=replace_source(original['id'],csv_bytes(amount_offset=1))
    second=run(pid,wid,use_published=True)
    assert second['status']=='succeeded'
    assert second['source_revisions'][original['id']]==updated['revision']
    assert get('data_workflow',wid)['published_run']==second['id']
    assert data.read_result(pid,first['id'])['rows']==old_page['rows']


@pytest.mark.parametrize('missing_hash',[None,'','not-a-sha256'])
def test_legacy_or_invalid_hash_cannot_be_silently_upgraded_to_a_verified_workflow(missing_hash):
    pid=project();current=source(pid)
    legacy=patch('source',current['id'],content_hash=missing_hash)
    with pytest.raises(connectors.SourceError) as caught:data.source_path(legacy)
    assert caught.value.code=='SOURCE_HASH_MISSING'
    with pytest.raises(connectors.SourceError) as caught:connectors.read_source(legacy)
    assert caught.value.code=='SOURCE_HASH_MISSING'
    if missing_hash is None:
        rows,_,evidence,_=connectors.read_source(legacy,limit=1)
        assert rows and evidence['source_integrity']=='unverified_legacy_source'
        assert evidence['warnings'][0]['code']=='SOURCE_HASH_MISSING'
    wid=registered(pid,document(current['id']))
    queued=data.enqueue_workflow(pid,wid);assert process_once()
    assert get_job(queued['id'])['error_code']=='SOURCE_HASH_MISSING'
    assert get('data_workflow',wid).get('published_run') is None


def test_csv_parser_uses_verified_buffer_and_workflow_checks_replacement_during_parse(monkeypatch):
    pid=project();current=source(pid);path=data.source_path(current)
    original=path.read_bytes();replacement=csv_bytes(amount_offset=1000)
    reader=connectors.csv.DictReader;buffers=[]
    def replace_before_parse(*args,**kwargs):
        buffers.append(args[0].getvalue())
        path.write_bytes(replacement)
        return reader(*args,**kwargs)
    monkeypatch.setattr(connectors.csv,'DictReader',replace_before_parse)
    with pytest.raises(connectors.SourceError) as caught:connectors.read_source(current)
    assert caught.value.code=='SOURCE_CONTENT_CHANGED'
    assert buffers==[original.decode('utf-8-sig')]
    path.write_bytes(original)
    with pytest.raises(connectors.SourceError) as caught:
        data.read_input(pid,{'source_id':current['id']})
    assert caught.value.code=='SOURCE_CONTENT_CHANGED'


@pytest.mark.parametrize('extension',['.csv','.json','.jsonl','.html','.txt','.xlsx','.docx','.pptx','.pdf','.png'])
def test_all_file_adapter_entrypoints_reject_hash_mismatch_even_during_probe(extension):
    raw=b'frozen source bytes';name=uid()+extension
    path=DATA/'uploads'/name;path.parent.mkdir(exist_ok=True);path.write_bytes(raw)
    current={'id':'s_'+uid(),'kind':'file','name':'source'+extension,
        'secret_config':seal({'stored_name':name}),'content_hash':hashlib.sha256(raw).hexdigest()}
    path.write_bytes(b'replaced source bytes')
    for limit in (1,connectors.MAX_ROWS):
        with pytest.raises(connectors.SourceError) as caught:connectors.read_source(current,limit=limit)
        assert caught.value.code=='SOURCE_CONTENT_CHANGED'
    current.pop('content_hash')
    with pytest.raises(connectors.SourceError) as caught:connectors.read_source(current)
    assert caught.value.code=='SOURCE_HASH_MISSING'


def test_legacy_workbook_probe_is_explicitly_unverified_and_full_read_is_blocked(tmp_path):
    from test_workbook_connector import source as workbook_source
    current,_=workbook_source(tmp_path,[['金额'],[10]])
    current.pop('content_hash')
    rows,_,evidence,_=connectors.read_source(current,limit=1)
    assert rows[0]['c_1']=='10' and evidence['source_integrity']=='unverified_legacy_source'
    assert any(item['code']=='SOURCE_HASH_MISSING' for item in evidence['warnings'])
    with pytest.raises(connectors.SourceError) as caught:connectors.read_source(current)
    assert caught.value.code=='SOURCE_HASH_MISSING'


def test_workbook_replacement_during_path_parsing_is_rejected(monkeypatch,tmp_path):
    from agent_service import workbook_reader
    from test_workbook_connector import source as workbook_source
    current,_=workbook_source(tmp_path,[['金额'],[10]])
    path=data.source_path(current);read=workbook_reader.read_table
    def replace_after_read(*args,**kwargs):
        result=read(*args,**kwargs)
        path.write_bytes(path.read_bytes()+b'replaced-during-parse')
        return result
    monkeypatch.setattr(workbook_reader,'read_table',replace_after_read)
    with pytest.raises(connectors.SourceError) as caught:connectors.read_source(current)
    assert caught.value.code=='SOURCE_CONTENT_CHANGED'


def test_legacy_pipeline_cannot_adopt_replaced_workbook_source(tmp_path):
    from openpyxl import Workbook
    from agent_service.execution import enqueue
    from test_end_to_end import upload, create
    book=Workbook();sheet=book.active
    sheet.append(['订单号','客户','金额']);sheet.append(['A1','甲',10])
    file=tmp_path/'legacy.xlsx';book.save(file)
    original_bytes=file.read_bytes()
    current=upload(original_bytes,'integrity-legacy.xlsx');pipeline=create(current)
    previous=get('pipeline',pipeline['id'])['published_run']
    assert previous
    queued=enqueue('pipeline',pipeline['id'],pipeline['commit'])
    sheet['C2']=999;book.save(file)
    stored=data.source_path(current);stored.write_bytes(file.read_bytes())
    assert process_once()
    failed=get_job(queued['id'])
    assert failed['status']=='failed' and failed['error_code']=='SOURCE_CONTENT_CHANGED'
    assert get('pipeline',pipeline['id'])['published_run']==previous
    stored.write_bytes(original_bytes)
    patch('source',current['id'],content_hash=None)
    legacy=enqueue('pipeline',pipeline['id'],pipeline['commit']);assert process_once()
    assert get_job(legacy['id'])['error_code']=='SOURCE_HASH_MISSING'
    assert get('pipeline',pipeline['id'])['published_run']==previous
