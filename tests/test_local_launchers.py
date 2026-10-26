"""Local launchers bind the selected folder; they never execute config text."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT=Path(__file__).resolve().parents[1]


def prepare(tmp_path):
    bundle=tmp_path/'程序包';bundle.mkdir()
    shutil.copyfile(ROOT/'start-local.sh',bundle/'start-local.sh')
    shutil.copyfile(ROOT/'compose.local.yaml',bundle/'compose.local.yaml')
    (bundle/'quickstart.py').write_text('# protocol fixture only\n')
    chosen=tmp_path/'业务, 空格 "引号"及单\'引号';chosen.mkdir()
    binary=tmp_path/'bin';binary.mkdir();log=tmp_path/'docker.jsonl'
    docker=binary/'docker'
    docker.write_text('#!'+sys.executable+'\n'+r'''
import csv,json,os,pathlib,sys
args=sys.argv[1:]
record={'args':args,'folder':os.environ.get('ATLAS_LOCAL_WORKSPACE_HOST'),
        'uid':os.environ.get('ATLAS_LOCAL_UID'),'network':os.environ.get('ATLAS_NETWORK_MODE')}
with pathlib.Path(os.environ['LOCAL_LAUNCH_LOG']).open('a') as output:output.write(json.dumps(record,ensure_ascii=False)+'\n')
if args[0]=='info':print('linux')
elif args[:2]==['compose','version']:print('Compose fixture')
elif args[0]=='run':
    mount=next(csv.reader([args[args.index('--mount')+1]]))
    source=next(item.split('=',1)[1] for item in mount if item.startswith('source='))
    if args[-1]=='setup':
        path=pathlib.Path(source)/'.env'
        if not path.exists():path.write_text('APP_TOKEN=fixture-local-access-token\nATLAS_HTTP_PORT=8127\n')
    elif args[-1]=='info':print('访问口令：fixture-local-access-token')
elif args[0]!='compose':raise AssertionError(args)
''')
    docker.chmod(0o755)
    env={**os.environ,'PATH':str(binary)+os.pathsep+os.environ.get('PATH',''),
        'LOCAL_LAUNCH_LOG':str(log),'ATLAS_NO_OPEN':'1','ATLAS_LOCAL_UID':'1000','ATLAS_LOCAL_GID':'1000'}
    env.pop('ATLAS_LOCAL_WORKSPACE_HOST',None)
    return bundle,chosen,log,env


def run(bundle,env,*arguments):
    return subprocess.run(['/bin/bash',str(bundle/'start-local.sh'),*arguments],env=env,
        cwd=bundle,text=True,capture_output=True,timeout=15)


def records(log):return [json.loads(line) for line in log.read_text().splitlines()]


def test_explicit_folder_is_one_exact_binding_and_no_host_python_needed(tmp_path):
    bundle,chosen,log,env=prepare(tmp_path)
    result=run(bundle,env,'up','--folder',str(chosen),'--no-open')
    assert result.returncode==0,result.stderr
    assert (bundle/'.atlas-local-folder').read_text().strip()==str(chosen)
    calls=records(log)
    assert all(item['folder']==str(chosen) for item in calls)
    assert all(item['uid']=='1000' for item in calls)
    up=next(item['args'] for item in calls if item['args'][0]=='compose' and 'up' in item['args'])
    assert up[up.index('-f')+1]=='compose.local.yaml' and '--wait' in up
    assert 'http://localhost:8127' in result.stdout
    assert list(chosen.iterdir())==[] # Launcher does not alter original files.


def test_reopen_and_stop_reuse_selected_folder_without_deleting_data(tmp_path):
    bundle,chosen,log,env=prepare(tmp_path)
    assert run(bundle,env,'up','--folder',str(chosen),'--no-open').returncode==0
    previous=len(records(log))
    assert run(bundle,env,'down').returncode==0
    commands=records(log)[previous:]
    assert not any(item['args'][0]=='run' for item in commands)
    assert commands[-1]['args'][-1]=='down'
    assert not any('-v' in item['args'] or '--volumes' in item['args'] for item in commands)
    assert all(item['folder']==str(chosen) for item in commands)


def test_offline_flag_is_application_local_only_policy_not_a_fake_network_none(tmp_path):
    bundle,chosen,log,env=prepare(tmp_path)
    result=run(bundle,env,'up','--folder',str(chosen),'--offline','--no-open')
    assert result.returncode==0,result.stderr
    assert all(item['network']=='local_only' for item in records(log))


def test_cancel_native_picker_does_not_start_or_create_state(tmp_path):
    bundle,chosen,log,env=prepare(tmp_path)
    chooser=Path(env['PATH'].split(os.pathsep)[0])/'osascript'
    chooser.write_text('#!/bin/sh\nexit 1\n');chooser.chmod(0o755)
    result=run(bundle,env,'up','--choose-folder')
    assert result.returncode==0 and not log.exists()
    assert not (bundle/'.atlas-local-folder').exists()


def test_linked_folder_and_root_are_rejected_before_docker(tmp_path):
    bundle,chosen,log,env=prepare(tmp_path)
    link=tmp_path/'linked';link.symlink_to(chosen,target_is_directory=True)
    for folder in (str(link),'/'):
        assert run(bundle,env,'up','--folder',folder).returncode==2
    assert not log.exists()


def test_local_manifest_keeps_metadata_in_selected_folder_and_private_ui_on_loopback():
    compose=yaml.safe_load((ROOT/'compose.local.yaml').read_text())
    service=compose['services']['atlas'];environment=service['environment']
    assert environment['DATA_DIR']=='/local-workspace/.atlas'
    assert environment['ATLAS_LOCAL_MODE']=='1' and environment['ATLAS_OPENCODE_RUNTIME']=='1'
    assert environment['DATABASE_URL']=='sqlite:////local-workspace/.atlas/metadata.sqlite'
    assert service['volumes'][0]['target']=='/local-workspace'
    assert service['volumes'][0]['bind']['create_host_path'] is False
    assert '127.0.0.1' in service['ports'][0]
    assert 'docker.sock' not in (ROOT/'compose.local.yaml').read_text()


def test_windows_picker_has_real_folder_dialog_and_bom_but_no_execution_claim():
    path=ROOT/'start-local.ps1';raw=path.read_bytes();text=raw.decode('utf-8-sig')
    assert raw.startswith(b'\xef\xbb\xbf') and '\r\n' in text
    assert 'System.Windows.Forms.FolderBrowserDialog' in text
    assert 'ReparsePoint' in text and 'ATLAS_LOCAL_WORKSPACE_HOST' in text
    assert 'compose.local.yaml' in text
    assert 'start-local.ps1' in (ROOT/'start-local.cmd').read_text()
