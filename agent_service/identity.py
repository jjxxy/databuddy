"""Optional signed OIDC identity for an owner-scoped workspace behind an OIDC gateway.

No trust in unsigned proxy headers, JWT-selected URLs or shared-token fallback in
OIDC mode. This does not implement multiple isolated tenants.
"""
import json,os,time,threading
from urllib.parse import urlsplit
import jwt,httpx
from fastapi import HTTPException
_cache={};_lock=threading.Lock()

def mode():return os.getenv('ATLAS_AUTH_MODE','token')
def validate_config():
    if mode() not in ('token','oidc'):raise ValueError('未知认证模式')
    if mode()=='oidc':
        for name in ('OIDC_ISSUER','OIDC_AUDIENCE','OIDC_JWKS_URL','OIDC_ALLOWED_SUBJECTS'):
            if not os.getenv(name):raise ValueError('OIDC配置不完整：'+name)
        for name in ('OIDC_ISSUER','OIDC_JWKS_URL'):
            p=urlsplit(os.environ[name])
            if p.scheme!='https' or not p.hostname or p.username or p.password or p.fragment:raise ValueError('OIDC端点必须为HTTPS且无凭证')

def keys(refresh=False):
    with _lock:
        if not refresh and _cache.get('until',0)>time.monotonic():return _cache['keys']
        if refresh and time.monotonic()-_cache.get('fetched',-100)<5:return _cache.get('keys',[])
        from .connectors import safe_api_url
        original=httpx.URL(os.environ['OIDC_JWKS_URL']);target=original.copy_with(host=safe_api_url(str(original)))
        with httpx.Client(timeout=5,follow_redirects=False,trust_env=False) as c:
            with c.stream('GET',target,headers={'Host':original.netloc.decode(),'Accept':'application/json'},extensions={'sni_hostname':original.host}) as response:
                response.raise_for_status();raw=bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw)>256000:raise ValueError('JWKS过长')
        value=json.loads(raw).get('keys',[])
        if not isinstance(value,list) or len(value)>100:raise ValueError('JWKS无效')
        _cache.update(keys=value,until=time.monotonic()+300,fetched=time.monotonic());return value

def principal(request):
    validate_config()
    token=request.headers.get('Authorization','').removeprefix('Bearer ').strip()
    if (not token) and os.getenv('OIDC_ACCEPT_PROXY_HEADER')=='1':token=request.headers.get('X-Forwarded-Access-Token','')
    if len(token)>16000:raise HTTPException(401,'身份凭据无效')
    try:
        header=jwt.get_unverified_header(token)
        if header.get('alg') not in ('RS256','ES256') or not isinstance(header.get('kid'),str):raise ValueError()
        selected=[k for k in keys() if k.get('kid')==header['kid']]
        if not selected:selected=[k for k in keys(True) if k.get('kid')==header['kid']]
        if len(selected)!=1:raise ValueError()
        value=jwt.decode(token,jwt.PyJWK.from_dict(selected[0]).key,algorithms=[header['alg']],audience=os.environ['OIDC_AUDIENCE'],issuer=os.environ['OIDC_ISSUER'],leeway=30,options={'require':['exp','iss','aud','sub']})
        subjects={s.strip() for s in os.environ['OIDC_ALLOWED_SUBJECTS'].split(',') if s.strip()}
        if value['sub'] not in subjects:raise HTTPException(403,'此账号没有该个人工作空间的权限')
        return {'authenticated':True,'mode':'oidc','subject':value['sub'],'scope':'owner_workspace'}
    except HTTPException:raise
    except Exception:raise HTTPException(401,'身份凭据无效或已过期') from None
