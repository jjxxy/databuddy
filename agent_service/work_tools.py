"""Owner-scoped MCP tools and frozen, single-use approvals for external writes.

MCP annotations are advisory. Only the owner's read allowlist grants read access.
An ambiguous external write is never automatically replayed.
"""
import hashlib,json,re,time
from urllib.parse import urlsplit
import httpx,jsonschema
from pydantic import Field,SecretStr
from sqlalchemy import select,update
from .models import StrictModel
from .store import get,put,patch,all_items,seal,unseal,uid,now,transaction,items

class ConnectorInput(StrictModel):
    name:str=Field(min_length=1,max_length=80)
    url:str=Field(min_length=8,max_length=1000)
    credential:SecretStr=Field(default=SecretStr(''))
    read_tools:list[str]=Field(default_factory=list,max_length=100)
    write_tools:list[str]=Field(default_factory=list,max_length=100)
    storage_approved:bool=False
    model_approved:bool=False
    approved_model_ids:list[str]=Field(default_factory=list,max_length=32)
class ConnectorTarget(StrictModel):connector_id:str=Field(max_length=64)
class ToolCall(ConnectorTarget):
    tool:str=Field(min_length=1,max_length=120)
    arguments:dict=Field(default_factory=dict)
class ActionDecision(StrictModel):
    digest:str=Field(pattern=r'^[a-f0-9]{64}$')
    approve:bool
class ActionTarget(StrictModel):action_id:str=Field(max_length=64)

class RPCError(ValueError):pass

class HTTPSession:
    def __init__(self,config):self.config=config;self.session=None;self.version='2025-06-18'
    def request(self,method,params=None,notification=False):
        from .connectors import safe_api_url
        original=httpx.URL(self.config['url']);ip=safe_api_url(str(original));target=original.copy_with(host=ip)
        key=uid();payload={'jsonrpc':'2.0','method':method}
        if not notification:payload['id']=key
        if params is not None:payload['params']=params
        headers={'Host':original.netloc.decode(),'Accept':'application/json, text/event-stream','MCP-Protocol-Version':self.version}
        if self.config.get('credential'):headers['Authorization']=self.config['credential']
        if self.session:headers['Mcp-Session-Id']=self.session
        with httpx.Client(timeout=httpx.Timeout(30,connect=5),trust_env=False,follow_redirects=False) as client:
            with client.stream('POST',target,json=payload,headers=headers,extensions={'sni_hostname':original.host}) as response:
                if response.status_code>=300:raise RPCError('工作系统请求未成功；请检查连接权限或系统状态')
                session=response.headers.get('Mcp-Session-Id')
                if session:
                    if len(session)>512 or not re.fullmatch(r'[\x21-\x7e]+',session):raise RPCError('工作系统会话标识无效')
                    self.session=session
                if notification:return {}
                content=response.headers.get('content-type','').split(';')[0];raw=bytearray()
                if content=='text/event-stream':
                    buffer='';size=0
                    for line in response.iter_lines():
                        size+=len(line.encode())+1
                        if size>1024*1024:raise RPCError('工作系统返回超过1MB限制')
                        if line.startswith('data:'):buffer+=line[5:].lstrip()+'\n'
                        elif not line and buffer:
                            value=json.loads(buffer);buffer=''
                            if value.get('id')==key and 'method' not in value:return self.result(value)
                    raise RPCError('工作系统未返回匹配的执行回执')
                if content!='application/json':raise RPCError('此连接需要MCP Streamable HTTP JSON或SSE响应')
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw)>1024*1024:raise RPCError('工作系统返回超过1MB限制')
                value=json.loads(raw)
                if value.get('id')!=key:raise RPCError('工作系统返回的请求编号不匹配')
                return self.result(value)
    @staticmethod
    def result(value):
        if 'error' in value:raise RPCError('工作系统拒绝了本次调用，请核对工具参数及账号权限')
        if not isinstance(value.get('result'),dict):raise RPCError('工作系统返回结构无效')
        return value['result']
    def __enter__(self):
        value=self.request('initialize',{'protocolVersion':self.version,'capabilities':{},'clientInfo':{'name':'atlas-workspace','version':'0.16.1'}})
        version=value.get('protocolVersion')
        if version not in ('2025-06-18','2025-03-26'):raise RPCError('工作系统协商了当前未验证的MCP协议版本')
        self.version=version
        if 'tools' not in value.get('capabilities',{}):raise RPCError('此MCP连接未提供工具能力')
        self.request('notifications/initialized',notification=True);return self
    def __exit__(self,*args):pass

class Session:
    def __new__(cls,config):
        if config.get('transport')=='stdio':
            from .mcp_stdio import Session as StdioSession
            return StdioSession(config)
        return HTTPSession(config)

def visible(record):
    from .actor_context import current_actor
    return not record.get('owner_id') or record['owner_id']==current_actor()

def fingerprint(config):return hashlib.sha256(json.dumps(config,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def schema_ok(schema):
    def visit(v):
        if isinstance(v,dict):
            if '$ref' in v and not v['$ref'].startswith('#'):raise ValueError('不接受远程JSON Schema引用')
            for x in v.values():visit(x)
        elif isinstance(v,list):
            for x in v:visit(x)
    if len(json.dumps(schema))>64000:raise ValueError('工具结构过长')
    visit(schema);jsonschema.Draft202012Validator.check_schema(schema)

def catalog(session):
    tools={};cursor=None;visited=set()
    for _ in range(10):
        value=session.request('tools/list',{'cursor':cursor} if cursor else {})
        for t in value.get('tools',[]):
            if len(tools)>=100:raise ValueError('每个工作连接最多支持100个工具')
            name=t.get('name','')
            if not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,120}',name):raise ValueError('工具名称无效')
            schema=t.get('inputSchema',{});schema_ok(schema)
            tools[name]={'name':name,'description':str(t.get('description',''))[:2000],'inputSchema':schema}
        cursor=value.get('nextCursor')
        if not cursor:return tools
        if cursor in visited:raise ValueError('工作工具目录分页重复')
        visited.add(cursor)
    raise ValueError('工作工具目录超过分页限制')

def owned(pid,cid):
    c=get('work_connector',cid)
    if c['project_id']!=pid or not visible(c):raise ValueError('工作连接不属于当前用户或项目')
    if not c.get('enabled'):raise ValueError('工作连接已停用')
    return c

def model_allowed(connector):
    from .company_search import model_approval_allowed
    from .provider import settings
    if not settings().get('share_data') or not model_approval_allowed(connector):raise ValueError('需由管理员在后台批准当前模型处理该系统资料')

def public(c):
    # All accepted historical connectors required this consent at save time.
    return {k:v for k,v in c.items() if k not in ('secret_config','model_fingerprint','approved_models')}|{'storage_approved':c.get('storage_approved',True)}
def configure(pid,value,cid=None,*,owner_id=None,origin=None,preset_id=None):
    from .connectors import safe_api_url
    from .company_search import model_fingerprint,capture_model_approvals
    get('project',pid);p=urlsplit(value.url)
    from .mcp_stdio import local_mode
    local_http=local_mode() and p.scheme=='http' and p.hostname in ('localhost','127.0.0.1','::1','host.docker.internal')
    if (p.scheme!='https' and not local_http) or p.username or p.password or p.query or p.fragment:raise ValueError('工作连接必须使用HTTPS；本地模式可使用已允许的本机HTTP地址')
    safe_api_url(value.url)
    if not value.storage_approved:raise ValueError('需确认部署允许保存该系统资料')
    if value.approved_model_ids and not value.model_approved:raise ValueError('批准指定模型需要明确启用模型处理许可')
    approved_models=capture_model_approvals(value.approved_model_ids)
    credential=value.credential.get_secret_value()
    if cid:
        old=owned(pid,cid)
        if old['url']==value.url and not credential:credential=unseal(old['secret_config']).get('credential','')
        if owner_id is None:owner_id=old.get('owner_id')
        if origin is None:origin=old.get('extension_origin')
        if preset_id is None:preset_id=old.get('preset_id')
    cfg={'url':value.url,'credential':credential}
    with Session(cfg) as session:tools=catalog(session)
    return save_connector(pid,value,cfg,tools,cid,owner_id=owner_id,origin=origin,preset_id=preset_id)

def save_connector(pid,value,cfg,tools,cid=None,*,owner_id=None,origin=None,preset_id=None,model_policy=None):
    """Publish a fully verified transport, including ownership in the same write."""
    from .company_search import model_fingerprint,capture_model_approvals
    get('project',pid)
    if not value.storage_approved:raise ValueError('需确认部署允许保存该系统资料')
    if value.approved_model_ids and not value.model_approved:raise ValueError('批准指定模型需要明确启用模型处理许可')
    approved_models=(model_policy or {}).get('approved_models') if model_policy is not None else capture_model_approvals(value.approved_model_ids)
    allowed=set(value.read_tools)|set(value.write_tools)
    if set(value.read_tools)&set(value.write_tools) or not allowed<=set(tools):raise ValueError('工具允许列表包含未知或冲突项')
    if not allowed:raise ValueError('请明确选择允许的只读工具或需确认的写入工具')
    record={'project_id':pid,'name':value.name,'url':value.url or ('stdio:'+value.command),'transport':cfg.get('transport','http'),'read_tools':value.read_tools,'write_tools':value.write_tools,'enabled':True,'secret_config':seal(cfg),'catalog':tools,'revision':uid(),'model_approved':value.model_approved,'approved_model_ids':value.approved_model_ids,'approved_models':approved_models or {},'model_fingerprint':(model_policy or {}).get('model_fingerprint') if model_policy is not None else (model_fingerprint() if value.model_approved else None),'updated_at':now(),**({'command':value.command} if cfg.get('transport')=='stdio' else {}),**({'owner_id':owner_id} if owner_id else {}),**({'extension_origin':origin} if origin else {}),**({'preset_id':preset_id} if preset_id else {})}
    record['storage_approved']=True
    saved=put('work_connector',record,cid)
    # A project can contain observations from several systems. Its complete
    # history can leave the workspace only when every system's owner-approved
    # model policy permits it. The last connected system must not override
    # restrictions on data already obtained from earlier systems.
    policies={c['id']:{k:c.get(k) for k in ('model_approved','model_fingerprint','approved_model_ids','approved_models')} for c in all_items('work_connector') if c['project_id']==pid}
    patch('project',pid,work_private=True,work_model_approval=model_fingerprint() if value.model_approved else None,work_model_policies=policies)
    for t in all_items('turn'):
        if t['project_id']==pid:patch('turn',t['id'],work_private=True)
    return public(saved)

def list_connectors(pid):return [public(c) for c in all_items('work_connector') if c['project_id']==pid and visible(c)]
def list_tools(turn,cid):
    c=owned(turn['project_id'],cid);model_allowed(c)
    return {'connector_id':cid,'tools':[dict(c['catalog'][n],access='read' if n in c['read_tools'] else 'requires_confirmation') for n in c['read_tools']+c['write_tools']]}

def checked(turn,value,write=False):
    c=owned(turn['project_id'],value.connector_id);model_allowed(c)
    if value.tool not in c['write_tools' if write else 'read_tools']:raise ValueError('此工具不在已批准的'+('写入' if write else '只读')+'允许列表')
    if len(json.dumps(value.arguments,ensure_ascii=False).encode())>64000:raise ValueError('工作工具参数过长')
    schema=c['catalog'][value.tool]['inputSchema'];schema_ok(schema)
    try:jsonschema.validate(value.arguments,schema)
    except jsonschema.ValidationError:raise ValueError('工作工具参数未通过结构验证') from None
    # Never ask the model to transport credentials in arguments.
    from .conversation import redact
    raw=json.dumps(value.arguments,ensure_ascii=False)
    if redact(raw)!=raw:raise ValueError('凭证只能由部署者在连接设置中保存')
    return c

def read_tool(turn,value):
    c=checked(turn,value)
    with Session(unseal(c['secret_config'])) as session:
        fresh=catalog(session)
        if value.tool not in fresh or fingerprint(fresh[value.tool])!=fingerprint(c['catalog'][value.tool]):raise ValueError('工具定义已变化，请重新验证工作连接')
        result=session.request('tools/call',{'name':value.tool,'arguments':value.arguments})
    if result.get('isError'):raise ValueError('工作系统返回工具失败，不得视为完成')
    return {'connector_id':c['id'],'tool':value.tool,'result':result,'untrusted_content':True}

def propose(turn,value):
    from .intents import require
    require(turn,'write');c=checked(turn,value,True)
    from .conversation import stable
    trace=get('turn',turn['id']).get('agent_trace',[])
    if not trace:raise ValueError('缺少持久工具步骤')
    aid=stable('wa_',turn.get('agent_origin',turn['id']),trace[-1]['id'])
    try:return public_action(get('work_action',aid))
    except KeyError:pass
    payload={'connector_id':c['id'],'connector_revision':c['revision'],'tool':value.tool,'arguments':value.arguments,'tool_fingerprint':fingerprint(c['catalog'][value.tool])}
    digest=fingerprint(payload)
    put('work_action',{'project_id':turn['project_id'],'session_id':turn['session_id'],'turn_id':turn['id'],'owner_id':c.get('owner_id'),'state':'pending','digest':digest,'secret_payload':seal(payload),'created_at':now(),'expires':time.time()+86400},aid)
    return public_action(get('work_action',aid))

def public_action(a,include_result=True):
    payload=unseal(a['secret_payload']);result={k:v for k,v in a.items() if k not in ('secret_payload','secret_output')}
    result.update(tool=payload['tool'],arguments=payload['arguments'],connector_id=payload['connector_id'])
    if include_result and a.get('secret_output'):result['result']=unseal(a['secret_output'])
    return result

def actions(pid,turn_id=None):return [public_action(a,False) for a in all_items('work_action') if a['project_id']==pid and visible(a) and (not turn_id or a['turn_id']==turn_id)]

def decide(pid,aid,value):
    from .jobs import enqueue
    with transaction() as c:
        row=c.execute(select(items.c.body).where(items.c.id==aid,items.c.kind=='work_action').with_for_update()).first()
        if not row:raise KeyError(aid)
        a=json.loads(row[0])
        if a['project_id']!=pid or not visible(a) or a['digest']!=value.digest:raise ValueError('确认对象、用户或参数已变化')
        if a['expires']<time.time():raise ValueError('本次确认已过期，请重新提出操作')
        if a['state']=='pending':
            a.update(state='approved' if value.approve else 'rejected',decided_at=now());c.execute(update(items).where(items.c.id==aid).values(body=json.dumps(a)))
        elif a['state']=='rejected' and value.approve:raise ValueError('本次操作已拒绝，请重新提出操作')
    if a['state']=='approved':enqueue('work_action',aid,scheduled_key='work-action:'+aid,max_attempts=1)
    return public_action(get('work_action',aid))

def perform(aid,before_call=None):
    with transaction() as c:
        row=c.execute(select(items.c.body).where(items.c.id==aid,items.c.kind=='work_action').with_for_update()).first()
        if not row:raise KeyError(aid)
        a=json.loads(row[0])
        if a['state']=='succeeded':return {'state':'succeeded'}
        if a['state']=='executing':
            a.update(state='unknown',finished_at=now());c.execute(update(items).where(items.c.id==aid).values(body=json.dumps(a)));return {'state':'unknown'}
        if a['state']!='approved':return {'state':a['state']}
        if a['expires']<time.time():
            a.update(state='expired',finished_at=now());c.execute(update(items).where(items.c.id==aid).values(body=json.dumps(a)));return {'state':'expired'}
        a.update(state='executing');c.execute(update(items).where(items.c.id==aid).values(body=json.dumps(a)))
    called=False
    try:
        p=unseal(a['secret_payload'])
        # The worker has no active chat context. Check the originating turn's
        # frozen model, rather than whichever model is currently the default.
        from .provider import task_scope
        with task_scope(a['turn_id']):
            conn=owned(a['project_id'],p['connector_id']);model_allowed(conn)
        if conn['revision']!=p['connector_revision'] or p['tool'] not in conn['write_tools'] or a['expires']<time.time():raise ValueError('工作连接或确认范围已变化')
        with Session(unseal(conn['secret_config'])) as session:
            fresh=catalog(session)
            if p['tool'] not in fresh or fingerprint(fresh[p['tool']])!=p['tool_fingerprint']:raise ValueError('工具定义已变化')
            if before_call:before_call()
            called=True
            result=session.request('tools/call',{'name':p['tool'],'arguments':p['arguments'],'_meta':{'atlas/idempotencyKey':aid}})
        state='failed' if result.get('isError') else 'succeeded'
        patch('work_action',aid,state=state,secret_output=seal(result),finished_at=now())
    except Exception:
        state='unknown' if called else 'failed';patch('work_action',aid,state=state,finished_at=now(),error='结果未确认，请在原系统核对；不会自动重复执行' if called else '连接或权限未通过验证，未提交外部操作')
    return {'state':state}

def queue_approved():
    from .jobs import enqueue
    for a in all_items('work_action'):
        if a['state']=='approved':enqueue('work_action',a['id'],scheduled_key='work-action:'+a['id'],max_attempts=1)

def cancel_actions(pid,cid):
    from .jobs import list_jobs,request_cancel
    for a in all_items('work_action'):
        if a['project_id']!=pid or a['session_id']!=cid:continue
        if a['state'] in ('pending','approved'):patch('work_action',a['id'],state='cancelled',finished_at=now())
        if a['state'] in ('approved','executing'):
            for job in list_jobs(target=a['id']):
                if job['status'] in ('queued','running','retrying'):request_cancel(job['id'])

def reconcile_actions():
    from .jobs import list_jobs,enqueue
    for a in all_items('work_action'):
        if a['state']=='pending' and a['expires']<time.time():patch('work_action',a['id'],state='expired',finished_at=now())
        if a['state']=='approved':enqueue('work_action',a['id'],scheduled_key='work-action:'+a['id'],max_attempts=1)
        if a['state']=='executing':
            latest=list_jobs(target=a['id'],limit=1)
            if latest and latest[0]['status'] in ('failed','cancelled'):patch('work_action',a['id'],state='unknown',finished_at=now(),error='执行进程失联，请到原系统核对；不会自动重发')
        if a['state']=='succeeded' and not a.get('continued'):
            from .conversation import submit,TurnInput
            original=get('turn',a['turn_id'])
            if original.get('resume_available') and original.get('outcome')!='cancelled':
                from .actor_context import actor_scope
                with actor_scope(original.get('actor_id','owner')):
                    submit(a['project_id'],TurnInput(text='继续',session_id=a['session_id'],resume_turn_id=a['turn_id'],request_id='action-followup-'+a['id']))
            patch('work_action',a['id'],continued=True)


def assert_project_model_allowed(pid):
    from .company_search import model_fingerprint,model_approval_allowed,CompanySearchError
    p=get('project',pid)
    if not p.get('work_private'):return
    from .provider import settings
    policies=p.get('work_model_policies')
    allowed=bool(settings().get('share_data') and p.get('work_model_approval')) and (all(model_approval_allowed(policy) for policy in policies.values()) if policies else p.get('work_model_approval')==model_fingerprint())
    if not allowed:raise CompanySearchError('项目包含工作系统资料；需由管理员在后台批准当前模型处理本项目各工作系统资料，当前未向模型发送内容')
