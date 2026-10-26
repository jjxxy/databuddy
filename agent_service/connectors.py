import csv
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
import httpx
import sqlalchemy as sa
from .config import DATA, MAX_ROWS, MAX_UPLOAD
from .store import unseal

class SourceError(ValueError):
    def __init__(self,message,code='SOURCE_INVALID',retryable=False):
        super().__init__(message);self.code=code;self.retryable=retryable

def exact_json(content):
    """Preserve source decimal tokens before any binary-float conversion."""
    def invalid_constant(_):raise SourceError('JSON数值不能包含NaN或Infinity','INVALID_JSON_NUMBER')
    def unique_object(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise SourceError('JSON对象存在重复字段，不能静默覆盖','DUPLICATE_JSON_FIELD')
            value[key]=item
        return value
    return json.loads(content,parse_float=Decimal,parse_constant=invalid_constant,object_pairs_hook=unique_object)

def scalar(v):
    if isinstance(v, (date, datetime)): return v.isoformat()
    if isinstance(v, Decimal): return str(v)
    if isinstance(v, bytes): return v.hex()
    if isinstance(v, (dict, list)): return json.dumps(v, ensure_ascii=False, default=str)
    return v

def normalize(rows):
    # Stable generated ASCII names are compatible with dlt/dbt. Original headers survive in metadata.
    keys = []
    for row in rows:
        for key in row:
            if str(key) not in keys: keys.append(str(key))
    mapping = {key: f'c_{i+1}' for i, key in enumerate(keys)}
    records = []
    for row in rows:
        value = {mapping[str(k)]: scalar(v) for k, v in row.items()}
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        value['record_hash'] = hashlib.sha256(payload.encode()).hexdigest()
        value['_source_index'] = len(records) + 1
        records.append(value)
    return records, [{'name': v, 'label': k} for k,v in mapping.items()]

def validate_config(kind, cfg):
    if kind == 'api':
        url = cfg.get('url','')
        parts = urlsplit(url)
        if parts.scheme not in ('https','http') or not parts.hostname or parts.username or parts.password:
            raise SourceError('API 必须使用 HTTP(S)，凭证请放请求头')
        if not isinstance(cfg.get('headers', {}), dict): raise SourceError('请求头必须是 JSON 对象')
        if int(cfg.get('max_pages',10)) not in range(1,1001):raise SourceError('max_pages 必须在1～1000')
        mode=cfg.get('pagination') or ('page' if cfg.get('page_param') else 'none')
        if mode not in ('none','page','offset','cursor'):raise SourceError('分页模式无效')
        if mode=='cursor' and (not cfg.get('cursor_param') or not cfg.get('next_cursor_path')):raise SourceError('游标分页需要游标参数和下一游标路径')
        if mode=='offset' and not cfg.get('offset_param'):raise SourceError('偏移分页需要参数名称')
        if mode=='page' and not cfg.get('page_param'):raise SourceError('页码分页需要参数名称')
        if not isinstance(cfg.get('params',{}),dict):raise SourceError('查询参数必须是对象')
        if int(cfg.get('page_size',100)) not in range(1,10001):raise SourceError('页大小超出范围')
    elif kind == 'database':
        url = sa.engine.make_url(cfg.get('connection_string', ''))
        if url.get_backend_name() not in ('postgresql', 'mysql'):
            raise SourceError('首版数据库连接只支持 PostgreSQL/MySQL')
        if not cfg.get('table'): raise SourceError('需要指定表名')
        if not cfg.get('read_only_ack'): raise SourceError('请确认使用只读数据库账号')
    else:
        raise SourceError('不支持的连接类型')

def safe_api_url(url):
    parts=urlsplit(url)
    if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password:
        raise SourceError('无效 API 地址')
    blocked={'169.254.169.254','metadata.google.internal','metadata.azure.internal'}
    if parts.hostname in blocked:raise SourceError('禁止访问云元数据地址','NETWORK_BLOCKED')
    allowed={x.strip() for x in os.getenv('API_ALLOWED_HOSTS','').split(',') if x.strip()}
    try:addresses=socket.getaddrinfo(parts.hostname,parts.port or (443 if parts.scheme=='https' else 80),type=socket.SOCK_STREAM)
    except OSError as e:raise SourceError('API 域名无法解析','NETWORK_FAILED',True) from e
    ips=list(dict.fromkeys(x[4][0] for x in addresses))
    from .network_policy import check_host
    check_host(parts.hostname,parts.port,addresses=ips)
    if not ips or any(ipaddress.ip_address(ip).is_link_local for ip in ips):raise SourceError('网络地址被拒绝','NETWORK_BLOCKED')
    if parts.hostname not in allowed and any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise SourceError('私网 API 需要在 API_ALLOWED_HOSTS 明确允许','NETWORK_BLOCKED')
    # Pin the validated IP. Host and TLS SNI retain the original hostname; a second DNS lookup cannot rebind it.
    return ips[0]

def verified_file_source(source, *, require_hash=True):
    """Return bounded bytes bound to a frozen source, or an explicit legacy probe.

    All file adapters and the workflow path gate share this integrity boundary.
    Missing hashes are never established from the very file being verified.
    """
    cfg=unseal(source['secret_config']);name=cfg.get('stored_name','')
    if not isinstance(name,str) or not name or Path(name).name!=name or '\\' in name:
        raise SourceError('来源文件路径无效','SOURCE_INVALID')
    path=(DATA/'uploads'/name).resolve()
    if not path.is_relative_to((DATA/'uploads').resolve()) or not path.is_file():
        raise SourceError('来源文件不可用','SOURCE_INVALID')
    expected=source.get('content_hash')
    if expected is not None and (not isinstance(expected,str) or not re.fullmatch(r'[a-f0-9]{64}',expected)):
        raise SourceError('来源版本内容摘要无效，请重新上传并建立可核验版本','SOURCE_HASH_MISSING')
    if expected is None and require_hash:
        raise SourceError('旧来源缺少内容摘要，请重新上传并建立可核验版本后执行','SOURCE_HASH_MISSING')
    with path.open('rb') as stream:content=stream.read(MAX_UPLOAD+1)
    if len(content)>MAX_UPLOAD:raise SourceError('来源文件超过文件体积限制','SOURCE_TOO_LARGE')
    actual=hashlib.sha256(content).hexdigest()
    if expected is not None and actual!=expected:
        raise SourceError('来源文件内容与冻结版本不一致，未读取或执行替换后的数据','SOURCE_CONTENT_CHANGED')
    return path,content,{'content_sha256':actual,
        'source_integrity':'verified' if expected else 'unverified_legacy_source'}


def file_rows(source, limit):
    path,content,integrity=verified_file_source(source,require_hash=limit>=MAX_ROWS)
    rows,evidence=_file_rows(source,limit,path,content)
    # Path-based Office/document readers may open a file more than once. Refuse
    # the entire read if its bytes changed during parsing, including legacy probes.
    after,_,final=verified_file_source(source,require_hash=limit>=MAX_ROWS)
    if after!=path or final['content_sha256']!=integrity['content_sha256']:
        raise SourceError('来源文件在读取期间发生变化，未采用本次数据','SOURCE_CONTENT_CHANGED')
    evidence.update(integrity)
    if integrity['source_integrity']=='unverified_legacy_source':
        evidence['warnings']=[*evidence.get('warnings',[]),{'code':'SOURCE_HASH_MISSING',
            'note':'旧来源仅可探查；正式执行前须重新上传并建立可核验版本'}]
    return rows,evidence


def _file_rows(source, limit, path, content):
    cfg = unseal(source['secret_config'])
    suffix = path.suffix.lower()
    if suffix in ('.xlsx','.docx','.pptx'):
        import zipfile
        try:
            with zipfile.ZipFile(path) as archive:
                if sum(x.file_size for x in archive.infolist())>200*1024*1024:raise SourceError('Office 解压体积超过限制')
        except zipfile.BadZipFile as e:raise SourceError('Office 文件格式损坏') from e
    if suffix == '.csv':
        # Decode and parse the same verified bytes. Reopening a path after a
        # checksum check could otherwise read a different on-disk revision.
        try:
            text=content.decode('utf-8-sig')
        except UnicodeDecodeError:
            text=content.decode('gb18030')
        reader=csv.DictReader(io.StringIO(text,newline=''))
        if not reader.fieldnames or len(reader.fieldnames)!=len(set(reader.fieldnames)):
            raise SourceError('CSV 表头为空或有重复列名')
        rows=[]
        for row in reader:
            if None in row:raise SourceError('CSV 列数不一致')
            rows.append(row)
            if len(rows)>limit:break
        return rows,{'parser':'csv','file':source['name'],'schema_labels':reader.fieldnames}
    if suffix == '.xlsx':
        from .workbook_reader import workbook_sheet_names,read_table,WorkbookReadError
        options=cfg.get('xlsx_options',{})
        if not isinstance(options,dict):raise SourceError('xlsx_options 必须为对象')
        allowed={'sheet','table','header_rows','cell_range','data_start_row','data_end_row','merged_label_columns','hidden_rows','subtotal_policy','html_table'}
        if set(options)-allowed:raise SourceError('Excel 读取选项包含未知或不受允许的参数')
        options={key:value for key,value in options.items() if value is not None and key!='html_table'}
        try:
            names=workbook_sheet_names(path)
            selected=options.pop('sheet',None) or cfg.get('sheet')
            implicit=not selected and not options.get('table')
            # Existing uploads retain first-sheet behavior, explicitly and visibly.
            # New read_table/workflow API requires an actual selection for multiple sheets.
            if implicit:selected=names[0]
            extracted=read_table(path,sheet=selected,limit=limit+1,
                                 require_formula_cache=limit>=MAX_ROWS,**options)
        except WorkbookReadError as exc:
            raise SourceError(str(exc),exc.code) from exc
        warnings=list(extracted['warnings'])
        if implicit and len(names)>1:
            warnings.append({'code':'LEGACY_FIRST_SHEET_SELECTED','sheet':selected,
                             'note':'本次兼容旧上传流程选择第一页；其他页签未拼接，请明确配置工作表'})
        evidence={key:value for key,value in extracted.items() if key not in ('rows','fields')}
        evidence.update(sheet=extracted['selection']['sheet'],sheets=names,file=source['name'],
                        schema_labels=[field['label'] for field in extracted['fields']],warnings=warnings,
                        complete=not extracted['truncated'] and len(extracted['rows'])<=limit and extracted['executable_values_complete'],
                        read_mode='execution' if limit>=MAX_ROWS else 'probe',
                        _normalized_fields=extracted['fields'])
        evidence['provenance']=dict(evidence['provenance'],complete=evidence['complete'])
        return extracted['rows'],evidence
    if suffix in ('.json', '.jsonl'):
        if suffix == '.jsonl':
            with io.StringIO(content.decode('utf-8')) as f:
                rows = []
                for line in f:
                    if line.strip(): rows.append(exact_json(line))
                    if len(rows) > limit: break
        else:
            rows = exact_json(content.decode('utf-8'))
            if cfg.get('records_path'):rows=json_path(rows,cfg['records_path'])
        if not isinstance(rows,list) or any(not isinstance(x,dict) for x in rows): raise SourceError('JSON 需要对象数组')
        return rows[:limit+1], {'parser':'json','file':source['name']}
    if suffix in ('.html','.htm'):
        from .evidence_tables import parse_html_tables
        tables=parse_html_tables(content.decode('utf-8'),source_id=source['id'])
        if len(tables)!=1:raise SourceError('网页包含多张表，请使用数据工作台明确选择表格')
        t=tables[0]
        if not t['complete']:raise SourceError('网页明细合并区域含义不明确，请使用授权导出或明确布局')
        labels=[f['label'] for f in t['fields']]
        rows=[{f['label']:r[f['name']] for f in t['fields']} for r in t['rows'][:limit+1]]
        return rows,dict(parser='atlas_html',schema_labels=labels,source_sha256=t['source_sha256'],semantic_review_required=True)
    if suffix in ('.txt','.md'):
        lines=content.decode('utf-8').splitlines()
        return [{'内容':line,'行号':i+1} for i,line in enumerate(lines) if line.strip()][:limit+1], {'parser':'text','note':'文字及行号；数值提取与语义需保留证据并核对'}
    if suffix in ('.pdf','.docx','.pptx','.png','.jpg','.jpeg','.tif','.tiff','.webp'):
        if os.getenv('USE_DOCLING', '0') != '1':
            from .document_reader import read_document,DocumentReadError
            try:document=read_document(path)
            except DocumentReadError as exc:raise SourceError(str(exc),exc.code) from exc
            if not document['supported']:
                raise SourceError('OCR_REQUIRED：文档缺少可读取文字层，需要 OCR、视觉核对或授权导出原始数据',document['error_code'])
            rows=[]
            for block in document['segments']:
                text=document['text'][block['start']:block['end']]
                if not text.strip():continue
                row={'文档':source['name'],'页码':block.get('page_index'),'内容':text,
                     '证据':json.dumps(block,ensure_ascii=False,sort_keys=True)}
                if suffix!='.pdf':row.update(幻灯片=block.get('slide_index'),块类型=block['kind'])
                rows.append(row)
                if len(rows)>limit:break
            evidence={key:value for key,value in document.items() if key not in ('text','segments')}
            evidence.update(block_count=len(document['segments']),probe_truncated=len(rows)>limit,
                            note='文字层及原始段落、单元格、页或幻灯片坐标；未执行 OCR、图形解读、合并值填充或语义实体核验')
            return rows,evidence
        try:
            from docling.document_converter import DocumentConverter
        except ImportError as exc:
            raise SourceError('该格式需要多模态镜像：docker compose -f compose.yaml -f compose.multimodal.yaml up -d --build') from exc
        result = DocumentConverter().convert(path)
        rows=[]
        for item,_ in result.document.iterate_items():
            text=getattr(item,'text','')
            if text:
                prov=getattr(item,'prov',[])
                rows.append({'文档':source['name'],'页码':prov[0].page_no if prov else None,'内容':text,'证据':json.dumps([x.model_dump(mode='json') for x in prov], ensure_ascii=False)})
            if len(rows)>limit: break
        return rows, {'parser':'docling','note':'文档块和证据位置；表格专用抽取及业务实体识别后续扩展'}
    raise SourceError('不支持的文件格式')

def json_path(value,path):
    for key in path.split('.'):
        if not key:continue
        if isinstance(value,dict):value=value.get(key)
        elif isinstance(value,list) and key.isdigit() and int(key)<len(value):value=value[int(key)]
        else:return None
    return value

def api_rows(source,limit):
    cfg=unseal(source['secret_config']);validate_config('api',cfg)
    mode=cfg.get('pagination') or ('page' if cfg.get('page_param') else 'none')
    rows=[];cursor=cfg.get('initial_cursor');seen_cursors=set();seen_pages=set();expected_total=None;complete=False
    with httpx.Client(timeout=30,follow_redirects=False,trust_env=False) as client:
        for page in range(1,int(cfg.get('max_pages',10))+1):
            ip=safe_api_url(cfg['url']);original=httpx.URL(cfg['url']);url=original.copy_with(host=ip)
            headers={k:v for k,v in cfg.get('headers',{}).items() if k.lower()!='host'};headers['Host']=original.netloc.decode()
            params=dict(cfg.get('params',{}));size=int(cfg.get('page_size',100))
            if mode=='page':params[cfg['page_param']]=int(cfg.get('start_page',1))+page-1
            if mode=='offset':params[cfg['offset_param']]=int(cfg.get('initial_offset',0))+len(rows)
            if mode=='cursor' and cursor is not None:params[cfg['cursor_param']]=cursor
            if cfg.get('page_size_param'):params[cfg['page_size_param']]=size
            with client.stream('GET',url,headers=headers,params=params,extensions={'sni_hostname':original.host}) as response:
                if response.status_code>=300:raise SourceError(f'API 请求失败：HTTP {response.status_code}','API_HTTP_FAILED',response.status_code==429 or response.status_code>=500)
                content=bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content)>MAX_UPLOAD:raise SourceError('单页响应超过体积限制')
                try:document=exact_json(content)
                except ValueError as e:raise SourceError('API 返回的内容不是 JSON') from e
            payload=json_path(document,cfg.get('records_path',''))
            if not isinstance(payload,list) or any(not isinstance(x,dict) for x in payload):raise SourceError('记录路径必须指向对象数组')
            if cfg.get('total_path'):
                total=json_path(document,cfg['total_path'])
                if not isinstance(total,int) or isinstance(total,bool) or total<0:raise SourceError('总数路径不是有效整数','TOTAL_INVALID')
                if expected_total is not None and total!=expected_total:raise SourceError('分页过程中总数变化，请使用来源快照或缩小范围','TOTAL_CHANGED',True)
                expected_total=total
            if payload:
                fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
                if mode!='none' and fingerprint in seen_pages:raise SourceError('分页返回重复页面，停止采集以避免循环或重复计数','PAGINATION_LOOP')
                seen_pages.add(fingerprint)
            rows.extend(payload)
            evidence={'parser':'httpx','pages':page,'pagination':mode,'expected_total':expected_total,'strategy':'full_refresh'}
            if len(rows)>limit:return rows[:limit+1],dict(evidence,probe_truncated=True)
            if expected_total is not None and len(rows)>expected_total:raise SourceError('采集行数超过来源总数','TOTAL_MISMATCH')
            if mode=='cursor':
                token=json_path(document,cfg['next_cursor_path'])
                if token is None or token=='':complete=True;break
                if not isinstance(token,(str,int)) or len(str(token))>2048:raise SourceError('下一游标无效','CURSOR_INVALID')
                if str(token) in seen_cursors:raise SourceError('来源游标循环，已停止采集','PAGINATION_LOOP')
                seen_cursors.add(str(token));cursor=token
            if mode!='cursor' and (not payload or mode=='none' or (expected_total is not None and len(rows)==expected_total)):complete=True;break
        if limit==MAX_ROWS and not complete:raise SourceError('达到分页上限但未确认完整采集','PAGINATION_INCOMPLETE')
        if limit==MAX_ROWS and expected_total is not None and len(rows)!=expected_total:raise SourceError('采集数量与来源总数不一致','TOTAL_MISMATCH')
    return rows,dict(evidence,complete=complete)

def database_rows(source,limit):
    cfg=unseal(source['secret_config'])
    validate_config('database',cfg)
    from .network_policy import check_host
    target=sa.engine.make_url(cfg['connection_string'])
    check_host(target.host,target.port)
    eng=sa.create_engine(cfg['connection_string'],pool_pre_ping=True,connect_args={'connect_timeout':10})
    try:
        with eng.connect() as conn:
            if eng.dialect.name=='postgresql':
                conn.exec_driver_sql('SET TRANSACTION READ ONLY')
                conn.exec_driver_sql("SET LOCAL statement_timeout = '30000'")
            else:
                conn.exec_driver_sql('SET SESSION TRANSACTION READ ONLY')
            table=sa.Table(cfg['table'],sa.MetaData(),schema=cfg.get('schema') or None,autoload_with=conn)
            rows=[dict(x) for x in conn.execute(sa.select(table).limit(limit+1)).mappings()]
        return rows, {'parser':'sqlalchemy','table':cfg['table'],'schema_labels':[column.name for column in table.columns],'strategy':'full_refresh'}
    finally:
        eng.dispose()

def _read_source(source,limit=MAX_ROWS,allow_empty=False,expected_labels=None):
    if source['kind']=='file': rows,evidence=file_rows(source,limit)
    elif source['kind']=='api': rows,evidence=api_rows(source,limit)
    else: rows,evidence=database_rows(source,limit)
    normalized_fields=evidence.pop('_normalized_fields',None)
    if not rows:
        if not allow_empty:raise SourceError('数据源没有可用记录','SOURCE_EMPTY')
        labels=evidence.get('schema_labels') or expected_labels
        if not labels:raise SourceError('空来源没有可确认的字段结构','SOURCE_EMPTY')
        return [],normalized_fields or [{'name':f'c_{i+1}','label':label} for i,label in enumerate(labels)],dict(evidence,empty=True,schema_from_previous=not bool(evidence.get('schema_labels'))),False
    if len(rows)>limit and limit==MAX_ROWS: raise SourceError(f'超过单次 {MAX_ROWS} 行限制，请缩小数据范围')
    if normalized_fields is not None:
        # The enhanced XLSX reader already binds c_N to exact Excel coordinates.
        # Re-normalizing would rename fields and discard its header/format semantics.
        fields=normalized_fields;records=[]
        for row in rows[:limit]:
            value={key:scalar(item) for key,item in row.items()}
            value['record_hash']=hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
            value['_source_index']=len(records)+1;records.append(value)
        if len(rows)>limit:
            evidence['source_rows']=evidence['source_rows'][:limit]
            visible={str(row) for row in evidence['source_rows']}
            evidence['special_cells']={row:cells for row,cells in evidence['special_cells'].items() if row in visible}
            evidence['provenance']=dict(evidence['provenance'],source_rows=evidence['source_rows'],special_cells=evidence['special_cells'])
    else:records, fields=normalize(rows[:limit])
    return records, fields, evidence, len(rows)>limit

def read_source(source,limit=MAX_ROWS,allow_empty=False,expected_labels=None):
    try:return _read_source(source,limit,allow_empty,expected_labels)
    except (httpx.TransportError,sa.exc.OperationalError) as e:
        raise SourceError('连接失败或超时，请检查服务状态、凭证及网络','CONNECTION_FAILED',True) from e
