"""User extension installation and separate operator preset routes."""
from fastapi import Depends,File,Form,UploadFile,Query
from . import extensions as ext


def install(app,auth,operator_auth):
    user=[Depends(auth)];operator=[Depends(operator_auth)]

    @app.get('/api/extensions',dependencies=user)
    def extensions(project_id:str|None=None):return ext.catalog(project_id)

    @app.post('/api/extensions/skills',dependencies=user)
    async def skill(file:UploadFile=File(...),name:str=Form('')):
        data=await file.read(ext.MAX_ARCHIVE+1)
        return ext.install_skill(file.filename or 'SKILL.md',data,name=name)

    @app.put('/api/extensions/skills/{skill_id}',dependencies=user)
    def enable_skill(skill_id:str,value:ext.Toggle):return ext.toggle_skill(skill_id,value)

    @app.delete('/api/extensions/skills/{skill_id}',dependencies=user)
    def remove_skill(skill_id:str,expected_digest:str=Query(...,pattern=r'^[a-f0-9]{64}$')):return ext.remove_skill(skill_id,expected_digest)

    @app.post('/api/extensions/mcp/probe',dependencies=user)
    def probe(value:ext.MCPInput):
        _,tools=ext.probe_mcp(value)
        return {'tools':list(tools.values()),'configured':False,'message':'仅探查目录，尚未授权工具；请核对只读工具和需要确认的写入工具'}

    @app.post('/api/projects/{project_id}/extensions/mcp',dependencies=user)
    def mcp(project_id:str,value:ext.MCPInput):return ext.configure_mcp(project_id,value)

    @app.put('/api/projects/{project_id}/extensions/mcp/{connector_id}',dependencies=user)
    def update_mcp(project_id:str,connector_id:str,value:ext.MCPInput):return ext.configure_mcp(project_id,value,connector_id)

    @app.delete('/api/projects/{project_id}/extensions/mcp/{connector_id}',dependencies=user)
    def disable_mcp(project_id:str,connector_id:str):return ext.disconnect_mcp(project_id,connector_id)

    @app.post('/api/projects/{project_id}/extensions/presets/{preset_id}/enable',dependencies=user)
    def enable_preset(project_id:str,preset_id:str,value:ext.Toggle):return ext.enable_preset(project_id,preset_id,value)

    @app.post('/api/operator/extensions/skills',dependencies=operator)
    async def preset_skill(file:UploadFile=File(...),name:str=Form(''),skill_id:str|None=Form(None)):
        data=await file.read(ext.MAX_ARCHIVE+1)
        return ext.install_skill(file.filename or 'SKILL.md',data,name=name,preset=True,identity=skill_id)

    @app.post('/api/operator/extensions/mcp',dependencies=operator)
    def preset_mcp(value:ext.MCPInput):return ext.preset_mcp(value)

    @app.put('/api/operator/extensions/mcp/{preset_id}',dependencies=operator)
    def update_preset_mcp(preset_id:str,value:ext.MCPInput):return ext.preset_mcp(value,preset_id)
