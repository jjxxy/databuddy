"""Deterministic editable Office deliverables; templates, never generated code."""
import hashlib,json,re,zipfile
from decimal import Decimal,InvalidOperation
from typing import Literal
from pydantic import Field,ValidationError
from .models import StrictModel
from .config import DATA
from .store import get,put,now
from . import workspace

RENDERER='office-v2'

class OfficeArgs(StrictModel):
    spec_path:str=Field(min_length=1,max_length=200,
        description='项目Office规格JSON路径。Excel采用text_exact：超安全精度或范围的有限数值无损存文本，precision_notes逐格披露；非有限数字拒绝。')
    name:str=Field(min_length=1,max_length=91,
        pattern=r'^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff _-]{0,90}$',
        description='不含路径和扩展名的文件基名，例如净收入明细；扩展名由规格kind决定，不能传report.xlsx')
class Section(StrictModel):
    heading:str=Field(default='',max_length=300)
    body:str=Field(default='',max_length=20000)
    bullets:list[str]=Field(default_factory=list,max_length=30)
class Sheet(StrictModel):
    name:str=Field(min_length=1,max_length=31)
    columns:list[str]=Field(min_length=1,max_length=60)
    rows:list[list[str|int|Decimal|bool|None]]=Field(default_factory=list,max_length=5000)
class Spec(StrictModel):
    kind:Literal['document','spreadsheet','presentation']
    title:str=Field(min_length=1,max_length=300)
    sections:list[Section]=Field(default_factory=list,max_length=30)
    sheets:list[Sheet]=Field(default_factory=list,max_length=10)

def _load_spec(text):
    def nonfinite(value):raise ValueError('Office数值必须有限')
    try:payload=json.loads(text,parse_float=Decimal,parse_constant=nonfinite)
    except (ValueError,InvalidOperation):
        raise ValueError('Office规格JSON或数值范围无效；数值必须有限，精确金额也可使用文本') from None
    try:return Spec.model_validate(payload)
    except ValidationError as exc:
        # Report locations/types, never raw input values or the whole project
        # file. The planner already has the canonical office_spec_schema.
        issues=[{'path':'.'.join(map(str,error['loc'])),'type':error['type']}
                for error in exc.errors(include_input=False,include_context=False)[:8]]
        raise ValueError('Office规格不符合office_spec_schema，请修正原规格文件：'
                         +json.dumps(issues,ensure_ascii=False)) from None

def _verify_xlsx_values(file,spec):
    from openpyxl import load_workbook
    book=load_workbook(file,read_only=True,data_only=False)
    try:
        if book.sheetnames!=[sheet.name for sheet in spec.sheets]:raise ValueError('交付工作表与原规格不一致')
        for source,sheet in zip(spec.sheets,book):
            expected=[source.columns,*source.rows]
            rows=sheet.iter_rows(min_row=1,max_row=len(expected),max_col=len(source.columns))
            for original,cells in zip(expected,rows):
                for value,cell in zip(original,cells):
                    actual=cell.value
                    if cell.data_type=='f':raise ValueError('文本内容被写成了可执行公式')
                    if isinstance(value,(int,Decimal)) and not isinstance(value,bool):
                        if actual is None or isinstance(actual,bool) or Decimal(str(actual))!=Decimal(value):
                            raise ValueError('Excel序列化改变了原始数值，产物未发布')
                    elif value=='' and actual is None:pass  # Excel's empty text cell.
                    elif type(value) is not type(actual) or value!=actual:
                        raise ValueError('Excel序列化改变了原始单元格，产物未发布')
    finally:book.close()


def _verify_structure(file,ext,spec=None):
    from .platform_files import assert_plain_path
    assert_plain_path(file)
    try:
        with zipfile.ZipFile(file) as archive:
            entries=archive.infolist()
            if len(entries)>10000 or sum(entry.file_size for entry in entries)>200*1024*1024:
                raise ValueError('Office产物超过结构验证范围')
            required={'docx':'word/document.xml','pptx':'ppt/presentation.xml','xlsx':'xl/workbook.xml'}[ext]
            if archive.testzip() is not None or '[Content_Types].xml' not in archive.namelist() or required not in archive.namelist():
                raise ValueError('Office产物结构不完整')
        if ext=='docx':
            from docx import Document
            Document(file)
        elif ext=='pptx':
            from pptx import Presentation
            Presentation(file)
        elif spec is not None:_verify_xlsx_values(file,spec)
        else:
            from openpyxl import load_workbook
            book=load_workbook(file,read_only=True,data_only=False);book.close()
    except Exception:
        raise ValueError('Office产物结构或完整性验证失败，未返回或覆盖') from None


def _build_file(spec,tmp,precision_notes):
    ext=tmp.suffix.lstrip('.')
    if ext=='docx':
        from docx import Document
        from docx.shared import Pt,Inches
        from docx.oxml.ns import qn
        doc=Document();style=doc.styles['Normal'];style.font.name='Calibri';style.font.size=Pt(11);style.element.rPr.rFonts.set(qn('w:eastAsia'),'Microsoft YaHei')
        doc.sections[0].top_margin=Inches(.8);doc.sections[0].bottom_margin=Inches(.8);doc.add_heading(spec.title,0)
        for section in spec.sections:
            if section.heading:doc.add_heading(section.heading,1)
            for line in section.body.splitlines():doc.add_paragraph(line)
            for line in section.bullets:doc.add_paragraph(line,style='List Bullet')
        doc.save(tmp)
    elif ext=='xlsx':
        from openpyxl import Workbook
        from openpyxl.styles import Font,PatternFill
        if not spec.sheets:raise ValueError('工作簿至少需要一张表')
        book=Workbook();book.remove(book.active);names=set()
        for sheet in spec.sheets:
            if re.search(r'[\\/*?:\[\]]',sheet.name) or sheet.name.lower() in names:raise ValueError('表名无效或重复')
            names.add(sheet.name.lower());ws=book.create_sheet(sheet.name);ws.append(sheet.columns);ws.freeze_panes='A2'
            for row in sheet.rows:
                if len(row)!=len(sheet.columns):raise ValueError('行数据与表头列数不一致')
                from .result_delivery import _excel_number,_excel_text
                values=[]
                for index,value in enumerate(row,1):
                    if isinstance(value,(int,Decimal)) and not isinstance(value,bool):
                        value,reason=_excel_number(value,'text_exact')
                        if reason:
                            from openpyxl.utils import get_column_letter
                            group=next((note for note in precision_notes if note['sheet']==sheet.name and note['reason']==reason),None)
                            if group is None:
                                group={'sheet':sheet.name,'reason':reason,'cells':[]};precision_notes.append(group)
                            group['cells'].append(get_column_letter(index)+str(ws.max_row+1))
                    if isinstance(value,str):value=_excel_text(value)
                    values.append(value)
                ws.append(values)
            for row in ws:
                for cell in row:
                    # User/imported strings must never become executable formulas.
                    if isinstance(cell.value,str):cell.data_type='s'
                    if cell.row==1:cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='214F42')
            ws.auto_filter.ref=ws.dimensions
            for column in ws.columns:ws.column_dimensions[column[0].column_letter].width=min(42,max(14,max(len(str(c.value or '')) for c in column)+2))
        book.save(tmp)
    else:
        from pptx import Presentation
        from pptx.util import Inches,Pt
        if not spec.sections:raise ValueError('演示文稿至少需要一页内容')
        deck=Presentation();deck.slide_width=Inches(13.333);deck.slide_height=Inches(7.5)
        title=deck.slides.add_slide(deck.slide_layouts[0]);title.shapes.title.text=spec.title
        for section in spec.sections:
            slide=deck.slides.add_slide(deck.slide_layouts[1]);slide.shapes.title.text=section.heading or spec.title
            tf=slide.placeholders[1].text_frame;tf.clear()
            lines=section.body.splitlines()+section.bullets
            if len(lines)>12 or any(len(line)>180 for line in lines):raise ValueError('单页内容过多，请拆分为更短的幻灯片')
            for i,line in enumerate(lines):
                p=tf.paragraphs[0] if i==0 else tf.add_paragraph();p.text=line;p.font.size=Pt(22)
        deck.save(tmp)
    _verify_structure(tmp,ext,spec)


def render(turn,spec_path,name):
    from .intents import require
    from .result_delivery import durable_cached_artifact
    from .locks import assert_owned
    from .platform_files import assert_plain_path
    require(turn,'write')
    try:OfficeArgs.model_validate({'spec_path':spec_path,'name':name})
    except ValidationError:
        raise ValueError('交付文件名须为不含路径和扩展名的基名，仅支持文字、数字、空格、横线，最多91字符') from None
    pid=turn['project_id']
    with workspace.lock(pid):
        text=workspace.read_file(pid,spec_path);spec=_load_spec(text)
        ext={'document':'docx','spreadsheet':'xlsx','presentation':'pptx'}[spec.kind]
        identity={'project_id':pid,'source_spec':spec_path,'source_sha256':hashlib.sha256(text.encode()).hexdigest(),
                  'renderer':RENDERER,'name':name+'.'+ext,'precision_policy':'text_exact','precision_notes_format':'grouped_cells_v1'}
        digest=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        aid='of_'+digest[:40];folder=DATA/'deliverables'/aid;file=folder/(name+'.'+ext)
        identity['relative_path']=str(file.relative_to(DATA))
        assert_plain_path(folder);folder.mkdir(parents=True,exist_ok=True)
        try:record=get('office_artifact',aid)
        except KeyError:record={}
        if record and (any(record.get(key)!=value for key,value in identity.items())
                or record.get('state') not in ('pending','committed')
                or not re.fullmatch(r'[a-f0-9]{64}',record.get('sha256') or '')):
            raise ValueError('交付物登记与原规格身份不一致，未返回或覆盖')
        assert_plain_path(file)
        if file.exists():
            if not record:raise ValueError('交付物缺少独立完整性回执，未采用或覆盖现存文件')
            if hashlib.sha256(file.read_bytes()).hexdigest()!=record['sha256']:
                raise ValueError('交付物完整性校验失败，未返回或重新登记篡改内容')
            _verify_structure(file,ext,spec)
        precision_notes=list(record.get('precision_notes',[]));built_bytes=[None]
        def build(temporary):
            precision_notes.clear();_build_file(spec,temporary,precision_notes)
            built_bytes[0]=temporary.stat().st_size
        def save_pending(checksum):
            record.update(put('office_artifact',dict(identity,sha256=checksum,bytes=built_bytes[0],
                precision_notes=precision_notes,created_at=record.get('created_at') or now(),state='pending'),aid))
        def save_committed(checksum):
            if record.get('sha256')!=checksum:raise ValueError('交付物发布回执不一致')
            record.update(put('office_artifact',dict(record,state='committed'),aid))
        durable_cached_artifact(file,expected_sha256=record.get('sha256') if record.get('state')=='committed' else None,
            pending_sha256=record.get('sha256') if record.get('state')=='pending' else None,
            build=build,save_pending=save_pending,save_committed=save_committed,before_publish=assert_owned)
        _verify_structure(file,ext,spec)
        workspace.write_file(pid,'deliverables/'+name+'.manifest.json',json.dumps(
            {key:value for key,value in dict(record,id=aid).items() if key!='relative_path'},ensure_ascii=False,separators=(',',':')))
        return {'artifact_id':aid,'sha256':record['sha256'],'format':ext,'structural_validation':True,
            'precision_policy':'text_exact','precision_notes':record.get('precision_notes',[]),
            'precision_notes_format':'grouped_cells_v1',
            'visual_validation':False,'formula_recalculation':False,
            'download':{'label':name+'.'+ext,'url':'/api/projects/'+pid+'/deliverables/'+aid}}

def artifact(pid,aid):
    value=get('office_artifact',aid)
    if value['project_id']!=pid:raise ValueError('交付物不属于该项目')
    from .platform_files import assert_plain_path
    file=DATA/value['relative_path'];assert_plain_path(file);file=file.resolve()
    if value.get('state')=='pending':raise ValueError('交付物尚未完成发布，请先恢复生成步骤')
    if not file.is_relative_to((DATA/'deliverables').resolve()) or hashlib.sha256(file.read_bytes()).hexdigest()!=value['sha256']:raise ValueError('交付物校验失败')
    _verify_structure(file,file.suffix.lstrip('.'))
    return file,value
