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
_TABLE_ROWS_PER_PAGE=7  # Table bottom stays above the footer even when full.


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
                values.append((f'atlas:text:{index}:body:{line_no}:{part}',text,False))
    for bullet_no,bullet in enumerate(section.bullets):
        for line_no,line in enumerate(bullet.split('\n')):
            for part,text in enumerate(_wrapped(line,_BODY_WIDTH-3)):
                values.append((f'atlas:text:{index}:bullet:{bullet_no}:{line_no}:{part}',text,line_no==0 and part==0))
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
        batches=[table.rows[i:i+_TABLE_ROWS_PER_PAGE] for i in range(0,len(table.rows),_TABLE_ROWS_PER_PAGE)] or [[]]
        for batch_no,rows in enumerate(batches):out.append({'kind':'table','section':section_index,'indices':indices,
            'row_offset':batch_no*_TABLE_ROWS_PER_PAGE,'rows':rows,'group':group_index,'groups':len(groups),'total':len(table.rows)})
    return out


def _layout(spec):
    pages=[]
    for index,section in enumerate(spec.sections):
        heading=section.heading or spec.title
        if len(_wrapped(heading,74))>2:raise ValueError('幻灯片标题过长，请拆分标题后重试')
        lines=_lines(section,index)
        pages.extend({'kind':'text','section':index,'items':lines[i:i+_MAX_LINES]} for i in range(0,len(lines),_MAX_LINES))
        if section.table:pages.extend(_table_pages(section.table,index))
        if not lines and not section.table:pages.append({'kind':'text','section':index,'items':[]})
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


def build(spec,path):
    from pptx import Presentation
    from pptx.util import Inches,Pt
    from pptx.enum.text import MSO_ANCHOR
    from pptx.dml.color import RGBColor
    if not spec.sections:raise ValueError('演示文稿至少需要一页内容')
    if len(_wrapped(spec.title,44))>4:raise ValueError('演示文稿封面标题过长，请拆分后重试')
    pages=_layout(spec);deck=Presentation();deck.slide_width=Inches(13.333);deck.slide_height=Inches(7.5)
    cover=deck.slides.add_slide(deck.slide_layouts[0]);_background(cover,'153F36')
    title=cover.shapes.title;title.left=Inches(.82);title.top=Inches(1.55);title.width=Inches(11.65);title.height=Inches(2.65)
    title.text=spec.title;title.text_frame.word_wrap=True;title.text_frame.vertical_anchor=MSO_ANCHOR.MIDDLE
    title.text_frame.margin_left=title.text_frame.margin_right=Pt(0)
    for p in title.text_frame.paragraphs:_font(p,34,'FFFFFF',True)
    if len(cover.placeholders)>1:cover.placeholders[1].text=''
    _rect(cover,.82,1.05,.8,.05,'80C5A2')
    _text(cover,.82,.57,8,.35,'业务报告',14,'C8E5D5')
    _text(cover,.82,6.65,10,.3,'内容与业务口径请结合实际资料复核',11,'C8E5D5')
    for page_no,page in enumerate(pages,2):
        section=spec.sections[page['section']];slide=deck.slides.add_slide(deck.slide_layouts[5]);_background(slide,'FFFFFF')
        _rect(slide,0,0,.12,7.5,'2A7960')
        title=slide.shapes.title;title.left=Inches(.68);title.top=Inches(.38);title.width=Inches(11.9);title.height=Inches(1.05)
        title.text=section.heading or spec.title;title.text_frame.word_wrap=True
        title.text_frame.margin_left=title.text_frame.margin_right=Pt(0)
        for p in title.text_frame.paragraphs:_font(p,24,'17352D',True)
        _rect(slide,.7,1.5,11.93,.012,'D8E5DF')
        if page['kind']=='text':
            for i,(name,text,bullet) in enumerate(page['items']):
                y=1.75+i*.41
                if bullet:_text(slide,.76,y,.22,.32,'•',17,'2A7960')
                _text(slide,1.02,y,11.55,.34,text,18,'253B34',name=name)
        else:
            table=section.table;indices=page['indices'];count=len(indices);width=11.9/count
            shape=slide.shapes.add_table(len(page['rows'])+1,count,Inches(.7),Inches(1.9),Inches(11.9),Inches(.60*(len(page['rows'])+1)))
            shape.name=f"atlas:table:{page['section']}:{page['group']}:{page['row_offset']}"
            t=shape.table
            for i in range(count):t.columns[i].width=Inches(width)
            for ri in range(len(page['rows'])+1):
                t.rows[ri].height=Inches(.60)
                for ci,col in enumerate(indices):
                    cell=t.cell(ri,ci);cell.text=table.columns[col] if ri==0 else page['rows'][ri-1][col]
                    cell.margin_left=cell.margin_right=Pt(6);cell.margin_top=cell.margin_bottom=Pt(3)
                    cell.vertical_anchor=MSO_ANCHOR.MIDDLE;cell.text_frame.word_wrap=True
                    cell.fill.solid();cell.fill.fore_color.rgb=RGBColor.from_string('E7F1EC' if ri==0 else ('F5F8F6' if ri%2==0 else 'FFFFFF'))
                    for p in cell.text_frame.paragraphs:_font(p,12,'17352D' if ri==0 else '253B34',ri==0)
            _text(slide,.72,1.58,11.8,.23,
                  f"完整数据：{page['total']} 行 · 本页 {page['row_offset']+1 if page['rows'] else 0}–{page['row_offset']+len(page['rows'])} 行 · 列组 {page['group']+1}/{page['groups']}",
                  10,'5D756C')
        _rect(slide,.7,6.83,11.93,.012,'D8E5DF')
        _text(slide,.72,6.93,10.8,.25,'内容与业务口径请结合实际资料复核',10,'6A8077')
        _text(slide,12.0,6.93,.55,.25,str(page_no),10,'6A8077')
    deck.save(path)


def verify(spec,deck):
    pages=_layout(spec)
    if len(deck.slides)!=len(pages)+1 or deck.slides[0].shapes.title.text!=spec.title:
        raise ValueError('演示文稿页数或封面不一致')
    for page,slide in zip(pages,list(deck.slides)[1:]):
        section=spec.sections[page['section']]
        if slide.shapes.title is None or slide.shapes.title.text!=(section.heading or spec.title):raise ValueError('演示文稿标题不一致')
        if page['kind']=='text':
            got=[(s.name,s.text) for s in slide.shapes if s.has_text_frame and s.name.startswith('atlas:text:')]
            if got!=[(name,text) for name,text,_ in page['items']]:raise ValueError('演示文稿正文不完整')
        else:
            tables=[s for s in slide.shapes if s.has_table]
            if len(tables)!=1:raise ValueError('演示文稿数据表不完整')
            actual=tables[0].table;indices=page['indices']
            expected=[[section.table.columns[i] for i in indices],*[[row[i] for i in indices] for row in page['rows']]]
            if len(actual.rows)!=len(expected) or len(actual.columns)!=len(indices):raise ValueError('演示文稿数据表尺寸不一致')
            if [[actual.cell(r,c).text for c in range(len(indices))] for r in range(len(expected))]!=expected:
                raise ValueError('演示文稿数据单元格不一致')
