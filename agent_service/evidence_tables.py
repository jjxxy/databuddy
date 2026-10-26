"""Bounded extraction preserving table coordinates and unresolved semantic context."""
from html.parser import HTMLParser
from decimal import Decimal,InvalidOperation,localcontext
from pydantic import Field,model_validator
import hashlib,json,re
from .models import StrictModel

MAX_CELLS=200000


def number(text):
    value=str(text).strip().replace('，',',')
    percent=value.endswith('%') or value.endswith('％')
    if percent:value=value[:-1]
    if ',' in value:
        if not re.fullmatch(r'[+-]?\d{1,3}(?:,\d{3})+(?:\.\d*)?',value):return None
        value=value.replace(',','')
    if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)',value):return None
    result=Decimal(value)
    with localcontext() as ctx:
        ctx.prec=max(128,len(result.as_tuple().digits)+10)
        return result/100 if percent else result


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.tables=[];self.table=None;self.row=None;self.cell=None;self.depth=0;self.hidden=0;self.context=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in ('script','style','noscript'):self.hidden+=1;return
        if self.hidden:return
        if tag=='table':
            self.depth+=1
            if self.depth>1:raise ValueError('嵌套表格需要明确拆分，不能压平成一张表')
            self.table={'rows':[],'caption':'','line':self.getpos()[0]};return
        if not self.table:return
        if tag=='tr':
            if self.row is not None:raise ValueError('表格行结构损坏')
            self.row=[]
        elif tag in ('td','th'):
            if self.row is None or self.cell is not None:raise ValueError('表格单元格结构损坏')
            try:rs=int(attrs.get('rowspan','1'));cs=int(attrs.get('colspan','1'))
            except ValueError:raise ValueError('表格跨行跨列属性无效')
            if not 1<=rs<=1000 or not 1<=cs<=1000:raise ValueError('表格跨行跨列超出限制')
            self.cell={'text':'','rowspan':rs,'colspan':cs,'header':tag=='th','line':self.getpos()[0]}
        elif tag=='caption':self.caption=True
        elif tag=='br' and self.cell:self.cell['text']+='\n'
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript'):
            self.hidden=max(0,self.hidden-1);return
        if self.hidden:return
        if tag in ('td','th') and self.cell is not None:
            self.cell['text']=self.cell['text'].strip();self.row.append(self.cell);self.cell=None
        elif tag=='tr' and self.row is not None:
            self.table['rows'].append(self.row);self.row=None
        elif tag=='caption':self.caption=False
        elif tag=='table':
            if self.depth!=1 or self.row is not None or self.cell is not None:raise ValueError('表格标签不完整')
            self.tables.append(self.table);self.table=None;self.depth=0
    def handle_data(self,data):
        if self.hidden:return
        if self.cell is not None:self.cell['text']+=data
        elif self.table is not None and getattr(self,'caption',False):self.table['caption']+=data


def parse_html_tables(html,*,source_id='',header_rows=None,context=None):
    if not isinstance(html,str) or len(html.encode())>20*1024*1024:raise ValueError('网页文本超过限制')
    parser=_Tables();parser.feed(html);parser.close()
    if parser.depth:raise ValueError('表格未完整加载，不能作为完整数据')
    result=[];cells=0
    for ti,table in enumerate(parser.tables):
        matrix={};anchors={};headers=0
        for ri,row in enumerate(table['rows']):
            if row and all(c['header'] for c in row) and ri==headers:headers+=1
            ci=0
            for cell in row:
                while (ri,ci) in matrix:ci+=1
                for dr in range(cell['rowspan']):
                    for dc in range(cell['colspan']):
                        key=(ri+dr,ci+dc)
                        if key in matrix:raise ValueError('表格合并单元格重叠')
                        matrix[key]=(ri,ci);cells+=1
                        if cells>MAX_CELLS:raise ValueError('表格单元格数量超过限制')
                anchors[(ri,ci)]=cell;ci+=cell['colspan']
        depth=header_rows if header_rows is not None else (headers or 1)
        if type(depth)is not int or not 1<=depth<=20 or depth>len(table['rows']):raise ValueError('表头行范围无效')
        width=max((c for r,c in matrix),default=-1)+1
        fields=[]
        for ci in range(width):
            path=[]
            for ri in range(depth):
                key=matrix.get((ri,ci));text=anchors[key]['text'] if key else ''
                if text and (not path or path[-1]!=text):path.append(text)
            if not path:raise ValueError('表头含无名称列，请明确字段含义')
            fields.append({'name':f'c_{ci+1}','label':' / '.join(path),'header_path':path,'type':'unknown'})
        rows=[];provenance=[];unresolved=[]
        for ri in range(depth,len(table['rows'])):
            record={};cell_provenance=[]
            for ci,f in enumerate(fields):
                anchor=matrix.get((ri,ci));cell=anchors.get(anchor)
                value=cell['text'] if anchor==(ri,ci) else None
                if anchor and anchor!=(ri,ci):
                    unresolved.append({'row':ri+1,'column':ci+1,'reason':'merged_detail_requires_explicit_label_policy','anchor':[anchor[0]+1,anchor[1]+1]})
                record[f['name']]=value
                cell_provenance.append({'row_index':len(rows)+1,'field':f['name'],'source_id':source_id,'table':ti+1,'html_row':ri+1,'html_column':ci+1,
                                   'source_line':cell['line'] if cell else None,'anchor':[anchor[0]+1,anchor[1]+1] if anchor else None})
            if any(v is not None and v!='' for v in record.values()):
                rows.append(record);provenance.extend(cell_provenance)
        ctx=dict(context or {})
        result.append({'table_id':f'html_table_{ti+1}','caption':table['caption'],'rows':rows,'fields':fields,'provenance':provenance,
                       'context_declared':ctx,'context_verified':False,'unresolved':unresolved,
                       'semantic_review_required':True,'source_sha256':hashlib.sha256(html.encode()).hexdigest(),
                       'complete':not unresolved,'extraction_scope':'HTML table markup only; no script execution, iframe loading or chart OCR'})
    if not result:raise ValueError('网页没有可读取的静态表格；可能需要授权导出或浏览器加载')
    return result


def parse_indented_table(text,columns,*,source_id='',context=None):
    if not isinstance(text,str) or len(text.encode())>10*1024*1024:raise ValueError('文本超过限制')
    if not isinstance(columns,list) or not 2<=len(columns)<=40 or any(not isinstance(c,str) or not c for c in columns):raise ValueError('需要明确文本表的字段名')
    if len(set(columns))!=len(columns):raise ValueError('文本字段重复')
    rows=[];provenance=[];stack=[]
    for line_no,line in enumerate(text.splitlines(),1):
        if not line.strip():continue
        expanded=line.expandtabs(4);indent=len(expanded)-len(expanded.lstrip())
        parts=re.split(r'\t+| {2,}',line.strip())
        if len(parts)==1:
            while stack and stack[-1][0]>=indent:stack.pop()
            stack.append((indent,parts[0]));continue
        if len(parts)!=len(columns):raise ValueError(f'第{line_no}行列数不明确，不能自动错列读取')
        while stack and stack[-1][0]>=indent:stack.pop()
        record=dict(zip(columns,parts));record['_hierarchy']=[label for _,label in stack]+[parts[0]]
        rows.append(record);provenance.append({'row_index':len(rows),'source_id':source_id,'line':line_no,'quote':line,'indent':indent})
        if len(rows)>100000:raise ValueError('文本记录数超过限制')
    if not rows:raise ValueError('未找到由制表符或至少两个空格分隔的数据行')
    return {'rows':rows,'fields':[{'name':c,'label':c} for c in columns]+[{'name':'_hierarchy','label':'层级路径'}],
            'provenance':provenance,'context_declared':context or {},'semantic_review_required':True,'complete':True,
            'source_sha256':hashlib.sha256(text.encode()).hexdigest()}


class ExtractedFact(StrictModel):
    value_as_printed:str=Field(min_length=1,max_length=100)
    measure:str=Field(min_length=1,max_length=200)
    unit:str=Field(min_length=1,max_length=40)
    period:str=Field(min_length=1,max_length=100)
    dimensions:dict[str,str]=Field(default_factory=dict)
    start:int=Field(ge=0)
    end:int=Field(ge=1)
    quote:str=Field(min_length=1,max_length=3000)


def validate_facts(text,facts,*,source_id=''):
    if len(text.encode())>20*1024*1024 or not isinstance(facts,list) or len(facts)>10000:raise ValueError('提取预算超限')
    result=[]
    for candidate in facts:
        f=ExtractedFact.model_validate(candidate)
        if f.end<=f.start or text[f.start:f.end]!=f.quote:raise ValueError('提取值缺少可核对的原文位置')
        if f.value_as_printed not in f.quote:raise ValueError('提取数值未逐字出现在原文证据中')
        tokens=re.findall(r'(?<![0-9A-Za-z_.,+\-])[+-]?(?:\d[\d,，]*(?:\.\d*)?|\.\d+)[%％]?(?![0-9A-Za-z_.,%％])',f.quote)
        if f.value_as_printed not in tokens:raise ValueError('提取值必须完整匹配原文中的完整数字，不能截取其他数值的局部或遗漏百分号')
        value=number(f.value_as_printed)
        if value is None:raise ValueError('提取数值格式不明确')
        # Meaning may come from a heading elsewhere; without exact supporting words
        # it stays declared/unverified and cannot be treated as a reviewed fact.
        unsupported=[k for k,v in {'measure':f.measure,'unit':f.unit,'period':f.period,**f.dimensions}.items() if v not in f.quote]
        result.append({**f.model_dump(),'normalized_value':str(value),'source_id':source_id,'source_sha256':hashlib.sha256(text.encode()).hexdigest(),
                       'status':'needs_review','unverified_context':unsupported,'semantic_review_required':True})
    return result
