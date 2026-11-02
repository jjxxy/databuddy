from pptx import Presentation
from pptx.util import Pt
import pytest

from agent_service.office import Spec
from agent_service.office_presentation import build, verify


@pytest.mark.parametrize('count',[7,8,13,19])
def test_finance_tables_are_balanced_readable_and_keep_exact_values(tmp_path,count):
    rows=[[f'区域 {i}',f'{i}.01'] for i in range(count)]
    spec=Spec.model_validate({'kind':'presentation','title':'经营汇报','sections':[
        {'heading':'收入明细','table':{'columns':['区域','金额（元）'],'rows':rows}}]})
    file=tmp_path/'review.pptx';build(spec,file);deck=Presentation(file);verify(spec,deck)
    table_slides=[shape.table for slide in deck.slides for shape in slide.shapes if shape.has_table]
    assert all(2<=len(table.rows)-1<=6 for table in table_slides)
    actual=[[table.cell(r,c).text for c in range(2)] for table in table_slides for r in range(1,len(table.rows))]
    assert actual==rows
    assert all(table.cell(1,0).text_frame.paragraphs[0].font.size>=Pt(14) for table in table_slides)
