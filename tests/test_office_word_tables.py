"""Actual editable Word tables preserve all supplied values and version identity."""
from pathlib import Path
import hashlib
import json

import pytest
from docx import Document

from agent_service import office, business_deliverables as delivery, workspace_artifacts
from tests.test_business_deliverables import execution


def spec():
    return {'kind':'document','title':'经营报告','sections':[
        {'heading':'收入','body':'来源已核对\n金额单位：元', 'bullets':['不含税'],
         'table':{'columns':['区域','金额','说明'], 'rows':[
             ['东部','9007199254740993.123456789012','首行\n续行'],
             ['西部','-118.15','=SUM(A1:A2)'],
             ['','0','<script>alert(1)</script>']]}},
        {'heading':'补充','body':'请人工核对', 'table':{'columns':['指标'], 'rows':[]}}]}


def test_word_tables_round_trip_as_editable_exact_content(tmp_path):
    value=office.Spec.model_validate(spec());path=tmp_path/'report.docx'
    office._build_file(value,path,[])
    document=Document(path)
    assert [p.text for p in document.paragraphs]==['经营报告','收入','来源已核对','金额单位：元','不含税','补充','请人工核对']
    assert len(document.tables)==2
    assert [[c.text for c in row.cells] for row in document.tables[0].rows]==[
        ['区域','金额','说明'],['东部','9007199254740993.123456789012','首行\n续行'],
        ['西部','-118.15','=SUM(A1:A2)'],['','0','<script>alert(1)</script>']]
    assert [[c.text for c in row.cells] for row in document.tables[1].rows]==[['指标']]
    assert document.tables[0].rows[0]._tr.trPr.find('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tblHeader') is not None
    # Real cells are directly editable using standard Word OOXML tooling.
    document.tables[0].cell(1,1).text='9007199254740993.123456789013'
    document.save(path)
    assert Document(path).tables[0].cell(1,1).text=='9007199254740993.123456789013'
    with pytest.raises(ValueError,match='结构或完整性'):
        office._verify_structure(path,'docx',value)


def test_word_table_cells_reject_unsupported_control_characters(tmp_path):
    payload=spec();payload['sections'][0]['table']['rows'][0][0]='east\rwest'
    with pytest.raises(ValueError,match='控制字符'):
        office._build_file(office.Spec.model_validate(payload),tmp_path/'control.docx',[])


def test_table_data_requires_all_rows_and_fields():
    payload=spec();payload['sections'][0]['table']['rows'][0].pop()
    with pytest.raises(ValueError):office.Spec.model_validate(payload)


def test_versioned_business_flow_word_table_and_legacy_behavior(execution):
    payload=spec();result=delivery.render_office(execution,{'name':'经营报告','renderer_version':office.RENDERER},{'spec':payload})
    assert result['renderer_version']==office.RENDERER and result['visual_validation'] is False and result['financial_verified'] is False
    path,record=workspace_artifacts.file(execution['project_id'],result['artifact_id'])
    assert record['sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    assert Document(path).tables[0].cell(2,1).text=='-118.15'
    with pytest.raises(ValueError,match='结构化章节'):
        delivery.render_office(execution,{'name':'历史报告','renderer_version':office.LEGACY_RENDERER},{'spec':payload})
    # Existing v5 reports without tables remain supported and keep their version.
    legacy={'kind':'document','title':'历史报告','sections':[{'body':'旧版正文'}]}
    output=delivery.render_office(execution,{'name':'历史报告','renderer_version':office.LEGACY_RENDERER},{'spec':legacy})
    assert output['renderer_version']==office.LEGACY_RENDERER
    old_path,_=workspace_artifacts.file(execution['project_id'],output['artifact_id'])
    assert [p.text for p in Document(old_path).paragraphs]==['历史报告','旧版正文']


def test_maximum_table_rows_and_columns_are_not_truncated(tmp_path):
    columns=['字段'+str(i) for i in range(20)]
    rows=[[f'行{r}-列{c}' for c in range(20)] for r in range(100)]
    value=office.Spec.model_validate({'kind':'document','title':'明细',
        'sections':[{'heading':'完整明细','table':{'columns':columns,'rows':rows}}]})
    path=tmp_path/'complete.docx';office._build_file(value,path,[])
    table=Document(path).tables[0]
    assert len(table.rows)==101 and len(table.columns)==20
    assert [cell.text for cell in table.rows[0].cells]==columns
    assert [cell.text for cell in table.rows[-1].cells]==rows[-1]


def test_word_table_moved_to_wrong_section_fails_verification(tmp_path):
    value=office.Spec.model_validate(spec());path=tmp_path/'ordering.docx'
    office._build_file(value,path,[])
    document=Document(path)
    table=document.tables[0]._tbl
    document.paragraphs[-1]._p.addnext(table)
    document.save(path)
    with pytest.raises(ValueError,match='结构或完整性'):
        office._verify_structure(path,'docx',value)
