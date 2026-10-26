"""Deterministic text evidence from Office XML and PDF text layers.

This reader does not perform OCR, render pages, infer table meanings or evaluate
Office fields. Each block retains its actual XML cell/paragraph or PDF page
location. Merged-cell values are never filled into neighbouring cells.
"""
from __future__ import annotations

import hashlib
import io
import posixpath
import stat
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET


W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
MC = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
NS = {'w': W, 'p': P, 'a': A, 'r': R}


class DocumentReadError(ValueError):
    def __init__(self, message, code='DOCUMENT_INVALID'):
        super().__init__(message)
        self.code = code


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _number(value, default=1, *, maximum=100000):
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        raise DocumentReadError('Office 文档中的行列坐标无效') from None
    if parsed < 0 or parsed > maximum:
        raise DocumentReadError('Office 文档中的行列坐标超出范围')
    return parsed


class _Blocks:
    def __init__(self, max_bytes, max_blocks):
        self.parts = []
        self.segments = []
        self.position = 0
        self.bytes = 0
        self.max_bytes = max_bytes
        self.max_blocks = max_blocks
        self.warnings = set()
        self.graphics = 0
        self.note_ids = {'footnotes': [], 'endnotes': []}

    def add(self, text, locator, kind, **metadata):
        if len(self.segments) >= self.max_blocks:
            raise DocumentReadError('文档块数超过读取预算', 'DOCUMENT_BUDGET_EXCEEDED')
        added = len(text.encode('utf-8')) + (2 if self.segments else 0)
        if self.bytes + added > self.max_bytes:
            raise DocumentReadError('提取文本超过读取预算', 'DOCUMENT_BUDGET_EXCEEDED')
        if self.segments:
            self.parts.append('\n\n')
            self.position += 2
        start = self.position
        self.parts.append(text)
        self.position += len(text)
        self.bytes += added
        self.segments.append(dict(start=start, end=self.position, locator=locator,
                                  kind=kind, text_sha256=_sha(text.encode()), **metadata))


class _Package:
    """Inspect a ZIP before reading bounded parts; never extract or fetch links."""
    def __init__(self, raw, *, max_archive_bytes, max_xml_bytes, max_entries):
        self.max_xml_bytes = max_xml_bytes
        try:
            self.archive = zipfile.ZipFile(io.BytesIO(raw))
            entries = self.archive.infolist()
        except (zipfile.BadZipFile, OSError):
            raise DocumentReadError('Office 文件不是有效的 OOXML ZIP') from None
        if len(entries) > max_entries or sum(item.file_size for item in entries) > max_archive_bytes:
            raise DocumentReadError('Office 解压体积或条目数超过预算', 'DOCUMENT_BUDGET_EXCEEDED')
        names = set()
        for item in entries:
            name = item.filename
            parts = PurePosixPath(name).parts
            if (name in names or not name or not parts or '\\' in name or name.startswith('/')
                    or '..' in parts or ':' in parts[0] or '\x00' in name
                    or stat.S_ISLNK(item.external_attr >> 16)):
                raise DocumentReadError('Office ZIP 包含重复或不安全路径')
            if item.flag_bits & 1:
                raise DocumentReadError('加密 Office 包需要可读取的授权原件', 'DOCUMENT_ENCRYPTED')
            if name.lower().endswith('vbaproject.bin'):
                raise DocumentReadError('不接受包含宏的 Office 文档', 'DOCUMENT_MACROS_UNSUPPORTED')
            names.add(name)
        self.names = names
        content_types = self.xml('[Content_Types].xml')
        if any('macroenabled' in value.lower() for node in content_types for value in node.attrib.values()):
            raise DocumentReadError('不接受包含宏的 Office 文档', 'DOCUMENT_MACROS_UNSUPPORTED')

    def close(self):
        self.archive.close()

    def xml(self, name):
        if name not in self.names:
            raise DocumentReadError('Office 文件缺少必要的 XML 部件')
        item = self.archive.getinfo(name)
        if item.file_size > self.max_xml_bytes:
            raise DocumentReadError('Office XML 部件超过读取预算', 'DOCUMENT_BUDGET_EXCEEDED')
        try:
            with self.archive.open(item) as stream:
                raw = stream.read(self.max_xml_bytes + 1)
        except (zipfile.BadZipFile, RuntimeError, OSError):
            raise DocumentReadError('Office ZIP 部件损坏') from None
        if len(raw) > self.max_xml_bytes:
            raise DocumentReadError('Office XML 部件超过读取预算', 'DOCUMENT_BUDGET_EXCEEDED')
        # DTDs/entities have no role in Office XML. Also reject UTF-16/32 XML
        # so the declaration check cannot be bypassed through embedded NULs.
        if b'\x00' in raw or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise DocumentReadError('Office XML 包含不受支持的编码、DTD 或实体')
        try:
            depth = nodes = 0
            parser = ET.iterparse(io.BytesIO(raw), events=('start', 'end'))
            for event, _ in parser:
                if event == 'start':
                    depth += 1
                    nodes += 1
                    if depth > 128 or nodes > 200000:
                        raise DocumentReadError('Office XML 深度或节点数超过预算', 'DOCUMENT_BUDGET_EXCEEDED')
                else:
                    depth -= 1
            root = parser.root
        except ET.ParseError:
            raise DocumentReadError('Office XML 结构损坏') from None
        return root

    def relationships(self, part, blocks):
        folder, basename = posixpath.split(part)
        rel_name = posixpath.join(folder, '_rels', basename + '.rels')
        if rel_name not in self.names:
            return {}
        result = {}
        for node in self.xml(rel_name):
            if node.tag != '{' + REL + '}Relationship':
                continue
            ident = node.get('Id')
            if not ident or ident in result:
                raise DocumentReadError('Office 关系标识重复或缺失')
            target = node.get('Target', '')
            if node.get('TargetMode', '').lower() == 'external':
                blocks.warnings.add('EXTERNAL_RELATIONSHIPS_NOT_FETCHED')
                result[ident] = {'type': node.get('Type', ''), 'external': True}
                continue
            # Package absolute targets are allowed, filesystem paths are not.
            if not target or '\\' in target or '?' in target or '#' in target or ':' in target:
                raise DocumentReadError('Office 关系路径无效')
            resolved = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(folder, target))
            if resolved.startswith('../') or resolved == '..' or resolved not in self.names:
                raise DocumentReadError('Office 关系目标缺失或超出包范围')
            result[ident] = {'target': resolved, 'type': node.get('Type', ''), 'external': False}
        return result


def _word_text(paragraph, blocks):
    # A modern Choice and its compatibility Fallback are alternatives. Reading
    # both can manufacture a new amount. Without rendering/feature negotiation,
    # omit the entire affected paragraph, including surrounding text, so digits
    # on either side of omitted content cannot be joined into a new number.
    if any(True for _ in paragraph.iter('{' + MC + '}AlternateContent')):
        blocks.warnings.add('ALTERNATE_CONTENT_PARAGRAPH_NOT_EXTRACTED_REQUIRES_VISUAL_REVIEW')
        return ''
    parts = []

    def visit(node):
        if node.tag in ('{' + W + '}del', '{' + W + '}moveFrom'):
            blocks.warnings.add('DELETED_OR_MOVED_FROM_TEXT_EXCLUDED')
            return
        if node.tag == '{' + W + '}txbxContent':
            blocks.warnings.add('TEXTBOX_LAYOUT_NOT_EXTRACTED')
            return
        if node.tag == '{' + W + '}r':
            props = node.find('w:rPr', NS)
            if props is not None:
                hidden = [props.find('w:' + key, NS) for key in ('vanish', 'webHidden')]
                if any(item is not None and item.get('{' + W + '}val', 'true').lower() not in ('0', 'false', 'off') for item in hidden):
                    blocks.warnings.add('EXPLICITLY_HIDDEN_TEXT_EXCLUDED')
                    return
        if node.tag == '{' + W + '}t':
            parts.append(node.text or '')
        elif node.tag == '{' + W + '}tab':
            parts.append('\t')
        elif node.tag in ('{' + W + '}br', '{' + W + '}cr'):
            parts.append('\n')
        elif node.tag in ('{' + W + '}instrText', '{' + W + '}fldChar', '{' + W + '}fldSimple'):
            blocks.warnings.add('OFFICE_FIELDS_NOT_EVALUATED')
        elif node.tag in ('{' + W + '}footnoteReference', '{' + W + '}endnoteReference'):
            name = 'footnotes' if node.tag.endswith('footnoteReference') else 'endnotes'
            ident = node.get('{' + W + '}id')
            if ident not in blocks.note_ids[name]:
                blocks.note_ids[name].append(ident)
        for child in node:
            visit(child)

    visit(paragraph)
    return ''.join(parts)


def _word_container(container, part, blocks, prefix, **context):
    paragraph_index = table_index = 0
    for child in container:
        if child.tag == '{' + W + '}p':
            paragraph_index += 1
            ppr = child.find('w:pPr', NS)
            metadata = dict(context, part=part, paragraph_index=paragraph_index)
            if ppr is not None:
                style = ppr.find('w:pStyle', NS)
                if style is not None:
                    metadata['style_id'] = style.get('{' + W + '}val')
                indent = ppr.find('w:ind', NS)
                if indent is not None:
                    metadata['indent_attributes'] = {key.split('}')[-1]: value for key, value in indent.attrib.items()}
                outline = ppr.find('w:outlineLvl', NS)
                if outline is not None:
                    metadata['outline_level'] = _number(outline.get('{' + W + '}val'), 0)
                num = ppr.find('w:numPr', NS)
                if num is not None:
                    level = num.find('w:ilvl', NS)
                    numbering = num.find('w:numId', NS)
                    metadata['list_level'] = _number(level.get('{' + W + '}val') if level is not None else None, 0)
                    metadata['numbering_id'] = numbering.get('{' + W + '}val') if numbering is not None else None
                    blocks.warnings.add('LIST_NUMBERING_AND_STYLE_NOT_RENDERED')
            blocks.add(_word_text(child, blocks), prefix + '/paragraph:' + str(paragraph_index),
                       'table_cell_paragraph' if 'table_index' in context else 'paragraph', **metadata)
        elif child.tag == '{' + W + '}tbl':
            table_index += 1
            _word_table(child, part, blocks, prefix + '/table:' + str(table_index), table_index, context)
        elif child.tag == '{' + W + '}sdt':
            content = child.find('w:sdtContent', NS)
            if content is not None:
                _word_container(content, part, blocks, prefix + '/content-control:' + str(len(blocks.segments) + 1), **context)
        elif child.tag in ('{' + W + '}ins', '{' + W + '}moveTo'):
            _word_container(child, part, blocks, prefix + '/tracked-insertion:' + str(len(blocks.segments) + 1), **context)
        elif child.tag in ('{' + W + '}del', '{' + W + '}moveFrom'):
            blocks.warnings.add('DELETED_OR_MOVED_FROM_TEXT_EXCLUDED')
        elif child.tag in ('{' + W + '}altChunk', '{' + W + '}customXml'):
            blocks.warnings.add('EMBEDDED_OR_CUSTOM_CONTENT_NOT_EXTRACTED')
        elif child.tag == '{' + MC + '}AlternateContent':
            blocks.warnings.add('ALTERNATE_CONTENT_NOT_EXTRACTED_REQUIRES_VISUAL_REVIEW')


def _word_table(table, part, blocks, prefix, table_index, context):
    active_merges = {}
    for row_index, row in enumerate(table.findall('w:tr', NS), 1):
        before = row.find('w:trPr/w:gridBefore', NS)
        column = 1 + _number(before.get('{' + W + '}val') if before is not None else None, 0)
        next_merges = {}
        for cell_index, cell in enumerate(row.findall('w:tc', NS), 1):
            span_node = cell.find('w:tcPr/w:gridSpan', NS)
            span = _number(span_node.get('{' + W + '}val') if span_node is not None else None)
            if not span:
                raise DocumentReadError('Office 表格的合并列数必须大于零')
            merge_node = cell.find('w:tcPr/w:vMerge', NS)
            merge = None if merge_node is None else merge_node.get('{' + W + '}val', 'continue')
            if merge not in (None, 'restart', 'continue'):
                raise DocumentReadError('Office 表格垂直合并标记无效')
            covered = {active_merges.get(col) for col in range(column, column + span)}
            anchor = row_index if merge == 'restart' else next(iter(covered)) if merge == 'continue' and len(covered) == 1 and None not in covered else None
            if merge == 'continue' and anchor is None:
                blocks.warnings.add('MERGE_CONTINUATION_WITHOUT_ANCHOR')
            if merge:
                for col in range(column, column + span):
                    next_merges[col] = anchor
            metadata = dict(context, table_index=table_index, row_index=row_index,
                            column_index=column, cell_index=cell_index, column_span=span,
                            vertical_merge=merge, vertical_merge_anchor_row=anchor)
            _word_container(cell, part, blocks, prefix + '/row:' + str(row_index) + '/column:' + str(column), **metadata)
            column += span
        active_merges = next_merges


def _docx(package, blocks):
    part = 'word/document.xml'
    root = package.xml(part)
    if root.tag != '{' + W + '}document':
        raise DocumentReadError('DOCX 主文档根节点无效')
    body = root.find('w:body', NS)
    if body is None:
        raise DocumentReadError('DOCX 缺少文档正文')
    blocks.graphics += sum(1 for node in root.iter() if node.tag in ('{' + W + '}drawing', '{' + W + '}pict', '{' + W + '}object'))
    _word_container(body, part, blocks, part, role='body')
    rels = package.relationships(part, blocks)
    seen = set()
    # Headers/footers are unique XML parts, not reconstructed physical pages.
    for node in root.iter():
        if node.tag not in ('{' + W + '}headerReference', '{' + W + '}footerReference'):
            continue
        ref = rels.get(node.get('{' + R + '}id'))
        role = 'header' if node.tag.endswith('headerReference') else 'footer'
        if not ref or ref.get('external') or not ref['type'].endswith('/' + role):
            raise DocumentReadError('DOCX 页眉页脚引用无效')
        target = ref['target']
        if target not in seen:
            seen.add(target)
            _word_container(package.xml(target), target, blocks, target, role=role)
            blocks.warnings.add('HEADER_FOOTER_PARTS_HAVE_NO_PHYSICAL_PAGE_MAPPING')
    for name, reference, element in [('footnotes', 'footnoteReference', 'footnote'), ('endnotes', 'endnoteReference', 'endnote')]:
        ids = list(blocks.note_ids[name])
        if not ids:
            continue
        ref = next((item for item in rels.values() if not item.get('external') and item['type'].endswith('/' + name)), None)
        if ref is None:
            raise DocumentReadError('DOCX 脚注或尾注引用缺失')
        notes = {node.get('{' + W + '}id'): node for node in package.xml(ref['target']).findall('w:' + element, NS)}
        for ident in ids:
            if ident not in notes:
                raise DocumentReadError('DOCX 引用的脚注或尾注缺失')
            _word_container(notes[ident], ref['target'], blocks, ref['target'] + '/note:' + str(ident), role=name, note_id=ident)
    blocks.warnings.add('OFFICE_XML_ORDER_NOT_RENDERED_PAGE_LAYOUT')
    return {'parser': 'atlas_docx_xml', 'representation': 'docx_xml_text_and_cells; physical_pages_and_graphics_not_verified'}


def _drawing_paragraphs(container, blocks, prefix, **metadata):
    for index, paragraph in enumerate(container.findall('a:p', NS), 1):
        parts = []
        if any(True for _ in paragraph.iter('{' + MC + '}AlternateContent')):
            blocks.warnings.add('ALTERNATE_CONTENT_PARAGRAPH_NOT_EXTRACTED_REQUIRES_VISUAL_REVIEW')
        else:
            for node in paragraph.iter():
                if node.tag == '{' + A + '}t':
                    parts.append(node.text or '')
                elif node.tag == '{' + A + '}br':
                    parts.append('\n')
                elif node.tag == '{' + A + '}tab':
                    parts.append('\t')
        text = ''.join(parts)
        if any(True for _ in paragraph.iter('{' + A + '}fld')):
            blocks.warnings.add('OFFICE_FIELDS_NOT_EVALUATED')
        props = paragraph.find('a:pPr', NS)
        current = dict(metadata, paragraph_index=index)
        if props is not None and props.get('lvl') is not None:
            current['list_level'] = _number(props.get('lvl'), 0)
        if props is not None:
            current['paragraph_attributes'] = {key: props.get(key) for key in ('lvl', 'marL', 'indent', 'algn') if props.get(key) is not None}
            bullet = props.find('a:buChar', NS)
            numbering = props.find('a:buAutoNum', NS)
            if bullet is not None:
                current['bullet_character'] = bullet.get('char')
            if numbering is not None:
                current['numbering_attributes'] = dict(numbering.attrib)
                blocks.warnings.add('LIST_NUMBERING_AND_STYLE_NOT_RENDERED')
        blocks.add(text, prefix + '/paragraph:' + str(index),
                   'table_cell_paragraph' if 'table_index' in metadata else 'slide_paragraph', **current)


def _pptx(package, blocks, max_pages):
    part = 'ppt/presentation.xml'
    root = package.xml(part)
    if root.tag != '{' + P + '}presentation':
        raise DocumentReadError('PPTX 演示文稿根节点无效')
    rels = package.relationships(part, blocks)
    slide_ids = root.findall('p:sldIdLst/p:sldId', NS)
    if len(slide_ids) > max_pages:
        raise DocumentReadError('幻灯片数超过读取预算', 'DOCUMENT_BUDGET_EXCEEDED')
    seen = set()
    for slide_index, ident in enumerate(slide_ids, 1):
        ref = rels.get(ident.get('{' + R + '}id'))
        if not ref or ref.get('external') or not ref['type'].endswith('/slide') or ref['target'] in seen:
            raise DocumentReadError('PPTX 幻灯片顺序或引用无效')
        target = ref['target']; seen.add(target)
        slide = package.xml(target)
        if slide.tag != '{' + P + '}sld':
            raise DocumentReadError('PPTX 幻灯片根节点无效')
        hidden = slide.get('show', '1') in ('0', 'false')
        if hidden:
            blocks.warnings.add('HIDDEN_SLIDES_INCLUDED_WITH_METADATA')
        tree = slide.find('p:cSld/p:spTree', NS)
        if tree is None:
            raise DocumentReadError('PPTX 幻灯片缺少形状树')
        table_index = 0

        def shapes(container):
            for shape in container:
                if shape.tag == '{' + P + '}grpSp':
                    yield from shapes(shape)
                elif shape.tag == '{' + MC + '}AlternateContent':
                    blocks.warnings.add('ALTERNATE_CONTENT_NOT_EXTRACTED_REQUIRES_VISUAL_REVIEW')
                elif shape.tag in ('{' + P + '}sp', '{' + P + '}graphicFrame', '{' + P + '}pic'):
                    yield shape

        for shape_index, shape in enumerate(shapes(tree), 1):
            props = shape.find('.//p:cNvPr', NS)
            shape_id = props.get('id') if props is not None else str(shape_index)
            if props is not None and props.get('hidden', '0') in ('1', 'true'):
                blocks.warnings.add('EXPLICITLY_HIDDEN_SHAPES_EXCLUDED')
                continue
            base = dict(part=target, slide_index=slide_index, slide_hidden=hidden,
                        shape_index=shape_index, shape_id=shape_id)
            locator = target + '/slide:' + str(slide_index) + '/shape:' + str(shape_index)
            text_body = shape.find('p:txBody', NS)
            if text_body is not None:
                _drawing_paragraphs(text_body, blocks, locator, **base)
            tables = shape.findall('.//a:tbl', NS)
            for table in tables:
                table_index += 1
                for row_index, row in enumerate(table.findall('a:tr', NS), 1):
                    for column_index, cell in enumerate(row.findall('a:tc', NS), 1):
                        # DrawingML merge placeholders are physical grid cells;
                        # preserve raw coordinates/flags instead of guessing fill.
                        merge = {key: cell.get(key) for key in ('gridSpan', 'rowSpan', 'hMerge', 'vMerge') if cell.get(key) is not None}
                        body = cell.find('a:txBody', NS)
                        if body is not None:
                            _drawing_paragraphs(body, blocks, locator + '/table:' + str(table_index) + '/row:' + str(row_index) + '/column:' + str(column_index),
                                                **base, table_index=table_index, row_index=row_index,
                                                column_index=column_index, merge_attributes=merge)
            if shape.tag == '{' + P + '}pic' or (shape.tag == '{' + P + '}graphicFrame' and not tables):
                blocks.graphics += 1
        srels = package.relationships(target, blocks)
        if any(item['type'].endswith('/notesSlide') for item in srels.values()):
            blocks.warnings.add('SPEAKER_NOTES_NOT_EXTRACTED')
    blocks.warnings.update(['OFFICE_XML_ORDER_NOT_RENDERED_READING_ORDER', 'SLIDE_MASTER_AND_LAYOUT_TEXT_NOT_EXTRACTED'])
    return {'parser': 'atlas_pptx_xml', 'representation': 'pptx_slide_xml_text_and_cells; layout_and_graphics_not_verified', 'slide_count': len(slide_ids)}


def _pdf(raw, blocks, max_pages):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise DocumentReadError('PDF 文字层读取依赖 pypdf 不可用', 'DOCUMENT_DEPENDENCY_UNAVAILABLE') from None
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise DocumentReadError('加密 PDF 需要可读取的授权原件', 'DOCUMENT_ENCRYPTED')
        if len(reader.pages) > max_pages:
            raise DocumentReadError('PDF 页数超过读取预算', 'DOCUMENT_BUDGET_EXCEEDED')
        for index, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ''
            blocks.add(text, 'page:' + str(index), 'page', page_index=index)
            if not text.strip():
                blocks.warnings.add('PAGES_WITHOUT_TEXT_LAYER_REQUIRE_VISUAL_REVIEW')
    except DocumentReadError:
        raise
    except Exception as exc:
        raise DocumentReadError('PDF 文字层损坏或无法读取') from exc
    blocks.warnings.update(['PDF_TABLE_GEOMETRY_NOT_RECONSTRUCTED', 'PDF_GRAPHICS_NOT_INTERPRETED'])
    return {'parser': 'pypdf', 'representation': 'pdf_text_layer; layout_and_graphics_not_verified', 'page_count': len(reader.pages)}


def read_document(path, *, max_file_bytes=25 * 1024 * 1024, max_text_bytes=10 * 1024 * 1024,
                  max_blocks=100000, max_pages=2000, max_archive_bytes=200 * 1024 * 1024,
                  max_xml_bytes=16 * 1024 * 1024, max_entries=10000):
    """Read text evidence, or return explicit OCR_REQUIRED for textless sources.

    Budget errors reject the whole extraction rather than returning partial data.
    PDF budgets bound input, page count and extracted output; the pypdf decoder
    runs in process, so deployments still need worker memory/time limits for
    untrusted PDFs. OOXML XML input sizes and archive expansion are also bounded.
    """
    budgets = (max_file_bytes, max_text_bytes, max_blocks, max_pages, max_archive_bytes, max_xml_bytes, max_entries)
    if any(type(value) is not int or value < 1 for value in budgets):
        raise ValueError('文档读取预算必须为正整数')
    path = Path(path)
    if path.stat().st_size > max_file_bytes:
        raise DocumentReadError('文档超过文件体积预算', 'DOCUMENT_BUDGET_EXCEEDED')
    with path.open('rb') as stream:
        raw = stream.read(max_file_bytes + 1)
    if len(raw) > max_file_bytes:
        raise DocumentReadError('文档超过文件体积预算', 'DOCUMENT_BUDGET_EXCEEDED')
    suffix = path.suffix.lower()
    blocks = _Blocks(max_text_bytes, max_blocks)
    metadata = {'parser': 'none', 'representation': 'unsupported_binary_document'}
    supported_format = suffix in ('.docx', '.pptx', '.pdf', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.webp')
    if suffix in ('.docx', '.pptx'):
        package = _Package(raw, max_archive_bytes=max_archive_bytes, max_xml_bytes=max_xml_bytes, max_entries=max_entries)
        try:
            metadata = _docx(package, blocks) if suffix == '.docx' else _pptx(package, blocks, max_pages)
        finally:
            package.close()
    elif suffix == '.pdf':
        metadata = _pdf(raw, blocks, max_pages)
    elif supported_format:
        metadata['representation'] = 'image_source; no_ocr_executed'
        blocks.graphics = 1
    text = ''.join(blocks.parts)
    supported = bool(text.strip())
    if blocks.graphics:
        blocks.warnings.add('GRAPHICS_NOT_INTERPRETED')
    code = None if supported else 'OCR_REQUIRED' if supported_format else 'DOCUMENT_FORMAT_UNSUPPORTED'
    return dict(metadata, text=text, segments=blocks.segments, supported=supported,
                unsupported_reason=None if supported else 'no_readable_text_layer_requires_ocr_or_source_export_and_visual_review',
                error_code=code, source_file_sha256=_sha(raw), source_text_sha256=_sha(text.encode()),
                offset_unit='extracted_unicode_codepoints', graphics_verified=False, ocr_executed=False,
                graphics_count=blocks.graphics, warnings=sorted(blocks.warnings),
                complete_within_supported_scope=True, semantic_facts_verified=False)
