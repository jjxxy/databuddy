from pathlib import Path

import pytest
from pydantic import ValidationError
from pptx import Presentation

from agent_service import office


def make_spec(**section):
    return office.Spec.model_validate({'kind': 'presentation', 'title': '收入汇报', 'sections': [section]})


def render(spec, path):
    office._build_file(spec, path, [])
    office._verify_structure(path, 'pptx', spec)
    return Presentation(path)


def test_long_text_is_complete_and_paginated(tmp_path):
    body='\n'.join(f'第 {i} 项：核实数据来源与金额。' for i in range(30))
    spec=make_spec(heading='待办', body=body)
    deck=render(spec,tmp_path/'report.pptx')
    assert len(deck.slides)==4
    texts=[s.text for slide in list(deck.slides)[1:] for s in slide.shapes
           if s.has_text_frame and s.name.startswith('nexus:text:')]
    assert texts==body.split('\n')


def test_wide_and_tall_table_exact_cells_and_repeated_identifier(tmp_path):
    amount='9007199254740993.123456789012'
    columns=['组织', '金额', '退款', '收入', '同比', '备注']
    rows=[[f'部门{i}',amount,'0.10','100.00','0.1%','核对'] for i in range(9)]
    spec=make_spec(heading='明细',table={'columns':columns,'rows':rows})
    path=tmp_path/'report.pptx'; deck=render(spec,path)
    tables=[s.table for slide in deck.slides for s in slide.shapes if s.has_table]
    assert len(tables)>=4
    for table in tables:
        assert table.cell(0,0).text=='组织'
        assert all(table.cell(r,0).text.startswith('部门') for r in range(1,len(table.rows)))
    assert sum(cell.text==amount for table in tables for row in table.rows for cell in row.cells)==9
    tables[0].cell(1,1).text='篡改'
    deck.save(path)
    with pytest.raises(ValueError,match='完整性验证失败'):office._verify_structure(path,'pptx',spec)


def test_never_silently_truncates_extreme_table_or_allows_incomplete_rows(tmp_path):
    with pytest.raises(ValidationError):make_spec(table={'columns':['a','b'],'rows':[['1']]})
    with pytest.raises(ValidationError):make_spec(table={'columns':['a'],'rows':[['1']]*101})
    spec=make_spec(table={'columns':['a','b'],'rows':[['1','9'*200]]})
    with pytest.raises(ValueError,match='未截断'):office._build_file(spec,tmp_path/'report.pptx',[])


def test_editable_chart_keeps_original_numeric_text_and_units(tmp_path):
    spec=make_spec(heading='收入对比',chart={'title':'各区域收入','unit':'万元',
        'points':[{'label':'华东','value':'1234.50'},{'label':'华南','value':'50.25'},
                  {'label':'华北','value':'0.00'}]})
    path=tmp_path/'chart.pptx';deck=render(spec,path)
    chart_shape=next(s for slide in deck.slides for s in slide.shapes if s.has_chart)
    assert [c.label for c in chart_shape.chart.plots[0].categories]==['华东','华南','华北']
    assert chart_shape.chart.category_axis.reverse_order is True
    assert list(chart_shape.chart.series[0].values)==[1234.5,50.25,0.0]
    labels={s.name:s.text for s in deck.slides[-1].shapes if s.has_text_frame}
    assert labels['nexus:chart-value:0:0']=='1234.50'
    assert labels['nexus:chart-value:0:2']=='0.00'
    assert labels['nexus:chart-unit:0']=='原始数值 · 万元'
    next(s for s in deck.slides[-1].shapes if s.name=='nexus:chart-value:0:0').text='1'
    deck.save(path)
    with pytest.raises(ValueError,match='完整性验证失败'):office._verify_structure(path,'pptx',spec)


def test_chart_rejects_unrepresentable_financial_values_or_ambiguous_categories():
    for value in ('-1','1234567890123456','0.1234567890123456','1,000','NaN'):
        with pytest.raises(ValidationError):
            make_spec(chart={'title':'金额','unit':'元','points':[{'label':'华东','value':value}]})
    with pytest.raises(ValidationError):
        make_spec(chart={'title':'金额','unit':'元','points':[{'label':'华东','value':'1'},{'label':'华东','value':'2'}]})
    with pytest.raises(ValidationError):
        make_spec(chart={'title':'金额','unit':'元','points':[{'label':'=SUM(1,1)','value':'1'}]})
    with pytest.raises(ValidationError):
        make_spec(chart={'title':'=SUM(1,1)','unit':'元','points':[{'label':'华东','value':'1'}]})


def test_chart_rejects_different_numbers_in_its_embedded_workbook(tmp_path):
    from io import BytesIO
    from openpyxl import load_workbook
    spec=make_spec(chart={'title':'收入','unit':'元','points':[{'label':'华东','value':'100.50'}]})
    path=tmp_path/'chart.pptx';deck=render(spec,path)
    chart=next(s.chart for slide in deck.slides for s in slide.shapes if s.has_chart)
    part=chart.part.chart_workbook.xlsx_part
    book=load_workbook(BytesIO(part.blob));book.active['B2']=999
    data=BytesIO();book.save(data);book.close();part._blob=data.getvalue()
    deck.save(path)
    with pytest.raises(ValueError,match='完整性验证失败'):office._verify_structure(path,'pptx',spec)
