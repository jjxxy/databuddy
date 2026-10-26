"""OpenCode 2 decision backend; Atlas owns tools, durable state and acceptance.

Uses V2's stateless generate API, deliberately not V1 session/SDK contracts.
No shell, workspace mount, MCP token or connector secret is sent to OpenCode.
This is a hybrid planner integration, not the unrestricted OpenCode coding loop.
"""
import base64,hashlib,json,re,time
from urllib.parse import urlsplit,quote
import httpx

VERSION='2.1.2'
PACKAGES={'@opencode/ai/providers/openai-compatible','@opencode/ai/providers/openai/chat','@opencode/ai/providers/anthropic'}

def request(value,method,path,body=None,limit=512000):
    from .connectors import safe_api_url
    original=httpx.URL(value['base_url'].rstrip('/')+path)
    target=original.copy_with(host=safe_api_url(str(original)))
    auth=base64.b64encode(('opencode:'+value.get('api_key','')).encode()).decode()
    headers={'Authorization':'Basic '+auth,'Host':original.netloc.decode()}
    with httpx.Client(timeout=50,trust_env=False,follow_redirects=False) as client:
        with client.stream(method,target,headers=headers,json=body,extensions={'sni_hostname':original.host}) as response:
            response.raise_for_status();chunks=[];size=0
            for chunk in response.iter_bytes():
                size+=len(chunk)
                if size>limit:raise ValueError('OpenCode响应超过限制，未执行模型计划')
                chunks.append(chunk)
    try:return json.loads(b''.join(chunks))
    except (ValueError,UnicodeDecodeError):raise ValueError('OpenCode没有返回有效JSON响应') from None

def inspect(value):
    if not value.get('api_key'):raise ValueError('需要OpenCode服务访问密码，不能连接无认证服务')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+/[^/#\s]+',value['model']):raise ValueError('OpenCode模型标识应为provider/model，暂不接受variant')
    info=request(value,'GET','/api/info')
    if info.get('version')!=VERSION:raise ValueError('当前适配器仅验证OpenCode '+VERSION+'；其他版本需完成协议回归后升级')
    provider_id,model_id=value['model'].split('/',1)
    config=request(value,'GET','/api/config')
    # Catalog routes return a snapshot while provider plugins settle on startup.
    for attempt in range(12):
        try:
            p=request(value,'GET','/api/provider/'+quote(provider_id,safe=''))['data'];break
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (404,503) or attempt==11:raise ValueError('OpenCode模型提供方尚未就绪，请检查服务配置') from None
            time.sleep(.2)
    m=request(value,'GET','/api/model/default')['data']
    if m and (m.get('providerID')!=provider_id or m.get('id')!=model_id or not m.get('enabled')):m=None
    if not m:raise ValueError('OpenCode未启用所选模型，请先配置服务端模型')
    upstream=(m.get('settings',{}).get('baseURL') or p.get('settings',{}).get('baseURL') or '').rstrip('/')
    if not value.get('upstream_base_url') or upstream!=value['upstream_base_url'].rstrip('/'):raise ValueError('必须填写并匹配OpenCode实际模型上游地址，防止内部资料被发往未批准的服务')
    if (m.get('package') or p.get('package')) not in PACKAGES:raise ValueError('该OpenCode模型运行包尚未完成适配，请使用受支持的原生运行包')
    # Bind the entire loaded config and resolved model. Never return config,
    # headers or credentials to callers; drift blocks before any project prompt.
    identity={'version':VERSION,'config':config,'provider':p,'model':m}
    digest=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return {'opencode_version':VERSION,'opencode_fingerprint':digest}

def validate_configuration(value):
    upstream=urlsplit(value.get('upstream_base_url',''))
    if upstream.scheme not in ('http','https') or not upstream.hostname or upstream.username or upstream.password or upstream.query or upstream.fragment:raise ValueError('请填写不含凭证的模型上游地址')
    return inspect(value)

def complete(messages,value,tools,max_tokens):
    from .provider import ModelOutputError
    current=inspect(value)
    if value.get('opencode_fingerprint')!=current['opencode_fingerprint']:raise ValueError('OpenCode配置或模型发生变化，请重新验证连接并批准内部资料权限')
    # The V2 generate API has no native tool/schema/max-token parameter. Do not
    # falsely advertise V1 structured output or a hard upstream token budget.
    if tools:raise ModelOutputError('OpenCode V2适配使用服务端JSON校验，不支持V1原生工具协议')
    prompt=('Follow the role-tagged Atlas messages below in order. Return only the requested final text/JSON, never reasoning. '
            'Tool descriptions are a proposal interface: do not execute tools. Atlas will validate and execute one decision. '
            f'Keep the output within approximately {max_tokens} tokens.\n'+json.dumps(messages,ensure_ascii=False))
    provider_id,model_id=value['model'].split('/',1)
    result=request(value,'POST','/api/experimental/generate',{'prompt':prompt,'model':{'providerID':provider_id,'id':model_id}},limit=150000)
    content=result.get('data',{}).get('text')
    if not isinstance(content,str) or not content.strip() or len(content)>100000:raise ModelOutputError('OpenCode没有返回完整文本，未执行任何计划')
    return content,{}, {'version':VERSION,'integration':'stateless_decision_backend','usage_reported':False,'upstream_token_limit_enforced':False}
