"""Real local files stay root-bounded; imports freeze bytes and exports verify data."""
import hashlib
import json
import os
import csv
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from agent_service import local_workspace as local, local_storage, workspace, office
from agent_service.config import DATA
from agent_service.local_api import router
from agent_service.store import all_items,delete,get,now,put,seal,uid,unseal


def link_file(alias,target):
    try:alias.symlink_to(target)
    except OSError:
        if os.name!='nt':raise
        # Standard Windows users cannot create symlinks; an actual hard link
        # still verifies refusal to read/write an aliased independent artifact.
        os.link(target,alias)


def link_directory(alias,target):
    try:alias.symlink_to(target,target_is_directory=True)
    except OSError:
        if os.name!='nt':raise
        # Junction creation needs no developer mode or elevated privileges.
        quote=lambda value:"'"+str(value).replace("'","''")+"'"
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
            'New-Item -ItemType Junction -Path '+quote(alias)+' -Target '+quote(target)+' | Out-Null'],
            capture_output=True,timeout=15)
        assert result.returncode==0,result.stderr.decode(errors='replace')


def storage_identity():
    return (None,None) if os.name=='nt' else (os.getuid(),os.getgid())


@pytest.fixture
def local_folder(tmp_path,monkeypatch):
    selected=tmp_path/'个人 业务文件夹';selected.mkdir()
    monkeypatch.setenv('ATLAS_LOCAL_WORKSPACE',str(selected))
    monkeypatch.setenv('ATLAS_LOCAL_MODE','1')
    monkeypatch.setenv('ATLAS_LOCAL_LABEL','我的月结')
    monkeypatch.setattr('agent_service.execution.enqueue',lambda *args,**kwargs:{'id':'fixture-probe'})
    pid='p_'+uid();put('project',{'name':'本地模式验收','created_at':now()},pid)
    uploads=set((DATA/'uploads').glob('*')) if (DATA/'uploads').exists() else set()
    yield selected,pid
    for kind in ('source','source_version','local_import','local_export','office_artifact','business_report','data_workflow','pipeline'):
        for item in all_items(kind):
            if item.get('project_id')==pid:delete(kind,item['id'])
    delete('project',pid)
    for file in (DATA/'uploads').glob('*'):
        if file not in uploads:file.unlink(missing_ok=True)


def test_root_disabled_is_not_a_remote_arbitrary_filesystem(monkeypatch):
    monkeypatch.delenv('ATLAS_LOCAL_WORKSPACE',raising=False)
    assert local.status()=={'enabled':False,'mode':'server'}
    with pytest.raises(ValueError,match='未启用'):local.files('')


def test_local_startup_rejects_incomplete_mode_before_creating_storage(tmp_path,monkeypatch):
    from agent_service import cloud
    selected=tmp_path/'业务';selected.mkdir()
    monkeypatch.setenv('ATLAS_LOCAL_WORKSPACE',str(selected))
    monkeypatch.delenv('ATLAS_LOCAL_MODE',raising=False)
    with pytest.raises(ValueError,match='both ATLAS_LOCAL_MODE'):cloud.prepare()
    assert list(selected.iterdir())==[]


def test_local_startup_rejects_incomplete_remote_backup_before_storage(tmp_path,monkeypatch):
    from agent_service import cloud
    selected=tmp_path/'业务';selected.mkdir()
    monkeypatch.setenv('ATLAS_LOCAL_WORKSPACE',str(selected))
    monkeypatch.setenv('ATLAS_LOCAL_MODE','1')
    for key in ('ATLAS_S3_ENDPOINT','ATLAS_S3_BUCKET','ATLAS_S3_ACCESS_KEY_ID','ATLAS_S3_SECRET_ACCESS_KEY','ATLAS_BACKUP_KEY'):
        monkeypatch.setenv(key,'fixture-not-used')
    with pytest.raises(ValueError,match='旧格式远程备份'):cloud.prepare()
    assert list(selected.iterdir())==[]


def test_local_browse_is_paginated_and_omits_credentials_and_managed_outputs(local_folder):
    root,pid=local_folder
    (root/'收入.csv').write_text('编号,金额\n1,100.25\n')
    (root/'成本.csv').write_text('id,cost\n1,30.05\n')
    (root/'子目录').mkdir()
    for name in ('.env','llm.json','secrets.json','connector.key','unknown.exe'):(root/name).write_text('PRIVATE')
    (root/local.OUTPUT_DIR).mkdir()
    response=local.files('',0,2)
    assert response['total']==3 and response['next_offset']==2
    names={item['name'] for item in response['entries']+local.files('',2,2)['entries']}
    assert names=={'收入.csv','成本.csv','子目录'}
    assert local.status()['label']=='我的月结'


@pytest.mark.parametrize('path',['../outside.csv','/etc/passwd','a/../income.csv','.atlas/metadata.sqlite','llm.json','secrets.json','x\\income.csv','Atlas成果/export.csv'])
def test_input_paths_never_reach_private_or_parent_files(local_folder,path):
    with pytest.raises(ValueError):local._copy_input(path)


def test_symlink_and_hardlink_inputs_are_not_read(local_folder,tmp_path):
    root,pid=local_folder
    private=tmp_path/'outside.csv';private.write_text('PRIVATE')
    link_file(root/'shortcut.csv',private)
    link_directory(root/'linked-dir',tmp_path)
    os.link(private,root/'hardlink.csv')
    assert local.files()['entries']==[]
    for name in ('shortcut.csv','linked-dir/outside.csv','hardlink.csv'):
        with pytest.raises((ValueError,OSError)):local._copy_input(name)
    assert private.read_text()=='PRIVATE'


def test_two_file_import_freezes_bytes_and_retries_same_request(local_folder):
    root,pid=local_folder
    (root/'收入.csv').write_bytes(b'id,amount\n1,100.25\n')
    (root/'成本.csv').write_bytes(b'id,cost\n1,30.05\n')
    result=local.import_files(pid,['收入.csv','成本.csv'],'request-abcdefgh')
    assert len(result['sources'])==2 and not result['failures']
    source=get('source',result['sources'][0]['id'])
    assert source['revision_id'] and source['local_origin']['path']=='收入.csv'
    stored=DATA/'uploads'/unseal(source['secret_config'])['stored_name']
    assert stored.read_bytes()==b'id,amount\n1,100.25\n'
    (root/'收入.csv').write_bytes(b'id,amount\n1,999.99\n')
    assert local.import_files(pid,['收入.csv','成本.csv'],'request-abcdefgh')==result
    later=local.import_files(pid,['收入.csv'],'request-new-version')
    assert later['sources'][0]['id']!=source['id']
    assert stored.read_bytes()==b'id,amount\n1,100.25\n'
    assert get('source',source['id'])['content_hash']==hashlib.sha256(stored.read_bytes()).hexdigest()
    with pytest.raises(ValueError,match='同一导入标识'):local.import_files(pid,['成本.csv'],'request-abcdefgh')


def test_partial_import_retains_success_and_does_not_fake_complete(local_folder):
    root,pid=local_folder;(root/'valid.csv').write_text('id,amount\n1,2\n')
    response=local.import_files(pid,['valid.csv','missing.csv'],'partial-request-123')
    assert response['partial'] is True and len(response['sources'])==1
    assert response['failures'][0]['path']=='missing.csv'
    assert response['originals_overwritten'] is False


def test_local_import_request_ids_are_scoped_to_verified_actor(local_folder):
    from agent_service.actor_context import actor_scope
    root,pid=local_folder;file=root/'valid.csv';file.write_text('id,amount\n1,2\n')
    first=local.import_files(pid,['valid.csv'],'personal-request-123')
    file.write_text('id,amount\n1,3\n')
    with actor_scope('u_'+'1'*64):
        second=local.import_files(pid,['valid.csv'],'personal-request-123')
    assert second['sources'][0]['content_hash']!=first['sources'][0]['content_hash']
    assert local.import_files(pid,['valid.csv'],'personal-request-123')==first


def test_input_mutated_during_read_is_rejected(local_folder,monkeypatch):
    root,pid=local_folder;file=root/'input.csv';file.write_text('id,n\n1,2\n',newline='\n')
    original=local.os.fstat;calls=0
    def fstat(descriptor):
        nonlocal calls
        value=original(descriptor)
        if value.st_size==9 and stat_regular(value.st_mode):
            calls+=1
            if calls==2:file.write_text('id,n\n1,9\n',newline='\n')
        return original(descriptor)
    monkeypatch.setattr(local.os,'fstat',fstat)
    with pytest.raises(ValueError,match='变化|替换'):local._copy_input('input.csv')


def stat_regular(mode):
    import stat
    return stat.S_ISREG(mode)


def test_live_project_code_uses_real_selected_folder_and_rejects_links(local_folder,tmp_path):
    root,pid=local_folder
    local.write_project_file(pid,'reports/summary.md','收入100.25元')
    target=root/local.CODE_DIR/pid/'files/reports/summary.md'
    assert target.read_text()=='收入100.25元'
    assert local.read_project_file(pid,'reports/summary.md')=='收入100.25元'
    assert local.list_project_files(pid)==[{'path':'reports/summary.md','bytes':len('收入100.25元'.encode())}]
    outside=tmp_path/'private.md';outside.write_text('PRIVATE')
    target.unlink();link_file(target,outside)
    with pytest.raises((ValueError,OSError)):local.read_project_file(pid,'reports/summary.md')
    with pytest.raises((ValueError,OSError)):local.write_project_file(pid,'reports/summary.md','overwrite')
    assert outside.read_text()=='PRIVATE'


def test_local_export_actual_code_and_verified_office_without_overwriting_originals(local_folder,monkeypatch):
    root,pid=local_folder
    local.write_project_file(pid,'reports/summary.md','收入100.25元')
    monkeypatch.setattr(workspace,'list_files',local.list_project_files)
    monkeypatch.setattr(workspace,'read_file',local.read_project_file)
    monkeypatch.setattr(workspace,'path_for',lambda project,name:root/local.CODE_DIR/project/'files'/name)
    deliverable=DATA/'deliverables'/'local-fixture.docx';deliverable.parent.mkdir(exist_ok=True)
    from docx import Document
    document=Document();document.add_paragraph('fixture-only-verified-content');document.save(deliverable)
    original_bytes=deliverable.read_bytes()
    aid='office_'+uid();put('office_artifact',{'project_id':pid,'relative_path':str(deliverable.relative_to(DATA)),
        'sha256':hashlib.sha256(deliverable.read_bytes()).hexdigest(),'name':'monthly.docx'},aid)
    try:
        first=local.export_project(pid);second=local.export_project(pid)
        assert first['complete'] and first['files']==2 and first['relative_path']!=second['relative_path']
        folder=root/first['relative_path'];manifest=json.loads((folder/'manifest.json').read_text())
        assert (folder/'code/reports/summary.md').read_text()=='收入100.25元'
        exported=folder/('office/'+aid+'-monthly.docx')
        assert exported.read_bytes()==deliverable.read_bytes()
        assert manifest['originals_overwritten'] is False
        receipt=local.verify_export(pid,first['id'])
        assert receipt['complete'] and receipt['files']==2 and receipt['manifest_sha256']==hashlib.sha256((folder/'manifest.json').read_bytes()).hexdigest()
        deliverable.write_bytes(b'tampered')
        third=local.export_project(pid)
        assert not third['complete'] and third['failures'][0]['kind']=='office'
        with pytest.raises(ValueError,match='未完成'):local.verify_export(pid,third['id'])
        assert exported.read_bytes()==original_bytes
        exported.write_bytes(b'tampered-saved-export')
        with pytest.raises(ValueError,match='大小|摘要'):local.verify_export(pid,first['id'])
    finally:deliverable.unlink(missing_ok=True)


def test_local_export_receipt_rejects_edited_manifest_extra_files_and_links(local_folder,tmp_path):
    root,pid=local_folder;local.write_project_file(pid,'summary.md','100.25')
    result=local.export_project(pid);folder=root/result['relative_path']
    assert local.verify_export(pid,result['id'])['complete']
    manifest=folder/'manifest.json';raw=manifest.read_bytes()
    manifest.write_text('{}')
    with pytest.raises(ValueError,match='清单'):local.verify_export(pid,result['id'])
    manifest.write_bytes(raw)
    (folder/'extra.txt').write_text('unrecorded')
    with pytest.raises(ValueError,match='新增'):local.verify_export(pid,result['id'])
    (folder/'extra.txt').unlink()
    outside=tmp_path/'outside';outside.mkdir();link_directory(folder/'shortcut',outside)
    with pytest.raises(ValueError,match='链接'):local.verify_export(pid,result['id'])
    with pytest.raises(ValueError,match='标识'):local.verify_export(pid,'../exports')


def test_local_export_copies_existing_verified_report_office_without_nested_locks(local_folder):
    import shutil
    from agent_service import business_reports
    root,pid=local_folder;rid='brief_'+uid()
    folder=DATA/'business-reports'/pid/rid;folder.mkdir(parents=True)
    payload={'recipe':{'title':'月结实际与预算','period':'2026-09','assumptions':[]},
        'evaluation':{'assertions':[{'label':'净收入','value':'300.000002','unit':'CNY','rows':2}],
                      'input_rows':2,'selected_rows':2},
        'run_id':'fixture-run','handoff_sha256':'a'*64,'plan_sha256':'b'*64}
    manifest=json.dumps(payload,ensure_ascii=False).encode();markdown='净收入300.000002'.encode()
    (folder/'manifest.json').write_bytes(manifest);(folder/'report.md').write_bytes(markdown)
    put('business_report',{'project_id':pid,'artifact_dir':str(folder.relative_to(DATA)),
        'manifest_sha256':hashlib.sha256(manifest).hexdigest(),
        'markdown_sha256':hashlib.sha256(markdown).hexdigest(),'office_exports':{},'created_at':now()},rid)
    try:
        docx=business_reports.report_file(pid,rid,'docx')
        pptx=business_reports.report_file(pid,rid,'pptx')
        original_docx=docx.read_bytes();original_pptx=pptx.read_bytes()
        exported=local.export_project(pid)
        assert exported['complete'] and exported['files']==4
        destination=root/exported['relative_path']/'reports'/rid
        assert (destination/'report.docx').read_bytes()==original_docx
        assert (destination/'report.pptx').read_bytes()==original_pptx
        assert local.verify_export(pid,exported['id'])['complete']
        docx.write_bytes(b'tampered-cache')
        partial=local.export_project(pid)
        assert not partial['complete'] and partial['failures']==[{
            'kind':'report','id':rid,'format':'docx','error':'该报告未通过完整性核验，未导出'}]
        assert (destination/'report.docx').read_bytes()==original_docx
    finally:shutil.rmtree(folder.parent)


def test_local_routes_require_workspace_auth(local_folder):
    def auth(authorization:str=Header(default='')):
        if authorization!='Bearer local-test':raise HTTPException(401,'missing')
    app=FastAPI();app.include_router(router(auth));client=TestClient(app)
    assert client.get('/api/local-workspace').status_code==401
    assert client.get('/api/local-workspace',headers={'Authorization':'Bearer local-test'}).json()['enabled']


def test_local_application_export_is_complete_decimal_and_spreadsheet_literal(local_folder,monkeypatch):
    import duckdb
    from agent_service import jobs,releases
    root,pid=local_folder
    pipeline_id='pipe_'+uid();run_id='run_'+uid()
    folder=DATA/'runs'/run_id;folder.mkdir(parents=True)
    parquet=folder/'business_objects.parquet'
    with duckdb.connect() as db:
        db.execute('CREATE TABLE objects(object_id VARCHAR, customer VARCHAR, amount DECIMAL(38,12))')
        db.executemany('INSERT INTO objects VALUES (?,?,?)',[(str(i),'=HYPERLINK("unsafe")' if i==0 else 'customer-'+str(i),Decimal('0.100000000001')) for i in range(40)])
        db.execute('COPY objects TO ? (FORMAT PARQUET)',[str(parquet)])
    commit='a'*40
    manifest={'run_id':run_id,'pipeline_id':pipeline_id,'commit':commit,'parquet_hash':hashlib.sha256(parquet.read_bytes()).hexdigest(),
              'fields':[{'name':'object_id','label':'编号'},{'name':'customer','label':' =客户'},{'name':'amount','label':'金额'}]}
    (folder/'output.json').write_text(json.dumps(manifest))
    run={'id':run_id,'kind':'pipeline','target':pipeline_id,'commit':commit,'validated':True,'status':'validated','artifact_dir':str(folder.relative_to(DATA))}
    put('pipeline',{'project_id':pid,'published_run':run_id,'candidate_run':None},pipeline_id)
    monkeypatch.setattr(jobs,'get_job',lambda key:run if key==run_id else None)
    validated=[]
    monkeypatch.setattr(releases,'verify_semantics',lambda *args:validated.append(args[1]['id']))
    result=local.export_project(pid)
    exported=root/result['relative_path']/'applications'/pipeline_id/run_id/'result.csv'
    with exported.open(encoding='utf-8-sig',newline='') as stream:rows=list(csv.DictReader(stream))
    assert len(rows)==40 and sum(Decimal(row['金额']) for row in rows)==Decimal('4.000000000040')
    assert rows[0]["' =客户"].startswith("'=HYPERLINK")
    assert validated==[run_id] and result['complete'] is True
    parquet.write_bytes(b'tampered')
    later=local.export_project(pid)
    assert not later['complete'] and later['failures'][0]['kind']=='application'
    assert len(rows)==40 # Existing user-visible snapshots are not rewritten.
    import shutil
    shutil.rmtree(folder)


def test_local_storage_is_no_follow_and_never_chowns_originals(tmp_path,monkeypatch):
    root=tmp_path/'work';root.mkdir();original=root/'income.csv';original.write_text('private')
    before=original.stat()
    monkeypatch.setenv('ATLAS_LOCAL_WORKSPACE',str(root));monkeypatch.setenv('DATA_DIR',str(root/'.atlas'))
    path=local_storage.prepare_storage(*storage_identity())
    assert path==root/'.atlas' and (path/'local-workspace.identity').is_file()
    assert local_storage.prepare_storage(*storage_identity())==path
    after=original.stat();assert (after.st_uid,after.st_gid,after.st_mode)==(before.st_uid,before.st_gid,before.st_mode)


def test_local_storage_rejects_metadata_symlink_before_any_target_write(tmp_path,monkeypatch):
    root=tmp_path/'work';root.mkdir();outside=tmp_path/'outside';outside.mkdir()
    link_directory(root/'.atlas',outside)
    monkeypatch.setenv('ATLAS_LOCAL_WORKSPACE',str(root));monkeypatch.setenv('DATA_DIR',str(root/'.atlas'))
    with pytest.raises(ValueError):local_storage.prepare_storage(*storage_identity())
    assert list(outside.iterdir())==[]


def test_local_backup_refuses_incomplete_format_before_output_with_real_code(local_folder,tmp_path,monkeypatch):
    from agent_service import maintenance
    root,pid=local_folder
    local.write_project_file(pid,'workflows/month.sql','SELECT 100.25 AS net_revenue')
    code=root/local.CODE_DIR/pid/'files/workflows/month.sql'
    assert code.is_file() and not code.is_relative_to(DATA)
    monkeypatch.setattr(maintenance,'guard',lambda **kwargs:pytest.fail('本地旧格式备份不应进入数据库或文件锁阶段'))
    output=tmp_path/'not-created'/'workspace.zip'
    with pytest.raises(ValueError,match='format2.*整个所选工作空间目录'):
        maintenance.backup(output)
    assert not output.parent.exists()
    assert code.read_text()=='SELECT 100.25 AS net_revenue'
    # Reject even a malformed/legacy local configuration rather than silently
    # treating the selected folder's sibling code as part of a DATA-only ZIP.
    monkeypatch.delenv('ATLAS_LOCAL_MODE')
    with pytest.raises(ValueError,match='format2'):maintenance.backup(output)
    assert not output.parent.exists()


def test_local_restore_refuses_old_layout_before_archive_or_database_access(local_folder,tmp_path,monkeypatch):
    from agent_service import maintenance
    root,pid=local_folder;local.write_project_file(pid,'summary.md','现有本地成果')
    code=root/local.CODE_DIR/pid/'files/summary.md';before=code.read_bytes()
    monkeypatch.setattr(maintenance,'guard',lambda **kwargs:pytest.fail('本地旧格式恢复不应进入数据库或文件锁阶段'))
    with pytest.raises(ValueError,match='format2.*停止Atlas服务'):
        maintenance.restore(tmp_path/'archive-does-not-exist.zip')
    assert code.read_bytes()==before
    monkeypatch.delenv('ATLAS_LOCAL_WORKSPACE')
    with pytest.raises(ValueError,match='format2'):maintenance.restore(tmp_path/'archive-does-not-exist.zip')
