"""Authenticated project data workbench routes, installed with the owner dependency."""
import hashlib,json
from typing import Literal
from pydantic import Field
from fastapi import Depends,HTTPException,Request
from fastapi.responses import FileResponse
from .models import StrictModel
from .store import get,put,patch,all_items,seal,unseal,uid,now
from .config import DATA
from . import project_data as data,workspace


class EditWorkflow(StrictModel):
    expected_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    document:data.WorkflowDocument
class AdoptInput(StrictModel):
    expected_plan_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    run_id:str|None=None
class Review(StrictModel):
    expected_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    target_id:str|None=None
    decision:Literal['approved','rejected']='approved'
class AlignmentInput(StrictModel):
    source:dict
    catalog:list[dict]=Field(min_length=1,max_length=500)
    model_candidates:list[dict]=Field(default_factory=list,max_length=100)
class CatalogAlignmentInput(StrictModel):
    source:dict
    catalog_id:str=Field(min_length=1,max_length=64)
    expected_catalog_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    as_of_date:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    model_candidates:list[dict]=Field(default_factory=list,max_length=100)
class MappingInput(StrictModel):mapping:dict
class TableConfiguration(StrictModel):options:data.ReadOptions
class ShareInput(StrictModel):
    allowed_subjects:list[str]=Field(min_length=1,max_length=100)
    expires_hours:int=Field(default=168,ge=1,le=720)
    allow_download:bool=False
class BriefInput(StrictModel):
    run_id:str=Field(min_length=1,max_length=64)
    recipe_path:str=Field(min_length=1,max_length=200)


def install(app,auth):
    dependencies=[Depends(auth)]
    def actor(request):
        identity=getattr(request.state,'identity',{})
        return identity.get('subject') or 'owner_workspace'

    @app.get('/api/deployment/qualification',dependencies=dependencies)
    def deployment_qualification():
        from .production_release import evaluate_release
        return evaluate_release()

    @app.get('/api/projects/{pid}/business-reports',dependencies=dependencies)
    def business_reports(pid:str):
        from .business_reports import list_reports
        return {'items':list_reports(pid)}

    @app.get('/api/projects/{pid}/business-report-inputs/{rid}',dependencies=dependencies)
    def business_report_input(pid:str,rid:str):
        from .business_reports import inspect_result
        return inspect_result(pid,rid)

    @app.post('/api/projects/{pid}/business-reports',dependencies=dependencies)
    def render_brief(pid:str,value:BriefInput):
        from .business_reports import render_saved
        return render_saved(pid,value.run_id,value.recipe_path)

    @app.get('/api/projects/{pid}/business-reports/{report_id}/download',dependencies=dependencies)
    def download_brief(pid:str,report_id:str,format:Literal['markdown','manifest','docx','pptx']='markdown'):
        from .business_reports import report_file
        path=report_file(pid,report_id,format)
        return FileResponse(path,filename='business-report-'+report_id+path.suffix)

    @app.get('/api/projects/{pid}/source-tables/{sid}',dependencies=dependencies)
    def source_tables(pid:str,sid:str):return data.inspect_source(pid,sid)

    @app.post('/api/projects/{pid}/source-tables/{sid}/preview',dependencies=dependencies)
    def source_preview(pid:str,sid:str,value:data.SourceTable):
        if value.source_id!=sid:raise ValueError('来源标识不一致')
        return data.preview_source(pid,value)

    @app.put('/api/projects/{pid}/source-tables/{sid}',dependencies=dependencies)
    def configure_table(pid:str,sid:str,value:TableConfiguration):
        from .sources import revision
        s=data.owned(pid,'source',sid)
        if s['kind']!='file' or not s['name'].lower().endswith('.xlsx'):raise ValueError('该配置用于Excel来源')
        cfg=unseal(s['secret_config']);options=value.options.model_dump(exclude_none=True,exclude={'html_table'})
        if 'sheet' in options:cfg['sheet']=options.pop('sheet')
        cfg['xlsx_options']=options
        s=patch('source',sid,secret_config=seal(cfg),status='queued');revision(s)
        from .execution import enqueue
        return {'source_id':sid,'job_id':enqueue('probe',sid)['id'],'options':value.options.model_dump()}

    @app.post('/api/projects/{pid}/indented-sources',dependencies=dependencies)
    def indented_source(pid:str,value:data.IndentedSource):return data.extract_indented_source(pid,value.source_id,value.columns)

    @app.get('/api/projects/{pid}/data-workflows',dependencies=dependencies)
    def workflows(pid:str):
        from .workflow_guidance import catalog
        return catalog(pid)

    @app.post('/api/projects/{pid}/data-workflows',dependencies=dependencies)
    def register_workflow(pid:str,value:data.RegisterWorkflow):
        receipt=data.register(pid,value.path);workspace.checkpoint(pid,reason='保存数据加工方案')
        return receipt

    @app.get('/api/projects/{pid}/data-workflows/{wid}',dependencies=dependencies)
    def workflow(pid:str,wid:str):
        info=data.inspect_workflow(pid,wid);info['document']=data.load_document(pid,info['path']).model_dump()
        return info

    @app.put('/api/projects/{pid}/data-workflows/{wid}',dependencies=dependencies)
    def edit_workflow(pid:str,wid:str,value:EditWorkflow):
        with workspace.lock(pid):
            w=data.owned(pid,'data_workflow',wid);current=data.load_document(pid,w['path'])
            if data.digest(current.model_dump())!=value.expected_sha256:raise HTTPException(409,'方案已变化，请重新读取并核对后保存')
            if set(value.document.bindings)!=set(value.document.plan.inputs):raise ValueError('输入绑定与方案不一致')
            for binding in value.document.bindings.values():
                if bool(binding.source_id)==bool(binding.result_id):raise ValueError('输入绑定必须唯一')
                if binding.source_id:data.owned(pid,'source',binding.source_id)
                else:data.result_folder(pid,binding.result_id)
            if value.document.contract:
                from .data_contracts import validate
                validate(value.document.contract)
            workspace.write_file(pid,w['path'],json.dumps(value.document.model_dump(),ensure_ascii=False,indent=2))
            receipt=data.register(pid,w['path']);workspace._checkpoint(pid,None,'修改数据加工方案')
        return receipt

    @app.post('/api/projects/{pid}/data-workflows/{wid}/run',dependencies=dependencies)
    def run_workflow(pid:str,wid:str):
        r=data.enqueue_workflow(pid,wid)
        return {k:r[k] for k in ('id','status','target','commit')}

    @app.put('/api/projects/{pid}/data-workflows/{wid}/schedule',dependencies=dependencies)
    def schedule_workflow(pid:str,wid:str,value:data.ScheduleWorkflow):
        if value.workflow_id!=wid:raise ValueError('流程标识不一致')
        return data.schedule(pid,wid,value.cron,value.watch_sources)

    @app.get('/api/projects/{pid}/data-results/{rid}',dependencies=dependencies)
    def data_result(pid:str,rid:str,offset:int=0,limit:int=100):return data.read_result(pid,rid,offset,limit)

    @app.post('/api/projects/{pid}/data-results/{rid}/publish',dependencies=dependencies)
    def adopt_result(pid:str,rid:str,value:AdoptInput):
        if value.run_id and value.run_id!=rid:raise ValueError('结果标识不一致')
        return data.publish_result(pid,rid,value.expected_plan_sha256)

    @app.get('/api/projects/{pid}/data-results/{rid}/download',dependencies=dependencies)
    def download_result(pid:str,rid:str,format:Literal['xlsx','csv','handoff']='handoff'):
        path=data.result_download(pid,rid,format)
        return FileResponse(path,filename='data-result-'+rid+{'xlsx':'.xlsx','csv':'.csv','handoff':'.zip'}[format])

    @app.post('/api/projects/{pid}/semantic-proposals',dependencies=dependencies)
    def propose_alignment(pid:str,value:AlignmentInput):
        from .semantic_service import propose
        return propose(pid,value.source,value.catalog,value.model_candidates)

    from .semantic_catalog import CatalogDefinition
    @app.get('/api/projects/{pid}/semantic-catalogs',dependencies=dependencies)
    def semantic_catalogs(pid:str,status:Literal['draft','approved','retired']|None=None,offset:int=0,limit:int=20):
        from .semantic_catalog import list_catalogs
        return list_catalogs(pid,status=status,offset=offset,limit=limit)

    @app.post('/api/projects/{pid}/semantic-catalogs',dependencies=dependencies)
    def propose_semantic_catalog(pid:str,value:CatalogDefinition,request:Request):
        from .semantic_catalog import propose
        return propose(pid,value,actor(request))

    @app.get('/api/projects/{pid}/semantic-catalogs/{catalog_id}',dependencies=dependencies)
    def semantic_catalog(pid:str,catalog_id:str):
        from .semantic_catalog import get_catalog
        return get_catalog(pid,catalog_id)

    @app.post('/api/projects/{pid}/semantic-catalogs/{catalog_id}/approve',dependencies=dependencies)
    def approve_semantic_catalog(pid:str,catalog_id:str,value:Review,request:Request):
        from .semantic_catalog import approve
        if value.decision!='approved':raise ValueError('批准语义包需要明确的approved决定')
        return approve(pid,catalog_id,value.expected_sha256,actor(request))

    @app.post('/api/projects/{pid}/semantic-catalogs/{catalog_id}/retire',dependencies=dependencies)
    def retire_semantic_catalog(pid:str,catalog_id:str,value:Review,request:Request):
        from .semantic_catalog import retire
        return retire(pid,catalog_id,value.expected_sha256,actor(request))

    @app.post('/api/projects/{pid}/semantic-catalog-alignments',dependencies=dependencies)
    def propose_catalog_alignment(pid:str,value:CatalogAlignmentInput):
        from .semantic_service import propose_from_catalog
        return propose_from_catalog(pid,value.source,value.catalog_id,value.expected_catalog_sha256,
                                    value.model_candidates,as_of_date=value.as_of_date)

    @app.post('/api/projects/{pid}/semantic-mappings',dependencies=dependencies)
    def propose_value_mapping(pid:str,value:MappingInput):
        from .semantic_service import propose_mapping
        return propose_mapping(pid,value.mapping)

    @app.get('/api/projects/{pid}/semantic-proposals',dependencies=dependencies)
    def proposals(pid:str,offset:int=0,limit:int=20):
        from .semantic_service import list_proposals
        return list_proposals(pid,offset,limit)

    @app.get('/api/projects/{pid}/semantic-proposals/{ident}',dependencies=dependencies)
    def proposal(pid:str,ident:str):
        from .semantic_service import get_proposal
        return get_proposal(pid,ident)

    @app.post('/api/projects/{pid}/semantic-proposals/{ident}/review',dependencies=dependencies)
    def review_proposal(pid:str,ident:str,value:Review,request:Request):
        from .semantic_service import get_proposal,review,review_mapping_record
        record=get_proposal(pid,ident)
        if record['kind']=='value_mapping':
            if value.decision!='approved':raise ValueError('映射可修改为新提案；拒绝操作不产生可执行核对回执')
            return review_mapping_record(pid,ident,value.expected_sha256,actor(request))
        return review(pid,ident,value.expected_sha256,value.target_id,actor(request),value.decision)

    from .data_tools import FactProposal
    @app.get('/api/projects/{pid}/data-documents/{sid}',dependencies=dependencies)
    def document_evidence(pid:str,sid:str,offset:int=0,limit:int=12000):
        from .fact_service import read_document
        return read_document(pid,sid,offset,limit)

    @app.post('/api/projects/{pid}/data-facts',dependencies=dependencies)
    def propose_facts(pid:str,value:FactProposal):
        from .fact_service import propose_facts
        return propose_facts(pid,value.source_id,value.facts)

    @app.get('/api/projects/{pid}/data-facts',dependencies=dependencies)
    def facts_list(pid:str,offset:int=0,limit:int=20):
        from .fact_service import list_facts
        return list_facts(pid,offset,limit)

    @app.get('/api/projects/{pid}/data-facts/{ident}',dependencies=dependencies)
    def facts_detail(pid:str,ident:str):
        from .fact_service import get_facts
        return get_facts(pid,ident)

    @app.post('/api/projects/{pid}/data-facts/{ident}/review',dependencies=dependencies)
    def facts_review(pid:str,ident:str,value:Review,request:Request):
        from .fact_service import review_facts
        return review_facts(pid,ident,value.expected_sha256,actor(request),value.decision)

    from .project_reports import ReportConnectorInput,FetchReportInput
    @app.post('/api/projects/{pid}/report-connectors',dependencies=dependencies)
    def report_connector(pid:str,value:ReportConnectorInput):
        from .project_reports import configure
        return configure(pid,value)

    @app.get('/api/projects/{pid}/report-connectors',dependencies=dependencies)
    def report_connectors(pid:str):
        from .project_reports import list_connectors
        return {'items':list_connectors(pid)}

    @app.post('/api/projects/{pid}/report-fetches',dependencies=dependencies)
    def report_fetch(pid:str,value:FetchReportInput):
        from .project_reports import enqueue_fetch
        return enqueue_fetch(pid,value.connector_id,value.filters,value.context)

    @app.get('/api/projects/{pid}/report-fetches/{rid}',dependencies=dependencies)
    def report_fetch_result(pid:str,rid:str):
        from .project_reports import read_fetch
        return read_fetch(pid,rid)

    @app.post('/api/projects/{pid}/data-results/{rid}/shares',dependencies=dependencies)
    def create_share(pid:str,rid:str,value:ShareInput,request:Request):
        from .data_sharing import create_share
        return create_share(pid,rid,value.allowed_subjects,actor(request),value.expires_hours,value.allow_download)

    @app.get('/api/projects/{pid}/data-shares',dependencies=dependencies)
    def shares(pid:str):
        from .data_sharing import list_shares
        return {'items':list_shares(pid)}

    @app.delete('/api/projects/{pid}/data-shares/{sid}',dependencies=dependencies)
    def revoke_share(pid:str,sid:str,request:Request):
        from .data_sharing import revoke
        return revoke(pid,sid,actor(request))

    @app.get('/api/shared-data/{sid}')
    def shared_data(sid:str,request:Request,offset:int=0,limit:int=100):
        from .data_sharing import authorize
        share=authorize(request,sid)
        return data.read_result(share['project_id'],share['result_id'],offset,limit)

    @app.get('/api/shared-data/{sid}/download')
    def shared_download(sid:str,request:Request):
        from .data_sharing import authorize
        share=authorize(request,sid)
        if not share['allow_download']:raise HTTPException(403,'该共享不允许下载')
        path=data.result_download(share['project_id'],share['result_id'])
        return FileResponse(path,filename='shared-data-result.zip')
