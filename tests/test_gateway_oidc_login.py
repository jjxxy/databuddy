"""Confidential OIDC+PKCE gateway sessions; no IdP token reaches the browser."""
import base64
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric import rsa
from agent_service.workspace_gateway import GatewayConfig, create_app


class JsonStream(httpx.AsyncByteStream):
    def __init__(self,value):self.value=value
    async def __aiter__(self):yield json.dumps(self.value).encode()


@pytest.fixture
def oidc(monkeypatch,tmp_path):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()));jwk.update(kid='test',alg='RS256',use='sig')
    keys=tmp_path/'jwks.json';keys.write_text(json.dumps({'keys':[jwk]}))
    secret=tmp_path/'secret.txt';secret.write_text('fixture-client-secret')
    config={'schema_version':1,'public_origin':'https://atlas.example',
            'oidc':{'issuer':'https://id.example/realm','audience':'atlas-api','jwks_file':str(keys)},
            'workspaces':[{'id':'alice','backend_url':'http://atlas-alice:8000','subjects':['alice-sub']},
                          {'id':'bob','backend_url':'http://atlas-bob:8000','subjects':['bob-sub']}]}
    login={'client_id':'atlas-client','client_secret_file':str(secret),
           'authorization_endpoint':'https://id.example/realm/auth','token_endpoint':'https://id.example/realm/token',
           'scope':'openid offline_access','session_seconds':3600}
    configfile=tmp_path/'login.json';configfile.write_text(json.dumps(login));monkeypatch.setenv('ATLAS_WORKSPACE_OIDC_LOGIN_CONFIG',str(configfile))
    state={'exchanges':[],'nonces':{},'codes':{},'refresh_count':0,'tokens':[],'short':False}
    def issue(subject,nonce=None):
        now=int(time.time());lifetime=8 if state['short'] and nonce else 300
        access=jwt.encode({'iss':config['oidc']['issuer'],'aud':'atlas-api','sub':subject,'iat':now,'exp':now+lifetime},key,algorithm='RS256',headers={'kid':'test'})
        result={'access_token':access,'token_type':'Bearer','refresh_token':'refresh-'+subject}
        if nonce:result['id_token']=jwt.encode({'iss':config['oidc']['issuer'],'aud':'atlas-client','sub':subject,'iat':now,'exp':now+300,'nonce':nonce},key,algorithm='RS256',headers={'kid':'test'})
        return result
    def handler(request):
        if request.url.host=='id.example':
            fields=parse_qs(request.read().decode());state['exchanges'].append(fields)
            assert fields['client_secret']==['fixture-client-secret'] and fields['client_id']==['atlas-client']
            if fields['grant_type']==['authorization_code']:
                code=fields['code'][0];v=fields['code_verifier'][0];entry=state['codes'].pop(code,None)
                if not entry or base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b'=').decode()!=entry['challenge']:
                    return httpx.Response(400,json={'error':'bad grant'})
                return httpx.Response(200,json=issue(entry['subject'],entry['nonce']))
            if fields['grant_type']==['refresh_token']:
                state['refresh_count']+=1;subject=fields['refresh_token'][0].removeprefix('refresh-')
                return httpx.Response(200,json=issue(subject))
            return httpx.Response(400)
        state['tokens'].append((request.url.host,request.headers.get('authorization'),request.headers.get('cookie')))
        return httpx.Response(200,headers={'content-type':'application/json'},stream=JsonStream({'authenticated':True,'mode':'oidc','host':request.url.host}))
    app=create_app(GatewayConfig.parse(config),transport=httpx.MockTransport(handler))
    def start(client,subject='alice-sub'):
        first=client.get('/auth/login',follow_redirects=False);assert first.status_code==302
        assert 'HttpOnly' in first.headers['set-cookie'] and 'Secure' in first.headers['set-cookie'] and 'SameSite=lax' in first.headers['set-cookie']
        q=parse_qs(urlsplit(first.headers['location']).query)
        assert q['code_challenge_method']==['S256'] and q['redirect_uri']==['https://atlas.example/auth/callback']
        code='code-'+subject;state['codes'][code]={'subject':subject,'nonce':q['nonce'][0],'challenge':q['code_challenge'][0]}
        result=client.get('/auth/callback',params={'state':q['state'][0],'code':code},follow_redirects=False)
        assert result.status_code==303,result.text
        assert result.headers['location']=='/' and 'HttpOnly' in result.headers['set-cookie'] and 'Secure' in result.headers['set-cookie']
        assert 'fixture-client-secret' not in result.text and 'access_token' not in result.text
        client.headers['X-Atlas-Session']=client.get('/auth/status').json()['generation']
        return q,code
    return app,state,start,login


def test_pkce_session_routes_users_without_exposing_tokens(oidc):
    app,state,start,_=oidc
    with TestClient(app,base_url='https://atlas.example') as alice, TestClient(app,base_url='https://atlas.example') as bob:
        assert alice.get('/auth/status').json()=={'enabled':True,'authenticated':False,'generation':None}
        start(alice);start(bob,'bob-sub')
        assert alice.get('/auth/status').json()['authenticated'] is True
        assert alice.get('/api/projects').json()['host']=='atlas-alice'
        assert bob.get('/api/projects').json()['host']=='atlas-bob'
        assert all(h.startswith('Bearer ') and cookie is None for _,h,cookie in state['tokens'])
        bob_bearer=next(header for host,header,_ in state['tokens'] if host=='atlas-bob')
        assert alice.get('/api/projects',headers={'Authorization':bob_bearer}).status_code==401
        assert all('fixture-client-secret' not in x for x in alice.cookies.values())
        assert alice.post('/api/projects',json={'name':'x'}).status_code==403
        assert alice.post('/api/projects',json={'name':'x'},headers={'Origin':'https://evil.example'}).status_code==403
        assert alice.post('/api/projects',json={'name':'x'},headers={'Origin':'https://atlas.example'}).status_code==200
        assert alice.get('/api/projects',headers=[(b'X-Atlas-Session',b'\xff')]).status_code==401
        assert alice.post('/auth/logout').status_code==403
        logged_out=alice.post('/auth/logout',headers={'Origin':'https://atlas.example'})
        assert logged_out.status_code==200 and 'set-cookie' not in logged_out.headers
        assert alice.get('/api/projects').status_code==401 and bob.get('/api/projects').status_code==200
        assert alice.get('/api/projects',headers={'Authorization':bob_bearer}).status_code==401


def test_account_switch_rotates_session_and_revokes_previous(oidc):
    app,state,start,_=oidc
    with TestClient(app,base_url='https://atlas.example') as client:
        start(client);old=client.cookies.get('__Host-atlas_gateway_session')
        old_generation=client.headers['X-Atlas-Session']
        start(client,'bob-sub');new=client.cookies.get('__Host-atlas_gateway_session')
        assert old!=new
        assert client.post('/auth/logout',headers={'Origin':'https://atlas.example','X-Atlas-Session':old_generation}).status_code==401
        assert client.get('/api/projects',headers={'Cookie':'__Host-atlas_gateway_session='+old}).status_code==401
        assert client.post('/api/projects',json={'name':'must-not-run'},headers={'Origin':'https://atlas.example','X-Atlas-Session':old_generation}).status_code==401
        assert client.get('/api/projects').json()['host']=='atlas-bob'


def test_delayed_logout_response_cannot_delete_a_new_login_cookie(oidc):
    app,state,start,_=oidc
    with TestClient(app,base_url='https://atlas.example') as client:
        start(client)
        old_logout=client.post('/auth/logout',headers={'Origin':'https://atlas.example'})
        assert old_logout.status_code==200 and 'set-cookie' not in old_logout.headers
        start(client,'bob-sub');new_cookie=client.cookies.get('__Host-atlas_gateway_session')
        # Applying the old HTTP response after the new login has no cookie
        # mutation to replay; the new server session remains valid.
        assert 'set-cookie' not in old_logout.headers
        assert client.cookies.get('__Host-atlas_gateway_session')==new_cookie
        assert client.get('/api/projects').json()['host']=='atlas-bob'


def test_revoked_session_during_upload_never_reaches_backend(oidc,monkeypatch):
    import agent_service.workspace_gateway as gateway
    app,state,start,_=oidc
    with TestClient(app,base_url='https://atlas.example') as client:
        start(client)
        original=gateway.tempfile.SpooledTemporaryFile
        class RevokeOnWrite:
            def __init__(self,*args,**kwargs):self.file=original(*args,**kwargs)
            def __enter__(self):return self
            def __exit__(self,*args):self.file.close()
            def write(self,chunk):
                for session in app.state.login.sessions.values():session['revoked']=True
                return self.file.write(chunk)
            def __getattr__(self,name):return getattr(self.file,name)
        monkeypatch.setattr(gateway.tempfile,'SpooledTemporaryFile',RevokeOnWrite)
        before=len(state['tokens'])
        result=client.post('/api/projects',content=b'complete-upload',headers={'Origin':'https://atlas.example'})
        assert result.status_code==401 and len(state['tokens'])==before


def test_state_nonce_and_replay_fail_closed(oidc):
    app,state,start,_=oidc
    with TestClient(app,base_url='https://atlas.example') as client:
        first=client.get('/auth/login',follow_redirects=False);q=parse_qs(urlsplit(first.headers['location']).query)
        state['codes']['bad-nonce']={'subject':'alice-sub','nonce':'wrong','challenge':q['code_challenge'][0]}
        assert client.get('/auth/callback',params={'state':'forged','code':'bad-nonce'}).status_code==400
        assert not state['exchanges']
        assert client.get('/auth/callback',params={'state':q['state'][0],'code':'bad-nonce'}).status_code==401
        assert client.get('/auth/callback',params={'state':q['state'][0],'code':'bad-nonce'}).status_code==400
        assert client.get('/api/projects').status_code==401
        q,code=start(client)
        assert client.get('/auth/callback',params={'state':q['state'][0],'code':code}).status_code==400


def test_expiring_access_token_refreshes_once_under_concurrency(oidc):
    app,state,start,_=oidc;state['short']=True
    with TestClient(app,base_url='https://atlas.example') as client:
        start(client)
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:client.get('/api/projects').status_code,range(4)))
        assert results==[200]*4 and state['refresh_count']==1


def test_login_config_rejects_untrusted_endpoint(oidc):
    app,state,start,config=oidc
    configfile=__import__('os').environ['ATLAS_WORKSPACE_OIDC_LOGIN_CONFIG']
    config['token_endpoint']='https://attacker.example/token'
    open(configfile,'w').write(json.dumps(config))
    with pytest.raises(ValueError):
        with TestClient(app,base_url='https://atlas.example'):pass
