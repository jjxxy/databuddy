"""Editable, bounded business presentation rendering with exact-content checks.

This is a general presentation layout, not an organization-specific template
or an assertion that financial information has been reviewed.
"""
from __future__ import annotations
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

_BODY_WIDTH=74  # Conservative half-em units at 18 pt, 11-inch content width.
_MAX_LINES=11
_MAX_SLIDES=120
_MAX_TABLE_ROWS=100
_MAX_TABLE_COLUMNS=5
_TABLE_ROWS_PER_PAGE=6  # Preserve space for larger, readable two-line cells.


def _width(ch):
    if unicodedata.combining(ch) or ch in ('\u200d','\ufe0f'):return 0
    # Wide Latin glyphs are not half-em: treating W/M as narrow can clip a
    # whole line even when the character count appears to fit.
    if ch in 'WwMm':return 2
    return 2 if unicodedata.east_asian_width(ch) in ('W','F','A') else 1


def _wrapped(text,units):
    """Split without deleting or modifying a character or bisecting a number."""
    if not isinstance(text,str):raise ValueError('演示文稿内容必须为文本')
    if any(ord(c)<32 and c!='\t' for c in text):raise ValueError('演示文稿文本含不支持的控制字符')
    if not text:return ['']
    out=[];start=0
    while start<len(text):
        pos=start;size=0
        while pos<len(text) and size+_width(text[pos])<=units:
            size+=_width(text[pos]);pos+=1
        if pos==start:raise ValueError('演示文稿内容无法安全排版')
        if pos<len(text) and re.match(r'[0-9.,%\-]',text[pos-1]) and re.match(r'[0-9.,%\-]',text[pos]):
            old=pos
            while pos>start and re.match(r'[0-9.,%\-]',text[pos-1]):pos-=1
            if pos==start:
                # A long numeric token must not be split across lines or rounded.
                raise ValueError('演示文稿中单个数字过长，请使用完整表格作为附件')
            if pos==old:pos=old
        # Do not leave a combining mark or joiner detached from its character.
        while pos<len(text) and (unicodedata.combining(text[pos]) or text[pos] in ('\u200d','\ufe0f')):pos+=1
        out.append(text[start:pos]);start=pos
    return out


def _lines(section,index):
    values=[]
    if section.body:
        for line_no,line in enumerate(section.body.split('\n')):
            for part,text in enumerate(_wrapped(line,_BODY_WIDTH)):
                values.append((f'nexus:text:{index}:body:{line_no}:{part}',text,False))
    for bullet_no,bullet in enumerate(section.bullets):
        for line_no,line in enumerate(bullet.split('\n')):
            for part,text in enumerate(_wrapped(line,_BODY_WIDTH-3)):
                values.append((f'nexus:text:{index}:bullet:{bullet_no}:{line_no}:{part}',text,line_no==0 and part==0))
    return values


def _table_pages(table,section_index):
    if len(table.rows)>_MAX_TABLE_ROWS:raise ValueError('PPT 表格超过 100 行，请交付完整明细表并单独制作汇报摘要')
    columns=table.columns;groups=[]
    def fits(indices):
        units=max(12,int(105/len(indices)))
        try:
            for col in indices:
                if len(_wrapped(columns[col],units))>2:return False
                for row in table.rows:
                    if sum(len(_wrapped(line,units)) for line in row[col].split('\n'))>2:return False
        except ValueError:
            return False
        return True
    all_indices=list(range(len(columns)))
    if len(columns)<=_MAX_TABLE_COLUMNS and fits(all_indices):groups=[all_indices]
    else:
        start=1
        while start<len(columns):
            end=min(start+_MAX_TABLE_COLUMNS-1,len(columns))
            while end>start and not fits([0,*range(start,end)]):end-=1
            if end==start:raise ValueError('PPT 表格内容过长，请使用完整表格附件；未截断内容')
            groups.append([0,*range(start,end)]);start=end
        if not groups:raise ValueError('PPT 表格内容过长，请使用完整表格附件；未截断内容')
    out=[]
    for group_index,indices in enumerate(groups):
        # Width is conservative; all cells are verified without truncation.
        units=max(12,int(105/len(indices)))
        for col in indices:
            if len(_wrapped(columns[col],units))>2:raise ValueError('PPT 表头过长，请缩短表头或使用完整表格附件')
        for row in table.rows:
            for col in indices:
                pieces=[]
                for line in row[col].split('\n'):pieces.extend(_wrapped(line,units))
                if len(pieces)>2:raise ValueError('PPT 单元格过长，请使用完整表格附件；未截断内容')
        # Balance pages so seven or eight rows do not strand a single row on
        # the last slide. Never reorder rows or assume which one is a total.
        count=max(1,(len(table.rows)+_TABLE_ROWS_PER_PAGE-1)//_TABLE_ROWS_PER_PAGE)
        base,extra=divmod(len(table.rows),count);offset=0
        for batch_no in range(count):
            size=base+(batch_no<extra);rows=table.rows[offset:offset+size]
            out.append({'kind':'table','section':section_index,'indices':indices,
                'row_offset':offset,'rows':rows,'group':group_index,'groups':len(groups),'total':len(table.rows)})
            offset+=size
    return out


def _layout(spec):
    pages=[]
    for index,section in enumerate(spec.sections):
        heading=section.heading or spec.title
        if len(_wrapped(heading,60))>2:raise ValueError('幻灯片标题过长，请拆分标题后重试')
        lines=_lines(section,index)
        pages.extend({'kind':'text','section':index,'items':lines[i:i+_MAX_LINES]} for i in range(0,len(lines),_MAX_LINES))
        if section.table:pages.extend(_table_pages(section.table,index))
        if section.chart:
            chart=section.chart
            if len(_wrapped(chart.title,58))>1 or len(_wrapped(chart.unit,26))>1:
                raise ValueError('图表标题或单位过长，无法安全排版')
            if any(len(_wrapped(point.label,20))>1 or len(_wrapped(point.value,25))>1 for point in chart.points):
                raise ValueError('图表类别或数值过长，请使用完整明细表')
            pages.append({'kind':'chart','section':index})
        if not lines and not section.table and not section.chart:pages.append({'kind':'text','section':index,'items':[]})
    if len(pages)+1>_MAX_SLIDES:raise ValueError('演示文稿超过 120 页，请拆分为多个报告；未截断内容')
    return pages


def _font(paragraph,size,color,bold=False):
    from pptx.dml.color import RGBColor
    from pptx.util import Pt
    paragraph.font.name='Noto Sans CJK SC';paragraph.font.size=Pt(size);paragraph.font.bold=bold
    paragraph.font.color.rgb=RGBColor.from_string(color)
    paragraph.line_spacing=Pt(size*1.25)
    paragraph.space_before=Pt(0);paragraph.space_after=Pt(0)


def _text(slide,x,y,w,h,text,size,color,bold=False,name=None):
    from pptx.util import Inches,Pt
    from pptx.enum.text import MSO_ANCHOR
    shape=slide.shapes.add_textbox(Inches(x),Inches(y),Inches(w),Inches(h))
    if name:shape.name=name
    frame=shape.text_frame;frame.clear();frame.word_wrap=False
    frame.margin_left=frame.margin_right=frame.margin_top=frame.margin_bottom=Pt(0)
    frame.vertical_anchor=MSO_ANCHOR.MIDDLE
    p=frame.paragraphs[0];p.text=text;_font(p,size,color,bold)
    return shape


def _background(slide,color):
    from pptx.dml.color import RGBColor
    slide.background.fill.solid();slide.background.fill.fore_color.rgb=RGBColor.from_string(color)


def _rect(slide,x,y,w,h,color):
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.dml.color import RGBColor
    from pptx.util import Inches
    shape=slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,Inches(x),Inches(y),Inches(w),Inches(h))
    shape.fill.solid();shape.fill.fore_color.rgb=RGBColor.from_string(color);shape.line.fill.background()
    return shape


def _signed_geometry(chart):
    """Use one exact zero axis and equal positive/negative Decimal scale.

    If a nonzero bar would be below the safe visible minimum, return None and
    use an exact table rather than exaggerating the value or hiding its sign.
    """
    from decimal import Decimal, localcontext
    from pptx.util import Inches
    values=[Decimal(point.value) for point in chart.points]
    largest=max(abs(value) for value in values)
    half=int(Inches(2.25));axis=int(Inches(5.15));minimum=int(Inches(.04))
    result=[]
    with localcontext() as ctx:
        ctx.prec=80
        for i,value in enumerate(values):
            width=int(Decimal(half)*abs(value)/largest) if value else 0
            if value and width<minimum:return None
            left=axis-width if value<0 else axis
            result.append((left,int(Inches(2.35+i*.46)),width,int(Inches(.22)),value<0))
    return result


def _signed_bar(slide,geometry,index,section):
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.dml.color import RGBColor
    left,top,width,height,negative=geometry
    if not width:return
    shape=slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,left,top,width,height)
    shape.name=f'nexus:signed-bar:{section}:{index}'
    shape.fill.solid();shape.fill.fore_color.rgb=RGBColor.from_string('B94E43' if negative else '2A7960')
    shape.line.fill.background()


def build(spec,path,*,on_slide=None):
    from pptx import Presentation
    from pptx.util import Inches,Pt
    from pptx.enum.text import MSO_ANCHOR,PP_ALIGN
    from pptx.dml.color import RGBColor
    if not spec.sections:raise ValueError('演示文稿至少需要一页内容')
    if len(_wrapped(spec.title,44))>4:raise ValueError('演示文稿封面标题过长，请拆分后重试')
    pages=_layout(spec);deck=Presentation();deck.slide_width=Inches(13.333);deck.slide_height=Inches(7.5)
    cover=deck.slides.add_slide(deck.slide_layouts[0]);_background(cover,'153F36')
    title=cover.shapes.title;title.left=Inches(.82);title.top=Inches(1.55);title.width=Inches(11.65);title.height=Inches(2.65)
    title.text=spec.title;title.text_frame.word_wrap=True;title.text_frame.vertical_anchor=MSO_ANCHOR.MIDDLE
    title.text_frame.margin_left=title.text_frame.margin_right=Pt(0)
    for p in title.text_frame.paragraphs:_font(p,36,'FFFFFF',True);p.alignment=PP_ALIGN.LEFT
    if len(cover.placeholders)>1:cover.placeholders[1].text=''
    _rect(cover,.82,1.05,.8,.05,'80C5A2')
    _text(cover,.82,.57,8,.35,'业务报告',14,'C8E5D5')
    _text(cover,.82,6.65,10,.3,'内容与业务口径请结合实际资料复核',11,'C8E5D5')
    if on_slide:on_slide(1,len(pages)+1,cover)
    for page_no,page in enumerate(pages,2):
        section=spec.sections[page['section']];slide=deck.slides.add_slide(deck.slide_layouts[5]);_background(slide,'FFFFFF')
        _rect(slide,0,0,.12,7.5,'2A7960')
        title=slide.shapes.title;title.left=Inches(.68);title.top=Inches(.38);title.width=Inches(11.9);title.height=Inches(1.05)
        title.text=section.heading or spec.title;title.text_frame.word_wrap=True
        title.text_frame.margin_left=title.text_frame.margin_right=Pt(0)
        for p in title.text_frame.paragraphs:_font(p,28,'17352D',True);p.alignment=PP_ALIGN.LEFT
        _rect(slide,.7,1.5,11.93,.012,'D8E5DF')
        if page['kind']=='text':
            # Give shorter summaries more breathing room without truncating or
            # changing supplied wording. Long pages retain a compact layout.
            short=len(page['items'])<=6
            for i,(name,text,bullet) in enumerate(page['items']):
                y=(1.86+i*.56) if short else (1.75+i*.44)
                if bullet:_text(slide,.76,y,.22,.38,'•',18 if short else 17,'2A7960')
                _text(slide,1.02,y,11.55,.42 if short else .38,text,22 if short else 20,'253B34',name=name)
        elif page['kind']=='table':
            table=section.table;indices=page['indices'];count=len(indices);width=11.9/count
            shape=slide.shapes.add_table(len(page['rows'])+1,count,Inches(.7),Inches(1.9),Inches(11.9),Inches(.68*(len(page['rows'])+1)))
            shape.name=f"nexus:table:{page['section']}:{page['group']}:{page['row_offset']}"
            t=shape.table
            for i in range(count):t.columns[i].width=Inches(width)
            for ri in range(len(page['rows'])+1):
                t.rows[ri].height=Inches(.68)
                for ci,col in enumerate(indices):
                    cell=t.cell(ri,ci);cell.text=table.columns[col] if ri==0 else page['rows'][ri-1][col]
                    cell.margin_left=cell.margin_right=Pt(6);cell.margin_top=cell.margin_bottom=Pt(3)
                    cell.vertical_anchor=MSO_ANCHOR.MIDDLE;cell.text_frame.word_wrap=True
                    cell.fill.solid();cell.fill.fore_color.rgb=RGBColor.from_string('E7F1EC' if ri==0 else ('F5F8F6' if ri%2==0 else 'FFFFFF'))
                    for p in cell.text_frame.paragraphs:_font(p,14,'17352D' if ri==0 else '253B34',ri==0)
            _text(slide,.72,1.58,11.8,.23,
                  f"完整数据：{page['total']} 行 · 本页 {page['row_offset']+1 if page['rows'] else 0}–{page['row_offset']+len(page['rows'])} 行 · 列组 {page['group']+1}/{page['groups']}",
                  10,'5D756C')
        else:
            from decimal import Decimal
            chart_spec=section.chart
            _text(slide,.75,1.58,7.35,.45,chart_spec.title,18,'17352D',True,name=f"nexus:chart-title:{page['section']}")
            if any(Decimal(point.value)<0 for point in chart_spec.points):
                geometry=_signed_geometry(chart_spec)
                if geometry is None:
                    # Magnitudes too small for reliable visual display are not
                    # enlarged: show every exact value in an editable table.
                    table=slide.shapes.add_table(len(chart_spec.points)+1,2,Inches(.76),Inches(2.16),Inches(7.12),Inches(.45*(len(chart_spec.points)+1)))
                    table.name=f"nexus:signed-table:{page['section']}"
                    table.table.columns[0].width=Inches(3.5);table.table.columns[1].width=Inches(3.62)
                    for row,(label,value) in enumerate([('类别','原始数值 · '+chart_spec.unit),*[(p.label,p.value) for p in chart_spec.points]]):
                        for col,text in enumerate((label,value)):
                            cell=table.table.cell(row,col);cell.text=text;cell.margin_left=cell.margin_right=Pt(6);cell.margin_top=cell.margin_bottom=Pt(2)
                            cell.vertical_anchor=MSO_ANCHOR.MIDDLE;cell.fill.solid();cell.fill.fore_color.rgb=RGBColor.from_string('E7F1EC' if row==0 else ('F5F8F6' if row%2==0 else 'FFFFFF'))
                            for para in cell.text_frame.paragraphs:_font(para,13,'17352D',row==0)
                    _text(slide,.8,6.20,7.0,.3,'数值跨度较大，以完整原值表格展示',10,'5D756C',name=f"nexus:signed-fallback:{page['section']}")
                    _text(slide,.8,6.47,7.0,.20,'Exact values shown; range exceeds chart scale',9,'5D756C',name=f"nexus:signed-fallback-en:{page['section']}")
                else:
                    axis=_rect(slide,5.15,2.17,.015,4.02,'667E72');axis.name=f"nexus:signed-axis:{page['section']}"
                    _text(slide,5.03,2.04,.5,.2,'0',10,'5D756C',name=f"nexus:signed-zero:{page['section']}")
                    for i,(point,bar) in enumerate(zip(chart_spec.points,geometry)):
                        _text(slide,.8,2.25+i*.46,1.95,.40,point.label,12,'435B50',name=f"nexus:signed-label:{page['section']}:{i}")
                        _signed_bar(slide,bar,i,page['section'])
                    _text(slide,.8,6.20,7.0,.3,'条形为独立形状，修改数值后需重新生成',10,'5D756C',name=f"nexus:signed-note:{page['section']}")
                    _text(slide,.8,6.47,7.0,.20,'Shapes do not update automatically after edits',9,'5D756C',name=f"nexus:signed-note-en:{page['section']}")
            else:
                from pptx.chart.data import ChartData
                from pptx.enum.chart import XL_CHART_TYPE
                # A horizontal chart displays the first data category at the
                # bottom in the standard Office axis order. Reverse category/value
                # pairs in the embedded chart together, leaving the displayed
                # exact-value list in the user's original order. This also avoids
                # relying on maxMin, which some browser viewers ignore.
                chart_points=list(reversed(chart_spec.points))
                data=ChartData();data.categories=[point.label for point in chart_points]
                data.add_series(chart_spec.title,[float(point.value) for point in chart_points])
                shape=slide.shapes.add_chart(XL_CHART_TYPE.BAR_CLUSTERED,Inches(.76),Inches(2.15),Inches(7.15),Inches(4.24),data)
                shape.name=f"nexus:chart:{page['section']}"
                chart=shape.chart;chart.has_legend=False;chart.has_title=False
                chart.category_axis.reverse_order=False
                chart.category_axis.tick_labels.font.name='Noto Sans CJK SC';chart.category_axis.tick_labels.font.size=Pt(11)
                chart.value_axis.tick_labels.font.name='Noto Sans CJK SC';chart.value_axis.tick_labels.font.size=Pt(10)
                chart.value_axis.tick_labels.number_format='#,##0'
                chart.value_axis.tick_labels.number_format_is_linked=False
                values=[float(point.value) for point in chart_spec.points]
                if min(values)>=0:chart.value_axis.minimum_scale=0
                if max(values)<=0 and min(values)<0:chart.value_axis.maximum_scale=0
                series=chart.series[0]
                series.format.fill.solid();series.format.fill.fore_color.rgb=RGBColor.from_string('2A7960')
            _rect(slide,8.15,1.7,4.4,4.68,'F3F8F5')
            _text(slide,8.38,1.86,3.95,.33,'原始数值 · '+chart_spec.unit,14,'17352D',True,name=f"nexus:chart-unit:{page['section']}")
            for i,point in enumerate(chart_spec.points):
                y=2.28+i*.46
                label=_text(slide,8.38,y,1.85,.40,point.label,12,'435B50',name=f"nexus:chart-label:{page['section']}:{i}")
                label.text_frame.word_wrap=True
                value=_text(slide,10.26,y,2.07,.40,point.value,12,'17352D',True,name=f"nexus:chart-value:{page['section']}:{i}")
                value.text_frame.paragraphs[0].alignment=PP_ALIGN.RIGHT
            _text(slide,8.38,6.08,3.95,.25,'图形用于比较；请以原始数值核对',9,'5D756C')
        _rect(slide,.7,6.83,11.93,.012,'D8E5DF')
        _text(slide,.72,6.93,10.8,.25,'内容与业务口径请结合实际资料复核',10,'6A8077')
        _text(slide,12.0,6.93,.55,.25,str(page_no),10,'6A8077')
        if on_slide:on_slide(page_no,len(pages)+1,slide)
    deck.save(path)


def verify(spec,deck):
    pages=_layout(spec)
    if len(deck.slides)!=len(pages)+1 or deck.slides[0].shapes.title.text!=spec.title:
        raise ValueError('演示文稿页数或封面不一致')
    for page,slide in zip(pages,list(deck.slides)[1:]):
        section=spec.sections[page['section']]
        if slide.shapes.title is None or slide.shapes.title.text!=(section.heading or spec.title):raise ValueError('演示文稿标题不一致')
        if page['kind']=='text':
            got=[(s.name,s.text) for s in slide.shapes if s.has_text_frame and s.name.startswith('nexus:text:')]
            if got!=[(name,text) for name,text,_ in page['items']]:raise ValueError('演示文稿正文不完整')
        elif page['kind']=='table':
            tables=[s for s in slide.shapes if s.has_table]
            if len(tables)!=1:raise ValueError('演示文稿数据表不完整')
            actual=tables[0].table;indices=page['indices']
            expected=[[section.table.columns[i] for i in indices],*[[row[i] for i in indices] for row in page['rows']]]
            if len(actual.rows)!=len(expected) or len(actual.columns)!=len(indices):raise ValueError('演示文稿数据表尺寸不一致')
            if [[actual.cell(r,c).text for c in range(len(indices))] for r in range(len(expected))]!=expected:
                raise ValueError('演示文稿数据单元格不一致')
        else:
            chart_spec=section.chart
            from decimal import Decimal
            if any(Decimal(point.value)<0 for point in chart_spec.points):
                from pptx.util import Inches
                if any(shape.has_chart for shape in slide.shapes):raise ValueError('负数图表包含未经验证的原生图表')
                named={}
                for shape in slide.shapes:
                    if shape.name.startswith('nexus:signed-'):
                        if shape.name in named:raise ValueError('负数图表含重复标识')
                        named[shape.name]=shape
                geometry=_signed_geometry(chart_spec);index=page['section']
                if geometry is None:
                    shape=named.get(f'nexus:signed-table:{index}')
                    if shape is None or not shape.has_table:raise ValueError('精确数据表缺失')
                    actual=shape.table
                    expected=[['类别','原始数值 · '+chart_spec.unit],*[[p.label,p.value] for p in chart_spec.points]]
                    if len(actual.rows)!=len(expected) or len(actual.columns)!=2 or [[actual.cell(r,c).text for c in range(2)] for r in range(len(expected))]!=expected:
                        raise ValueError('精确数据表不一致')
                    note=named.get(f'nexus:signed-fallback:{index}')
                    if note is None or note.text!='数值跨度较大，以完整原值表格展示':raise ValueError('数据表说明缺失')
                    note_en=named.get(f'nexus:signed-fallback-en:{index}')
                    if note_en is None or note_en.text!='Exact values shown; range exceeds chart scale':raise ValueError('数据表说明缺失')
                    if len(named)!=3:raise ValueError('数据表含额外图形')
                else:
                    axis=named.get(f'nexus:signed-axis:{index}')
                    zero=named.get(f'nexus:signed-zero:{index}')
                    note=named.get(f'nexus:signed-note:{index}')
                    note_en=named.get(f'nexus:signed-note-en:{index}')
                    if (axis is None or axis.left!=int(Inches(5.15)) or axis.top!=int(Inches(2.17))
                            or axis.width!=int(Inches(.015)) or axis.height!=int(Inches(4.02))
                            or str(axis.fill.fore_color.rgb)!='667E72' or zero is None or zero.text!='0'
                            or note is None or note.text!='条形为独立形状，修改数值后需重新生成'
                            or note_en is None or note_en.text!='Shapes do not update automatically after edits'):
                        raise ValueError('负数图表零轴或说明不一致')
                    expected_count=4+len(chart_spec.points)+sum(bar[2]>0 for bar in geometry)
                    if len(named)!=expected_count:raise ValueError('负数图表标记数量不一致')
                    for i,(point,bar) in enumerate(zip(chart_spec.points,geometry)):
                        label=named.get(f'nexus:signed-label:{index}:{i}')
                        if label is None or label.text!=point.label:raise ValueError('负数图表类别不一致')
                        shape=named.get(f'nexus:signed-bar:{index}:{i}')
                        if not bar[2]:
                            if shape is not None:raise ValueError('零值不能显示条形')
                            continue
                        if (shape is None or (shape.left,shape.top,shape.width,shape.height)!=bar[:4]
                                or str(shape.fill.fore_color.rgb)!=('B94E43' if bar[4] else '2A7960')):
                            raise ValueError('负数图表条形方向或长度不一致')
            else:
                charts=[s for s in slide.shapes if s.has_chart]
                if len(charts)!=1 or len(charts[0].chart.series)!=1:raise ValueError('演示文稿图表不完整')
                chart=charts[0].chart
                chart_points=list(reversed(chart_spec.points))
                if chart.category_axis.reverse_order is not False:
                    raise ValueError('演示文稿图表类别方向不一致')
                if [c.label for c in chart.plots[0].categories]!=[p.label for p in chart_points]:
                    raise ValueError('演示文稿图表类别不一致')
                from decimal import Decimal
                if [Decimal(str(v)) for v in chart.series[0].values]!=[Decimal(p.value) for p in chart_points]:
                    raise ValueError('演示文稿图表数值不一致')
                # PowerPoint edits charts through a separate embedded workbook.
                # Verify it too, so opening the editor cannot reveal different
                # numbers from those visible in the slide's chart cache.
                from io import BytesIO
                from openpyxl import load_workbook
                workbook=load_workbook(BytesIO(chart.part.chart_workbook.xlsx_part.blob),read_only=True,data_only=False)
                try:
                    if len(workbook.worksheets)!=1:raise ValueError('图表数据工作簿不一致')
                    sheet=workbook.worksheets[0]
                    if sheet.max_row!=len(chart_spec.points)+1 or sheet.max_column!=2:
                        raise ValueError('图表数据工作簿尺寸不一致')
                    rows=list(sheet.iter_rows())
                    if rows[0][1].data_type!='s' or rows[0][1].value!=chart_spec.title:raise ValueError('图表数据系列不一致')
                    for cells,point in zip(rows[1:],chart_points):
                        if (cells[0].data_type!='s' or cells[0].value!=point.label or cells[1].data_type!='n'
                                or isinstance(cells[1].value,bool)
                                or Decimal(str(cells[1].value))!=Decimal(point.value)):
                            raise ValueError('图表数据工作簿数值不一致')
                finally:workbook.close()
            texts={s.name:s.text for s in slide.shapes if s.has_text_frame and s.name.startswith('nexus:chart-')}
            expected={f"nexus:chart-title:{page['section']}":chart_spec.title,
                      f"nexus:chart-unit:{page['section']}":'原始数值 · '+chart_spec.unit}
            for i,point in enumerate(chart_spec.points):
                expected[f"nexus:chart-label:{page['section']}:{i}"]=point.label
                expected[f"nexus:chart-value:{page['section']}:{i}"]=point.value
            if texts!=expected:raise ValueError('演示文稿图表标签或原始数值不一致')
