"""Evidence-bound numeric facts from text layers, with owner-only review.

Offsets refer to the canonical extracted Unicode text served by read_document,
not PDF byte positions. Graphics and image-only documents require a separate OCR
and visual review workflow and cannot be automatically verified here.
"""
from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from sqlalchemy import select, update

from . import workspace
from .config import DATA
from .evidence_tables import ExtractedFact, validate_facts
from .semantic_alignment import SemanticField, Provenance, canonical_sha256
from .store import get, all_items, put, items, transaction, now, seal, unseal

KIND='fact_proposal'
MAX_FILE_BYTES=25*1024*1024
MAX_TEXT_BYTES=10*1024*1024
MAX_PDF_PAGES=2000


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.parts=[];self.hidden=0;self.stack=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        blocked=bool(self.hidden or tag in ('script','style','noscript','canvas','svg','iframe')
                     or 'hidden' in attrs or re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)',attrs.get('style',''),re.I))
        if tag not in ('area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'):
            self.stack.append((tag,blocked))
            if blocked:self.hidden+=1
        if not blocked and tag in ('p','div','br','tr','td','th','li','h1','h2','h3','section'):self.parts.append('\n')
    def handle_endtag(self,tag):
        match=next((i for i in range(len(self.stack)-1,-1,-1) if self.stack[i][0]==tag),None)
        if match is not None:
            for _,blocked in self.stack[match:]:
                if blocked:self.hidden-=1
            del self.stack[match:]
        if not self.hidden and tag in ('p','div','tr','td','th','li','section'):self.parts.append('\n')
    def handle_data(self,text):
        if not self.hidden:self.parts.append(text)


def _source(pid,sid):
    get('project',pid);source=get('source',sid)
    if source.get('project_id')!=pid or source.get('kind')!='file' or source.get('archived'):
        raise ValueError('文档来源不属于当前项目、已归档或不是本地文件')
    cfg=unseal(source['secret_config']);name=cfg.get('stored_name','')
    path=(DATA/'uploads'/name).resolve()
    if not name or Path(name).name!=name or not path.is_relative_to((DATA/'uploads').resolve()) or not path.is_file():
        raise ValueError('文档原件路径无效或不可用')
    if path.stat().st_size>MAX_FILE_BYTES:raise ValueError('文档超过25MB读取预算')
    return source,path


def _document(pid,sid):
    source,path=_source(pid,sid);raw=path.read_bytes();suffix=path.suffix.lower()
    segments=[];text='';supported=True;reason=None;representation='';extraction={}
    if suffix in ('.txt','.md','.html','.htm'):
        try:text=raw.decode('utf-8-sig');encoding='utf-8-sig'
        except UnicodeDecodeError:
            try:text=raw.decode('gb18030');encoding='gb18030'
            except UnicodeDecodeError:raise ValueError('文本编码不明确，未以替换字符继续读取') from None
        if '\x00' in text:raise ValueError('文件含二进制空字符，不能作为文本读取')
        if suffix in ('.html','.htm'):
            parser=_VisibleText();parser.feed(text);parser.close();text=''.join(parser.parts)
            representation='static_html_text; no_iframe_or_chart_rendering; browser_css_layout_not_executed'
        else:representation='decoded_plain_text:'+encoding
        segments=[{'start':0,'end':len(text),'locator':'document'}]
    elif suffix in ('.pdf','.docx','.pptx','.png','.jpg','.jpeg','.tif','.tiff','.webp'):
        from .document_reader import read_document as extract_document
        extraction=extract_document(path,max_file_bytes=MAX_FILE_BYTES,max_text_bytes=MAX_TEXT_BYTES,max_pages=MAX_PDF_PAGES)
        if extraction['source_file_sha256']!=hashlib.sha256(raw).hexdigest():
            raise ValueError('读取期间文档原件发生变化，请重新获取来源快照')
        text=extraction.pop('text');segments=extraction.pop('segments');supported=extraction.pop('supported')
        reason=extraction.pop('unsupported_reason');representation=extraction.pop('representation')
    else:
        supported=False;reason='unsupported_format_requires_text_extraction_or_ocr_and_visual_review'
    if len(text.encode())>MAX_TEXT_BYTES:raise ValueError('提取文本超过10MB预算')
    if supported and not text.strip():supported=False;reason='no_readable_text_requires_source_export_or_visual_review'
    return {**extraction,'source_id':sid,'source_revision':source.get('revision',1),'source_file_sha256':hashlib.sha256(raw).hexdigest(),
            'source_text_sha256':hashlib.sha256(text.encode()).hexdigest(),'text':text,'segments':segments,
            'supported':supported,'unsupported_reason':reason,'representation':representation,
            'offset_unit':'extracted_unicode_codepoints','graphics_verified':False,'ocr_executed':False,
            'source':source}


def read_document(pid,sid,offset=0,limit=12000):
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=64000:
        raise ValueError('无效文档分页参数')
    doc=_document(pid,sid);text=doc.pop('text');doc.pop('source')
    if offset>len(text):raise ValueError('文档偏移超过完整提取文本长度')
    end=min(offset+limit,len(text));segments=doc.pop('segments')
    visible=[s for s in segments if (s['end']>offset and s['start']<end) or (s['start']==s['end'] and offset<=s['start']<end)]
    if len(visible)>200:
        end=min(end,visible[200]['start']);visible=visible[:200]
    return dict(doc,text=text[offset:end],segments=visible,segment_count=len(segments),segment_scope='current_text_window',
                offset=offset,end_offset=end,total_characters=len(text),next_offset=end if end<len(text) else None,
                status='text_evidence_available' if doc['supported'] else 'unsupported_needs_ocr_or_visual_review',
                scope='text_layer_evidence_only; no_automatic_chart_or_causal_interpretation')


def _period(value):
    value=value.strip()
    match=re.fullmatch(r'(\d{4})年(\d{1,2})月(?:(\d{1,2})日)?',value)
    if match:
        return f'{int(match[1]):04d}-{int(match[2]):02d}'+(f'-{int(match[3]):02d}' if match[3] else '')
    match=re.fullmatch(r'(\d{4})年',value)
    if match:return match[1]
    return value


def _blockers(facts):
    issues=[]
    for index,fact in enumerate(facts):
        current=['context_not_in_quote:'+key for key in fact.get('unverified_context',[])]
        dims=fact['dimensions']
        if not dims:current.append('dimensions_or_scope_missing')
        if len(dims)>20 or any(not k.strip() or not v.strip() or len(k)>100 or len(v)>300 for k,v in dims.items()):
            current.append('invalid_dimension_context')
        if not fact['measure'].strip() or not fact['unit'].strip():current.append('measure_or_unit_missing')
        try:
            SemanticField(id='fact',name=fact['measure'],meaning=fact['measure'],kind='measure',
                          grain=list(dims),unit=fact['unit'],value_format='decimal',
                          temporal_kind='interval',period=_period(fact['period']),aggregation='unknown')
        except ValueError:current.append('period_not_concrete_or_semantic_context_invalid')
        if current:issues.append({'fact_index':index,'missing_or_unsupported':current})
    return issues


def _owned(pid,ident):
    get('project',pid);record=get(KIND,ident)
    if record.get('project_id')!=pid:raise ValueError('提取事实不属于当前项目')
    payload=unseal(record['secret_payload'])
    if canonical_sha256(payload)!=record['content_sha256']:raise ValueError('提取事实完整性校验失败')
    return record,payload


def _public(record,payload):
    return {**{k:v for k,v in record.items() if k!='secret_payload'},'facts':payload['facts'],
            'numeric_context_reviewed':record['status']=='approved','semantic_alignment_required':True,
            'source_file_sha256':payload['source_file_sha256'],'source_text_sha256':payload['source_text_sha256'],
            'source_revision':payload['source_revision'],'representation':payload['representation']}


def propose_facts(pid,sid,facts):
    if not isinstance(facts,list) or not 1<=len(facts)<=1000:raise ValueError('每批提取需要1至1000条事实')
    doc=_document(pid,sid)
    if not doc['supported']:raise ValueError('该文档缺少可验证文字层，需要OCR和视觉核对，不能自动提取为已验证事实')
    parsed=[ExtractedFact.model_validate(f).model_dump() for f in facts]
    validated=validate_facts(doc['text'],parsed,source_id=sid)
    for fact in validated:
        # A substring of a larger printed number is not evidence of this value.
        token=r'(?<![\d.,，%％+\-])'+re.escape(fact['value_as_printed'])+r'(?![\d.,，%％])'
        if not re.search(token,fact['quote']):raise ValueError('提取值必须完整匹配原文数值，不能截取数字或遗漏百分号')
    payload={k:doc[k] for k in ('source_id','source_revision','source_file_sha256','source_text_sha256','representation')}
    payload['facts']=validated
    if len(json.dumps(payload,ensure_ascii=False).encode())>4*1024*1024:raise ValueError('事实提案超过4MB限制，请分批')
    sha=canonical_sha256(payload);ident='fact_'+canonical_sha256([pid,sha])[:40]
    with workspace.lock(pid):
        try:
            existing,original=_owned(pid,ident)
            return _public(existing,original)
        except KeyError:pass
        record={'project_id':pid,'source_id':sid,'schema_version':1,'status':'pending','version':1,
                'content_sha256':sha,'fact_count':len(validated),'secret_payload':seal(payload),
                'created_at':now(),'review':None,'output_source_id':None,'approval_blockers':_blockers(validated),
                'scope':'text_layer_numeric_evidence; owner_review_required; graphics_not_verified'}
        return _public(put(KIND,record,ident),payload)


def get_facts(pid,ident):
    return _public(*_owned(pid,ident))


def list_facts(pid,offset=0,limit=20):
    get('project',pid)
    if type(offset)is not int or offset<0 or type(limit)is not int or not 1<=limit<=40:raise ValueError('无效事实分页参数')
    rows=[r for r in all_items(KIND) if r.get('project_id')==pid]
    rows.sort(key=lambda r:(r['created_at'],r['id']),reverse=True)
    return {'items':[{k:v for k,v in r.items() if k not in ('secret_payload','review')} for r in rows[offset:offset+limit]],
            'total':len(rows),'next_offset':offset+limit if offset+limit<len(rows) else None}


def _source_from_facts(pid,record,payload):
    source_id='facts_'+canonical_sha256([pid,record['id']])[:40]
    dimension_names=sorted({key for fact in payload['facts'] for key in fact['dimensions']})
    rows=[]
    for index,fact in enumerate(payload['facts']):
        row={'fact_id':canonical_sha256([record['id'],index]),'measure':fact['measure'],
             'value':fact['normalized_value'],'value_as_printed':fact['value_as_printed'],'unit':fact['unit'],
             'period':_period(fact['period']),'period_as_printed':fact['period'],'dimensions':fact['dimensions'],
             'source_id':payload['source_id'],'source_locator':f'text:{fact["start"]}:{fact["end"]}',
             'source_text_sha256':payload['source_text_sha256'],'source_file_sha256':payload['source_file_sha256'],
             'fact_review_id':record['id']}
        row.update({'dim:'+name:fact['dimensions'].get(name) for name in dimension_names});rows.append(row)
    raw=json.dumps(rows,ensure_ascii=False,indent=2).encode();name=source_id+'.json'
    uploads=DATA/'uploads';uploads.mkdir(parents=True,exist_ok=True);path=uploads/name
    if path.exists():
        if path.read_bytes()!=raw:raise ValueError('已存在的事实数据文件与当前提案不一致')
    else:
        # The filename binds this immutable proposal. An orphan file after a
        # crash is safe: it becomes accessible only with the DB approval below.
        with path.open('xb') as output:
            path.chmod(0o600);output.write(raw);output.flush()
    source={'id':source_id,'project_id':pid,'name':'已核对数值事实-'+record['id'][-12:]+'.json',
            'kind':'file','secret_config':seal({'stored_name':name}),'created_at':now(),
            'status':'ready','content_hash':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),
            'fact_review_id':record['id'],'fact_sha256':record['content_sha256'],
            'origin_source_id':payload['source_id'],'provenance_scope':'verified_text_offsets_and_owner_context_review',
            'revision':1,'revision_id':'svf_'+canonical_sha256([source_id,hashlib.sha256(raw).hexdigest()])[:40]}
    # Deterministic profile only; never invoke a model while approving evidence.
    from .planner import profile_node,suggest_node
    state={'source':source};state.update(profile_node(state));state.update(suggest_node(state))
    source.update({k:state[k] for k in ('profile','suggestions','proposed_spec')})
    source.update(planner_mode='reviewed_facts_rules')
    source['profile']['evidence'].update(fact_review_id=record['id'],fact_sha256=record['content_sha256'],
                                       semantic_alignment_required=True,do_not_sum_heterogeneous_measures=True)
    return source


def review_facts(pid,ident,expected_sha256,actor,decision='approved'):
    if not isinstance(actor,str) or not actor.strip() or len(actor)>2000:raise ValueError('需要已鉴权的核对人')
    if decision not in ('approved','rejected'):raise ValueError('核对决定必须是 approved 或 rejected')
    with workspace.lock(pid):
        record,payload=_owned(pid,ident)
        if record['content_sha256']!=expected_sha256:raise ValueError('事实提案版本已变化，请重新核对')
        if record['status']!='pending':
            if record['status']==decision:return _public(record,payload)
            raise ValueError('事实已有不可变核对记录；修改后请创建新提案')
        source=None
        if decision=='approved':
            doc=_document(pid,record['source_id'])
            if not doc['supported'] or any(doc[k]!=payload[k] for k in ('source_revision','source_file_sha256','source_text_sha256')):
                raise ValueError('来源或文字层版本已变化，请重新提取和核对')
            verified=validate_facts(doc['text'],[{k:v for k,v in fact.items() if k in ExtractedFact.model_fields} for fact in payload['facts']],source_id=record['source_id'])
            missing=_blockers(verified)
            if missing:raise ValueError('事实缺少原文支持的口径、单位、期间或维度：'+json.dumps(missing,ensure_ascii=False))
            source=_source_from_facts(pid,record,payload)
        with transaction() as connection:
            row=connection.execute(select(items.c.body).where(items.c.id==ident,items.c.kind==KIND).with_for_update()).first()
            current=json.loads(row[0])
            if current['content_sha256']!=expected_sha256 or current['status']!='pending':raise ValueError('事实核对状态已变化，请重新读取')
            if source:
                original=connection.execute(select(items.c.body).where(items.c.id==record['source_id'],items.c.kind=='source').with_for_update()).first()
                original=json.loads(original[0]) if original else {}
                if original.get('project_id')!=pid or original.get('archived') or original.get('revision',1)!=payload['source_revision']:
                    raise ValueError('来源版本已变化，请重新核对')
                # The approved source and its consent become visible atomically.
                connection.execute(items.insert().values(id=source['id'],kind='source',body=json.dumps(source,ensure_ascii=False)))
                version=dict(source,id=source['revision_id'],source_id=source['id'],recorded_at=now())
                connection.execute(items.insert().values(id=source['revision_id'],kind='source_version',body=json.dumps(version,ensure_ascii=False)))
            record.update(status=decision,review={'decision':decision,'actor_hash':hashlib.sha256(actor.encode()).hexdigest(),
                          'reviewed_at':now(),'content_sha256':expected_sha256},
                          output_source_id=source['id'] if source else None)
            connection.execute(update(items).where(items.c.id==ident,items.c.kind==KIND).values(body=json.dumps(record,ensure_ascii=False)))
        return _public(record,payload)


def align_facts(pid,ident,catalog,aggregation_by_measure=None):
    record,payload=_owned(pid,ident)
    if record['status']!='approved':raise ValueError('先通过原文与数值人工核对，再提出语义匹配建议')
    from .semantic_alignment import ExtractedFact as SemanticFact,align_fact,suggest_alignment
    rules=aggregation_by_measure or {};result=[]
    for index,fact in enumerate(payload['facts']):
        field=SemanticField(id='fact_'+canonical_sha256([ident,index])[:40],name=fact['measure'],meaning=fact['measure'],
            kind='measure',grain=sorted(fact['dimensions']),unit=fact['unit'],
            value_format='percent_fraction' if fact['value_as_printed'].endswith(('%','％')) else 'decimal',
            temporal_kind='interval',period=_period(fact['period']),aggregation=rules.get(fact['measure'],'unknown'))
        provenance=Provenance(source_id=record['source_id'],locator=f'text:{fact["start"]}:{fact["end"]}',
                              content_sha256=payload['source_text_sha256'],quote=fact['quote'])
        extracted=SemanticFact(field=field,value=fact['normalized_value'],provenance=[provenance],extraction_status='reviewed')
        if field.aggregation=='unknown':
            proposal=suggest_alignment(field,catalog)
            result.append({'fact_index':index,'status':'needs_semantic_metadata','missing':['aggregation_behavior'],
                           'fact':extracted.model_dump(mode='json'),'proposal':proposal.model_dump(mode='json'),
                           'proposal_sha256':canonical_sha256(proposal),'ready_for_automatic_consumption':False})
        else:
            result.append(dict(align_fact(extracted,catalog),fact_index=index,status='semantic_review_required'))
    return {'fact_proposal_id':ident,'source_id':record['output_source_id'],'items':result,
            'scope':'reviewed_numeric_evidence; semantic_matching_is_a_separate_review; no_automatic_sum_assumption'}
