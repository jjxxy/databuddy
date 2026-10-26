"""Desktop provisioning may validate shape, but must not contact a model at boot."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('configuration',[None, '{invalid json}', json.dumps({'default_model_id':'test','models':[
    {'id':'test','name':'离线测试模型','base_url':'https://model.invalid/v1','model':'test-model','api_key':'invalid-test-key','share_data':False}]})])
def test_prepare_without_any_network_probe(tmp_path,configuration):
    source=Path(__file__).resolve().parents[1]
    model=tmp_path/'model.json'
    if configuration is not None:model.write_text(configuration,encoding='utf-8')
    env={k:v for k,v in os.environ.items() if not k.startswith(('ATLAS_','LLM_','RAILWAY_','OPENCODE_'))
         and k not in ('DATA_DIR','DATABASE_URL','APP_TOKEN','PORT')}
    env.update(APP_TOKEN='test-desktop-token-long-enough',DATA_DIR=str(tmp_path/'data'),PORT='8000',
               ATLAS_DESKTOP_MODE='1',ATLAS_OPENCODE_RUNTIME='1',ATLAS_LLM_CONFIG_FILE=str(model),PYTHONUTF8='1')
    script='''
import socket,json
def forbidden(*args,**kwargs):raise AssertionError('Startup attempted networking')
socket.create_connection=forbidden
socket.getaddrinfo=forbidden
socket.socket.connect=forbidden
from agent_service.cloud import prepare
assert prepare()==8000
from agent_service.model_catalog import public_catalog
from agent_service.store import get
value=public_catalog()
if value['configured']:assert get('system','model-provider')['verified_at'] is None
print(json.dumps({'ok':True,'configured':value['configured']}))
'''
    result=subprocess.run([sys.executable,'-X','utf8','-c',script],cwd=source,env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['ok']
