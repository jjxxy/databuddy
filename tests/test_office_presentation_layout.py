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
           if s.has_text_frame and s.name.startswith('atlas:text:')]
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
