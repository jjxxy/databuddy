"""Deterministic editable Office deliverables; templates, never generated code."""
import hashlib,json,math,re,zipfile
from decimal import Decimal,InvalidOperation
from typing import Literal
from pydantic import Field,ValidationError,model_validator
from .models import StrictModel
from .config import DATA
from .store import get,put,now
from . import workspace

RENDERER='office-v7'
PREVIOUS_RENDERER='office-v6'
LEGACY_RENDERER='office-v5'
SUPPORTED_RENDERERS=(RENDERER,PREVIOUS_RENDERER,LEGACY_RENDERER)
PRECISION_NOTES_FORMAT='grouped_cells_or_column_rows_v1'
MANIFEST_MAX_BYTES=256*1024

class OfficeArgs(StrictModel):
    spec_path:str=Field(min_length=1,max_length=200,
        description='项目Office规格JSON路径。Excel采用text_exact：超安全精度或范围的有限数值无损存文本，precision_notes按cells或column_rows（含闭区间）完整披露；非有限数字拒绝。')
    name:str=Field(min_length=1,max_length=91,
        pattern=r'^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff _-]{0,90}$',
        description='不含路径和扩展名的文件基名，例如净收入明细；扩展名由规格kind决定，不能传report.xlsx')
class SectionTable(StrictModel):
    columns:list[str]=Field(min_length=1,max_length=20)
    rows:list[list[str]]=Field(default_factory=list,max_length=100)
    @model_validator(mode='after')
    def complete(self):
        if any(not c.strip() or len(c)>300 for c in self.columns):raise ValueError('表格表头无效')
        if any(len(row)!=len(self.columns) or any(len(value)>2000 for value in row) for row in self.rows):
            raise ValueError('表格行不完整或单元格过长')
        return self
class ChartPoint(StrictModel):
    label:str=Field(min_length=1,max_length=48,description='图表类别，来自已确认的数据，不推断')
    value:str=Field(min_length=1,max_length=24,pattern=r'^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$',
                    description='原始十进制数值文本，不使用千分位或百分号；请单独填写单位')
    @model_validator(mode='after')
    def exact_chart_value(self):
        value=Decimal(self.value)
        numeric=float(value)
        # The embedded workbook uses binary floating point and Excel supports
        # only 15 significant digits. Reject values it cannot represent
        # faithfully rather than silently changing a financial figure.
        if (len(value.normalize().as_tuple().digits)>15 or not math.isfinite(numeric)
                or Decimal(str(numeric))!=value):
            raise ValueError('图表数值无法安全写入可编辑工作簿，请保留原始明细表')
        return self
class SectionChart(StrictModel):
    title:str=Field(min_length=1,max_length=80,description='准确描述图表指标，不自动推断结论')
    unit:str=Field(min_length=1,max_length=20,description='数值实际单位，例如元、万元、%或件')
    points:list[ChartPoint]=Field(min_length=1,max_length=8)
    @model_validator(mode='after')
    def unambiguous_labels(self):
        labels=[p.label for p in self.points]
        if any(not label.strip() or label.startswith('=') or any(ord(c)<32 for c in label) for label in labels) or len(set(labels))!=len(labels):
            raise ValueError('图表类别不能为空、重复、包含控制字符或以公式符号开头')
        if not self.title.strip() or self.title.startswith('=') or not self.unit.strip() or any(ord(c)<32 for c in self.title+self.unit):
            raise ValueError('图表标题或单位无效')
        return self
class Section(StrictModel):
    heading:str=Field(default='',max_length=300)
    body:str=Field(default='',max_length=20000)
    bullets:list[str]=Field(default_factory=list,max_length=30)
    table:SectionTable|None=None
    chart:SectionChart|None=None
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
            doc=Document(file)
            if spec is not None:
                # Verify text, editable cells and document order, not only ZIP
                # validity. A table beside the wrong section is misleading.
                from docx.table import Table
                from docx.text.paragraph import Paragraph
                expected=[('paragraph',spec.title)]
                for section in spec.sections:
                    if section.heading:expected.append(('paragraph',section.heading))
                    expected.extend(('paragraph',line) for line in section.body.splitlines())
                    expected.extend(('paragraph',line) for line in section.bullets)
                    if section.table is not None:expected.append(('table',[section.table.columns,*section.table.rows]))
                actual=[]
                for item in doc.iter_inner_content():
                    if isinstance(item,Paragraph):actual.append(('paragraph',item.text))
                    elif isinstance(item,Table):actual.append(('table',[[cell.text for cell in row.cells] for row in item.rows]))
                    else:raise ValueError('Word 文档含无法核验的元素')
                if actual!=expected:raise ValueError('Word 内容或顺序与规格不一致')
        elif ext=='pptx':
            from pptx import Presentation
            deck=Presentation(file)
            if spec is not None:
                from .office_presentation import verify
                verify(spec,deck)
        elif spec is not None:_verify_xlsx_values(file,spec)
        else:
            from openpyxl import load_workbook
            book=load_workbook(file,read_only=True,data_only=False);book.close()
    except Exception:
        raise ValueError('Office产物结构或完整性验证失败，未返回或覆盖') from None


def _build_file(spec,tmp,precision_notes,*,renderer_version=RENDERER):
    ext=tmp.suffix.lstrip('.')
    if renderer_version not in SUPPORTED_RENDERERS:raise ValueError('Office渲染器版本不可用')
    if ext=='pptx' and renderer_version!=RENDERER and any(s.chart and any(Decimal(p.value)<0 for p in s.chart.points) for s in spec.sections):
        raise ValueError('旧版 Office 渲染器不支持负数图表，请重新验证并使用新版')
    if ext!='pptx' and any(section.chart is not None or (section.table is not None and (ext!='docx' or renderer_version==LEGACY_RENDERER)) for section in spec.sections):
        raise ValueError('结构化章节表格或图表目前仅支持演示文稿，请使用工作簿交付完整数据')
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
            if section.table is not None:
                from docx.oxml import OxmlElement
                values=[section.table.columns,*section.table.rows]
                if any(any(any(ord(ch)<32 and ch not in ('\n','\t') for ch in cell) for cell in row) for row in values):
                    raise ValueError('Word 表格含不支持的控制字符，未生成成果')
                table=doc.add_table(rows=1,cols=len(section.table.columns))
                table.style='Table Grid'
                table.rows[0]._tr.get_or_add_trPr().append(OxmlElement('w:tblHeader'))
                for index,value in enumerate(section.table.columns):table.rows[0].cells[index].text=value
                for row in section.table.rows:
                    cells=table.add_row().cells
                    for index,value in enumerate(row):cells[index].text=value
                for row in table.rows:
                    for cell in row.cells:
                        for paragraph in cell.paragraphs:
                            for run in paragraph.runs:run.font.size=Pt(9)
                from docx.shared import RGBColor
                for cell in table.rows[0].cells:
                    shade=OxmlElement('w:shd');shade.set(qn('w:fill'),'214F42');cell._tc.get_or_add_tcPr().append(shade)
                    for paragraph in cell.paragraphs:
                        for run in paragraph.runs:
                            run.font.bold=True;run.font.color.rgb=RGBColor.from_string('FFFFFF')
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
        from .office_presentation import build
        build(spec,tmp)
    _verify_structure(tmp,ext,spec)


def _compact_precision_notes(notes):
    """Lossless coordinates: large notes use column row numbers/ranges."""
    if len(json.dumps(notes,ensure_ascii=False,separators=(',',':')).encode())<=192*1024:return
    for note in notes:
        columns={}
        for cell in note.get('cells',[]):
            match=re.fullmatch(r'([A-Z]+)([1-9][0-9]*)',cell)
            if match is None:raise ValueError('精度说明坐标无效，未发布产物')
            columns.setdefault(match[1],[]).append(int(match[2]))
        compact={}
        for column,rows in columns.items():
            result=[];rows=sorted(rows);start=end=rows[0]
            for row in [*rows[1:],None]:
                if row is not None and row==end+1:end=row;continue
                if end-start>=2:result.append([start,end])
                else:result.extend(range(start,end+1))
                start=end=row
            compact[column]=result
        note.pop('cells',None);note['column_rows']=compact


def _manifest_text(record,aid):
    value=json.dumps({key:item for key,item in dict(record,id=aid).items() if key!='relative_path'},
        ensure_ascii=False,separators=(',',':'))
    if len(value.encode())>MANIFEST_MAX_BYTES:raise ValueError('精度清单超过256KB，请拆分交付文件；未发布新产物')
    return value


def render(turn,spec_path,name,*,expected_sha256=None,frozen_spec=None,renderer_version=RENDERER):
    from .intents import require
    from .result_delivery import durable_cached_artifact
    from .locks import assert_owned
    from .platform_files import assert_plain_path
    require(turn,'write')
    try:OfficeArgs.model_validate({'spec_path':spec_path,'name':name})
    except ValidationError:
        raise ValueError('交付文件名须为不含路径和扩展名的基名，仅支持文字、数字、空格、横线，最多91字符') from None
    if renderer_version not in SUPPORTED_RENDERERS:raise ValueError('Office渲染器版本不可用')
    pid=turn['project_id']
    with workspace.lock(pid):
        # Business flows can provide a frozen JSON value in memory. Its actual
        # data must not be written into the project Git workspace. The caller
        # is responsible for persisting the approved input in protected run
        # storage, and must provide an independently computed expected hash.
        if frozen_spec is not None and (not isinstance(frozen_spec,str) or expected_sha256 is None):
            raise ValueError('固定成果规格缺少可验证的输入摘要')
        text=workspace.read_file(pid,spec_path) if frozen_spec is None else frozen_spec
        if expected_sha256 is not None and (not isinstance(expected_sha256,str) or not re.fullmatch(r'[a-f0-9]{64}',expected_sha256) or hashlib.sha256(text.encode()).hexdigest()!=expected_sha256):
            raise ValueError('交付规格与已冻结输入不一致，未生成成果')
        spec=_load_spec(text)
        ext={'document':'docx','spreadsheet':'xlsx','presentation':'pptx'}[spec.kind]
        identity={'project_id':pid,'source_spec':spec_path,'source_sha256':hashlib.sha256(text.encode()).hexdigest(),
                  'renderer':renderer_version,'name':name+'.'+ext,'precision_policy':'text_exact','precision_notes_format':PRECISION_NOTES_FORMAT}
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
            precision_notes.clear();_build_file(spec,temporary,precision_notes,renderer_version=renderer_version)
            _compact_precision_notes(precision_notes)
            built_bytes[0]=temporary.stat().st_size
        def save_pending(checksum):
            pending=dict(identity,sha256=checksum,bytes=built_bytes[0],
                precision_notes=precision_notes,created_at=record.get('created_at') or now(),state='pending')
            _manifest_text(dict(pending,state='committed'),aid)  # Before registration or publication.
            record.update(put('office_artifact',pending,aid))
        def save_committed(checksum):
            if record.get('sha256')!=checksum:raise ValueError('交付物发布回执不一致')
            record.update(put('office_artifact',dict(record,state='committed'),aid))
        durable_cached_artifact(file,expected_sha256=record.get('sha256') if record.get('state')=='committed' else None,
            pending_sha256=record.get('sha256') if record.get('state')=='pending' else None,
            build=build,save_pending=save_pending,save_committed=save_committed,before_publish=assert_owned)
        _verify_structure(file,ext,spec)
        workspace.write_file(pid,'deliverables/'+name+'.manifest.json',_manifest_text(record,aid))
        return {'artifact_id':aid,'sha256':record['sha256'],'format':ext,'structural_validation':True,
            'source_spec':spec_path if frozen_spec is None else None,
            'source_sha256':identity['source_sha256'],
            'source_spec_editable_in_workspace':frozen_spec is None,
            'precision_policy':'text_exact','precision_notes':record.get('precision_notes',[]),
            'precision_notes_format':PRECISION_NOTES_FORMAT,
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
