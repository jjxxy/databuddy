"""Versioned, actor-scoped Skills and controlled personal/admin MCP installs."""
import hashlib,io,json,os,re,stat,zipfile
from pathlib import Path,PurePosixPath
from typing import Literal
import yaml
from pydantic import Field,SecretStr,model_validator
from .models import StrictModel
from .store import get,put,patch,all_items,delete,seal,unseal,uid,now
from .config import DATA
from .actor_context import current_actor
from . import work_tools

MAX_ARCHIVE=5*1024*1024
MAX_EXPANDED=10*1024*1024

class SkillTarget(StrictModel):skill_id:str=Field(min_length=1,max_length=64)
class SkillQuery(StrictModel):query:str=Field(default='',max_length=200)
class SkillFile(SkillTarget):
    path:str=Field(min_length=1,max_length=200)
    offset:int=Field(default=0,ge=0,le=1000000)
    limit:int=Field(default=12000,ge=1,le=20000)
class Toggle(StrictModel):
    enabled:bool
    expected_digest:str=Field(pattern=r'^[a-f0-9]{64}$')
class MCPInput(StrictModel):
    name:str=Field(min_length=1,max_length=80)
    transport:Literal['http','stdio']='http'
    url:str=Field(default='',max_length=1000)
    credential:SecretStr=Field(default=SecretStr(''))
    command:str=Field(default='',max_length=100)
    args:list[str]=Field(default_factory=list,max_length=100)
    env:dict[str,str]=Field(default_factory=dict)
    read_tools:list[str]=Field(default_factory=list,max_length=100)
    write_tools:list[str]=Field(default_factory=list,max_length=100)
    storage_approved:bool=False
    model_approved:bool=False
    approved_model_ids:list[str]=Field(default_factory=list,max_length=32)
    command_approved:bool=False
    @model_validator(mode='after')
    def transport_fields(self):
        if self.transport=='http' and (not self.url or self.command or self.args or self.env):raise ValueError('HTTP连接须填写地址，不填写本地命令')
        if self.transport=='stdio' and (not self.command or self.url or self.credential.get_secret_value()):raise ValueError('本地MCP须选择命令模板，凭证通过加密环境项保存')
        if len(self.env)>40 or any(len(v)>8000 for v in self.env.values()):raise ValueError('环境配置过长')
        return self


def digest(value):return hashlib.sha256(value).hexdigest()
def owner_visible(record):return record.get('origin')=='preset' or record.get('owner_id')==current_actor()
def _skill(identity):
    value=get('installed_skill',identity)
    if not owner_visible(value):raise ValueError('此技能不属于当前用户')
    return value

def _setting_id(identity):return 'es_'+digest((current_actor()+':'+identity).encode())[:48]
def _state(value):
    try:state=get('extension_setting',_setting_id(value['id']))
    except KeyError:return bool(value.get('default_enabled',True))
    return state.get('enabled',False) and state.get('digest')==value['digest']

def skill_public(value):
    return {k:value[k] for k in ('id','name','description','digest','version','origin','files_count','updated_at')}|{'enabled':_state(value),'can_remove':value['origin']=='personal'}

def _safe_name(name):
    p=PurePosixPath(name)
    if '\\' in name or '\x00' in name or p.is_absolute() or any(v in ('','.','..') or v.startswith('.') for v in p.parts) or len(name)>200 or ':' in name:raise ValueError('技能包包含无效路径')
    return p.as_posix()

def _unpack(filename,data):
    if not data or len(data)>MAX_ARCHIVE:raise ValueError('技能包须为不超过5MB的Markdown或ZIP文件')
    if filename.lower().endswith('.md'):files={'SKILL.md':data}
    elif filename.lower().endswith('.zip'):
        files={}
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries=archive.infolist()
                if len(entries)>120 or sum(v.file_size for v in entries)>MAX_EXPANDED:raise ValueError('技能包文件数或解压体积超过限制')
                for item in entries:
                    name=_safe_name(item.filename.rstrip('/'))
                    mode=item.external_attr>>16
                    if stat.S_IFMT(mode) not in (0,stat.S_IFREG,stat.S_IFDIR):raise ValueError('技能包不能包含链接或设备文件')
                    if item.is_dir():continue
                    if name in files:raise ValueError('技能包包含重复文件')
                    if item.file_size>MAX_ARCHIVE or item.flag_bits&1:raise ValueError('技能包文件过大或已加密')
                    content=archive.read(item)
                    if len(content)!=item.file_size:raise ValueError('技能包文件不完整')
                    files[name]=content
        except (zipfile.BadZipFile,RuntimeError,NotImplementedError):raise ValueError('技能ZIP格式无效') from None
        roots=[name for name in files if name=='SKILL.md' or name.endswith('/SKILL.md')]
        if len(roots)!=1:raise ValueError('技能包必须只有一个SKILL.md')
        prefix=roots[0][:-len('SKILL.md')]
        if any(not name.startswith(prefix) for name in files):raise ValueError('技能文件必须在同一包目录中')
        files={name[len(prefix):]:body for name,body in files.items()}
    else:raise ValueError('请上传SKILL.md或含SKILL.md的ZIP包')
    if len(files.get('SKILL.md',b''))>65536:raise ValueError('SKILL.md不能超过64KB')
    try:text=files['SKILL.md'].decode('utf-8-sig')
    except (KeyError,UnicodeError):raise ValueError('SKILL.md须为UTF-8文本') from None
    if '\x00' in text:raise ValueError('SKILL.md含无效字符')
    metadata={}
    if text.startswith('---\n'):
        if '\n---' not in text[4:]:raise ValueError('技能头部格式无效')
        try:metadata=yaml.safe_load(text.split('---',2)[1]) or {}
        except yaml.YAMLError:raise ValueError('技能头部格式无效') from None
        if not isinstance(metadata,dict):raise ValueError('技能头部须为对象')
    return files,metadata

def install_skill(filename,data,*,name='',preset=False,identity=None):
    files,metadata=_unpack(filename,data)
    title=name.strip() or metadata.get('name') or Path(filename).stem
    description=metadata.get('description','用户安装的业务工作方法')
    version=metadata.get('version','1')
    if not isinstance(title,str) or not 1<=len(title)<=100 or any(ord(c)<32 for c in title):raise ValueError('技能名称无效')
    if not isinstance(description,str) or len(description)>1500:raise ValueError('技能描述无效')
    if not isinstance(version,(str,int,float)) or len(str(version))>40:raise ValueError('技能版本无效')
    manifest={name:digest(body) for name,body in sorted(files.items())}
    package_digest=digest(json.dumps(manifest,sort_keys=True).encode())
    root=DATA/'extensions'/'packages'/package_digest
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    for rel,body in files.items():
        target=root/rel;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if target.is_symlink():raise ValueError('技能保存路径不允许链接')
        if target.exists() and digest(target.read_bytes())!=manifest[rel]:raise ValueError('已有技能包被修改，未采用')
        if not target.exists():target.write_bytes(body);target.chmod(0o600)
    origin='preset' if preset else 'personal'
    if identity:
        try:old=_skill(identity)
        except KeyError:old=None
        if old and old['origin']!=origin:raise ValueError('不能覆盖其他来源的技能')
        if old and old['digest']==package_digest:return skill_public(old)
    else:
        existing=next((s for s in all_items('installed_skill') if s.get('digest')==package_digest and s.get('origin')==origin and (preset or s.get('owner_id')==current_actor())),None)
        if existing:return skill_public(existing)
        identity='sk_'+uid()
    value={'name':title,'description':description,'version':str(version),'digest':package_digest,'manifest':manifest,'origin':origin,'owner_id':None if preset else current_actor(),'default_enabled':True,'files_count':len(files),'updated_at':now()}
    saved=put('installed_skill',value,identity)
    # Personal updates are selected explicitly; preset updates never silently
    # replace a package already chosen by this user's task or preference.
    if origin=='personal':put('extension_setting',{'owner_id':current_actor(),'skill_id':identity,'digest':package_digest,'enabled':True},_setting_id(identity))
    return skill_public(saved)

def toggle_skill(identity,value):
    record=_skill(identity)
    if value.expected_digest!=record['digest']:raise ValueError('技能版本已变化，请重新核对')
    put('extension_setting',{'owner_id':current_actor(),'skill_id':identity,'digest':record['digest'],'enabled':value.enabled},_setting_id(identity))
    return skill_public(record)

def remove_skill(identity,expected_digest):
    record=_skill(identity)
    if record['origin']!='personal':raise ValueError('管理员预置技能由管理员维护，可停用但不能删除')
    if record['digest']!=expected_digest:raise ValueError('技能版本已变化，请重新核对')
    delete('installed_skill',identity);delete('extension_setting',_setting_id(identity))
    return {'removed':True}

def _read_package(package_digest,manifest,rel):
    rel=_safe_name(rel)
    if rel not in manifest:raise ValueError('技能包没有该文件')
    root=DATA/'extensions'/'packages'/package_digest;target=root/rel
    if any(p.is_symlink() for p in [target,*target.parents]) or not target.resolve().is_relative_to(root.resolve()):raise ValueError('技能文件路径越界')
    body=target.read_bytes()
    if digest(body)!=manifest[rel]:raise ValueError('技能文件摘要不匹配，未加载')
    return body

def list_skills(query=''):
    return {'scope':'current_actor','items':[skill_public(v) for v in all_items('installed_skill') if owner_visible(v) and (not query or query.casefold() in (v['name']+' '+v['description']).casefold())]}

def load_skill(turn,identity):
    from .provider import settings
    if not settings().get('share_data'):raise ValueError('加载个人技能需要允许所选模型读取内容')
    from .actor_context import current_actor
    if turn.get('actor_id','owner')!=current_actor():raise ValueError('任务用户不匹配')
    record=_skill(identity)
    receipts=list(get('turn',turn['id']).get('loaded_skills',[]))
    prior=next((v for v in receipts if v['id']==identity),None)
    try:preference=get('extension_setting',_setting_id(identity))
    except KeyError:preference=None
    if prior:
        if preference is not None and not preference.get('enabled'):raise ValueError('此技能已停用')
    elif not _state(record):raise ValueError('此技能已停用或更新后尚未重新启用')
    selected=prior or {k:record[k] for k in ('id','digest','manifest','name','version','origin')}
    content=_read_package(selected['digest'],selected['manifest'],'SKILL.md').decode('utf-8-sig')
    if not prior:
        if len(receipts)>=12:raise ValueError('单项任务最多加载12个技能')
        receipts.append(selected);patch('turn',turn['id'],loaded_skills=receipts)
        if record['origin']=='preset' and preference is None:
            put('extension_setting',{'owner_id':current_actor(),'skill_id':identity,'digest':record['digest'],'enabled':True},_setting_id(identity))
    return {'skill_id':identity,'name':selected['name'],'version':selected['version'],'digest':selected['digest'],'instructions':content,'files':list(selected['manifest']),'untrusted_instructions':True,'execution_policy':'技能只能指导已授权工具；安装包脚本不会自动运行，不扩大模型、文件或外部写入权限'}

def read_skill_file(turn,value):
    receipts=get('turn',turn['id']).get('loaded_skills',[])
    record=next((v for v in receipts if v['id']==value.skill_id),None)
    if not record:raise ValueError('请先加载当前任务的技能')
    from .provider import settings
    if not settings().get('share_data'):raise ValueError('读取技能文件需要内容授权')
    from .actor_context import current_actor
    if turn.get('actor_id','owner')!=current_actor():raise ValueError('任务用户不匹配')
    _skill(value.skill_id)
    try:preference=get('extension_setting',_setting_id(value.skill_id))
    except KeyError:preference=None
    if preference is not None and not preference.get('enabled'):raise ValueError('此技能已停用')
    body=_read_package(record['digest'],record['manifest'],value.path)
    try:content=body.decode('utf-8-sig')
    except UnicodeError:return {'path':value.path,'bytes':len(body),'sha256':digest(body),'binary':True,'message':'二进制资源保留在技能包中，不能冒充已解析数据'}
    return {'skill_id':value.skill_id,'path':value.path,'digest':record['digest'],'content':content[value.offset:value.offset+value.limit],'offset':value.offset,'next_offset':value.offset+value.limit if len(content)>value.offset+value.limit else None,'untrusted_content':True}


def _http_input(value):
    return work_tools.ConnectorInput(**value.model_dump(exclude={'transport','command','args','env','command_approved'}))

def probe_mcp(value):
    if value.transport=='stdio':
        from .mcp_stdio import validate
        if not value.command_approved:raise ValueError('启用本地MCP前需明确确认运行此预置程序')
        config,_,_=validate({'transport':'stdio','command':value.command,'args':value.args,'env':value.env})
    else:
        from .connectors import safe_api_url
        from urllib.parse import urlsplit
        from .mcp_stdio import local_mode
        p=urlsplit(value.url)
        local=local_mode() and p.scheme=='http' and p.hostname in ('localhost','127.0.0.1','::1','host.docker.internal')
        if (p.scheme!='https' and not local) or p.username or p.password or p.query or p.fragment:raise ValueError('MCP须为HTTPS地址；本地模式可使用已允许的本机HTTP')
        safe_api_url(value.url)
        config={'url':value.url,'credential':value.credential.get_secret_value()}
    with work_tools.Session(config) as session:tools=work_tools.catalog(session)
    return config,tools

def configure_mcp(pid,value,cid=None,*,preset_id=None):
    get('project',pid)
    if cid:
        old=get('work_connector',cid)
        if old.get('project_id')!=pid or not work_tools.visible(old):raise ValueError('连接不属于当前用户或项目')
        if old.get('extension_origin')=='preset':raise ValueError('管理员预置连接不能由用户改写，请停用后新建个人连接')
        if old.get('owner_id')!=current_actor():raise ValueError('不能修改其他用户或管理员的连接')
        if value.transport=='http' and old.get('url')==value.url and not value.credential.get_secret_value():
            value=value.model_copy(update={'credential':SecretStr(unseal(old['secret_config']).get('credential',''))})
        if value.transport=='stdio' and old.get('command')==value.command:
            previous=unseal(old['secret_config']);updates={}
            for key in ('args','env'):
                if key not in value.model_fields_set:updates[key]=previous.get(key,[] if key=='args' else {})
            if updates:value=value.model_copy(update=updates)
    config,tools=probe_mcp(value)
    result=work_tools.save_connector(pid,value,config,tools,cid,owner_id=current_actor(),origin='preset' if preset_id else 'personal',preset_id=preset_id)
    return mcp_public(get('work_connector',result['id']))

def mcp_public(record):
    value=work_tools.public(record)
    value.pop('owner_id',None)
    value['origin']=record.get('extension_origin','administrator')
    value['can_remove']=value['origin']=='personal'
    return value

def disconnect_mcp(pid,cid):
    record=get('work_connector',cid)
    if record.get('project_id')!=pid or record.get('owner_id')!=current_actor():raise ValueError('连接不属于当前用户或项目')
    patch('work_connector',cid,enabled=False,revision=uid(),updated_at=now())
    return {'disabled':True,'connector_id':cid}

def preset_mcp(value,identity=None,*,verify=True):
    if verify:config,tools=probe_mcp(value)
    else:config=None;tools={name:{} for name in value.read_tools+value.write_tools}
    if not value.storage_approved:raise ValueError('预置MCP需明确批准资料保存')
    if value.approved_model_ids and not value.model_approved:raise ValueError('批准指定模型需要明确启用模型处理许可')
    if not value.read_tools and not value.write_tools:raise ValueError('请明确选择允许的只读工具或需确认的写入工具')
    if set(value.read_tools)&set(value.write_tools) or not set(value.read_tools+value.write_tools)<=set(tools):raise ValueError('工具允许列表无效')
    raw=value.model_dump();raw['credential']=value.credential.get_secret_value()
    # Public revision markers must not expose a reusable hash of credentials.
    frozen=digest(os.urandom(32)+json.dumps(raw,sort_keys=True).encode())
    if identity:
        try:prior=get('mcp_preset',identity)
        except KeyError:prior=None
        if prior and prior.get('origin')!='preset':raise ValueError('预置来源无效')
    from .company_search import capture_model_approvals,model_fingerprint
    policy={'model_approved':value.model_approved,'approved_model_ids':value.approved_model_ids,
            'approved_models':capture_model_approvals(value.approved_model_ids),'model_fingerprint':model_fingerprint() if value.model_approved else None}
    permissions={k:raw[k] for k in ('read_tools','write_tools','storage_approved','model_approved','approved_model_ids')}
    saved=put('mcp_preset',{'name':value.name,'origin':'preset','digest':frozen,'version':uid(),'secret_input':seal(raw),'transport':value.transport,'updated_at':now(),'verified':verify,'permissions':permissions,'model_policy':policy},identity or 'mp_'+uid())
    return {k:saved[k] for k in ('id','name','origin','digest','version','transport')}

def enable_preset(pid,identity,value):
    record=get('mcp_preset',identity)
    if record['digest']!=value.expected_digest:raise ValueError('预置MCP已更新，请重新核对')
    existing=next((c for c in all_items('work_connector') if c.get('project_id')==pid and c.get('preset_id')==identity and c.get('owner_id')==current_actor()),None)
    if not value.enabled:
        return disconnect_mcp(pid,existing['id']) if existing else {'disabled':True}
    from .company_search import capture_model_approvals,model_fingerprint
    policy=record.get('model_policy') or {}
    if policy.get('model_approved'):
        ids=policy.get('approved_model_ids') or []
        if ids:
            current=capture_model_approvals(ids)
            if any(current.get(key,{}).get('fingerprint')!=(policy.get('approved_models') or {}).get(key,{}).get('fingerprint') for key in ids):raise ValueError('预置连接批准的模型配置已变化，请管理员重新核对授权')
        elif policy.get('model_fingerprint')!=model_fingerprint():raise ValueError('预置连接批准的默认模型已变化，请管理员重新核对授权')
    if existing and existing.get('preset_digest')==record['digest']:
        patch('work_connector',existing['id'],enabled=True,revision=uid());return mcp_public(get('work_connector',existing['id']))
    raw=unseal(record['secret_input']);parsed=MCPInput.model_validate(raw)
    # No hidden widening on preset updates. A user explicitly enables a checked
    # digest; existing prepared external actions become invalid via revision.
    config,tools=probe_mcp(parsed)
    saved=work_tools.save_connector(pid,parsed,config,tools,existing['id'] if existing else None,owner_id=current_actor(),origin='preset',preset_id=identity,model_policy=policy)
    patch('work_connector',saved['id'],preset_digest=record['digest'])
    return mcp_public(get('work_connector',saved['id']))

def catalog(pid=None):
    from .mcp_stdio import local_mode,templates
    connectors=[c for c in all_items('work_connector') if c.get('project_id')==pid and work_tools.visible(c)] if pid else []
    presets=[]
    for record in all_items('mcp_preset'):
        linked=next((c for c in connectors if c.get('preset_id')==record['id']),None)
        presets.append({k:record[k] for k in ('id','name','digest','version','transport')}|{'kind':'mcp','permissions':record.get('permissions',{}),'installed':bool(linked),'enabled':bool(linked and linked.get('enabled')),'update_available':bool(linked and linked.get('preset_digest')!=record['digest'])})
    local=local_mode()
    return {'scope':'current_actor','skills':list_skills()['items'],'mcp':[mcp_public(c) for c in connectors],'presets':presets,'policy':{'local_mode':local,'stdio_available':local and bool(templates()),'stdio_allowed_commands':list(templates()) if local else [],'os_network_isolation':False,'stdio_network_unrestricted':local and os.getenv('ATLAS_NETWORK_MODE','connected')!='local_only','privacy':'个人连接配置和凭据按已验证身份隔离；共享口令对应同一用户。在项目中取得的数据、任务记录和成果按项目范围共享。'}}


def provision_presets(configuration=None):
    """Trusted startup preset publication, without contacting optional servers.

    Presets describe available MCPs; first enabling one verifies its actual
    catalog and connection. A server outage must not prevent local startup.
    """
    configuration=configuration or {}
    if not isinstance(configuration,dict) or set(configuration)-{'skills','mcp'}:raise ValueError('管理员预置配置字段无效')
    if any(not isinstance(configuration.get(k,[]),list) for k in ('skills','mcp')) or sum(len(configuration.get(k,[])) for k in ('skills','mcp'))>60:raise ValueError('管理员预置项目过多或格式无效')
    builtin=Path(__file__).with_name('builtin_skills')/'finance-close'/'SKILL.md'
    result={'skills':[install_skill('SKILL.md',builtin.read_bytes(),preset=True,identity='skp_finance_monthly_close')],'mcp':[]}
    for entry in configuration.get('skills',[]):
        if not isinstance(entry,dict) or set(entry)-{'id','name','content'} or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',str(entry.get('id',''))) or not isinstance(entry.get('content'),str) or not isinstance(entry.get('name',''),str):raise ValueError('预置技能须包含稳定id及Markdown content')
        identity='skp_'+digest(entry['id'].encode())[:40]
        result['skills'].append(install_skill('SKILL.md',entry['content'].encode(),name=entry.get('name',''),preset=True,identity=identity))
    for entry in configuration.get('mcp',[]):
        if not isinstance(entry,dict) or set(entry)!={'id','configuration'} or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',str(entry['id'])):raise ValueError('预置MCP须包含稳定id及configuration')
        try:value=MCPInput.model_validate(entry['configuration'],strict=True)
        except Exception:raise ValueError('预置MCP连接格式无效') from None
        identity='mpp_'+digest(entry['id'].encode())[:40]
        raw=value.model_dump();raw['credential']=value.credential.get_secret_value()
        internal=digest(json.dumps(raw,sort_keys=True).encode())
        try:old=get('mcp_preset',identity)
        except KeyError:old=None
        if old and old.get('configuration_sha256')==internal:
            result['mcp'].append({'id':identity,'unchanged':True});continue
        installed=preset_mcp(value,identity,verify=False)
        patch('mcp_preset',identity,configuration_sha256=internal)
        result['mcp'].append(installed)
    return result
