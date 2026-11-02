"""Optional OIDC authorization-code login for the isolated workspace gateway.

Tokens stay in gateway process memory and are sent only to the configured IdP or
verified workspace backend. This is deliberately not a distributed session store.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

SESSION_COOKIE='__Host-nexus_gateway_session'
STATE_COOKIE='__Host-nexus_gateway_state'


def _endpoint(value, issuer):
    if not isinstance(value,str) or len(value)>2048:raise ValueError('Invalid IdP endpoint')
    p=urlsplit(value);i=urlsplit(issuer)
    if (p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment
        or p.hostname.lower()!=i.hostname.lower() or (p.port or 443)!=(i.port or 443) or '\\' in value):
        raise ValueError('IdP endpoints must use the configured HTTPS issuer origin')
    return value


class Login:
    def __init__(self,active,configuration,client):
        allowed={'client_id','client_secret_file','authorization_endpoint','token_endpoint','scope','session_seconds'}
        if not isinstance(configuration,dict) or set(configuration)-allowed or not {'client_id','client_secret_file','authorization_endpoint','token_endpoint'}<=set(configuration):
            raise ValueError('Invalid OIDC login configuration')
        self.client_id=configuration['client_id']
        if not isinstance(self.client_id,str) or not 1<=len(self.client_id)<=256 or any(ord(c)<33 for c in self.client_id):raise ValueError('Invalid OIDC client id')
        secret=Path(configuration['client_secret_file'])
        if not secret.is_absolute() or secret.is_symlink() or not secret.is_file() or secret.stat().st_size>4096:
            raise ValueError('OIDC client secret must be a bounded regular file')
        self.secret=secret.read_text().strip()
        if not 1<=len(self.secret)<=4096 or '\n' in self.secret or '\r' in self.secret:raise ValueError('Invalid OIDC client secret')
        self.authorize=_endpoint(configuration['authorization_endpoint'],active.issuer)
        self.token_url=_endpoint(configuration['token_endpoint'],active.issuer)
        scope=configuration.get('scope','openid offline_access')
        if not isinstance(scope,str) or len(scope)>512 or 'openid' not in scope.split() or not all(x.isascii() and x.replace('_','').isalnum() for x in scope.split()):raise ValueError('Invalid OIDC scope')
        self.scope=scope
        self.lifetime=configuration.get('session_seconds',28800)
        if type(self.lifetime) is not int or not 300<=self.lifetime<=86400:raise ValueError('Invalid login session lifetime')
        self.active=active;self.client=client;self.pending={};self.sessions={}
        self.callback=active.public_origin+'/auth/callback'

    def _prune(self):
        now=time.time()
        self.pending={k:v for k,v in self.pending.items() if v['expires']>now}
        self.sessions={k:v for k,v in self.sessions.items() if v['deadline']>now and not v.get('revoked')}

    def has_session(self,request):
        self._prune();session=self.sessions.get(request.cookies.get(SESSION_COOKIE,''))
        return bool(session and (session['expiry']>time.time() or session.get('refresh')))

    def generation(self,request):
        if not self.has_session(request):return None
        return self.sessions[request.cookies.get(SESSION_COOKIE,'')]['generation']

    def still_current(self,request,generation,token):
        session=self.sessions.get(request.cookies.get(SESSION_COOKIE,''))
        return bool(session and not session.get('revoked') and session['deadline']>time.time()
                    and session['expiry']>time.time() and session['generation']==generation and session['token']==token)

    def start(self):
        self._prune()
        if len(self.pending)>=10000:raise HTTPException(503,'登录请求过多，请稍后重试')
        state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(64);nonce=secrets.token_urlsafe(32)
        self.pending[state]={'verifier':verifier,'nonce':nonce,'expires':time.time()+300}
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        params={'response_type':'code','client_id':self.client_id,'redirect_uri':self.callback,'scope':self.scope,
                'state':state,'nonce':nonce,'code_challenge':challenge,'code_challenge_method':'S256'}
        response=RedirectResponse(self.authorize+'?'+urlencode(params),status_code=302)
        response.set_cookie(STATE_COOKIE,state,secure=True,httponly=True,samesite='lax',path='/',max_age=300)
        return response

    def _id_claims(self,raw,nonce=None):
        if not isinstance(raw,str) or not 1<=len(raw)<=16000:raise ValueError('Invalid ID token')
        header=jwt.get_unverified_header(raw)
        alg,key=self.active.keys[header['kid']]
        if header.get('alg')!=alg or header.get('crit') or header.get('jku') or header.get('jwk') or header.get('x5u'):
            raise ValueError('Unsupported ID token header')
        claims=jwt.decode(raw,key,algorithms=[alg],audience=self.client_id,issuer=self.active.issuer,
                          leeway=5,options={'require':['exp','iat','iss','aud','sub']})
        aud=claims.get('aud')
        if isinstance(aud,list) and len(aud)>1 and claims.get('azp')!=self.client_id:raise ValueError('ID token party mismatch')
        if nonce is not None and (not isinstance(claims.get('nonce'),str) or not hmac.compare_digest(claims['nonce'],nonce)):
            raise ValueError('ID token nonce mismatch')
        return claims

    async def _tokens(self,data):
        try:
            async with self.client.stream('POST',self.token_url,data={**data,'client_id':self.client_id,'client_secret':self.secret},
                                          headers={'Accept':'application/json'}) as response:
                if response.status_code!=200:raise ValueError()
                raw=bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw)>64000:raise ValueError()
            payload=json.loads(raw)
            kind=payload.get('token_type') if isinstance(payload,dict) else None
            if not isinstance(kind,str) or kind.lower()!='bearer':raise ValueError()
            token=payload.get('access_token');refresh=payload.get('refresh_token')
            self.active.authenticate(token)
            claims=jwt.decode(token,options={'verify_signature':False})
            if refresh is not None and (not isinstance(refresh,str) or not 1<=len(refresh)<=16000):raise ValueError()
            return payload,token,refresh,claims
        except (ValueError,TypeError,KeyError,httpx.HTTPError,HTTPException,jwt.PyJWTError):
            raise HTTPException(401,'企业登录未成功，请重新登录') from None

    async def callback_response(self,request):
        states=request.query_params.getlist('state');codes=request.query_params.getlist('code')
        cookie=request.cookies.get(STATE_COOKIE,'')
        if (len(states)!=1 or len(codes)!=1 or not re.fullmatch(r'[A-Za-z0-9_-]{43}',cookie)
            or not re.fullmatch(r'[A-Za-z0-9_-]{43}',states[0]) or not hmac.compare_digest(states[0],cookie)
            or len(codes[0])>4096):raise HTTPException(400,'登录状态不匹配，请重新登录')
        transaction=self.pending.pop(states[0],None)
        if not transaction or transaction['expires']<=time.time():raise HTTPException(400,'登录请求已过期，请重新登录')
        payload,token,refresh,claims=await self._tokens({'grant_type':'authorization_code','code':codes[0],
                  'code_verifier':transaction['verifier'],'redirect_uri':self.callback})
        try:
            identity=self._id_claims(payload.get('id_token'),transaction['nonce'])
            if identity.get('sub')!=claims.get('sub'):raise ValueError()
        except Exception:raise HTTPException(401,'企业身份校验失败，请重新登录') from None
        self._prune()
        if len(self.sessions)>=10000:raise HTTPException(503,'登录会话已满')
        # Switching identities invalidates the old browser session; never
        # upgrade or reuse an attacker-chosen session identifier.
        old=self.sessions.pop(request.cookies.get(SESSION_COOKIE,''),None)
        if old:old['revoked']=True
        sid=secrets.token_urlsafe(32)
        self.sessions[sid]={'token':token,'refresh':refresh,'subject':claims['sub'],'expiry':claims['exp'],
                            'deadline':time.time()+self.lifetime,'lock':asyncio.Lock(),'revoked':False,
                            'generation':secrets.token_urlsafe(32)}
        result=RedirectResponse('/',status_code=303)
        result.delete_cookie(STATE_COOKIE,path='/',secure=True,httponly=True,samesite='lax')
        result.set_cookie(SESSION_COOKIE,sid,secure=True,httponly=True,samesite='lax',path='/',max_age=self.lifetime)
        return result

    async def token(self,request):
        self._prune();sid=request.cookies.get(SESSION_COOKIE,'');session=self.sessions.get(sid)
        if not session or session['deadline']<=time.time():raise HTTPException(401,'企业登录已过期')
        async with session['lock']:
            if session.get('revoked') or self.sessions.get(sid) is not session:raise HTTPException(401,'企业登录已过期')
            if session['expiry']<=time.time()+10:
                if not session['refresh']:
                    session['revoked']=True;raise HTTPException(401,'企业登录已过期')
                try:
                    payload,token,refresh,claims=await self._tokens({'grant_type':'refresh_token','refresh_token':session['refresh']})
                    if claims['sub']!=session['subject']:raise ValueError()
                    if payload.get('id_token') and self._id_claims(payload['id_token']).get('sub')!=session['subject']:raise ValueError()
                except Exception:
                    session['revoked']=True
                    raise HTTPException(401,'企业登录已过期，请重新登录') from None
                if session.get('revoked') or self.sessions.get(sid) is not session:
                    raise HTTPException(401,'企业登录已退出')
                session.update(token=token,refresh=refresh or session['refresh'],expiry=claims['exp'])
            self.active.authenticate(session['token'])
            return session['token']

    def logout(self,request):
        sid=request.cookies.get(SESSION_COOKIE,'');session=self.sessions.pop(sid,None)
        if session:session['revoked']=True
        # Revoke server-side only: a delayed logout response must not delete a
        # newer login's cookie. The next login replaces this now-invalid value.
        return JSONResponse({'ok':True})
