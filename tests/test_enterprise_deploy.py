import importlib.util,subprocess,sys,json
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('render_enterprise',ROOT/'scripts/render_enterprise.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

def config():return {'namespace':'atlas-test','image':'registry.test/atlas@sha256:'+'a'*64,'gatewayImage':'registry.test/proxy@sha256:'+'b'*64,'rwxClaim':'shared-test','storageId':'test-workspace','secretName':'owner-secrets','hostname':'atlas.test','tlsSecret':'tls','provisionPostgres':True,'databaseStorageClass':'fast'}

def test_fail_closed_without_real_images_and_shared_storage_inputs():
    c=config();c['image']='atlas:latest'
    with pytest.raises(ValueError):module.render(c)
    c=config();c.pop('rwxClaim')
    with pytest.raises(ValueError):module.render(c)

def test_ha_topology_no_hostpath_and_identity_gateway_only():
    docs=module.render(config());deployments=[v for v in docs if v['kind']=='Deployment'];assert len(deployments)==4
    for d in deployments:
        assert d['spec']['replicas']==2
        pod=d['spec']['template']['spec'];assert pod['securityContext']['runAsNonRoot'] and not pod['automountServiceAccountToken']
        assert pod['affinity']['podAntiAffinity']['requiredDuringSchedulingIgnoredDuringExecution']
        for volume in pod.get('volumes',[]):assert 'hostPath' not in volume
    db=next(v for v in docs if v['kind']=='Cluster');assert db['spec']['instances']==3
    ingress=next(v for v in docs if v['kind']=='Ingress');assert ingress['spec']['rules'][0]['http']['paths'][0]['backend']['service']['name']=='atlas-gateway'
    assert len([v for v in docs if v['kind']=='PodDisruptionBudget'])==4
    api=next(d for d in deployments if d['metadata']['name']=='atlas-api');assert api['spec']['template']['spec']['containers'][0]['readinessProbe']['httpGet']['path']=='/ready'


def test_separate_role_deployments_do_not_wait_for_an_unstarted_local_opencode():
    for deployment in module.render(config()):
        if deployment['kind'] != 'Deployment' or deployment['metadata']['name'] == 'atlas-gateway':continue
        container = deployment['spec']['template']['spec']['containers'][0]
        assert 'agent_service.cloud' not in container['command']
        environment = {item['name']: item.get('value') for item in container['env']}
        assert environment['ATLAS_OPENCODE_RUNTIME'] == '0'


def app_containers(docs):
    for document in docs:
        if document['kind'] in ('Deployment','Job'):
            pod=document['spec']['template']['spec']
        elif document['kind']=='CronJob':
            pod=document['spec']['jobTemplate']['spec']['template']['spec']
        else:continue
        for container in pod.get('containers',[]):
            if any(item['name']=='DATA_DIR' for item in container.get('env',[])):yield container


def test_unconfigured_enterprise_template_does_not_inject_daily_model_quotas():
    documents=module.render(dict(config(),offsiteBackup=True))
    containers=list(app_containers(documents));assert len(containers)==5
    for container in containers:
        names={item['name'] for item in container['env']}
        assert 'MAX_DAILY_MODEL_CALLS' not in names and 'MAX_DAILY_RESERVED_TOKENS' not in names


def test_default_example_does_not_reintroduce_model_quotas_when_copied():
    example=json.loads((ROOT/'deploy/enterprise/config.example.json').read_text())
    assert 'dailyModelCalls' not in example and 'dailyReservedTokens' not in example
    # Only replace the deliberate infrastructure placeholders for local rendering.
    example.update(image=config()['image'],gatewayImage=config()['gatewayImage'])
    for container in app_containers(module.render(example)):
        assert not any(item['name'].startswith('MAX_DAILY_') for item in container['env'])


@pytest.mark.parametrize('quotas',[
    {'dailyModelCalls':500}, {'dailyReservedTokens':1600000},
    {'dailyModelCalls':0,'dailyReservedTokens':0},
    {'dailyModelCalls':0,'dailyReservedTokens':2000000},
])
def test_explicit_enterprise_quotas_preserve_positive_and_zero_operator_policy(quotas):
    import copy
    baseline=module.render(dict(config(),offsiteBackup=True))
    documents=module.render(dict(config(),offsiteBackup=True,**quotas))
    expected={name:str(quotas[key]) for key,name in (
        ('dailyModelCalls','MAX_DAILY_MODEL_CALLS'),('dailyReservedTokens','MAX_DAILY_RESERVED_TOKENS')) if key in quotas}
    for container in app_containers(documents):
        actual={item['name']:item.get('value') for item in container['env'] if item['name'].startswith('MAX_DAILY_')}
        assert actual==expected
    normalized=copy.deepcopy(documents)
    for container in app_containers(normalized):
        container['env']=[item for item in container['env'] if not item['name'].startswith('MAX_DAILY_')]
    assert normalized==baseline,'Quota configuration must not change authorization, storage or resource limits'


@pytest.mark.parametrize('key',['dailyModelCalls','dailyReservedTokens'])
@pytest.mark.parametrize('value',[-1,True,None,'500'])
def test_invalid_enterprise_quota_is_rejected_before_rendering(key,value):
    with pytest.raises(ValueError,match='非负整数'):module.render(dict(config(),**{key:value}))


def test_compose_roles_keep_compatibility_while_quickstart_starts_native_runtime():
    import yaml
    legacy = yaml.safe_load((ROOT / 'compose.yaml').read_text())
    for name, service in legacy['services'].items():
        if name == 'postgres':continue
        assert 'agent_service.cloud' not in service['command']
        assert service['environment']['ATLAS_OPENCODE_RUNTIME'] == '0'
    quickstart = yaml.safe_load((ROOT / 'compose.quickstart.yaml').read_text())['services']['atlas']
    assert quickstart['environment']['ATLAS_OPENCODE_RUNTIME'] == '1'
    assert 'command' not in quickstart
    assert 'CMD ["python", "-m", "agent_service.cloud"]' in (ROOT / 'Dockerfile.quickstart').read_text()

@pytest.mark.skipif(__import__('agent_service.store',fromlist=['engine']).engine.dialect.name!='postgresql',reason='requires real PostgreSQL')
def test_cross_node_postgres_lock_and_recovery_after_holder_killed(tmp_path):
    import os,time
    script='''import sys
from agent_service.config import DATA
from agent_service.locks import FileLock,Timeout
try:
 with FileLock(str(DATA/'cross-node-fault.lock'),timeout=0):
  print('OWNED',flush=True)
  if sys.argv[1]=='hold':sys.stdin.read()
except Timeout:print('BUSY',flush=True)
'''
    env=dict(os.environ,DATA_DIR=str(tmp_path/'nodeA'));p=subprocess.Popen([sys.executable,'-c',script,'hold'],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        assert p.stdout.readline().strip()=='OWNED'
        env['DATA_DIR']=str(tmp_path/'nodeB')
        assert subprocess.check_output([sys.executable,'-c',script,'once'],env=env,text=True).strip()=='BUSY'
        p.kill();p.wait(5)
        assert subprocess.check_output([sys.executable,'-c',script,'once'],env=env,text=True).strip()=='OWNED'
    finally:
        if p.poll() is None:p.kill();p.wait(5)

@pytest.mark.skipif(__import__('agent_service.store',fromlist=['engine']).engine.dialect.name!='postgresql',reason='requires real PostgreSQL')
def test_database_session_loss_fences_late_writes():
    from agent_service.locks import FileLock,assert_owned,LockLost
    from agent_service.store import engine,put
    from agent_service.config import DATA
    from sqlalchemy import text
    with FileLock(str(DATA/'fence-fault.lock'),timeout=1) as lock:
        with engine.begin() as c:c.execute(text('SELECT pg_terminate_backend(:pid)'),{'pid':lock.backend})
        with pytest.raises(LockLost):assert_owned()
        with pytest.raises(LockLost):put('system',{'must_not_write':True},'fence-invalid-write')

@pytest.mark.parametrize('name',['orchestrator','worker'])
def test_process_exits_instead_of_heartbeating_forever_after_lock_loss(name,monkeypatch):
    import importlib
    module=importlib.import_module('agent_service.'+name)
    from agent_service.locks import LockLost
    def lost(*args,**kwargs):raise LockLost('database ownership lost')
    monkeypatch.setattr(module,'tick' if name=='orchestrator' else 'queue_due',lost)
    monkeypatch.setenv('AGENT_ORCHESTRATION_MODE','separate');monkeypatch.setenv('SCHEDULER_MODE','worker')
    with pytest.raises(LockLost):module.main()
