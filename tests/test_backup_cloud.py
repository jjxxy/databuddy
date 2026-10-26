import os,json,subprocess,shutil
from pathlib import Path
import pytest
from agent_service import backup_cloud as b

def test_backup_environment_does_not_export_app_or_model_credentials(monkeypatch):
    for key,value in {'ATLAS_S3_ENDPOINT':'https://s3.test','ATLAS_S3_BUCKET':'private','ATLAS_S3_ACCESS_KEY_ID':'bucket-only-key','ATLAS_S3_SECRET_ACCESS_KEY':'bucket-only-secret','ATLAS_BACKUP_KEY':'x'*40,'APP_TOKEN':'application-private','LLM_API_KEY':'model-private','DATABASE_URL':'database-private'}.items():monkeypatch.setenv(key,value)
    e=b.environment();assert e['RESTIC_REPOSITORY']=='s3:https://s3.test/private/atlas'
    assert 'APP_TOKEN' not in e and 'LLM_API_KEY' not in e and 'DATABASE_URL' not in e

@pytest.mark.parametrize('url',['http://s3.test','https://user:password@s3.test','https://s3.test?token=bad'])
def test_backup_destination_validation(monkeypatch,url):
    monkeypatch.setenv('ATLAS_S3_ENDPOINT',url)
    with pytest.raises(ValueError):b.environment()

@pytest.mark.skipif(not shutil.which('restic'),reason='Restic binary is installed in deployment images')
def test_real_restic_encrypted_repository_roundtrip_and_wrong_password(tmp_path):
    repo=tmp_path/'repo';file=tmp_path/'workspace.zip';file.write_bytes(b'private-business-data'*1000)
    env=dict(os.environ,RESTIC_REPOSITORY=str(repo),RESTIC_PASSWORD='random-fixture-password-1234567890')
    subprocess.run(['restic','init'],env=env,check=True,capture_output=True)
    subprocess.run(['restic','backup',str(file)],env=env,check=True,capture_output=True)
    target=tmp_path/'restored';subprocess.run(['restic','restore','latest','--target',str(target)],env=env,check=True,capture_output=True)
    assert next(target.rglob('workspace.zip')).read_bytes()==file.read_bytes()
    bad=dict(env,RESTIC_PASSWORD='wrong-password');r=subprocess.run(['restic','snapshots'],env=bad,capture_output=True)
    assert r.returncode!=0
    assert b'private-business-data' not in (repo/'config').read_bytes()
