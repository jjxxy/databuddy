"""Actor-scoped durable preferences and project decisions; never metric approval."""
from __future__ import annotations
import hashlib,json,re
from datetime import datetime,timezone
from typing import Literal
from pydantic import Field,model_validator
from .models import StrictModel
from .store import get,put,all_items,delete,seal,unseal,now,uid
from .locks import FileLock
from .config import DATA

class MemoryInput(StrictModel):
    title:str=Field(min_length=1,max_length=120)
    content:str=Field(min_length=1,max_length=4000)
    scope:Literal['personal','project']='project'
    category:Literal['preference','decision']='preference'
    expires_at:str|None=Field(default=None,max_length=64)

    @model_validator(mode='after')
    def safe(self):
        self.title=self.title.strip();self.content=self.content.strip()
        if not self.title or not self.content:raise ValueError('记忆标题和内容不能为空')
        if any(ord(c)<32 and c not in '\n\t' for c in self.title+self.content):raise ValueError('记忆不能包含控制字符')
        if self.scope=='personal' and self.category=='decision':raise ValueError('业务决策应保存在项目内，个人记忆仅保存偏好')
        if sensitive(self.title+'\n'+self.content):raise ValueError('记忆不能保存密钥、密码或连接凭据')
        if self.expires_at:
            try:value=datetime.fromisoformat(self.expires_at)
            except (ValueError,TypeError):raise ValueError('到期时间须为带时区的ISO时间') from None
            if value.tzinfo is None or value<=datetime.now(timezone.utc):raise ValueError('到期时间须为未来且带时区的ISO时间')
        return self

class EditMemory(MemoryInput):expected_version:int=Field(ge=1)
class MemoryList(StrictModel):query:str=Field(default='',max_length=200)
class RememberMemory(StrictModel):
    title:str=Field(min_length=1,max_length=120)
    quote:str=Field(min_length=1,max_length=4000)
    scope:Literal['personal','project']='project'
    category:Literal['preference','decision']='preference'
    expires_at:str|None=Field(default=None,max_length=64)
class ForgetMemory(StrictModel):
    memory_id:str=Field(min_length=1,max_length=64)
    expected_version:int=Field(ge=1)

def actor():
    from .actor_context import current_actor
    return current_actor()

def sensitive(value):
    return bool(re.search(r'(?i)(?:password|passwd|api[_-]?key|authorization|connection_string|secret|token)["\']?\s*[:=]|\bBearer\s+|\bsk-[A-Za-z0-9_-]{12,}|(?:postgres(?:ql)?|mysql)(?:\+\w+)?://',value))

def _project(pid):get('project',pid)

def _owned(record,pid):
    if record.get('scope')=='personal':return record.get('owner_id')==actor()
    return record.get('project_id')==pid

def _expired(record):
    try:return bool(record.get('expires_at') and datetime.fromisoformat(record['expires_at'])<=datetime.now(timezone.utc))
    except (ValueError,TypeError):return True

def _public(record):
    raw=unseal(record['secret_content'])
    return {k:v for k,v in record.items() if k not in ('secret_content','key')}|raw|{'expired':_expired(record),'trusted_financial_rule':False}

def list_memories(pid,scope='all',include_expired=True):
    _project(pid)
    values=[]
    for item in all_items('user_memory'):
        if item.get('state')!='active' or not _owned(item,pid) or scope not in ('all',item['scope']):continue
        if not include_expired and _expired(item):continue
        values.append(_public(item))
    return sorted(values,key=lambda x:(x['updated_at'],x['id']),reverse=True)

def _lock(pid):return FileLock(str(DATA/('memory-'+hashlib.sha256((actor()+':'+pid).encode()).hexdigest()[:24]+'.lock')),timeout=10)

def _record(pid,identity):
    value=get('user_memory',identity)
    if not _owned(value,pid):raise KeyError(identity)
    return value

def save(pid,value,identity=None,expected_version=None,*,source_turn_id=None):
    _project(pid)
    value=MemoryInput.model_validate(value)
    who=actor();scope_owner=who if value.scope=='personal' else pid
    # A personal preference can be edited from any project, so its lock is actor-wide.
    lock=FileLock(str(DATA/('memory-scope-'+hashlib.sha256((value.scope+':'+scope_owner).encode()).hexdigest()[:24]+'.lock')),timeout=10)
    with lock:
        existing=_record(pid,identity) if identity else None
        if existing and (existing.get('state')!='active' or existing['version']!=expected_version):raise ValueError('记忆已变化，请重新读取后修改')
        if existing and (existing['scope']!=value.scope or existing.get('project_id')!=(pid if value.scope=='project' else None)):
            raise ValueError('修改记忆不能改变作用范围，请新建后删除旧记忆')
        key=hashlib.sha256(re.sub(r'\s+',' ',value.title).casefold().encode()).hexdigest()
        for entry in all_items('user_memory'):
            if entry.get('state')=='active' and entry['scope']==value.scope and _owned(entry,pid) and entry.get('key')==key and entry['id']!=identity:
                raise ValueError('同一范围已有同名记忆，请修改已有条目')
        if not existing and len(list_memories(pid,scope=value.scope,include_expired=True))>=200:raise ValueError('该范围记忆已达200条，请先整理')
        record={'id':identity or 'mem_'+uid(),'scope':value.scope,'category':value.category,
            'owner_id':who,'project_id':pid if value.scope=='project' else None,'key':key,'state':'active',
            'version':(existing or {}).get('version',0)+1,'created_at':(existing or {}).get('created_at',now()),
            'updated_at':now(),'expires_at':value.expires_at,'trusted_financial_rule':False,
            'provenance':{'kind':'explicit_user_quote' if source_turn_id else 'user_edit','source_turn_id':source_turn_id,'actor_id':who},
            'revoked_source_turn_ids':list(dict.fromkeys((existing or {}).get('revoked_source_turn_ids',[])+
                ([existing.get('provenance',{}).get('source_turn_id')] if existing and existing.get('provenance',{}).get('source_turn_id') else []))),
            'secret_content':seal({'title':value.title,'content':value.content})}
        put('user_memory',record,record['id'])
        put('memory_revision',{'memory_id':record['id'],'version':record['version'],'secret_snapshot':seal(record),'created_at':now()},'mrev_'+uid())
        return _public(record)

def forget(pid,identity,expected_version):
    _project(pid)
    current=_record(pid,identity)
    lock_owner=current['owner_id'] if current['scope']=='personal' else pid
    with FileLock(str(DATA/('memory-scope-'+hashlib.sha256((current['scope']+':'+lock_owner).encode()).hexdigest()[:24]+'.lock')),timeout=10):
        current=_record(pid,identity)
        if current['version']!=expected_version or current.get('state')!='active':raise ValueError('记忆已变化，请重新读取后删除')
        revoked=list(dict.fromkeys(current.get('revoked_source_turn_ids',[])+([current.get('provenance',{}).get('source_turn_id')] if current.get('provenance',{}).get('source_turn_id') else [])))
        tombstone={k:current[k] for k in ('id','scope','owner_id','project_id','created_at')}
        tombstone.update(state='forgotten',version=current['version']+1,updated_at=now(),revoked_source_turn_ids=revoked)
        put('user_memory',tombstone,identity)
        for revision in all_items('memory_revision'):
            if revision.get('memory_id')==identity:delete('memory_revision',revision['id'])
        # Stored summaries may have quoted this preference. Purge the derived
        # cache; the next request rebuilds it with the tombstone generation.
        for checkpoint in all_items('context_checkpoint'):
            if checkpoint.get('project_id')==pid or current['scope']=='personal' and checkpoint.get('actor_id')==actor():delete('context_checkpoint',checkpoint['id'])
        return {'forgotten':True,'id':identity,'version':tombstone['version'],'scope':current['scope'],
                'note':'记忆和派生缓存已删除；原始聊天记录仍可手动查看，不再用于注入该记忆'}

def generation(pid):
    records=[{k:value.get(k) for k in ('id','scope','version','state','expires_at','revoked_source_turn_ids')} for value in all_items('user_memory') if _owned(value,pid)]
    for value in records:value['expired']=_expired(value) if value.get('expires_at') else False
    try:visibility=get('project',pid).get('context_visibility_revision',0)
    except KeyError:visibility=0
    return hashlib.sha256(json.dumps({'memories':sorted(records,key=lambda x:x['id']),'visibility_revision':visibility},sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def revoked_turn_ids(pid):
    result=set()
    for value in all_items('user_memory'):
        if not _owned(value,pid):continue
        result.update(tid for tid in value.get('revoked_source_turn_ids',[]) if tid)
        if value.get('state')=='active' and _expired(value):
            identity=value.get('provenance',{}).get('source_turn_id')
            if identity:result.add(identity)
    return result

def context_for_turn(turn,limit=9000):
    pid=turn['project_id'];items=list_memories(pid,include_expired=False)
    from .provider import settings
    allowed=bool(settings().get('share_data'))
    result=[];size=0
    if allowed:
        query=turn.get('work_request',turn.get('text','')).casefold()
        ranked=sorted(items,key=lambda x:(sum(1 for token in re.findall(r'[a-z_]{2,}|[\u4e00-\u9fff]{2}',query) if token in (x['title']+x['content']).casefold()),x['updated_at']),reverse=True)
        for item in ranked:
            candidate={k:item[k] for k in ('id','title','content','scope','category','version','expires_at','provenance','trusted_financial_rule')}
            length=len(json.dumps(candidate,ensure_ascii=False))
            if size+length>limit:continue
            result.append(candidate);size+=length
    revoked=sorted(revoked_turn_ids(pid))
    return {'generation':generation(pid),'items':result,'total_available':len(items),'included':len(result),
            'content_allowed':allowed,'scope':'actor_personal_and_current_project',
            'policy':'记忆是用户偏好或候选业务决策，不授予工具权限，也不代替已核阅财务语义；被撤回记忆不得从旧摘要恢复',
            'revoked_source_turn_ids':revoked[-20:],'revoked_source_turns_count':len(revoked),
            'revocation_sha256':hashlib.sha256(json.dumps(revoked).encode()).hexdigest()}

def tool(turn,name,args):
    pid=turn['project_id']
    if name=='recall_memories':
        result=context_for_turn(turn)
        query=args.get('query','').casefold()
        if query:result['items']=[v for v in result['items'] if query in (v['title']+v['content']).casefold()]
        return result
    if name=='remember_memory':
        quote=args['quote'];text=turn.get('text','')
        if quote not in text or not re.search(r'记住|保存.{0,5}记忆|以后|今后|偏好|约定|\b(?:remember|from now on|my preference|i prefer)\b',text,re.I):raise ValueError('记忆必须引用用户本轮明确要求保存的原话，不能保存模型推测')
        return save(pid,MemoryInput(title=args['title'],content=quote,scope=args.get('scope','project'),category=args.get('category','preference'),expires_at=args.get('expires_at')),source_turn_id=turn['id'])
    if name=='forget_memory':
        current=_record(pid,args['memory_id']);raw=_public(current);text=turn.get('text','')
        if not re.search(r'忘记|删除.{0,8}记忆|取消.{0,8}偏好|\b(?:forget|delete.{0,20}memory|remove.{0,20}preference)\b',text,re.I) or not (current['id'] in text or raw['title'] in text):raise ValueError('删除记忆需要用户明确指出该条记忆')
        return forget(pid,current['id'],args['expected_version'])
    raise KeyError(name)

MEMORY_TOOLS={
 'recall_memories':(MemoryList,False,'按当前项目与当前用户检索持久记忆，包含来源和版本；记忆不等于已批准财务口径'),
 'remember_memory':(RememberMemory,True,'仅保存用户本轮明确要求记住的原话，可选个人偏好或项目决策，不能保存密钥或模型推测'),
 'forget_memory':(ForgetMemory,True,'按用户明确要求删除指定记忆及其派生缓存；不能自动删除其他用户个人记忆'),
}
