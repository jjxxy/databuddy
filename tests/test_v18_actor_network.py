"""Personal identity survives request/worker boundaries; local policy fails closed.

These are application policy checks, not evidence of operating-system isolation.
No external server, browser, model or Restic executable is contacted.
"""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from agent_service import activity, api, backup_cloud, browser_reports, identity, network_policy, provider
from agent_service.actor_context import actor_from_identity, actor_scope, current_actor
from agent_service.config import TOKEN
from agent_service.store import delete, put, uid


def person(subject, issuer='https://identity.example.test'):
    return actor_from_identity({'authenticated':True,'mode':'oidc','subject':subject,'issuer':issuer})


def forbidden(*args, **kwargs):
    raise AssertionError('blocked operation attempted a network or child-process call')


def test_actor_identity_uses_authenticated_issuer_and_subject_not_display_names():
    assert actor_from_identity({'authenticated':True,'mode':'token'})=='owner'
    assert person('finance')==person('finance')
    assert person('finance')!=person('sales')
    assert person('finance')!=person('finance','https://other.example.test')
    assert len(person('finance'))==66
    for identity_value in ({}, {'authenticated':False,'mode':'token'},
                           {'authenticated':True,'mode':'oidc','subject':''},
                           {'authenticated':True,'mode':'unknown','subject':'finance'}):
        with pytest.raises(ValueError):actor_from_identity(identity_value)
    for value in ('finance','u_'+'f'*63,'owner/../other',None):
        with pytest.raises(ValueError):
            with actor_scope(value):pass


def test_async_actor_scope_is_isolated_and_restored_after_failure():
    async def run():
        entered=asyncio.Event()
        async def first():
            with actor_scope(person('first')):
                entered.set()
                await asyncio.sleep(0)
                assert current_actor()==person('first')
                with pytest.raises(RuntimeError):
                    with actor_scope(person('nested')):raise RuntimeError('fixture')
                return current_actor()
        async def second():
            await entered.wait()
            with actor_scope(person('second')):
                await asyncio.sleep(0)
                return current_actor()
        values=await asyncio.gather(first(),second())
        assert current_actor()=='owner'
        return values
    assert asyncio.run(run())==[person('first'),person('second')]
    assert current_actor()=='owner'


def test_real_auth_dependency_propagates_actor_into_sync_routes_without_request_leak(monkeypatch):
    monkeypatch.setenv('ATLAS_AUTH_MODE','oidc')
    monkeypatch.setattr(identity,'principal',lambda request:{'authenticated':True,'mode':'oidc',
        'subject':request.headers['x-fixture-subject'],'issuer':'https://identity.example.test'})
    app=FastAPI()
    @app.get('/actor',dependencies=[Depends(api.auth)])
    def route(request:Request):
        return {'actor':current_actor(),'subject':request.state.identity['subject']}
    with TestClient(app) as client:
        def fetch(subject):
            result=client.get('/actor',headers={'x-fixture-subject':subject})
            assert result.status_code==200,result.text
            return result.json()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(fetch,['finance','sales','finance','sales']))
    assert results==[{'actor':person(s),'subject':s} for s in ('finance','sales','finance','sales')]
    assert current_actor()=='owner'


def test_shared_token_is_explicitly_one_personal_owner(monkeypatch):
    monkeypatch.setenv('ATLAS_AUTH_MODE','token')
    app=FastAPI()
    @app.get('/actor',dependencies=[Depends(api.auth)])
    def route():return {'actor':current_actor()}
    with TestClient(app) as client:
        assert client.get('/actor').status_code==401
        assert client.get('/actor',headers={'Authorization':'Bearer '+TOKEN}).json()=={'actor':'owner'}
    assert current_actor()=='owner'


def test_worker_task_scope_uses_frozen_actor_and_restores_outer_scope():
    tid='t_'+uid();put('turn',{'actor_id':person('task-owner'),'project_id':'fixture-project'},tid)
    try:
        with actor_scope(person('request-owner')):
            with pytest.raises(RuntimeError):
                with provider.task_scope(tid):
                    assert current_actor()==person('task-owner')
                    assert provider.current_turn_id()==tid
                    raise RuntimeError('fixture')
            assert current_actor()==person('request-owner')
            assert provider.current_turn_id() is None
    finally:delete('turn',tid)
    assert current_actor()=='owner'


def test_local_policy_requires_explicit_private_host_and_rejects_mixed_dns(monkeypatch):
    monkeypatch.setenv('ATLAS_NETWORK_MODE','local_only')
    monkeypatch.delenv('ATLAS_LOCAL_ONLY_HOSTS',raising=False)
    monkeypatch.setattr(network_policy.socket,'getaddrinfo',forbidden)
    for host in ('example.com','10.0.0.5','169.254.169.254'):
        with pytest.raises(ValueError):network_policy.check_host(host,addresses=['10.0.0.5'])
    network_policy.check_host('127.0.0.1',addresses=['127.0.0.1'])
    network_policy.check_host('::1',addresses=['::1'])
    monkeypatch.setenv('ATLAS_LOCAL_ONLY_HOSTS','db.internal,10.0.0.5')
    network_policy.check_host('DB.INTERNAL.',addresses=['10.0.0.5'])
    network_policy.check_host('10.0.0.5',addresses=['10.0.0.5'])
    for addresses in ([], ['10.0.0.5','8.8.8.8'], ['169.254.169.254'], ['0.0.0.0'], ['224.0.0.1']):
        with pytest.raises(ValueError):network_policy.check_host('db.internal',addresses=addresses)
    with pytest.raises(ValueError):network_policy.check_host('localhost',addresses=['10.0.0.5'])
    assert network_policy.status()=={'mode':'local_only','application_connections_restricted':True,'os_network_isolation':False}


@pytest.mark.parametrize('url',['https://public.example.test','file:///tmp/input',
    'https://user:secret@localhost','https://169.254.169.254/latest/meta-data'])
def test_local_policy_rejects_remote_or_credential_bearing_urls_before_dns(monkeypatch,url):
    monkeypatch.setenv('ATLAS_NETWORK_MODE','local_only')
    monkeypatch.delenv('ATLAS_LOCAL_ONLY_HOSTS',raising=False)
    monkeypatch.setattr(network_policy.socket,'getaddrinfo',forbidden)
    with pytest.raises(ValueError):network_policy.check_url(url)


def test_local_policy_blocks_model_before_request_budget_or_transport(monkeypatch):
    monkeypatch.setenv('ATLAS_NETWORK_MODE','local_only')
    monkeypatch.setattr(provider.httpx,'Client',forbidden)
    monkeypatch.setattr('agent_service.quotas.reserve',forbidden)
    with pytest.raises(ValueError,match='仅本地'):
        provider.complete([{'role':'user','content':'private business data'}],
            configuration={'base_url':'https://model.example.test/v1','model':'fixture','api_key':'secret'})


def test_local_policy_checks_legacy_opencode_upstream_before_forwarding_business_prompt(monkeypatch):
    monkeypatch.setenv('ATLAS_NETWORK_MODE','local_only')
    monkeypatch.setattr(network_policy.socket,'getaddrinfo',lambda *args,**kwargs:[(2,1,6,'',('127.0.0.1',80))])
    monkeypatch.setattr('agent_service.opencode.request',forbidden)
    monkeypatch.setattr('agent_service.quotas.reserve',forbidden)
    with pytest.raises(ValueError,match='仅本地'):
        provider.complete([{'role':'user','content':'private business data'}],configuration={
            'base_url':'http://127.0.0.1:4096','model':'provider/fixture','engine':'opencode',
            'upstream_base_url':'https://cloud-model.example.test/v1','api_key':'fixture'})


def test_local_policy_blocks_browser_even_with_owner_verified_flag_before_page_or_file_access(monkeypatch,tmp_path):
    monkeypatch.setenv('ATLAS_NETWORK_MODE','local_only')
    destination=tmp_path/'not-created'/'report.csv'
    recipe={'url':'http://localhost/report','export_selector':'#export','required_context_text':['2026']}
    with pytest.raises(browser_reports.BrowserUnavailable) as error:
        browser_reports.export_with_page(object(),recipe,destination,validator=forbidden,network_policy_verified=True)
    assert error.value.code=='BROWSER_LOCAL_ONLY_UNAVAILABLE'
    assert not destination.parent.exists()


def test_local_policy_blocks_webpush_before_dns_crypto_and_http(monkeypatch):
    monkeypatch.setenv('ATLAS_NETWORK_MODE','local_only')
    monkeypatch.delenv('ATLAS_LOCAL_ONLY_HOSTS',raising=False)
    monkeypatch.setattr(activity.socket,'getaddrinfo',forbidden)
    monkeypatch.setattr(activity.httpx,'Client',forbidden)
    encode=lambda value:base64.urlsafe_b64encode(value).rstrip(b'=').decode()
    key=ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
    subscription={'endpoint':'https://fcm.googleapis.com/fcm/send/fixture',
                  'keys':{'p256dh':encode(key),'auth':encode(b'0'*16)}}
    with pytest.raises(ValueError,match='仅本地'):
        activity.pinned_push(subscription,{'project_id':'fixture'})


def test_local_policy_blocks_remote_backup_before_plaintext_snapshot_or_child_process(monkeypatch):
    monkeypatch.setenv('ATLAS_NETWORK_MODE','local_only')
    monkeypatch.setenv('ATLAS_LOCAL_ONLY_HOSTS','s3.internal')
    for key,value in {'ATLAS_S3_ENDPOINT':'https://s3.internal','ATLAS_S3_BUCKET':'private',
        'ATLAS_S3_ACCESS_KEY_ID':'fixture','ATLAS_S3_SECRET_ACCESS_KEY':'fixture','ATLAS_BACKUP_KEY':'x'*40}.items():
        monkeypatch.setenv(key,value)
    monkeypatch.setattr(backup_cloud.subprocess,'run',forbidden)
    monkeypatch.setattr('agent_service.maintenance.backup',forbidden)
    with pytest.raises(ValueError,match='仅本地'):backup_cloud.environment()
    with pytest.raises(ValueError,match='仅本地'):backup_cloud.command(['snapshots'],{})
    with pytest.raises(ValueError,match='仅本地'):backup_cloud.save()


def test_connected_mode_preserves_network_option_and_does_not_claim_firewall(monkeypatch):
    monkeypatch.setenv('ATLAS_NETWORK_MODE','connected')
    monkeypatch.setattr(network_policy.socket,'getaddrinfo',forbidden)
    network_policy.check_url('https://example.test')
    assert network_policy.status()=={'mode':'connected','application_connections_restricted':False,'os_network_isolation':False}
    monkeypatch.setenv('ATLAS_NETWORK_MODE','misspelled')
    with pytest.raises(ValueError):network_policy.check_url('http://localhost')
