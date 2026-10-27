"""Archives have a stable encoding even under the Windows non-UTF8 locale."""
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile


def test_backup_and_restore_unicode_metadata_with_utf8_mode_disabled(tmp_path):
    root=Path(__file__).resolve().parents[1]
    source=tmp_path/'source';target=tmp_path/'restored';archive=tmp_path/'unicode.atlas'
    label='财经核对🧾与预算'
    environment=dict(os.environ,DATA_DIR=str(source),DATABASE_URL='sqlite:///'+str(source/'metadata.sqlite'),
                     PYTHONPATH=str(root),PYTHONUTF8='0',APP_TOKEN='utf8-fixture-only-access-token')
    script=('from agent_service.store import put; from agent_service.audit import record; '
            'from agent_service.maintenance import backup; '
            f"put('project',{{'name':{label!r}}},'utf8_project'); "
            f"record('fixture.unicode','utf8_project',{{'label':{label!r}}}); "
            f'backup({str(archive)!r})')
    created=subprocess.run([sys.executable,'-X','utf8=0','-c',script],env=environment,
                           cwd=root,capture_output=True,timeout=30)
    assert created.returncode==0,created.stderr.decode('utf-8',errors='backslashreplace')
    with zipfile.ZipFile(archive) as content:
        metadata=content.read('metadata.jsonl').decode('utf-8')
    assert label in metadata
    environment.update(DATA_DIR=str(target),DATABASE_URL='sqlite:///'+str(target/'metadata.sqlite'))
    restored=subprocess.run([sys.executable,'-X','utf8=0','-m','agent_service.maintenance','restore',str(archive)],
                            env=environment,cwd=root,capture_output=True,timeout=30)
    assert restored.returncode==0,restored.stderr.decode('utf-8',errors='backslashreplace')
    assert json.loads(restored.stdout)['ok']
    checked=subprocess.run([sys.executable,'-X','utf8=0','-c',
                            "import json; from agent_service.store import get; from agent_service.audit import verify; "
                            "print(json.dumps({'name':get('project','utf8_project')['name'],'audit_ok':verify()['ok']}))"],
                           env=environment,cwd=root,capture_output=True,timeout=30)
    assert checked.returncode==0,checked.stderr.decode('utf-8',errors='backslashreplace')
    assert json.loads(checked.stdout)=={'name':label,'audit_ok':True}
