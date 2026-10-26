"""Private, project-scoped company search API and versioned evidence.

No public search fallback, browser cookies, global index, or model-supplied URLs.
The upstream API must enforce the configured employee's document permissions.
"""
import hashlib,json,os,re,ssl,time
from datetime import datetime,timezone,timedelta
from html.parser import HTMLParser
from urllib.parse import urlsplit,urlunsplit,quote,parse_qsl
from typing import Literal
import httpx
from .locks import FileLock
from pydantic import Field,SecretStr,model_validator
from .models import StrictModel
from .store import get,put,patch,all_items,delete,seal,unseal,now
from .config import DATA

class DocumentFields(StrictModel):
    id:str='id'
    title:str='title'
    url:str='url'
    content:str='content'
    snippet:str='snippet'
    number:str='number'
    department:str='department'
    published_at:str='published_at'
    updated_at:str='updated_at'
    effective_from:str='effective_from'
    effective_to:str='effective_to'
    status:str='status'
    version:str='version'
    superseded_by:str='superseded_by'

class CompanySearchConfig(StrictModel):
    name:str=Field(default='公司发文',min_length=1,max_length=80)
    search_url:str=Field(min_length=8,max_length=1000)
    method:Literal['GET','POST']='GET'
    query_field:str=Field(default='q',min_length=1,max_length=100)
    limit_field:str=Field(default='limit',max_length=100)
    offset_field:str=Field(default='offset',max_length=100)
    parameters:dict=Field(default_factory=dict)
    headers:dict[str,SecretStr]=Field(default_factory=dict)
    results_path:str=Field(default='results',max_length=200)
    total_path:str=Field(default='total',max_length=200)
    detail_url:str=Field(default='',max_length=1000)
    detail_path:str=Field(default='',max_length=200)
    document_origins:list[str]=Field(default_factory=list,max_length=10)
    fields:DocumentFields=Field(default_factory=DocumentFields)
    principal_label:str=Field(min_length=1,max_length=120)
    user_scoped_ack:bool=False
    storage_approved:bool=False
    model_approved:bool=False
    approved_model_ids:list[str]=Field(default_factory=list,max_length=32)
    require_for_generation:bool=True
    retention_days:int=Field(default=7,ge=1,le=90)
    keep_credentials:bool=True

    @model_validator(mode='after')
    def bounds(self):
        if len(self.headers)>20 or len(json.dumps(self.parameters))>12000:raise ValueError('接口配置超过限制')
        for path in [self.query_field,self.limit_field,self.offset_field,self.results_path,self.total_path,self.detail_path,*self.fields.model_dump().values()]:
            if path and not re.fullmatch(r'[\w.-]{1,200}',path):raise ValueError('字段路径只支持点分JSON对象路径')
        if not self.user_scoped_ack or not self.storage_approved:raise ValueError('需确认接口使用本人权限，且此部署允许保存内部资料')
        if self.detail_url and self.detail_url.count('{id}')!=1:raise ValueError('详情地址需包含一个 {id} 占位符')
        if self.query_field in (self.limit_field,self.offset_field) or self.limit_field and self.limit_field==self.offset_field:raise ValueError('查询、条数和分页参数不能重名')
        if len(set(self.approved_model_ids))!=len(self.approved_model_ids) or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',mid) for mid in self.approved_model_ids):raise ValueError('批准的模型ID须唯一且为后台模型目录中的ID')
        if self.approved_model_ids and not self.model_approved:raise ValueError('批准指定模型需要明确启用模型处理许可')
        return self

class SearchInput(StrictModel):
    query:str=Field(min_length=1,max_length=1000)
    limit:int=Field(default=5,ge=1,le=10)
    offset:int=Field(default=0,ge=0,le=1000)

class EvidenceInput(StrictModel):
    evidence_id:str=Field(min_length=1,max_length=64)
    offset:int=Field(default=0,ge=0,le=64000)
    limit:int=Field(default=8000,ge=1,le=12000)

class Citation(StrictModel):
    evidence_id:str=Field(min_length=1,max_length=64)
    claim:str=Field(min_length=1,max_length=1000)
    quote:str=Field(min_length=8,max_length=600)

class CompanySearchError(ValueError):pass

def _key(pid):return 'kb_'+pid
def _hash(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
def _document_hash(doc):
    # Search snippets and UI links may differ from the detail endpoint without
    # changing the source text or its effective version.
    return _hash({k:v for k,v in doc.items() if k not in ('snippet','url')})
def _config(pid):return unseal(get('company_search',_key(pid))['secret_config'])
def _origin(url):
    p=urlsplit(url)
    return (p.scheme,p.hostname,p.port or (443 if p.scheme=='https' else 80))

def _url(url,allow_http=False):
    p=urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.query or p.fragment:raise CompanySearchError('接口地址须为不含凭证、查询参数和片段的HTTP(S)地址')
    allowed={v.strip() for v in os.getenv('API_ALLOWED_HOSTS','').split(',') if v.strip()}
    if p.scheme!='https' and (not allow_http or p.hostname not in allowed):raise CompanySearchError('公司接口应使用HTTPS；内网HTTP需要运维明确允许地址')
    return url

def model_fingerprint(configuration=None):
    from .provider import settings
    s=settings() if configuration is None else configuration
    identity={k:s.get(k,'') for k in ('base_url','model')}
    # Keep existing Atlas approvals stable, but never carry them across engines
    # or a change of the model gateway behind OpenCode.
    if s.get('engine')=='opencode':identity.update(engine='opencode',upstream_base_url=s.get('upstream_base_url',''),opencode_version=s.get('opencode_version',''),runtime=s.get('opencode_fingerprint',''))
    return _hash(identity)

def capture_model_approvals(model_ids):
    """Freeze explicit operator grants; a future model never inherits a grant.

    The catalog revision is provenance. Matching the connection fingerprint also
    lets an unrelated catalog entry change without revoking unchanged models.
    Credentials and connection details never appear in the returned snapshot.
    """
    from .model_catalog import owner_configuration
    catalog=owner_configuration();entries={entry['id']:entry for entry in catalog['models']}
    if len(set(model_ids))!=len(model_ids) or any(mid not in entries for mid in model_ids):raise CompanySearchError('批准的模型ID不在当前后台模型目录中，请先核对目录')
    return {mid:{'fingerprint':model_fingerprint(entries[mid]),'catalog_revision':catalog['revision']} for mid in model_ids}

def model_approval_allowed(record):
    if not record.get('model_approved'):return False
    if record.get('approved_model_ids') or record.get('approved_models'):
        from .model_catalog import active_selection
        grant=(record.get('approved_models') or {}).get(active_selection().get('model_id'))
        return bool(grant and grant.get('fingerprint')==model_fingerprint())
    # Existing single-model approvals remain restricted to that connection.
    return record.get('model_fingerprint')==model_fingerprint()

def status(pid):
    get('project',pid)
    try:c=_config(pid)
    except KeyError:return {'configured':False,'name':'公司发文','model_allowed':False,'private_project':bool(get('project',pid).get('company_private'))}
    return {'configured':True,'name':c['name'],'principal_label':c['principal_label'],'revision':c['revision'],'model_allowed':model_approval_allowed(c),'require_for_generation':c['require_for_generation'],'retention_days':c['retention_days'],'detail_supported':bool(c['detail_url']),'last_verified_at':get('company_search',_key(pid)).get('verified_at')}

def assert_model_allowed(pid):
    if pid:
        from .work_tools import assert_project_model_allowed
        assert_project_model_allowed(pid)
    if not pid or not get('project',pid).get('company_private'):return
    try:allowed=status(pid)['model_allowed']
    except KeyError:allowed=False
    if not allowed:raise CompanySearchError('此项目包含公司内部资料；需由管理员在后台批准当前模型，当前未向模型发送项目内容')

def configure(pid,value):
    get('project',pid)
    if any(t['project_id']==pid and t['state'] not in ('done','failed') for t in all_items('turn')):raise CompanySearchError('请先停止或等待本项目当前任务结束，再变更公司检索连接')
    c=value.model_dump();c['headers']={k:v.get_secret_value() for k,v in value.headers.items()}
    _url(c['search_url'],True)
    if c['detail_url']:
        _url(c['detail_url'].replace('{id}','document'),True)
        if _origin(c['detail_url'])!=_origin(c['search_url']):raise CompanySearchError('搜索与详情API必须同源，避免向其他主机转发认证信息')
    for origin in c['document_origins']:
        _url(origin,True)
        if urlsplit(origin).path not in ('','/'):raise CompanySearchError('原文站点白名单只填写协议和主机')
    forbidden={'host','content-length','transfer-encoding','connection','proxy-authorization','x-forwarded-for'}
    if any(k.lower() in forbidden or '\n' in k or '\r' in k or '\n' in v or '\r' in v for k,v in c['headers'].items()):raise CompanySearchError('认证请求头无效')
    try:old=_config(pid)
    except KeyError:old={}
    if c.pop('keep_credentials') and not c['headers'] and old and old['search_url']==c['search_url'] and old['principal_label']==c['principal_label']:c['headers']=old['headers']
    c.update(revision=__import__('secrets').token_hex(12),model_fingerprint=model_fingerprint(),approved_models=capture_model_approvals(value.approved_model_ids))
    # Validate address, TLS and bounded response before retaining credentials.
    _search_response(c,'连接验证',1,0,None)
    put('company_search',{'project_id':pid,'secret_config':seal(c),'verified_at':now()},_key(pid))
    patch('project',pid,company_private=True)
    for turn in all_items('turn'):
        if turn['project_id']==pid:patch('turn',turn['id'],company_private=True)
    purge(pid,expired_only=True)
    return status(pid)

def disconnect(pid):
    get('project',pid);delete('company_search',_key(pid));purge(pid)
    # company_private intentionally remains: disconnect must not unlock model export.
    return status(pid)

def purge(pid,expired_only=False):
    count=0
    for kind in ('company_evidence','company_operation'):
        for item in all_items(kind):
            if item.get('project_id')==pid and (not expired_only or item.get('expires_at','')<now()):delete(kind,item['id']);count+=1
    return count

def _at(value,path,default=None):
    if not path:return value
    for part in path.split('.'):
        if not isinstance(value,dict) or part not in value:return default
        value=value[part]
    return value

def _set(value,path,data):
    parts=path.split('.');obj=value
    for part in parts[:-1]:
        if part not in obj:obj[part]={}
        if not isinstance(obj[part],dict):raise CompanySearchError('请求参数路径与固定参数冲突')
        obj=obj[part]
    obj[parts[-1]]=data

def _budget(turn,kind,bytes_received=0):
    if not turn:return
    root=turn.get('agent_origin',turn['id'])
    with FileLock(str(DATA/('kb-budget-'+root+'.lock')),timeout=5):
        t=get('turn',root);usage=t.get('knowledge_usage',{})
        field='search_calls' if kind=='search' else 'read_calls' if kind=='read' else None
        if field and usage.get(field,0)>=(8 if kind=='search' else 24):raise CompanySearchError('本任务内部检索预算已用完，已有证据保留；请缩小范围后继续')
        if field:usage[field]=usage.get(field,0)+1
        usage['response_bytes']=usage.get('response_bytes',0)+bytes_received
        patch('turn',root,knowledge_usage=usage)
        if root!=turn['id']:patch('turn',turn['id'],knowledge_usage=usage)
        if usage['response_bytes']>2*1024*1024:raise CompanySearchError('本任务内部检索响应超过2MB预算')

def _request(c,url,method='GET',parameters=None,turn=None,kind='read'):
    from .connectors import safe_api_url
    _budget(turn,kind)
    ip=safe_api_url(url);original=httpx.URL(url);target=original.copy_with(host=ip)
    headers=dict(c['headers']);headers['Host']=original.netloc.decode()
    context=ssl.create_default_context(cafile=os.getenv('KNOWLEDGE_CA_FILE') or None)
    if os.getenv('KNOWLEDGE_CLIENT_CERT_FILE'):context.load_cert_chain(os.environ['KNOWLEDGE_CLIENT_CERT_FILE'],os.getenv('KNOWLEDGE_CLIENT_KEY_FILE'))
    try:
        with httpx.Client(timeout=15,trust_env=False,follow_redirects=False,verify=context) as client:
            kwargs={'params':parameters} if method=='GET' else {'json':parameters}
            with client.stream(method,target,headers=headers,extensions={'sni_hostname':original.host},**kwargs) as r:
                if r.status_code in (401,403):raise CompanySearchError('公司接口拒绝访问，请检查本人权限或更新认证信息；不会改用其他账号')
                if r.status_code==404:raise CompanySearchError('原文已不可访问或已移除，不能继续作为当前依据')
                if r.status_code>=300:raise CompanySearchError('公司检索接口暂不可用，状态码 '+str(r.status_code)+'；未使用公网搜索替代')
                chunks=[];size=0
                for chunk in r.iter_bytes():
                    size+=len(chunk)
                    if size>512*1024:raise CompanySearchError('单次公司接口响应超过512KB，请缩小返回范围')
                    chunks.append(chunk)
                _budget(turn,'bytes',size)
                try:return json.loads(b''.join(chunks))
                except (ValueError,UnicodeError):raise CompanySearchError('接口没有返回JSON；可能是登录页或响应映射需要调整') from None
    except httpx.TransportError:raise CompanySearchError('无法连接公司接口，请检查内网/VPN路由、证书及服务状态') from None

def _search_response(c,query,limit,offset,turn):
    params=json.loads(json.dumps(c['parameters']));_set(params,c['query_field'],query)
    if c['limit_field']:_set(params,c['limit_field'],limit)
    if c['offset_field']:_set(params,c['offset_field'],offset)
    value=_request(c,c['search_url'],c['method'],params,turn,'search')
    rows=_at(value,c['results_path'])
    if not isinstance(rows,list) or any(not isinstance(x,dict) for x in rows):raise CompanySearchError('检索响应的结果数组路径不匹配，请检查接口映射')
    return rows,_at(value,c['total_path'])

class _Text(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.parts=[];self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript'):self.skip+=1
        if not self.skip and tag in ('p','div','br','li','tr','h1','h2','h3'):self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript') and self.skip:self.skip-=1
    def handle_data(self,data):
        if not self.skip:self.parts.append(data)

def _plain(value,limit=64000):
    if value is None:return ''
    if not isinstance(value,(str,int,float)):return ''
    parser=_Text();parser.feed(str(value));text=''.join(parser.parts)
    return '\n'.join(line.strip() for line in text.splitlines() if line.strip())[:limit]

def _link(c,url):
    if not isinstance(url,str):return None
    p=urlsplit(url)
    origins={_origin(c['search_url']),*(_origin(x) for x in c['document_origins'])}
    try:
        if p.scheme not in ('https','http') or not p.hostname or p.username or p.password or _origin(url) not in origins:return None
        if any(re.search(r'token|password|authorization|secret|signature|api.?key|session',k,re.I) for k,v in parse_qsl(p.query)):return None
    except ValueError:return None
    return urlunsplit(p)

def _normalize(c,row):
    f=c['fields'];raw_id=_at(row,f['id'])
    if not isinstance(raw_id,(str,int)) or len(str(raw_id))>300:raise CompanySearchError('文档ID字段缺失或无效')
    doc={k:_plain(_at(row,path),1000 if k not in ('content','snippet') else 64000 if k=='content' else 2000) for k,path in f.items()}
    doc['id']=str(raw_id);doc['url']=_link(c,_at(row,f['url']))
    if not doc['title']:raise CompanySearchError('文档标题字段缺失，请检查字段映射')
    doc['content_truncated']=len(str(_at(row,f['content'],'')))>64000
    doc['scope']='full_text' if doc['content'] else 'snippet_only'
    doc['warnings']=[]
    if doc['content_truncated']:doc['warnings'].append('原文超过读取上限，当前正文已截断，不能自动制定完整业务规则')
    if not doc['published_at']:doc['warnings'].append('发布日期未知，不能按采集时间推断发布日期')
    if re.search(r'废止|失效|撤回|已替代|superseded|revoked|withdrawn|expired',doc['status'],re.I) or doc['superseded_by']:doc['warnings'].append('此文标记为失效、撤回或被替代，不能作为现行规则')
    for field,future in [('effective_from',True),('effective_to',False)]:
        try:
            day=datetime.fromisoformat(doc[field].replace('Z','+00:00')).date()
            today=datetime.now(timezone.utc).date()
            if future and day>today:doc['warnings'].append('尚未生效')
            if not future and day<today:doc['warnings'].append('已超过有效期')
        except ValueError:
            if doc[field]:doc['warnings'].append('生效日期格式未识别，需人工核对')
    return doc

def _save(pid,c,doc,query,offset):
    digest=_document_hash(doc);eid='ev_'+_hash([pid,c['revision'],doc['id'],digest])[:40]
    payload={'document':doc,'query':query,'offset':offset}
    expires=(datetime.now(timezone.utc)+timedelta(days=c['retention_days'])).isoformat()
    put('company_evidence',{'project_id':pid,'connection_revision':c['revision'],'content_hash':digest,'secret_payload':seal(payload),'created_at':now(),'expires_at':expires,'accessible':True},eid)
    return eid

def _load(pid,eid):
    item=get('company_evidence',eid)
    if item['project_id']!=pid:raise CompanySearchError('证据不属于本项目')
    c=_config(pid)
    if item['connection_revision']!=c['revision']:raise CompanySearchError('检索连接或账号已变更，请重新检索，旧证据不再用于当前操作')
    if item['expires_at']<now():raise CompanySearchError('证据快照已过保存期限，请重新检索')
    return c,item,unseal(item['secret_payload'])

def search(pid,value,turn=None):
    get('project',pid)
    purge(pid,expired_only=True)
    try:c=_config(pid)
    except KeyError:raise CompanySearchError('请先连接本项目的公司检索API；尚未接入真实公司网站') from None
    rows,total=_search_response(c,value.query,value.limit,value.offset,turn)
    results=[];seen=set()
    for row in rows[:value.limit]:
        doc=_normalize(c,row)
        if doc['id'] in seen:continue
        seen.add(doc['id']);eid=_save(pid,c,doc,value.query,value.offset)
        results.append({'evidence_id':eid,**{k:v for k,v in doc.items() if k!='content'},'content_hash':get('company_evidence',eid)['content_hash'],'retrieved_at':now()})
    if turn:
        refs=list(dict.fromkeys(get('turn',turn['id']).get('knowledge_refs',[])+[r['evidence_id'] for r in results]))[-20:]
        patch('turn',turn['id'],knowledge_refs=refs)
    return {'results':results,'total':total if isinstance(total,int) and total>=0 else None,'returned':len(results),'has_more':(value.offset+len(results)<total) if isinstance(total,int) else None,'complete_search':False,'scope':'company_api','connection_revision':c['revision']}

def read(pid,eid,turn=None,revalidate=True):
    c,item,payload=_load(pid,eid);doc=payload['document']
    if revalidate:
        try:
            if c['detail_url']:
                value=_request(c,c['detail_url'].replace('{id}',quote(doc['id'],safe='')),turn=turn)
                row=_at(value,c['detail_path'])
                if not isinstance(row,dict):raise CompanySearchError('详情响应映射不是文档对象')
                fresh=_normalize(c,row)
            else:
                rows,_=_search_response(c,payload['query'],10,payload['offset'],turn)
                fresh=next((_normalize(c,r) for r in rows if str(_at(r,c['fields']['id']))==doc['id']),None)
                if not fresh:raise CompanySearchError('当前检索范围内无法重新确认此文权限和版本，请重新检索')
            if fresh['id']!=doc['id']:raise CompanySearchError('详情接口返回了不同文档，不能作为原文依据')
            if _document_hash(fresh)!=item['content_hash']:
                new_id=_save(pid,c,fresh,payload['query'],payload['offset'])
                if doc['scope']=='snippet_only' and fresh['scope']=='full_text':
                    patch('company_evidence',eid,accessible=False,upgraded_to=new_id)
                    eid=new_id;doc=fresh;item=get('company_evidence',eid)
                else:raise CompanySearchError('文档内容或状态已变化，请重新检索并读取最新证据 '+new_id)
            else:doc=fresh
        except CompanySearchError:
            patch('company_evidence',eid,accessible=False)
            raise
        patch('company_evidence',eid,accessible=True,access_checked_at=now())
    if turn:
        refs=list(dict.fromkeys(get('turn',turn['id']).get('knowledge_read_refs',[])+[eid]))[-10:]
        candidates=list(dict.fromkeys(get('turn',turn['id']).get('knowledge_refs',[])+[eid]))[-20:]
        patch('turn',turn['id'],knowledge_read_refs=refs,knowledge_refs=candidates)
    return {'evidence_id':eid,**doc,'content_hash':item['content_hash'],'retrieved_at':item['created_at']}

def for_model(turn):
    refs=turn.get('knowledge_read_refs',[])[-3:]
    if not refs:return []
    assert_model_allowed(turn['project_id']);docs=[]
    for eid in refs:
        try:
            doc=read(turn['project_id'],eid,turn)
            chunk=turn.get('knowledge_slices',{}).get(eid,{'offset':0,'limit':5000})
            start=chunk['offset'];doc['content']=doc['content'][start:start+min(chunk['limit'],5000)];doc['content_offset']=start;doc['snippet']=doc['snippet'][:1000]
            docs.append(doc)
        except (CompanySearchError,KeyError) as e:docs.append({'evidence_id':eid,'unavailable':True,'reason':str(e) if isinstance(e,CompanySearchError) else '证据已不可用'})
    return docs

def search_for_model(turn,value):
    assert_model_allowed(turn['project_id'])
    result=search(turn['project_id'],value,turn)
    # No internal text is copied into the persistent plaintext tool trace.
    return {'evidence_ids':[r['evidence_id'] for r in result['results']],'returned':result['returned'],'total':result['total'],'has_more':result['has_more'],'next_step':'用read_company_document读取相关原文；摘要不等于原文'}

def candidates_for_model(turn):
    if not turn.get('knowledge_refs'):return []
    assert_model_allowed(turn['project_id']);values=[]
    for eid in turn['knowledge_refs'][-10:]:
        try:
            c,item,payload=_load(turn['project_id'],eid)
            if item.get('accessible') and (datetime.now(timezone.utc)-datetime.fromisoformat(item['created_at'])).total_seconds()<60:
                doc=payload['document'];values.append({'evidence_id':eid,**{k:doc[k] for k in ('title','snippet','number','department','published_at','status','version','warnings','scope')}})
        except (KeyError,CompanySearchError):pass
    return values

def requires_evidence(turn):
    text=turn.get('work_request',turn['text'])
    explicit=bool(re.search(r'公司.*(?:发文|制度|规定|通知)|内部.*(?:发文|制度|文档)|根据.*(?:发文|制度)|检索.*(?:发文|制度)|查询.*(?:发文|制度)',text))
    try:c=_config(turn['project_id'])
    except KeyError:return explicit
    return explicit or bool(c['require_for_generation'] and turn.get('task_contract',{}).get('kind')=='build')

def validate_citations(turn,citations,answer=''):
    used=[];read_refs=set(turn.get('knowledge_read_refs',[]))
    if requires_evidence(turn) and not citations:raise CompanySearchError('本任务需要读取公司原文并提供可核对引用，不能仅凭摘要或模型记忆完成')
    for citation in citations:
        value=citation.model_dump() if isinstance(citation,Citation) else Citation.model_validate(citation).model_dump()
        if answer and value['claim'] not in answer:raise CompanySearchError('引用对应的结论必须实际出现在回答中')
        if value['evidence_id'] not in read_refs:raise CompanySearchError('引用必须来自本任务实际读取的原文')
        doc=read(turn['project_id'],value['evidence_id'],turn)
        if doc['scope']!='full_text':raise CompanySearchError('仅有检索摘要，不能当作已核验原文引用')
        if value['quote'] not in doc['content']:raise CompanySearchError('引用片段不在原文中，未通过引用验收')
        used.append({**value,'content_hash':doc['content_hash']})
    return used

def manifest(turn):
    values=[]
    for eid in turn.get('knowledge_read_refs',[]):
        doc=read(turn['project_id'],eid,turn)
        if doc['scope']!='full_text' or any(re.search(r'失效|撤回|替代|尚未生效|有效期|需人工核对|截断',w) for w in doc['warnings']):continue
        values.append({'evidence_id':eid,'content_hash':doc['content_hash'],'connection_revision':_config(turn['project_id'])['revision'],'retrieved_at':doc['retrieved_at']})
    if requires_evidence(turn) and not values:raise CompanySearchError('生成应用前需读取当前有效的公司原文；摘要、失效文档和未确认生效日期不能自动形成业务规则')
    return values

def basis_from_ids(pid,ids):
    if not pid:return []
    try:c=_config(pid)
    except KeyError:
        if ids or get('project',pid).get('company_private'):raise CompanySearchError('此项目内部资料连接已断开，请恢复后再生成新规则') from None
        return []
    if c['require_for_generation'] and not ids:raise CompanySearchError('生成或调整此项目应用需要提供已读取的公司原文证据')
    values=[]
    for eid in list(dict.fromkeys(ids))[:10]:
        doc=read(pid,eid)
        if doc['scope']!='full_text' or any(re.search(r'失效|撤回|替代|尚未生效|有效期|需人工核对|截断',w) for w in doc['warnings']):raise CompanySearchError('该证据不是完整的当前有效原文，不能自动生成业务规则')
        values.append({'evidence_id':doc['evidence_id'],'content_hash':doc['content_hash'],'connection_revision':c['revision'],'retrieved_at':doc['retrieved_at']})
    return values

def validate_basis(pid,basis):
    if not pid:return
    try:c=_config(pid)
    except KeyError:
        if get('project',pid).get('company_private'):raise CompanySearchError('公司检索连接已断开，不能生成新的内部规则') from None
        return
    if c['require_for_generation'] and not basis:raise CompanySearchError('缺少经过核对的公司原文依据')
    for value in basis:
        doc=read(pid,value['evidence_id'],revalidate=False)
        if not get('company_evidence',value['evidence_id']).get('accessible') or doc['content_hash']!=value['content_hash']:raise CompanySearchError('应用依据已失效或内容哈希不匹配')

def public_sources(turn):
    result=[]
    for value in turn.get('knowledge_citations',[]):
        try:
            c,item,payload=_load(turn['project_id'],value['evidence_id'])
            if not item.get('accessible'):continue
            doc=payload['document']
            result.append({k:doc[k] for k in ('title','url','number','published_at','updated_at','status','version','warnings')}|{'evidence_id':value['evidence_id'],'content_hash':item['content_hash']})
        except (KeyError,CompanySearchError):pass
    return result

def visible_answer(turn,model=False):
    if not turn.get('secret_knowledge_answer'):return turn.get('answer','')
    try:
        if model:assert_model_allowed(turn['project_id'])
        ids=turn.get('knowledge_citations') or [{'evidence_id':x} for x in turn.get('knowledge_read_refs',[])]
        for value in ids:
            c,item,payload=_load(turn['project_id'],value['evidence_id'])
            if not item.get('accessible'):raise CompanySearchError('证据权限已失效')
        return unseal(turn['secret_knowledge_answer'])
    except (KeyError,CompanySearchError):return '[内部资料连接、权限或证据版本已失效，相关回答已隐藏；请重新检索核对]'

def configuration(pid):
    try:
        c=_config(pid)
        return {k:v for k,v in c.items() if k not in ('headers','model_fingerprint','approved_models','revision')}|{'credential_headers':list(c['headers']),'headers':{}}
    except KeyError:return None

def read_for_model(turn,value):
    assert_model_allowed(turn['project_id'])
    doc=read(turn['project_id'],value.evidence_id,turn)
    slices=get('turn',turn['id']).get('knowledge_slices',{})
    slices[doc['evidence_id']]={'offset':value.offset,'limit':min(value.limit,5000)}
    patch('turn',turn['id'],knowledge_slices=slices)
    return {'evidence_id':doc['evidence_id'],'scope':doc['scope'],'offset':value.offset,'characters_read':len(doc['content'][value.offset:value.offset+min(value.limit,5000)]),'next_offset':value.offset+min(value.limit,5000) if len(doc['content'])>value.offset+min(value.limit,5000) else None,'content_hash':doc['content_hash']}
