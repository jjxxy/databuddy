"""Owner file catalog is normalized before privilege drop; no runtime import."""
import json
import os
import subprocess
import sys

import pytest

import quickstart
from agent_service.quickstart_model import load_configuration


def catalog():
    return {'default_model_id':'finance','models':[
        {'id':'finance','name':'财经分析','base_url':'http://localhost:11434/v1',
         'model':'fixture-finance','api_key':'FIXTURE-PRIVATE-KEY','share_data':True},
        {'id':'fast','name':'快速处理','base_url':'https://models.example.org/v1/chat/completions',
         'model':'fixture-fast','api_key':'FIXTURE-PRIVATE-KEY-2','share_data':False}]}


def write(tmp_path,value):
    path=tmp_path/'llm.json';path.write_text(json.dumps(value),encoding='utf-8');return path


def test_multiple_owner_models_normalized_and_bootstrap_can_generate_access_token(tmp_path):
    path=write(tmp_path,catalog());original=path.read_bytes()
    result=load_configuration(path,container=True)
    assert result['models'][0]['base_url']=='http://host.docker.internal:11434/v1'
    assert result['models'][1]['base_url']=='https://models.example.org/v1'
    assert result['default_model_id']=='finance' and result['models'][1]['share_data'] is False
    assert path.read_bytes()==original
    assert quickstart.setup(tmp_path)['share_data'] is True
    assert len(quickstart.read_env(tmp_path/'.env')['APP_TOKEN'])>=32
    assert 'FIXTURE-' not in (tmp_path/'.env').read_text()


@pytest.mark.parametrize('mutation',[
    lambda c:c.update(default_model_id='missing'),
    lambda c:c.update(models=[]),
    lambda c:c.update(models=c['models']*11),
    lambda c:c['models'][1].update(id='finance'),
    lambda c:c['models'][1].update(id='../secret'),
    lambda c:c['models'][1].update(name=''),
    lambda c:c['models'][1].update(name='bad\nname'),
    lambda c:c['models'][1].update(api_key={'secret':'FIXTURE-PRIVATE'}),
    lambda c:c.update(api_key='FIXTURE-PRIVATE'),
])
def test_invalid_catalog_fails_without_reflecting_secrets(tmp_path,mutation):
    value=catalog();mutation(value)
    with pytest.raises(ValueError) as error:load_configuration(write(tmp_path,value))
    assert 'FIXTURE-' not in str(error.value)


def test_placeholder_in_nondefault_model_is_not_ignored(tmp_path):
    value=catalog();value['models'][1]['api_key']='YOUR_API_KEY';write(tmp_path,value)
    with pytest.raises(ValueError,match='填写 LLM'):quickstart.configuration(tmp_path)


def test_cloud_provisions_all_owner_entries_and_does_not_leak_file_to_children(tmp_path):
    path=write(tmp_path,catalog());data=tmp_path/'data'
    env={**os.environ,'DATA_DIR':str(data),'DATABASE_URL':'sqlite:///'+str(data/'metadata.sqlite'),
         'APP_TOKEN':'fixture-only-workspace-token-123456789','ATLAS_LLM_CONFIG_FILE':str(path),
         'ATLAS_QUICKSTART_CONTAINER':'1','API_ALLOWED_HOSTS':'existing.test'}
    script='''import os
from agent_service import model_catalog
from agent_service.cloud import prepare
calls=[]
def provision(value):
    assert value['default_model_id']=='finance'
    assert value['models'][0]['base_url']=='http://host.docker.internal:11434/v1'
    assert value['models'][1]['api_key']=='FIXTURE-PRIVATE-KEY-2'
    calls.append(value)
model_catalog.provision=provision
assert prepare()==8000
assert len(calls)==1
assert {'existing.test','host.docker.internal','models.example.org'}<=set(os.environ['API_ALLOWED_HOSTS'].split(','))
assert 'ATLAS_LLM_CONFIG_FILE' not in os.environ and 'ATLAS_INITIAL_MODEL' not in os.environ
print('CATALOG_BOOTSTRAP_OK')
'''
    result=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='CATALOG_BOOTSTRAP_OK'
    assert 'FIXTURE-' not in result.stdout+result.stderr
