"""Bounded, pinned-destination transports for explicit A2A wire profiles."""
import json
from urllib.parse import urlsplit,urlunsplit

import httpx

from .store import uid

MAX_RESPONSE=4*1024*1024

class RemoteRejected(ValueError):
    def __init__(self,message,*,auth=False):super().__init__(message);self.auth=auth

class RemoteAmbiguous(ValueError):pass


def validate_url(url):
    p=urlsplit(url)
    from .mcp_stdio import local_mode
    local=local_mode() and p.scheme=='http' and p.hostname in ('localhost','127.0.0.1','::1','host.docker.internal')
    if (p.scheme!='https' and not local) or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError('Agent地址须为HTTPS且不含凭证或查询参数；本地模式可使用明确允许的本机HTTP')
    from .connectors import safe_api_url
    safe_api_url(url)
    return url


def _json(raw):
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise ValueError('协作响应有重复JSON字段')
            value[key]=item
        return value
    def invalid(_):raise ValueError('协作响应包含无效数值')
    return json.loads(raw,object_pairs_hook=unique,parse_constant=invalid)


def request(config,method,payload=None,*,url=None,notification=False):
    target_url=validate_url(url or config['url'])
    from .connectors import safe_api_url
    original=httpx.URL(target_url);ip=safe_api_url(target_url);target=original.copy_with(host=ip)
    headers={'Host':original.netloc.decode(),'Accept':'application/json','A2A-Version':config.get('protocol_version','1.0')}
    if config.get('credential'):headers['Authorization']='Bearer '+config['credential'].removeprefix('Bearer ')
    if config.get('mcp_version'):
        headers['MCP-Protocol-Version']=config['mcp_version']
        headers['Accept']='application/json, text/event-stream'
    if config.get('mcp_session'):headers['Mcp-Session-Id']=config['mcp_session']
    body=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None
    if body is not None and len(body)>MAX_RESPONSE:raise ValueError('完整协作输入超过4MB限制，未发送')
    if body is not None:headers['Content-Type']='application/json'
    try:
        from .locks import assert_owned
        assert_owned()
        if config.get('mesh_task_id'):
            from .store import get
            task=get('collab_task',config['mesh_task_id']);peer=get('collab_peer',task['peer_id'])
            if not peer.get('enabled') or peer.get('revision')!=task.get('peer_revision'):
                raise RemoteRejected('Agent授权在请求前已变化，未继续使用旧凭据',auth=True)
            if config.get('mesh_cleanup'):
                rpc_method=(payload or {}).get('method')
                permitted={'GetTask','tasks/get','CancelTask','tasks/cancel','initialize','notifications/initialized','tools/list'}
                if rpc_method=='tools/call':
                    params=(payload or {}).get('params',{})
                    expected=params.get('name') in (config.get('get_tool'),config.get('cancel_tool')) and params.get('arguments')=={'task_id':task.get('remote_task_id')}
                elif rpc_method in ('GetTask','tasks/get','CancelTask','tasks/cancel'):
                    expected=(payload or {}).get('params',{}).get('id')==task.get('remote_task_id')
                else:expected=rpc_method in permitted
                if not task.get('lifecycle_cancelled_at') or not task.get('cancel_requested') or not task.get('remote_task_id') or not expected:
                    raise RemoteRejected('回收站任务只允许取消或查询已绑定的远端编号',auth=True)
            else:
                if task.get('lifecycle_cancelled_at'):raise RemoteRejected('项目或对话已删除，未继续发送任务或资料',auth=True)
                from .agent_mesh import _check_peer
                try:_check_peer(get('turn',task['parent_turn_id']),peer)
                except ValueError:raise RemoteRejected('Agent资料或模型权限在请求前已变化，未继续外发',auth=True) from None
        with httpx.Client(timeout=httpx.Timeout(15,connect=4),trust_env=False,follow_redirects=False) as client:
            with client.stream(method,target,headers=headers,content=body,extensions={'sni_hostname':original.host}) as response:
                if response.status_code in (401,403):raise RemoteRejected('协作Agent拒绝认证或授权，需核对连接凭据',auth=True)
                if response.status_code>=300:raise RemoteAmbiguous('协作Agent未返回可核对的处理回执')
                sid=response.headers.get('Mcp-Session-Id')
                if sid:
                    if len(sid)>512 or not all(33<=ord(c)<=126 for c in sid):raise RemoteAmbiguous('MCP Agent会话标识无效')
                    config['mcp_session']=sid
                if notification:return {}
                content_type=response.headers.get('content-type','').split(';')[0]
                sse=bool(config.get('mcp_version') and content_type=='text/event-stream')
                if content_type!='application/json' and not sse:raise RemoteAmbiguous('协作Agent未返回JSON回执')
                raw=bytearray()
                buffer=b''
                for part in response.iter_bytes():
                    raw.extend(part)
                    if len(raw)>MAX_RESPONSE:raise RemoteAmbiguous('协作Agent响应超过4MB，未截断采用')
                    if sse:
                        buffer=(buffer+part).replace(b'\r\n',b'\n')
                        while b'\n\n' in buffer:
                            event,buffer=buffer.split(b'\n\n',1)
                            data=b'\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith(b'data:'))
                            if not data:continue
                            try:value=_json(data)
                            except (ValueError,UnicodeError):raise RemoteAmbiguous('MCP Agent事件JSON无效') from None
                            if isinstance(value,dict) and value.get('id')==payload.get('id') and 'method' not in value:return value
                if sse:raise RemoteAmbiguous('MCP Agent事件流没有匹配的最终回执')
                try:return _json(raw)
                except (ValueError,UnicodeError):raise RemoteAmbiguous('协作Agent返回的JSON无效') from None
    except httpx.HTTPError:raise RemoteAmbiguous('协作请求连接中断或超时，不能据此判断远端是否已执行') from None


def rpc(config,method,params):
    identity=uid()
    value=request(config,'POST',{'jsonrpc':'2.0','id':identity,'method':method,'params':params})
    if not isinstance(value,dict) or value.get('jsonrpc')!='2.0' or value.get('id')!=identity:
        raise RemoteAmbiguous('协作Agent请求编号或协议回执不一致')
    if 'error' in value:
        code=value['error'].get('code') if isinstance(value['error'],dict) else None
        if code in (-32600,-32601,-32602):raise RemoteRejected('协作Agent拒绝此协议方法或参数')
        raise RemoteAmbiguous('协作Agent报错，任务是否已创建尚不明确')
    result=value.get('result')
    if not isinstance(result,dict):raise RemoteAmbiguous('协作Agent没有返回结构化任务')
    return result


def probe(config):
    if config['transport']=='a2a':
        p=urlsplit(config['url']);card_url=urlunsplit((p.scheme,p.netloc,'/.well-known/agent-card.json','',''))
        value=request(config,'GET',url=card_url)
        version=config.get('protocol_version','1.0')
        if config.get('auto_protocol') and isinstance(value,dict) and value.get('protocolVersion')=='0.3.0':version='0.3.0'
        if version=='0.3.0':
            if not isinstance(value,dict) or value.get('protocolVersion')!='0.3.0':raise ValueError('Agent卡片未声明所选A2A 0.3.0配置')
            if value.get('url')!=config['url'] or value.get('preferredTransport','JSONRPC')!='JSONRPC':raise ValueError('Agent卡片地址或传输与配置不一致')
        else:
            interfaces=value.get('supportedInterfaces',[]) if isinstance(value,dict) else []
            if not any(i.get('protocolVersion')=='1.0' and i.get('protocolBinding')=='JSONRPC' and i.get('url')==config['url'] for i in interfaces):
                raise ValueError('Agent卡片未声明地址匹配的A2A 1.0 JSONRPC接口')
        # Never follow interfaces, card URLs or capability links supplied by a peer.
        return {'protocol_version':version,'protocol_profile':'a2a-'+version+'-jsonrpc-nonstreaming','capabilities':{'streaming':False,'pushNotifications':False},
                'card_sha256':__import__('hashlib').sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
                'tools':[]}
    from .work_tools import catalog
    with MCPAgentSession(config) as session:tools=catalog(session)
    required={config['submit_tool'],config['get_tool'],config['cancel_tool']}
    if not required<=set(tools):raise ValueError('MCP Agent缺少提交、状态或取消工具')
    return {'protocol_profile':'mcp-agent-tools','capabilities':{'streaming':False,'pushNotifications':False},'tools':list(tools.values()),'catalog':tools}


def mcp_call(config,tool,arguments):
    from .work_tools import catalog,fingerprint
    import jsonschema
    with MCPAgentSession(config) as session:
        tools=catalog(session)
        expected=config.get('catalog',{})
        if tool not in tools or fingerprint(tools[tool])!=fingerprint(expected.get(tool,{})):raise RemoteRejected('MCP Agent工具结构已变化，请重新连接')
        try:jsonschema.validate(arguments,tools[tool]['inputSchema'])
        except jsonschema.ValidationError:raise RemoteRejected('MCP Agent参数未通过已冻结工具结构验证') from None
        result=session.request('tools/call',{'name':tool,'arguments':arguments})
    if result.get('isError'):raise RemoteAmbiguous('MCP Agent返回工具错误，不能推断任务没有创建')
    value=result.get('structuredContent')
    if value is None:
        texts=[p.get('text','') for p in result.get('content',[]) if p.get('type')=='text']
        if len(texts)==1:
            try:value=_json(texts[0])
            except ValueError:pass
    if not isinstance(value,dict):raise RemoteAmbiguous('MCP Agent未返回结构化任务数据')
    return value


class MCPAgentSession:
    """JSON-only MCP profile with the same full-bundle 4MB wire budget as A2A."""
    def __init__(self,config):self.config=dict(config);self.config['mcp_version']='2025-06-18'
    def request(self,method,params=None,notification=False):
        identity=uid();payload={'jsonrpc':'2.0','method':method}
        if not notification:payload['id']=identity
        if params is not None:payload['params']=params
        result=request(self.config,'POST',payload,notification=notification)
        if notification:return {}
        if not isinstance(result,dict) or result.get('id')!=identity or result.get('jsonrpc')!='2.0':raise RemoteAmbiguous('MCP Agent回执编号不一致')
        if result.get('error'):
            code=result['error'].get('code') if isinstance(result['error'],dict) else None
            if code in (-32600,-32601,-32602):raise RemoteRejected('MCP Agent拒绝方法或参数')
            raise RemoteAmbiguous('MCP Agent请求报错，无法推断远端执行情况')
        if not isinstance(result.get('result'),dict):raise RemoteAmbiguous('MCP Agent缺少结构化回执')
        return result['result']
    def __enter__(self):
        result=self.request('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'atlas-agent-mesh','version':'0.20.0'}})
        if result.get('protocolVersion') not in ('2025-03-26','2025-06-18','2025-11-25'):raise RemoteRejected('MCP Agent协商了未支持协议版本')
        if 'tools' not in result.get('capabilities',{}):raise RemoteRejected('MCP Agent未提供工具能力')
        self.config['mcp_version']=result['protocolVersion']
        self.request('notifications/initialized',notification=True)
        return self
    def __exit__(self,*args):pass


def submit(config,task,bundle):
    payload={'project_id':config['remote_project_id'],'request_id':task['id'],'goal':task['goal'],
             'source_ids':[],'input_bundle':bundle,'provenance':task['provenance']}
    if config.get('remote_model_id'):payload['model_id']=config['remote_model_id']
    if config['transport']=='mcp':return mcp_call(config,config['submit_tool'],payload)
    if config.get('protocol_version','1.0')=='1.0':
        result=rpc(config,'SendMessage',{'message':{'role':'ROLE_USER','messageId':task['id'],
            'parts':[{'text':task['goal']},{'data':{'atlas':payload}}]},
            'configuration':{'returnImmediately':True,'acceptedOutputModes':['application/json','text/plain']}})
        if not isinstance(result.get('task'),dict):raise RemoteAmbiguous('A2A 1.0未返回异步任务编号')
        return result['task']
    return rpc(config,'message/send',{'message':{'kind':'message','role':'user','messageId':task['id'],
        'parts':[{'kind':'text','text':task['goal']},{'kind':'data','data':{'atlas':payload}}]},
        'configuration':{'blocking':False,'acceptedOutputModes':['application/json','text/plain']}})


def poll(config,remote_id):
    if config['transport']=='mcp':return mcp_call(config,config['get_tool'],{'task_id':remote_id})
    return rpc(config,'GetTask' if config.get('protocol_version','1.0')=='1.0' else 'tasks/get',{'id':remote_id,'historyLength':0})


def cancel(config,remote_id):
    if config['transport']=='mcp':return mcp_call(config,config['cancel_tool'],{'task_id':remote_id})
    return rpc(config,'CancelTask' if config.get('protocol_version','1.0')=='1.0' else 'tasks/cancel',{'id':remote_id})
