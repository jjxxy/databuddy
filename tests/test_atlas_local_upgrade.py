"""Old local folders are reused without silently creating a second workspace."""
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

from agent_service import local_storage
import portable_runtime


def _make_legacy(root):
    (root/'.atlas').mkdir()
    (root/'.atlas'/'local-workspace.identity').write_text(json.dumps({'format':'atlas-local-v1'}))
    (root/'Atlas工作区').mkdir();(root/'Atlas成果').mkdir()
    (root/'Atlas工作区'/'keep.txt').write_text('original')
    (root/'.atlas'/'metadata.sqlite').write_bytes(b'old-metadata')


def test_old_storage_is_reused_without_moving_old_files(tmp_path,monkeypatch):
    root=tmp_path/'business';root.mkdir();_make_legacy(root)
    before=(root/'.atlas'/'metadata.sqlite').read_bytes()
    monkeypatch.setenv('NEXUS_LOCAL_WORKSPACE',str(root))
    monkeypatch.setenv('DATA_DIR',str(root/'.nexus'))
    monkeypatch.setenv('NEXUS_LOCAL_AUTO_STORAGE','1')
    result=local_storage.prepare_storage(os.getuid(),os.getgid())
    assert result==root/'.atlas' and not (root/'.nexus').exists()
    assert (root/'.atlas'/'metadata.sqlite').read_bytes()==before
    assert (root/'Atlas工作区'/'keep.txt').read_text()=='original'
    assert not (root/'Nexus WorkStudio工作区').exists()


def test_dual_metadata_or_other_managed_names_are_rejected_before_changes(tmp_path,monkeypatch):
    root=tmp_path/'business';root.mkdir();_make_legacy(root)
    monkeypatch.setenv('NEXUS_LOCAL_WORKSPACE',str(root));monkeypatch.setenv('NEXUS_LOCAL_AUTO_STORAGE','1')
    monkeypatch.setenv('DATA_DIR',str(root/'.nexus'))
    (root/'.nexus').mkdir()
    with pytest.raises(ValueError,match='同时存在'):local_storage.prepare_storage(os.getuid(),os.getgid())
    assert not (root/'.nexus'/'local-workspace.identity').exists()
    (root/'.nexus').rmdir();(root/'Nexus WorkStudio成果').mkdir()
    with pytest.raises(ValueError,match='同时存在'):local_storage.prepare_storage(os.getuid(),os.getgid())
    assert (root/'Atlas工作区'/'keep.txt').read_text()=='original'


def test_unmarked_existing_directories_and_bad_legacy_marker_are_not_modified(tmp_path,monkeypatch):
    root=tmp_path/'business';root.mkdir();(root/'Atlas工作区').mkdir()
    monkeypatch.setenv('NEXUS_LOCAL_WORKSPACE',str(root));monkeypatch.setenv('DATA_DIR',str(root/'.nexus'))
    with pytest.raises(ValueError,match='缺少元数据'):local_storage.prepare_storage(os.getuid(),os.getgid())
    assert not (root/'.nexus').exists()
    (root/'Atlas工作区').rmdir();(root/'.atlas').mkdir()
    (root/'.atlas'/'local-workspace.identity').write_text('{"format":"other"}')
    monkeypatch.setenv('DATA_DIR',str(root/'.atlas'))
    with pytest.raises(ValueError):local_storage.prepare_storage(os.getuid(),os.getgid())
    assert not (root/'.nexus').exists()


def test_legacy_managed_paths_cannot_be_imported_as_business_sources():
    from agent_service import local_workspace
    for item in ('Atlas工作区/example.csv','Atlas成果/result.csv','Nexus WorkStudio工作区/example.csv','Nexus WorkStudio成果/result.csv'):
        with pytest.raises(ValueError):local_workspace._parts(item)


def test_legacy_managed_paths_are_selected_from_old_data_dir(tmp_path):
    root=tmp_path/'folder';root.mkdir();_make_legacy(root)
    import sqlite3
    (root/'.atlas'/'metadata.sqlite').unlink()
    sqlite3.connect(root/'.atlas'/'metadata.sqlite').close()
    env=dict(os.environ,DATA_DIR=str(root/'.atlas'),NEXUS_LOCAL_MODE='1',NEXUS_LOCAL_WORKSPACE=str(root),
             DATABASE_URL='sqlite:///'+str(root/'.atlas'/'metadata.sqlite'))
    # Only inspect names; no code/data writes are required.
    source='from agent_service.local_workspace import CODE_DIR,OUTPUT_DIR; print(CODE_DIR+"|"+OUTPUT_DIR)'
    result=subprocess.run([sys.executable,'-c',source],env=env,text=True,capture_output=True,check=True)
    assert result.stdout.strip()=='Atlas工作区|Atlas成果'


def test_old_desktop_credentials_are_reused_without_rewriting(tmp_path):
    old={'APP_TOKEN':'a'*40,'ATLAS_OPERATOR_TOKEN':'b'*40}
    path=tmp_path/'credentials.json';path.write_text(json.dumps(old))
    before=path.read_bytes();value=portable_runtime.credentials(tmp_path)
    assert value['APP_TOKEN']==old['APP_TOKEN'] and value['NEXUS_OPERATOR_TOKEN']==old['ATLAS_OPERATOR_TOKEN']
    assert path.read_bytes()==before
    path.write_text(json.dumps({**old,'NEXUS_OPERATOR_TOKEN':'c'*40}))
    with pytest.raises(ValueError,match='冲突'):portable_runtime.credentials(tmp_path)


def test_cloud_local_auto_selects_old_metadata_and_database(tmp_path):
    root=tmp_path/'folder';root.mkdir();_make_legacy(root)
    import sqlite3
    (root/'.atlas'/'metadata.sqlite').unlink()
    sqlite3.connect(root/'.atlas'/'metadata.sqlite').close()
    env=dict(os.environ,APP_TOKEN='valid-token-123456789',NEXUS_LOCAL_MODE='1',NEXUS_LOCAL_AUTO_STORAGE='1',
             NEXUS_LOCAL_WORKSPACE=str(root),DATA_DIR=str(root/'.nexus'),
             DATABASE_URL='sqlite:///'+str(root/'.nexus'/'metadata.sqlite'),NEXUS_NETWORK_MODE='connected')
    env.pop('NEXUS_DROP_PRIVILEGES',None);env.pop('NEXUS_LLM_CONFIG_FILE',None);env.pop('NEXUS_INITIAL_MODEL',None)
    source='from agent_service.cloud import prepare; import os; prepare(); print(os.environ["DATA_DIR"]+"|"+os.environ["DATABASE_URL"])'
    result=subprocess.run([sys.executable,'-c',source],env=env,text=True,capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()==str(root/'.atlas')+'|'+'sqlite:///'+str(root/'.atlas'/'metadata.sqlite')
    assert not (root/'.nexus').exists()


def test_native_legacy_credentials_reused_without_rewrite(tmp_path):
    if sys.platform!='linux':pytest.skip('Linux/WSL native launcher')
    import native_local
    old={'APP_TOKEN':'a'*40,'ATLAS_OPERATOR_TOKEN':'b'*40}
    path=tmp_path/'credentials.json';path.write_text(json.dumps(old));before=path.read_bytes()
    value=native_local.credentials(tmp_path)
    assert value['NEXUS_OPERATOR_TOKEN']==old['ATLAS_OPERATOR_TOKEN']
    assert path.read_bytes()==before
    path.write_text(json.dumps({**old,'NEXUS_OPERATOR_TOKEN':'c'*40}))
    with pytest.raises(ValueError,match='Conflicting'):native_local.credentials(tmp_path)


def test_cloud_does_not_create_workspace_with_unrelated_database(tmp_path,monkeypatch):
    from agent_service import cloud
    root=tmp_path/'empty';root.mkdir()
    monkeypatch.setenv('APP_TOKEN','valid-token-123456789')
    monkeypatch.setenv('NEXUS_LOCAL_MODE','1');monkeypatch.setenv('NEXUS_LOCAL_AUTO_STORAGE','1')
    monkeypatch.setenv('NEXUS_LOCAL_WORKSPACE',str(root));monkeypatch.setenv('DATA_DIR',str(root/'.nexus'))
    monkeypatch.setenv('DATABASE_URL','sqlite:////tmp/unrelated.sqlite')
    for name in ('NEXUS_S3_ENDPOINT','ATLAS_S3_ENDPOINT','NEXUS_INITIAL_MODEL','NEXUS_LLM_CONFIG_FILE'):
        monkeypatch.delenv(name,raising=False)
    with pytest.raises(ValueError,match='数据库配置'):cloud.prepare()
    assert list(root.iterdir())==[]
