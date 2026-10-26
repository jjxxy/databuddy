"""User-visible memory and compression controls under the existing identity."""
from typing import Literal
from fastapi import Depends,HTTPException,Query
from pydantic import Field
from .models import StrictModel
from . import memory_service as memory,context_compaction as context

class CompactRequest(StrictModel):session_id:str|None=Field(default=None,max_length=64)

def install(app,auth):
    dependencies=[Depends(auth)]
    def conflict(fn,*args,**kwargs):
        try:return fn(*args,**kwargs)
        except ValueError as exc:
            if '已变化' in str(exc) or '同名' in str(exc):raise HTTPException(409,str(exc)) from None
            raise
    @app.get('/api/projects/{pid}/memories',dependencies=dependencies)
    def memories(pid:str,scope:Literal['all','project','personal']='all',include_expired:bool=True):
        return {'items':memory.list_memories(pid,scope,include_expired),'generation':memory.generation(pid),
                'scope_note':'个人偏好仅当前身份可见；项目决策与项目成员共享，不等于已核阅财务口径'}
    @app.post('/api/projects/{pid}/memories',dependencies=dependencies)
    def add(pid:str,value:memory.MemoryInput):return conflict(memory.save,pid,value)
    @app.put('/api/projects/{pid}/memories/{identity}',dependencies=dependencies)
    def edit(pid:str,identity:str,value:memory.EditMemory):
        return conflict(memory.save,pid,value.model_dump(exclude={'expected_version'}),identity,value.expected_version)
    @app.delete('/api/projects/{pid}/memories/{identity}',dependencies=dependencies)
    def forget(pid:str,identity:str,expected_version:int=Query(ge=1)):
        return conflict(memory.forget,pid,identity,expected_version)
    @app.get('/api/projects/{pid}/context',dependencies=dependencies)
    def state(pid:str,session_id:str|None=None):return context.status(pid,session_id)
    @app.post('/api/projects/{pid}/context/compact',dependencies=dependencies)
    def compact(pid:str,value:CompactRequest):return context.status(pid,value.session_id,force=True)
    @app.get('/api/projects/{pid}/context/original',dependencies=dependencies)
    def original(pid:str,turn_id:str,offset:int=Query(default=0,ge=0),limit:int=Query(default=4000,ge=1,le=10000)):
        from .store import get
        target=get('turn',turn_id)
        if target.get('project_id')!=pid:raise HTTPException(404,'对话不属于当前项目')
        from .conversation import redact
        from .company_search import visible_answer
        # Manual recovery is a UI read, independent from model share-data grants.
        text=__import__('json').dumps({'turn_id':target['id'],'user':redact(target.get('text','')),
            'agent':visible_answer(target),'state':target.get('state'),'outcome':target.get('outcome')},ensure_ascii=False)
        return {'turn_id':turn_id,'content':text[offset:offset+limit],'next_offset':offset+limit if offset+limit<len(text) else None,
                'total_characters':len(text),'scope':'manual_original_history','memory_policy':'原文不重新保存为有效记忆'}
