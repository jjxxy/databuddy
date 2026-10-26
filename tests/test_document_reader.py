"""Synthetic format fixtures test parsing/evidence, not real financial accuracy."""
import hashlib
import json
import zipfile
from xml.sax.saxutils import escape

import pytest

from agent_service import fact_service
from agent_service.config import DATA
from agent_service.connectors import SourceError, file_rows
from agent_service.document_reader import A, P, R, REL, W, DocumentReadError, read_document
from agent_service.store import put, seal, uid


CT = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'


def package(path, parts):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', CT)
        for name, content in parts.items():
            archive.writestr(name, content)
    return path


def paragraph(text):
    return '<w:p><w:r><w:t>' + escape(text) + '</w:t></w:r></w:p>'


def docx(path, body, extra=None):
    return package(path, {'word/document.xml': f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>{body}</w:body></w:document>', **(extra or {})})


def relationships(entries):
    return f'<Relationships xmlns="{REL}">' + ''.join(
        f'<Relationship Id="{ident}" Type="{R}/{kind}" Target="{target}"' + (' TargetMode="External"' if external else '') + '/>'
        for ident, kind, target, external in entries) + '</Relationships>'


def pptx(path, slides):
    # Deliberately non-numeric filenames/order: presentation relationships rule.
    ids = ''.join(f'<p:sldId id="{100+i}" r:id="r{i}"/>' for i, _ in enumerate(slides, 1))
    parts = {'ppt/presentation.xml': f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>',
             'ppt/_rels/presentation.xml.rels': relationships([(f'r{i}', 'slide', 'slides/' + name, False) for i, (name, _, _) in enumerate(slides, 1)])}
    for name, content, hidden in slides:
        parts['ppt/slides/' + name] = f'<p:sld xmlns:p="{P}" xmlns:a="{A}" show="{0 if hidden else 1}"><p:cSld><p:spTree>{content}</p:spTree></p:cSld></p:sld>'
    return package(path, parts)


def shape(text, ident=2):
    return f'<p:sp><p:nvSpPr><p:cNvPr id="{ident}"/></p:nvSpPr><p:txBody><a:p><a:r><a:t>{escape(text)}</a:t></a:r></a:p></p:txBody></p:sp>'


def assert_evidence(document):
    for segment in document['segments']:
        exact = document['text'][segment['start']:segment['end']]
        assert segment['text_sha256'] == hashlib.sha256(exact.encode()).hexdigest()
    assert not document['graphics_verified'] and not document['ocr_executed']
    assert not document['semantic_facts_verified']


def test_docx_cell_grid_offsets_merges_hidden_text_and_hashes(tmp_path):
    body = paragraph('2026-10 净收入123.4500元')
    body += '<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>999999</w:t></w:r><w:del><w:r><w:delText>888</w:delText></w:r></w:del><w:r><w:t>正文</w:t><w:tab/><w:t>单位：元</w:t></w:r></w:p>'
    body += '''<w:tbl><w:tr><w:trPr><w:gridBefore w:val="1"/></w:trPr>
      <w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge w:val="restart"/></w:tcPr>''' + paragraph('办事处') + '''</w:tc>
      <w:tc>''' + paragraph('123.4500') + '''</w:tc></w:tr>
      <w:tr><w:trPr><w:gridBefore w:val="1"/></w:trPr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge/></w:tcPr>''' + paragraph('') + '''</w:tc><w:tc>''' + paragraph('200') + '</w:tc></w:tr></w:tbl>'
    path = docx(tmp_path/'source.docx', body)
    document = read_document(path)
    assert document['supported'] and document['parser'] == 'atlas_docx_xml'
    assert document['source_file_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert '999999' not in document['text'] and '888' not in document['text']
    assert '正文\t单位：元' in document['text']
    cells = [s for s in document['segments'] if s['kind'] == 'table_cell_paragraph']
    assert [(s['row_index'], s['column_index']) for s in cells] == [(1,2),(1,4),(2,2),(2,4)]
    assert cells[0]['column_span'] == 2
    assert cells[2]['vertical_merge'] == 'continue' and cells[2]['vertical_merge_anchor_row'] == 1
    assert document['text'][cells[2]['start']:cells[2]['end']] == ''  # never fill merged labels/amounts
    assert document['text'][cells[3]['start']:cells[3]['end']] == '200'
    assert_evidence(document)


def test_referenced_header_and_footnote_but_external_links_not_fetched(tmp_path):
    body = paragraph('收入') + '<w:p><w:r><w:footnoteReference w:id="2"/></w:r></w:p><w:sectPr><w:headerReference r:id="header1"/></w:sectPr>'
    extra = {'word/_rels/document.xml.rels': relationships([('header1','header','header2.xml',False),('fn','footnotes','footnotes.xml',False),('link','hyperlink','https://example.invalid/private',True)]),
             'word/header2.xml': f'<w:hdr xmlns:w="{W}">' + paragraph('金额单位：万元') + '</w:hdr>',
             'word/header-unused.xml': f'<w:hdr xmlns:w="{W}">' + paragraph('旧稿9999') + '</w:hdr>',
             'word/footnotes.xml': f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="2">' + paragraph('含税口径') + '</w:footnote></w:footnotes>'}
    doc = read_document(docx(tmp_path/'notes.docx', body, extra))
    assert '金额单位：万元' in doc['text'] and '含税口径' in doc['text'] and '旧稿9999' not in doc['text']
    assert {s['role'] for s in doc['segments']} == {'body','header','footnotes'}
    assert 'EXTERNAL_RELATIONSHIPS_NOT_FETCHED' in doc['warnings']
    assert_evidence(doc)


def test_deleted_note_reference_excluded_and_indented_hierarchy_retained(tmp_path):
    body = '<w:p><w:pPr><w:ind w:left="720" w:hanging="360"/><w:outlineLvl w:val="2"/></w:pPr><w:r><w:t>子级：合肥</w:t></w:r><w:del><w:r><w:footnoteReference w:id="999"/></w:r></w:del></w:p>'
    doc = read_document(docx(tmp_path/'indent.docx',body))
    assert doc['text']=='子级：合肥'
    assert doc['segments'][0]['indent_attributes']=={'left':'720','hanging':'360'}
    assert doc['segments'][0]['outline_level']==2


def test_pptx_uses_presentation_order_and_retains_table_cells_and_hidden_slide(tmp_path):
    table = '''<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="5"/></p:nvGraphicFramePr><a:graphic><a:graphicData><a:tbl><a:tr>
      <a:tc gridSpan="2"><a:txBody><a:p><a:r><a:t>合肥</a:t></a:r></a:p></a:txBody></a:tc>
      <a:tc hMerge="1"><a:txBody><a:p/></a:txBody></a:tc>
      <a:tc><a:txBody><a:p><a:r><a:t>123.4500</a:t></a:r></a:p></a:txBody></a:tc>
      </a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>'''
    path = pptx(tmp_path/'report.pptx', [('slide10.xml',shape('第一张 单位：元')+table,False),('slide2.xml',shape('第二张')+'<p:pic/>',True)])
    doc = read_document(path)
    assert doc['parser'] == 'atlas_pptx_xml' and doc['slide_count'] == 2
    assert doc['text'].index('第一张') < doc['text'].index('第二张')
    cells = [s for s in doc['segments'] if s['kind'] == 'table_cell_paragraph']
    assert [s['column_index'] for s in cells] == [1,2,3]
    assert cells[0]['merge_attributes'] == {'gridSpan':'2'}
    assert cells[1]['merge_attributes'] == {'hMerge':'1'}
    assert doc['segments'][-1]['slide_index'] == 2 and doc['segments'][-1]['slide_hidden']
    assert 'HIDDEN_SLIDES_INCLUDED_WITH_METADATA' in doc['warnings']
    assert 'GRAPHICS_NOT_INTERPRETED' in doc['warnings']
    assert_evidence(doc)


def test_pptx_line_breaks_cannot_manufacture_a_new_number(tmp_path):
    content = '<p:sp><p:txBody><a:p><a:r><a:t>12</a:t></a:r><a:br/><a:r><a:t>34</a:t></a:r><a:tab/><a:r><a:t>56</a:t></a:r></a:p></p:txBody></p:sp>'
    path = pptx(tmp_path/'breaks.pptx',[('slide1.xml',content,False)])
    doc = read_document(path)
    assert doc['text']=='12\n34\t56' and '1234' not in doc['text']
    pid,source=stored_source(path)
    with pytest.raises(ValueError,match='未逐字'):
        fact_service.propose_facts(pid,source['id'],[{'value_as_printed':'1234','measure':'金额','unit':'元','period':'2026-10',
            'dimensions':{},'start':0,'end':len(doc['text']),'quote':doc['text']}])


def test_word_compatibility_alternatives_are_not_duplicate_or_joined_numbers(tmp_path):
    mc='http://schemas.openxmlformats.org/markup-compatibility/2006'
    body=f'<w:p><w:r><w:t>12</w:t></w:r><mc:AlternateContent xmlns:mc="{mc}"><mc:Choice Requires="w"><w:r><w:t>123.45</w:t></w:r></mc:Choice><mc:Fallback><w:r><w:t>123.45</w:t></w:r></mc:Fallback></mc:AlternateContent><w:r><w:t>34</w:t></w:r></w:p>'
    doc=read_document(docx(tmp_path/'alternative.docx',body))
    assert not doc['supported'] and doc['text']==''
    assert 'ALTERNATE_CONTENT_PARAGRAPH_NOT_EXTRACTED_REQUIRES_VISUAL_REVIEW' in doc['warnings']


def pdf(path, texts):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=612,height=792)
        if text is not None:
            font = DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
            stream = DecodedStreamObject(); stream.set_data(('BT /F1 12 Tf 72 720 Td (' + text + ') Tj ET').encode())
            page[NameObject('/Contents')] = stream
    with path.open('wb') as output:
        writer.write(output)
    return path


def test_pdf_page_text_and_empty_page_not_ocr(tmp_path):
    doc = read_document(pdf(tmp_path/'source.pdf',['Net revenue 123.4500',None,'Second page']))
    assert doc['parser'] == 'pypdf' and doc['page_count'] == 3
    assert [s['page_index'] for s in doc['segments']] == [1,2,3]
    assert 'PAGES_WITHOUT_TEXT_LAYER_REQUIRE_VISUAL_REVIEW' in doc['warnings']
    assert 'PDF_TABLE_GEOMETRY_NOT_RECONSTRUCTED' in doc['warnings']
    assert_evidence(doc)
    with pytest.raises(DocumentReadError) as err:
        read_document(tmp_path/'source.pdf',max_pages=2)
    assert err.value.code == 'DOCUMENT_BUDGET_EXCEEDED'


@pytest.mark.parametrize('extension',['.png','.docx','.pptx','.pdf'])
def test_image_only_is_explicit_ocr_required(tmp_path, extension):
    path = tmp_path/('scan'+extension)
    if extension == '.docx':docx(path,'<w:p><w:r><w:drawing/></w:r></w:p>')
    elif extension == '.pptx':pptx(path,[('slide1.xml','<p:pic/>',False)])
    elif extension == '.pdf':pdf(path,[None])
    else:path.write_bytes(b'not processed as OCR')
    doc = read_document(path)
    assert not doc['supported'] and doc['error_code'] == 'OCR_REQUIRED'
    assert not doc['ocr_executed'] and not doc['graphics_verified']


@pytest.mark.parametrize('part,content,code',[
    ('../evil.xml','x','DOCUMENT_INVALID'),
    ('word/vbaProject.bin','x','DOCUMENT_MACROS_UNSUPPORTED'),
    ('word/document.xml',f'<!DOCTYPE w:document [<!ENTITY x "999">]><w:document xmlns:w="{W}"><w:body/></w:document>','DOCUMENT_INVALID'),
])
def test_unsafe_zip_xml_and_macros_rejected(tmp_path,part,content,code):
    path = package(tmp_path/'unsafe.docx',{part:content})
    with pytest.raises(DocumentReadError) as err:read_document(path)
    assert err.value.code == code


def test_duplicate_zip_paths_and_invalid_relationship_rejected(tmp_path):
    path = tmp_path/'duplicate.docx'
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('[Content_Types].xml',CT)
        with pytest.warns(UserWarning):archive.writestr('[Content_Types].xml',CT)
    with pytest.raises(DocumentReadError,match='重复'):read_document(path)
    extra = {'word/_rels/document.xml.rels':relationships([('h','header','../../outside.xml',False)])}
    path = docx(tmp_path/'reference.docx',paragraph('123'),extra)
    with pytest.raises(DocumentReadError,match='范围'):read_document(path)


def test_deep_xml_fails_budget_before_recursive_text_traversal(tmp_path):
    body = '<w:p>' + '<w:r>'*140 + '<w:t>123</w:t>' + '</w:r>'*140 + '</w:p>'
    path = docx(tmp_path/'deep.docx',body)
    with pytest.raises(DocumentReadError) as err:read_document(path)
    assert err.value.code == 'DOCUMENT_BUDGET_EXCEEDED'


@pytest.mark.parametrize('budget,value',[
    ('max_file_bytes',5),('max_text_bytes',5),('max_blocks',1),
    ('max_archive_bytes',5),('max_xml_bytes',20),('max_entries',1),
])
def test_budget_exhaustion_is_failure_not_partial_success(tmp_path,budget,value):
    path = docx(tmp_path/'budget.docx',paragraph('123.4500')+paragraph('200.0000'))
    with pytest.raises(DocumentReadError) as err:read_document(path,**{budget:value})
    assert err.value.code == 'DOCUMENT_BUDGET_EXCEEDED'


def stored_source(path):
    pid = put('project',{'name':'文档证据'},'p_'+uid())['id']
    name = uid()+path.suffix
    (DATA/'uploads').mkdir(parents=True,exist_ok=True)
    (DATA/'uploads'/name).write_bytes(path.read_bytes())
    source = put('source',{'project_id':pid,'kind':'file','name':'个人报告'+path.suffix,'revision':1,
                 'secret_config':seal({'stored_name':name}),'status':'ready'},'s_'+uid())
    return pid,source


@pytest.mark.parametrize('extension',['.docx','.pptx','.pdf'])
def test_regular_upload_rows_preserve_block_coordinates_without_docling(tmp_path,monkeypatch,extension):
    monkeypatch.setenv('USE_DOCLING','0')
    path = tmp_path/('report'+extension)
    if extension == '.docx':docx(path,paragraph('净收入123.4500元'))
    elif extension == '.pptx':pptx(path,[('slide1.xml',shape('净收入123.4500元'),False)])
    else:pdf(path,['Net revenue 123.4500'])
    _,source = stored_source(path)
    rows,evidence = file_rows(source,20)
    assert len(rows) == 1 and '123.4500' in rows[0]['内容']
    block = json.loads(rows[0]['证据'])
    assert block['locator'] and block['text_sha256']
    assert evidence['source_file_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert not evidence['semantic_facts_verified'] and not evidence['ocr_executed']
    assert evidence['parser'] == ('.pdf' == extension and 'pypdf' or 'atlas_'+extension[1:]+'_xml')


def test_image_upload_fails_explicitly_and_fact_api_returns_unsupported(tmp_path,monkeypatch):
    monkeypatch.setenv('USE_DOCLING','0')
    path = tmp_path/'scan.png'; path.write_bytes(b'no OCR attempted')
    pid,source = stored_source(path)
    with pytest.raises(SourceError) as err:file_rows(source,20)
    assert err.value.code == 'OCR_REQUIRED'
    doc = fact_service.read_document(pid,source['id'])
    assert doc['error_code'] == 'OCR_REQUIRED' and doc['status'] == 'unsupported_needs_ocr_or_visual_review'


def test_docx_fact_offsets_keep_review_pending_and_page_metadata_bounded(tmp_path):
    printed = '2026-10 合肥办事处净收入123.4500元。'
    path = docx(tmp_path/'facts.docx',paragraph(printed)+''.join(paragraph('其他段落') for _ in range(450)))
    pid,source = stored_source(path)
    offset = 0;parts=[];seen=[]
    while True:
        page = fact_service.read_document(pid,source['id'],offset,64000)
        parts.append(page['text']);seen.extend(page['segments'])
        assert len(page['segments']) <= 200
        if page['next_offset'] is None:break
        assert page['next_offset']>offset
        offset=page['next_offset']
    canonical=read_document(path)
    assert ''.join(parts) == canonical['text'] and len(seen)==451
    first=fact_service.read_document(pid,source['id'])
    assert first['source_file_sha256'] == canonical['source_file_sha256']
    proposal=fact_service.propose_facts(pid,source['id'],[{'value_as_printed':'123.4500','measure':'净收入','unit':'元',
        'period':'2026-10','dimensions':{'office':'合肥办事处'},'start':0,'end':len(printed),'quote':printed}])
    assert proposal['status']=='pending' and proposal['output_source_id'] is None
    assert not proposal['numeric_context_reviewed']
