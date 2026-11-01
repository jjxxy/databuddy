from pptx import Presentation
from pptx.util import Inches
import pytest
from agent_service import business_reports as reports


def manifest():
    return {'recipe': {'title': '年度经营简报', 'period': '2025-12', 'assumptions': ['业务假设' * 230]},
            'run_id': 'run_123', 'source_reference': '/api/projects/p/data-results/run_123',
            'complete_handoff_reference': '/api/projects/p/data-results/run_123/download?format=handoff',
            'handoff_sha256': 'a' * 64, 'plan_sha256': 'b' * 64,
            'evaluation': {'input_rows': 100001, 'selected_rows': 99999, 'schema_sha256': 'c' * 64,
                'assertions': [{'id': f'm{i}', 'label': '净收入' + str(i), 'kind': 'sum',
                    'value': '9007199254740993.123456789012', 'unit': '元', 'rows': 99999,
                    'columns': ['amount'], 'filters': {'region': '华东'}, 'rounding_mode': 'exact_only'}
                    for i in range(9)]}}


def test_financial_ppt_preserves_editable_values_and_complete_evidence(tmp_path):
    data = manifest()
    path = tmp_path / 'brief.pptx'
    reports._write_pptx(data, path)
    reports._verify_office_values(data, path, 'pptx')
    deck = Presentation(path)
    tables = [shape.table for slide in deck.slides for shape in slide.shapes if shape.has_table]
    assert len(tables) == 2
    assert [[cell.text for cell in row.cells] for table in tables for row in list(table.rows)[1:]] == reports._assertion_rows(data)
    texts = [shape.text for slide in deck.slides for shape in slide.shapes if shape.has_text_frame]
    combined = ''.join(texts)
    assumption_lines = [shape.text for slide in deck.slides
                        if any(shape.has_text_frame and shape.text == '用户声明的假设' for shape in slide.shapes)
                        for shape in slide.shapes if shape.has_text_frame and Inches(1.6) <= shape.top < Inches(6.8)]
    assert ''.join(assumption_lines) == '以下内容未作为事实核实。' + data['recipe']['assumptions'][0]
    assert data['handoff_sha256'] in combined and data['plan_sha256'] in combined
    assert data['source_reference'] in combined and '未经业务确认' in combined
    assert '不舍入，要求精确' in combined and '华东' in combined
    tables[0].cell(1, 1).text = '0'
    deck.save(path)
    with pytest.raises(ValueError, match='不一致'):
        reports._verify_office_values(data, path, 'pptx')


def test_unrenderable_table_value_fails_explicitly(tmp_path):
    data = manifest()
    data['evaluation']['assertions'][0]['value'] = '9' * 10000
    with pytest.raises(ValueError, match='无法完整呈现'):
        reports._write_pptx(data, tmp_path / 'brief.pptx')


def test_wide_characters_and_long_exact_values_get_conservative_row_height(tmp_path):
    data = manifest()
    data['evaluation']['assertions'] = [data['evaluation']['assertions'][0]]
    data['evaluation']['assertions'][0].update(label='W' * 110 + '中文',
                                               value='9' * 95 + '.123456789012')
    path = tmp_path / 'wide.pptx'
    reports._write_pptx(data, path)
    reports._verify_office_values(data, path, 'pptx')
    table = next(shape.table for slide in Presentation(path).slides
                 for shape in slide.shapes if shape.has_table)
    # Each long W is treated as at least one full-size Chinese glyph.
    assert table.rows[1].height >= Inches(.29 * 8)
    assert table.cell(1, 0).text == 'W' * 110 + '中文'
    assert table.cell(1, 1).text == '9' * 95 + '.123456789012'
