"""Bounded PPTX template text replacement; no inference or original overwrite.

This helper deliberately supports only existing text placeholders. It does not
promise arbitrary PowerPoint editing or visual fidelity for complex objects.
"""
import hashlib
import io
import re
import zipfile
from xml.etree import ElementTree

from pptx import Presentation
from pydantic import Field

from . import project_data
from .models import StrictModel

MAX_BYTES=25*1024*1024
MAX_EXPANDED=96*1024*1024

class TemplateRef(StrictModel):
    source_id:str=Field(min_length=1,max_length=100)
    revision:int=Field(ge=1)
    expected_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')

class Replacement(StrictModel):
    page:int=Field(ge=1,le=120)
    shape_id:int=Field(ge=1)
    text:str=Field(max_length=1000)

class TemplateRender(TemplateRef):
    name:str=Field(min_length=1,max_length=91,pattern=r'^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff _-]{0,90}$')
    replacements:list[Replacement]=Field(min_length=1,max_length=200)


def _template(pid,source_id,revision,expected_sha256):
    source=project_data.owned(pid,'source',source_id)
    if source.get('archived') or source.get('deleted_at') or source.get('kind')!='file' or not source.get('name','').lower().endswith('.pptx'):
        raise ValueError('请选择当前项目中的 PPTX 模板文件')
    if type(revision) is not int or revision<1 or source.get('revision',1)!=revision:
        raise ValueError('模板版本已变化，请重新选择')
    if not isinstance(expected_sha256,str) or not re.fullmatch('[a-f0-9]{64}',expected_sha256) or source.get('content_hash')!=expected_sha256:
        raise ValueError('模板摘要不匹配，请重新选择')
    path=project_data.source_path(source)
    if path.stat().st_size>MAX_BYTES:raise ValueError('模板文件过大')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected_sha256:raise ValueError('模板文件已变化')
    # PPTX is a ZIP: reject excessive expansion, duplicate members and active
    # external relationships before handing untrusted XML to python-pptx.
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            entries=z.infolist();names=[i.filename for i in entries]
            if len(entries)>2048 or len(set(names))!=len(names) or sum(i.file_size for i in entries)>MAX_EXPANDED or any(i.file_size>MAX_BYTES for i in entries):raise ValueError('模板内容超过安全范围')
            if 'ppt/presentation.xml' not in names or any('vbaproject.bin' in n.lower() or '/embeddings/' in n.lower() for n in names):raise ValueError('模板包含暂不支持的活动内容')
            for name in names:
                if name.endswith('.rels'):
                    root=ElementTree.fromstring(z.read(name))
                    if any(x.get('TargetMode','').lower()=='external' for x in root):raise ValueError('模板包含外部连接，请移除后重试')
    except (zipfile.BadZipFile,ElementTree.ParseError):raise ValueError('模板文件格式无效') from None
    return raw


def inspect(pid,source_id,revision,expected_sha256):
    raw=_template(pid,source_id,revision,expected_sha256);deck=Presentation(io.BytesIO(raw))
    if len(deck.slides)>120:raise ValueError('模板页数超过安全范围')
    slides=[];count=0;remaining=20000
    for number,slide in enumerate(deck.slides,1):
        shapes=[]
        for s in slide.shapes:
            if not s.has_text_frame or not s.is_placeholder:continue
            count+=1
            if count>200:raise ValueError('模板占位符超过安全范围，请拆分模板')
            text=s.text;shown=text[:min(2000,remaining)];remaining-=len(shown)
            shapes.append({'shape_id':s.shape_id,'name':s.name[:200],'text':shown,'text_truncated':len(shown)<len(text)})
        slides.append({'page':number,'placeholders':shapes})
    return {'source_id':source_id,'revision':revision,'sha256':expected_sha256,'slides':slides,
            'notice':'仅列出可填充的原有占位符；不自动推断数据或修改模板原件。'}


def render_bytes(pid,source_id,revision,expected_sha256,replacements):
    raw=_template(pid,source_id,revision,expected_sha256)
    if not isinstance(replacements,list) or not 1<=len(replacements)<=200:raise ValueError('请提供明确的模板填充内容')
    deck=Presentation(io.BytesIO(raw));seen=set()
    for entry in replacements:
        if not isinstance(entry,dict) or set(entry)!={'page','shape_id','text'}:raise ValueError('填充项格式无效')
        page,shape_id,text=entry['page'],entry['shape_id'],entry['text']
        if type(page) is not int or not 1<=page<=len(deck.slides) or type(shape_id) is not int or not isinstance(text,str) or len(text)>1000 or any(ord(c)<32 and c not in '\n\t' for c in text):raise ValueError('填充内容或位置无效')
        key=(page,shape_id)
        if key in seen:raise ValueError('同一占位符不能重复填充')
        seen.add(key);matches=[s for s in deck.slides[page-1].shapes if s.shape_id==shape_id and s.has_text_frame and s.is_placeholder]
        if len(matches)!=1:raise ValueError('模板占位符不存在或不允许修改')
        frame=matches[0].text_frame
        # Keep the first run's formatting; avoid flattening untouched shapes.
        paragraphs=frame.paragraphs
        first=paragraphs[0]
        if first.runs:first.runs[0].text=text
        else:first.add_run().text=text
        for run in list(first.runs)[1:]:run.text=''
        for para in paragraphs[1:]:
            for run in para.runs:run.text=''
    out=io.BytesIO();deck.save(out);result=out.getvalue()
    check=Presentation(io.BytesIO(result))
    for entry in replacements:
        shapes=[s for s in check.slides[entry['page']-1].shapes if s.shape_id==entry['shape_id']]
        if len(shapes)!=1 or shapes[0].text_frame.paragraphs[0].runs[0].text!=entry['text']:raise ValueError('生成的模板内容未通过核对')
    # Recheck the source after generation to catch concurrent replacement.
    _template(pid,source_id,revision,expected_sha256)
    return result


def render(turn,source_id,revision,expected_sha256,name,replacements):
    """Create an immutable registered artifact from an authorized template."""
    import json
    from . import office,workspace
    from .intents import require
    from .locks import assert_owned
    from .platform_files import assert_plain_path
    from .result_delivery import durable_cached_artifact
    from .store import get,put,now
    require(turn,'write')
    value=TemplateRender.model_validate({'source_id':source_id,'revision':revision,'expected_sha256':expected_sha256,'name':name,'replacements':replacements})
    entries=[x.model_dump() for x in value.replacements];pid=turn['project_id']
    identity={'project_id':pid,'template_source_id':source_id,'template_revision':revision,'template_sha256':expected_sha256,
              'spec_sha256':hashlib.sha256(json.dumps(entries,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
              'renderer':'ppt-template-v1','name':name+'.pptx'}
    digest=hashlib.sha256(json.dumps(identity,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    aid='of_'+digest[:40];folder=office.DATA/'deliverables'/aid;file=folder/(name+'.pptx')
    identity['relative_path']=str(file.relative_to(office.DATA))
    with workspace.lock(pid):
        _template(pid,source_id,revision,expected_sha256)
        assert_plain_path(folder);folder.mkdir(parents=True,exist_ok=True);assert_plain_path(file)
        try:record=get('office_artifact',aid)
        except KeyError:record={}
        if record and (any(record.get(k)!=v for k,v in identity.items()) or record.get('state') not in ('pending','committed') or not re.fullmatch(r'[a-f0-9]{64}',record.get('sha256') or '')):raise ValueError('成果身份不一致')
        if file.exists() and (not record or hashlib.sha256(file.read_bytes()).hexdigest()!=record.get('sha256')):raise ValueError('成果完整性校验失败')
        size=[None]
        def build(path):
            data=render_bytes(pid,source_id,revision,expected_sha256,entries);path.write_bytes(data);size[0]=len(data)
        def pending(checksum):
            record.update(put('office_artifact',dict(identity,sha256=checksum,bytes=size[0],created_at=record.get('created_at') or now(),state='pending'),aid))
        def committed(checksum):
            if record.get('sha256')!=checksum:raise ValueError('成果回执不一致')
            record.update(put('office_artifact',dict(record,state='committed'),aid))
        durable_cached_artifact(file,expected_sha256=record.get('sha256') if record.get('state')=='committed' else None,
            pending_sha256=record.get('sha256') if record.get('state')=='pending' else None,build=build,save_pending=pending,save_committed=committed,before_publish=assert_owned)
        office._verify_structure(file,'pptx')
    return {'artifact_id':aid,'sha256':record['sha256'],'format':'pptx','template_source_id':source_id,'template_revision':revision,'template_sha256':expected_sha256,
            'visual_validation':False,'notice':'已填充指定占位符；版式和财务内容仍需人工核对。','download':{'label':name+'.pptx','url':'/api/projects/'+pid+'/deliverables/'+aid}}
