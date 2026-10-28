"""Immutable, bounded, exact data exchange across Agent trust boundaries.

Checksums prove the bytes received, not the truth of another Agent's claims.
No URL or arbitrary path from a peer is fetched. Results retain the existing
typed complete-result format instead of flattening a preview into a new table.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import zipfile

from .config import DATA, MAX_ROWS
from .locks import FileLock
from .store import get, put, seal, uid, now
from .platform_files import open_regular_read,create_regular_exclusive,fsync_directory,assert_plain_path,atomic_replace,fsync_file

FORMAT = 'atlas-agent-handoff'
SOURCE_SUFFIXES = {'.csv','.xlsx','.json','.jsonl','.pdf','.docx','.pptx','.png','.jpg','.jpeg','.tiff','.webp','.html','.htm','.txt','.md'}
PRIVATE_NAMES = {'llm.json','llm.example.json','llm.multi.example.json','credentials.json','secrets.json','token.json','auth.json','opencode.json','connector.key'}
RESULT_FILES = {'manifest.json','rows.jsonl','schema.json','context.json','provenance.jsonl','stages.json'}
ITEM_KEYS = {'id','kind','name','media_type','bytes','sha256','encoding','content_base64','source_version','result_metadata','provenance'}
HEX = re.compile(r'^[a-f0-9]{64}$')


class HandoffError(ValueError):
    pass


def _limit():
    value=int(os.getenv('ATLAS_AGENT_HANDOFF_MAX_BYTES',str(2*1024*1024)))
    if not 1024<=value<=32*1024*1024:raise HandoffError('Agent交接体积限制配置无效')
    return value


def _json(value):
    try:return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    except (ValueError,TypeError,RecursionError):raise HandoffError('Agent交接必须是有限且有效的JSON数据') from None


def _digest(raw):return hashlib.sha256(raw).hexdigest()


def _identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value):raise HandoffError('Agent交接标识无效')
    return value


def _name(value,kind='source'):
    if (not isinstance(value,str) or not 1<=len(value)<=160 or value.startswith('.') or
            any(c in value for c in ('/','\\','\x00')) or any(ord(c)<32 for c in value) or value in ('.','..')):
        raise HandoffError('Agent交接文件名必须为普通文件名')
    if value.lower() in PRIVATE_NAMES or value.lower().startswith(('llm.','credentials.','secrets.')):
        raise HandoffError('不能交接模型配置或连接凭据文件')
    suffix=Path(value).suffix.lower()
    if kind=='source' and suffix not in SOURCE_SUFFIXES:raise HandoffError('Agent交接来源格式不受支持')
    if kind=='result' and suffix!='.zip':raise HandoffError('完整结果必须使用受验证的交接包')
    return value


def _metadata(value):
    if not isinstance(value,dict) or len(_json(value))>65536:raise HandoffError('Agent交接元数据无效或过大')
    return value


def _root():
    root=DATA/'agent-handoffs'
    if root.is_symlink():raise HandoffError('Agent交接目录不能是链接')
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    assert_plain_path(root,missing_ok=False)
    return root


def _read_bytes(path,maximum=None):
    maximum=_limit() if maximum is None else maximum
    descriptor=open_regular_read(path)
    with os.fdopen(descriptor,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>maximum:
            raise HandoffError('交接文件不是普通独立文件或超过体积限制；未截断')
        raw=stream.read(maximum+1);after=os.fstat(stream.fileno())
        if len(raw)>maximum or (before.st_ino,before.st_dev,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_dev,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise HandoffError('交接文件读取时发生变化；未使用不完整内容')
    return raw


def _atomic_bytes(path,raw):
    path=Path(path);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():raise HandoffError('交接存储不允许链接')
    if path.exists():
        if _read_bytes(path,max(len(raw),1))!=raw:raise HandoffError('已有不可变交接内容与回执不一致')
        return
    temporary=path.with_name('.'+path.name+'.'+uid()+'.part')
    try:
        descriptor=create_regular_exclusive(temporary)
        with os.fdopen(descriptor,'wb') as output:output.write(raw);output.flush();os.fsync(output.fileno())
        atomic_replace(temporary,path)
        fsync_directory(path.parent)
    finally:temporary.unlink(missing_ok=True)


def _manifest(bundle):
    return {k:([ {a:b for a,b in item.items() if a!='content_base64'} for item in v] if k=='items' else v)
            for k,v in bundle.items() if k!='manifest_sha256'}


def _bundle(items):
    value={'format':FORMAT,'version':1,'complete':True,'items':items}
    value['manifest_sha256']=_digest(_json(_manifest(value)))
    validate_bundle(value)
    raw=_json(value)
    with FileLock(str(DATA/('agent-handoff-'+value['manifest_sha256']+'.lock')),timeout=10):
        _atomic_bytes(_root()/'bundles'/(value['manifest_sha256']+'.json'),raw)
    return value


def _item(identity,kind,name,raw,**metadata):
    return {'id':_identifier(identity),'kind':kind,'name':_name(name,kind),'media_type':mimetypes.guess_type(name)[0] or 'application/octet-stream',
            'bytes':len(raw),'sha256':_digest(raw),'encoding':'base64','content_base64':base64.b64encode(raw).decode('ascii'),**metadata}


def _result_zip(raw,metadata):
    """Validate a fixed six-file archive without using extract/extractall."""
    from .result_delivery import verify_handoff,read_all_handoff
    from .data_workflows import TableSchema,Column
    maximum=_limit()*8
    temporary=tempfile.TemporaryDirectory(prefix='atlas-agent-result-')
    try:
        folder=Path(temporary.name)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names=archive.namelist()
            if len(names)!=len(set(names)) or set(names)!=RESULT_FILES:raise HandoffError('结果交接包文件集合不完整、重复或包含非法路径')
            if sum(info.file_size for info in archive.infolist())>maximum:raise HandoffError('结果交接包解压后超过预算；未截断')
            for info in archive.infolist():
                mode=info.external_attr>>16
                if info.is_dir() or info.flag_bits&1 or stat.S_IFMT(mode) not in (0,stat.S_IFREG):raise HandoffError('结果交接包不允许链接、特殊文件或加密内容')
                content=archive.read(info.filename)
                if len(content)!=info.file_size:raise HandoffError('结果交接包字节数不一致')
                (folder/info.filename).write_bytes(content)
        expected=metadata.get('handoff_manifest_sha256')
        if not isinstance(expected,str) or not HEX.fullmatch(expected):raise HandoffError('完整结果缺少独立交接清单摘要')
        receipt=verify_handoff(folder,expected)
        if type(metadata.get('records')) is not int or metadata['records']!=receipt['row_count']:raise HandoffError('完整结果行数与清单不一致')
        table=read_all_handoff(folder,expected,max_rows=MAX_ROWS,native_types=True)
        semantics=table['context'].get('output_semantics') if isinstance(table['context'],dict) else None
        if not isinstance(semantics,dict):raise HandoffError('完整结果缺少字段、单位、期间与粒度语义')
        parsed=TableSchema.model_validate(semantics)
        actual=[Column.model_validate(field).model_dump() for field in table['schema']]
        if actual!=[column.model_dump() for column in parsed.columns] or metadata.get('output_semantics')!=semantics:
            raise HandoffError('完整结果字段语义与实际交接结构不一致')
        if not HEX.fullmatch(str(metadata.get('plan_sha256',''))):raise HandoffError('完整结果方案摘要无效')
        return temporary,folder,table,receipt
    except (zipfile.BadZipFile,KeyError,UnicodeError,json.JSONDecodeError,TypeError,RecursionError) as exc:
        temporary.cleanup();raise HandoffError('结果交接包格式无效，未导入') from None
    except BaseException:
        temporary.cleanup();raise


def _decode(bundle):
    if not isinstance(bundle,dict) or set(bundle)!={'format','version','complete','items','manifest_sha256'}:
        raise HandoffError('Agent交接包结构无效')
    if bundle['format']!=FORMAT or type(bundle['version']) is not int or bundle['version']!=1 or bundle['complete'] is not True:
        raise HandoffError('Agent交接版本不受支持或不是完整内容')
    if not isinstance(bundle['items'],list) or len(bundle['items'])>20:raise HandoffError('一次最多交接20份完整资料')
    if len(_json(bundle))>_limit()*4//3+256*1024:raise HandoffError('Agent交接报文超过体积预算；未截断')
    if not isinstance(bundle['manifest_sha256'],str) or bundle['manifest_sha256']!=_digest(_json(_manifest(bundle))):raise HandoffError('Agent交接清单摘要不一致')
    result=[];total=0;identities=set()
    for item in bundle['items']:
        if not isinstance(item,dict) or set(item)-ITEM_KEYS or not {'id','kind','name','media_type','bytes','sha256','encoding','content_base64'}<=set(item):raise HandoffError('Agent交接文件结构无效')
        _identifier(item['id'])
        if item['id'] in identities:raise HandoffError('Agent交接文件标识重复')
        identities.add(item['id'])
        if item['kind'] not in ('source','result','artifact'):raise HandoffError('Agent交接文件类型无效')
        _name(item['name'],item['kind'])
        if not isinstance(item['media_type'],str) or len(item['media_type'])>150 or '\n' in item['media_type']:raise HandoffError('Agent交接媒体类型无效')
        if type(item['bytes']) is not int or item['bytes']<0 or item['bytes']>_limit():raise HandoffError('Agent交接字节数无效或超限')
        total+=item['bytes']
        if total>_limit():raise HandoffError('Agent交接总体积超过预算；未截断')
        if item['encoding']!='base64' or not isinstance(item['content_base64'],str):raise HandoffError('Agent交接编码无效')
        if len(item['content_base64'])>4*((item['bytes']+2)//3):raise HandoffError('Agent交接编码大小与声明不符')
        try:raw=base64.b64decode(item['content_base64'],validate=True)
        except (binascii.Error,ValueError):raise HandoffError('Agent交接Base64编码无效') from None
        if len(raw)!=item['bytes'] or not isinstance(item['sha256'],str) or not HEX.fullmatch(item['sha256']) or _digest(raw)!=item['sha256']:
            raise HandoffError('Agent交接文件摘要或字节数不一致')
        for key in ('source_version','result_metadata','provenance'):
            if key in item:_metadata(item[key])
        if item['kind']=='source':
            version=item.get('source_version',{})
            if type(version.get('revision')) is not int or version['revision']<1 or version.get('content_sha256')!=item['sha256']:raise HandoffError('来源缺少固定版本与字节证据')
            if not raw:raise HandoffError('来源文件为空')
            if Path(item['name']).suffix.lower() in ('.xlsx','.docx','.pptx'):
                try:
                    with zipfile.ZipFile(io.BytesIO(raw)) as office:
                        if sum(i.file_size for i in office.infolist())>200*1024*1024 or '[Content_Types].xml' not in office.namelist():raise HandoffError('Office来源格式损坏或解压体积超限')
                except zipfile.BadZipFile:raise HandoffError('Office来源格式损坏') from None
        elif item['kind']=='result':
            temporary,*_= _result_zip(raw,item.get('result_metadata',{}));temporary.cleanup()
        result.append((item,raw))
    return result,total


def validate_bundle(bundle):
    decoded,total=_decode(bundle)
    return {'manifest_sha256':bundle['manifest_sha256'],'complete':True,'files':len(decoded),'decoded_bytes':total,
            'items':[{k:v for k,v in item.items() if k!='content_base64'} for item,_ in decoded],
            'integrity_verified':True,'financial_verified':False,'policy':'字节与完整性核对不等于财务事实或口径已经批准'}


def build_input(turn,source_ids=None,result_ids=None):
    from . import project_data
    from .conversation import validate_attachment_versions
    from .result_delivery import read_all_handoff
    pid=turn['project_id'];get('project',pid);validate_attachment_versions(turn)
    source_ids=list(source_ids or []);result_ids=list(result_ids or [])
    if len(source_ids)+len(result_ids)>20 or len(set(source_ids))!=len(source_ids) or len(set(result_ids))!=len(result_ids):raise HandoffError('交接资料不能重复且总数最多20份')
    items=[];total=0
    for sid in source_ids:
        s=project_data.owned(pid,'source',sid)
        if s.get('archived') or s.get('kind')!='file':raise HandoffError('仅交接可核对的来源原件，连接配置不会发送给其他Agent')
        if project_data.source_requires_fact_alignment(s,{}):
            raise HandoffError('来源事实或外部资料尚缺少可验证的业务语义及完整来源链，不能作为普通资料交接给其他Agent')
        path=project_data.source_path(s);raw=_read_bytes(path)
        current=get('source',sid)
        if any(current.get(k)!=s.get(k) for k in ('revision','revision_id','content_hash')) or _digest(raw)!=s.get('content_hash'):
            raise HandoffError('来源版本或内容已变化，未交接')
        total+=len(raw)
        if total>_limit():raise HandoffError('完整来源超过Agent交接预算；请缩小范围，未发送预览代替原件')
        items.append(_item('source_'+sid,'source',s['name'],raw,source_version={'source_id':sid,'revision':s.get('revision',1),'revision_id':s.get('revision_id'),'content_sha256':s['content_hash']}))
    for rid in result_ids:
        folder,meta=project_data.result_folder(pid,rid)
        table=read_all_handoff(folder/'handoff',meta['handoff_sha256'],max_rows=MAX_ROWS)
        path=project_data.result_download(pid,rid,'handoff');raw=_read_bytes(path)
        total+=len(raw)
        if total>_limit():raise HandoffError('完整结果超过Agent交接预算；未截断为预览')
        metadata={'result_id':rid,'workflow_id':meta['workflow_id'],'plan_sha256':meta['plan_sha256'],'handoff_manifest_sha256':meta['handoff_sha256'],
                  'records':meta['records'],'output_semantics':table['context'].get('output_semantics'),
                  'requires_semantic_review':bool(meta.get('requires_semantic_review')),'semantic_review_reasons':meta.get('semantic_review_reasons',[])}
        items.append(_item('result_'+rid,'result','result-'+rid+'.zip',raw,result_metadata=metadata))
    return _bundle(items)


def import_bundle(pid,bundle,*,scope_id,request_id):
    """Import only explicit full inputs; imported results remain unapproved facts."""
    from .actor_context import current_actor
    from .sources import revision
    from .execution import enqueue
    from .jobs import save
    get('project',pid)
    if not isinstance(scope_id,str) or not 1<=len(scope_id)<=250 or not isinstance(request_id,str) or not 1<=len(request_id)<=250:raise HandoffError('交接请求范围无效')
    decoded,total=_decode(bundle)
    if any(item['kind']=='artifact' for item,_ in decoded):raise HandoffError('普通成果不能自动冒充来源或已核对结果，请明确选择资料导入')
    key='ahi_'+_digest(_json([pid,current_actor(),scope_id,request_id]))[:48]
    with FileLock(str(DATA/(key+'.lock')),timeout=20):
        try:existing=get('agent_handoff_import',key)
        except KeyError:existing=None
        if existing:
            if existing['manifest_sha256']!=bundle['manifest_sha256']:raise HandoffError('同一交接请求不能替换已经接受的完整资料')
            from . import project_data
            from .result_delivery import verify_handoff
            for source_id in existing['source_ids']:
                source=get('source',source_id)
                if source.get('project_id')!=pid or source.get('archived'):raise HandoffError('已接收来源被归档或不再属于当前项目')
                project_data.source_path(source)
            for result_id in existing['result_ids']:
                folder,metadata=project_data.result_folder(pid,result_id)
                verify_handoff(folder/'handoff',metadata['handoff_sha256'])
            return {k:v for k,v in existing.items() if k not in ('scope_id','actor_id')}
        source_ids=[];result_ids=[]
        for item,raw in decoded:
            identity=_digest(_json([key,bundle['manifest_sha256'],item['id']]))[:44]
            if item['kind']=='source':
                sid='as_'+identity;suffix=Path(item['name']).suffix.lower();stored=sid+suffix
                _atomic_bytes(DATA/'uploads'/stored,raw)
                try:source=get('source',sid)
                except KeyError:
                    source=put('source',{'project_id':pid,'name':item['name'],'kind':'file','bytes':len(raw),'content_hash':item['sha256'],
                        'status':'queued','created_at':now(),'origin_agent_handoff':bundle['manifest_sha256'],'skip_model_planning':True,
                        'external_provenance':item['source_version'],'requires_semantic_review':True,'semantic_alignment_required':True,
                        'secret_config':seal({'stored_name':stored,'agent_handoff':{'scope':scope_id,'manifest_sha256':bundle['manifest_sha256'],'source_version':item['source_version']}})},sid)
                    source=revision(source)
                if source.get('project_id')!=pid or source.get('content_hash')!=item['sha256']:raise HandoffError('交接来源与已有回执不一致')
                enqueue('probe',sid);source_ids.append(sid)
            else:
                rid='ar_'+identity;target='aw_'+identity;folder=DATA/'runs'/rid
                temporary,result_folder,table,receipt=_result_zip(raw,item['result_metadata'])
                try:
                    folder.mkdir(mode=0o700,parents=True,exist_ok=True)
                    destination=folder/'handoff'
                    if not destination.exists():
                        stage=folder/('.handoff-'+uid());shutil.copytree(result_folder,stage)
                        for file in stage.iterdir():fsync_file(file)
                        atomic_replace(stage,destination)
                    from .result_delivery import verify_handoff
                    verify_handoff(destination,item['result_metadata']['handoff_manifest_sha256'])
                    metadata={'run_id':rid,'workflow_id':target,'project_id':pid,'plan_sha256':item['result_metadata']['plan_sha256'],
                        'handoff_sha256':receipt['manifest_sha256'],'records':receipt['row_count'],'created_at':now(),'source_revisions':{},'complete':True,
                        'contract':None,'requires_semantic_review':True,'semantic_review_reasons':['EXTERNAL_AGENT_RESULT_REVIEW_REQUIRED'],
                        'external_handoff_manifest_sha256':bundle['manifest_sha256'],'external_result':item['result_metadata']}
                    try:old=get('run',rid)
                    except KeyError:old=None
                    if old:metadata=old['data_result']
                    _atomic_bytes(folder/'data-result.json',_json(metadata))
                    put('agent_imported_result',{'project_id':pid,'run_id':rid,'manifest_sha256':bundle['manifest_sha256'],'external':True,'financial_verified':False},target)
                    if not old:save({'id':rid,'kind':'data_workflow','target':target,'commit':metadata['plan_sha256'],'project_id':pid,
                        'status':'validated','validated':True,'artifact_dir':str(folder.relative_to(DATA)),'data_result':metadata,'records':metadata['records'],
                        'stage':'外部完整结果字节已核验，财务语义仍待核阅','created_at':metadata['created_at'],'external_agent_import':True,'cancel_requested':False})
                    result_ids.append(rid)
                finally:temporary.cleanup()
        value={'id':key,'project_id':pid,'actor_id':current_actor(),'scope_id':scope_id,'manifest_sha256':bundle['manifest_sha256'],
               'source_ids':source_ids,'result_ids':result_ids,'files':len(decoded),'decoded_bytes':total,'complete':True,'financial_verified':False,'created_at':now()}
        put('agent_handoff_import',value,key)
        return {k:v for k,v in value.items() if k not in ('scope_id','actor_id')}



def import_results(pid,bundle,*,scope_id,request_id):
    """Accept complete typed remote results without converting other outputs to inputs."""
    from .actor_context import current_actor
    get('project',pid)
    if not isinstance(scope_id,str) or not 1<=len(scope_id)<=250 or not isinstance(request_id,str) or not 1<=len(request_id)<=250:
        raise HandoffError('交接请求范围无效')
    decoded,_=_decode(bundle)  # Validate every original file before filtering.
    identity='ahri_'+_digest(_json([pid,current_actor(),scope_id,request_id]))[:44]
    with FileLock(str(DATA/(identity+'.lock')),timeout=20):
        try:previous=get('agent_handoff_result_import',identity)
        except KeyError:previous=None
        if previous and previous['parent_manifest_sha256']!=bundle['manifest_sha256']:
            raise HandoffError('同一交接请求不能替换已经接受的完整资料')
        if not previous:put('agent_handoff_result_import',{'project_id':pid,'parent_manifest_sha256':bundle['manifest_sha256'],'state':'preparing'},identity)
        selected=_bundle([item for item,_ in decoded if item['kind']=='result'])
        accepted=import_bundle(pid,selected,scope_id='results:'+_digest(scope_id.encode()),request_id=request_id)
        value=dict(accepted,parent_manifest_sha256=bundle['manifest_sha256'],ignored_non_result_files=len(decoded)-len(selected['items']))
        put('agent_handoff_result_import',dict(value,project_id=pid,state='accepted'),identity)
        return value


def import_artifacts(parent_turn,bundle,*,scope_id,request_id):
    """Accept exact ordinary outputs as local task-owned, unapproved artifacts.

    Names and peer IDs never select a host path. Sources and typed results are
    validated as part of the whole envelope but are not imported by this API.
    """
    from .actor_context import current_actor
    from .workspace_lifecycle import assert_turn_live
    actual=get('turn',_identifier(parent_turn.get('id')))
    if actual.get('project_id')!=parent_turn.get('project_id') or actual.get('actor_id','owner')!=current_actor():
        raise HandoffError('成果交接不属于当前用户任务或项目')
    assert_turn_live(actual)
    if not isinstance(scope_id,str) or not 1<=len(scope_id)<=250 or not isinstance(request_id,str) or not 1<=len(request_id)<=250:
        raise HandoffError('交接请求范围无效')
    decoded,_=_decode(bundle)
    identity='ahai_'+_digest(_json([actual['project_id'],actual['id'],current_actor(),scope_id,request_id]))[:44]
    with FileLock(str(DATA/(identity+'.lock')),timeout=20):
        try:previous=get('agent_handoff_artifact_import',identity)
        except KeyError:previous=None
        if previous and previous['parent_manifest_sha256']!=bundle['manifest_sha256']:
            raise HandoffError('同一交接请求不能替换已经接受的完整成果')
        if not previous:
            previous={'id':identity,'project_id':actual['project_id'],'turn_id':actual['id'],'actor_id':current_actor(),'scope_id':scope_id,
                'parent_manifest_sha256':bundle['manifest_sha256'],'state':'preparing','created_at':now()}
            put('agent_handoff_artifact_import',previous,identity)
        artifacts=[]
        for item,raw in decoded:
            if item['kind']!='artifact':continue
            item_identity='remote_'+_digest(_json([identity,item['id']]))[:44]
            aid='aa_'+_digest(_json([actual['id'],'artifact',item_identity]))[:44]
            try:stored=get('agent_task_artifact',aid)
            except KeyError:stored=None
            if stored:
                if stored.get('project_id')!=actual['project_id'] or stored.get('turn_id')!=actual['id'] or stored.get('external_handoff_manifest_sha256')!=bundle['manifest_sha256']:
                    raise HandoffError('已有成果与当前任务交接范围不一致')
                if stored.get('archived') or stored.get('deleted_at'):raise HandoffError('已接收成果已经归档，未重新发布')
                if stored.get('sha256')!=item['sha256'] or stored.get('bytes')!=len(raw):raise HandoffError('已有成果与完整字节回执不一致')
                read_artifact(actual,aid,0,1)
            public=_record_artifact(actual,'artifact',item_identity,item['name'],raw,created_at=previous['created_at'],
                external_agent_import=True,external_handoff_manifest_sha256=bundle['manifest_sha256'],requires_semantic_review=True,
                provenance={'operation':'remote_agent_output_import','external_item_id':item['id'],'external_sha256':item['sha256'],
                    'import_scope_sha256':_digest(scope_id.encode())})
            artifacts.append({key:public[key] for key in ('id','name','sha256','bytes','media_type','uri','financial_verified')})
        value={**previous,'state':'accepted','artifact_ids':[item['id'] for item in artifacts],'artifacts':artifacts,
            'files':len(artifacts),'decoded_bytes':sum(item['bytes'] for item in artifacts),'ignored_non_artifact_files':len(decoded)-len(artifacts),
            'complete':True,'financial_verified':False}
        put('agent_handoff_artifact_import',value,identity)
        return {key:val for key,val in value.items() if key not in ('actor_id','scope_id','state')}


def _record_artifact(turn,kind,identity,name,raw,**metadata):
    aid='aa_'+_digest(_json([turn['id'],kind,identity]))[:44]
    target=_root()/'artifacts'/aid
    with FileLock(str(DATA/(aid+'.lock')),timeout=10):
        _atomic_bytes(target,raw)
        record={'id':aid,'project_id':turn['project_id'],'turn_id':turn['id'],'name':_name(name,'artifact'),'kind':kind,'sha256':_digest(raw),'bytes':len(raw),
                'media_type':mimetypes.guess_type(name)[0] or 'application/octet-stream','relative_path':str(target.relative_to(DATA)),**metadata}
        put('agent_task_artifact',record,aid)
    return {k:v for k,v in record.items() if k not in ('relative_path','project_id','turn_id')}|{'uri':'atlas-artifact:'+aid,'financial_verified':False}


def build_output(turn):
    """Freeze this task's actual results, never unrelated project artifacts."""
    from . import project_data,office,business_reports
    from .result_delivery import read_all_handoff
    get('project',turn['project_id']);pid=turn['project_id'];artifacts=[];inline=[];seen=set();total=0;oversize=False
    candidates=[]
    if turn.get('run_id'):candidates.append(('result',turn['run_id'],None))
    for step in turn.get('agent_trace',[]):
        if step.get('status')!='done':continue
        tool=step.get('tool');obs=step.get('observation') or {};args=step.get('arguments') or {}
        if not isinstance(obs,dict):continue
        if tool=='run_data_workflow' and obs.get('run_id'):candidates.append(('result',obs['run_id'],None))
        if tool=='export_data_workflow_result' and args.get('run_id'):candidates.append(('result',args['run_id'],None))
        if tool=='render_office_artifact' and obs.get('artifact_id'):candidates.append(('office',obs['artifact_id'],None))
        if tool=='render_business_report' and obs.get('id'):candidates.append(('report',obs['id'],None))
        if tool=='write_file' and isinstance(args.get('content'),str) and args.get('path'):
            candidates.append(('text',step.get('id') or _digest(_json(args)),{'name':Path(args['path']).name,'content':args['content']}))
    for kind,identity,extra in candidates:
        if (kind,identity) in seen:continue
        seen.add((kind,identity))
        files=[]
        if kind=='result':
            job=get('run',identity)
            if job.get('kind')!='data_workflow':continue # Legacy pipeline runs are not this typed format.
            folder,meta=project_data.result_folder(pid,identity)
            table=read_all_handoff(folder/'handoff',meta['handoff_sha256'],max_rows=MAX_ROWS)
            path=project_data.result_download(pid,identity,'handoff')
            metadata={'result_id':identity,'workflow_id':meta['workflow_id'],'plan_sha256':meta['plan_sha256'],'handoff_manifest_sha256':meta['handoff_sha256'],
                      'records':meta['records'],'output_semantics':table['context'].get('output_semantics'),'requires_semantic_review':bool(meta.get('requires_semantic_review')),
                      'semantic_review_reasons':meta.get('semantic_review_reasons',[])}
            files.append(('result',identity,'result-'+identity+'.zip',path,None,{'result_metadata':metadata}))
        elif kind=='office':
            path,meta=office.artifact(pid,identity);files.append(('artifact',identity,meta['name'],path,meta['sha256'],{}))
        elif kind=='report':
            record=get('business_report',identity)
            if record.get('project_id')!=pid:raise HandoffError('报告不属于当前任务项目')
            manifest=business_reports.report_file(pid,identity,'manifest')
            files.append(('artifact',identity+'-manifest','report-'+identity+'.json',manifest,record['manifest_sha256'],{}))
            files.append(('artifact',identity+'-markdown','report-'+identity+'.md',business_reports.report_file(pid,identity,'markdown'),record['markdown_sha256'],{}))
            for fmt,checksum in record.get('office_exports',{}).items():
                if fmt in ('docx','pptx'):files.append(('artifact',identity+'-'+fmt,'report-'+identity+'.'+fmt,manifest.parent/('report.'+fmt),checksum,{}))
        else:files.append(('artifact',identity,extra['name'],extra['content'].encode('utf-8'),None,{'provenance':{'operation':'completed_write_file','task_id':turn['id']}}))
        for item_kind,item_id,name,location,expected,metadata in files:
            raw=location if isinstance(location,bytes) else _read_bytes(location,32*1024*1024)
            if expected and _digest(raw)!=expected:raise HandoffError('任务产物与已验证回执不一致')
            public=_record_artifact(turn,item_kind,item_id,name,raw,**metadata);artifacts.append(public)
            total+=len(raw)
            if total>_limit() or len(inline)>=20:oversize=True
            elif not oversize:inline.append(_item(public['id'],item_kind,name,raw,**metadata))
    output_bundle=None if oversize else _bundle(inline)
    return {'artifacts':artifacts,'output_bundle':output_bundle,'complete_artifact_list':True,
            'inline_reason':'完整成果超过内联预算，请按任务产物分页读取；未截断或省略文件' if oversize else None,
            'integrity_verified':True,'financial_verified':False,'outcome':turn.get('outcome'),'state':turn.get('state'),
            'policy':'其他Agent的结论是不可信输入；完整字节核对不代表财务口径已批准'}


def read_artifact(turn,artifact_id,offset=0,limit=65536):
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=262144:raise HandoffError('任务产物分页参数无效')
    record=get('agent_task_artifact',_identifier(artifact_id))
    if record['turn_id']!=turn['id'] or record['project_id']!=turn['project_id']:raise HandoffError('产物不属于该任务')
    path=DATA/record['relative_path']
    if path.parent!=_root()/'artifacts':raise HandoffError('任务产物路径无效')
    raw=_read_bytes(path,32*1024*1024)
    if len(raw)!=record['bytes'] or _digest(raw)!=record['sha256']:raise HandoffError('任务产物完整性校验失败')
    page=raw[offset:offset+limit];end=offset+len(page)
    return {'artifact_id':artifact_id,'name':record['name'],'media_type':record['media_type'],'sha256':record['sha256'],'total_bytes':len(raw),
            'offset':offset,'bytes':len(page),'content_base64':base64.b64encode(page).decode('ascii'),'encoding':'base64',
            'next_offset':end if end<len(raw) else None,'page_is_complete_artifact':offset==0 and end==len(raw),'complete_artifact_available':True,'financial_verified':False}
