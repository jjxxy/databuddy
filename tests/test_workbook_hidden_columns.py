"""Hidden Excel columns remain intact and are never silently trusted."""
from decimal import Decimal
import xml.etree.ElementTree as ET
import zipfile

import pytest
from openpyxl import Workbook

from agent_service import project_data
from agent_service.workbook_reader import WorkbookReadError, inspect_workbook, read_table
from test_project_data import project, registered, run
from test_source_semantic_review_guard import file_source


def workbook(tmp_path):
    book=Workbook();sheet=book.active;sheet.title='收入'
    sheet.append(['法人','正式金额','草稿金额','备注'])
    sheet.append(['甲',10,999,'需核对']);sheet.append(['乙',20,888,''])
    sheet.column_dimensions.group('C','D',hidden=True)
    path=tmp_path/'income.xlsx';book.save(path);book.close()
    return path


def test_hidden_excel_columns_in_selected_range_are_retained_and_flagged(tmp_path):
    path=workbook(tmp_path)
    metadata=inspect_workbook(path)['sheets'][0]
    assert (3,4,True) in metadata['column_visibility']
    result=read_table(path,sheet='收入',cell_range='A1:D3',header_rows=[1])
    assert result['hidden_columns']==['C','D']
    assert result['semantic_review_required'] is True
    assert result['rows'][0]['c_2']==Decimal(10) and result['rows'][0]['c_3']==Decimal(999)
    assert result['fields'][2]['hidden_in_source'] is True
    assert result['fields'][2]['semantic_status']=='requires_review'
    assert result['provenance']['columns']['c_3']['hidden_in_source'] is True
    assert any(w['code']=='HIDDEN_COLUMNS_PRESENT' for w in result['warnings'])
    # A truly excluded hidden column is not confused with a selected one.
    visible=read_table(path,sheet='收入',cell_range='A1:B3',header_rows=[1])
    assert 'hidden_columns' not in visible and not visible.get('semantic_review_required',False)


def test_hidden_columns_force_source_and_semantic_review_in_business_input(tmp_path):
    path=workbook(tmp_path);pid=project()
    source=file_source(pid,'income.xlsx',path.read_bytes())
    rows,fields,evidence,provenance=project_data.read_input(pid,{
        'source_id':source['id'], 'options':{'sheet':'收入','cell_range':'A1:D3','header_rows':[1]}})
    assert len(rows)==2 and rows[0]['c_3']==Decimal(999)
    assert fields[2]['hidden_in_source'] is True
    assert evidence['hidden_columns']==['C','D']
    assert evidence['requires_source_review'] is True and evidence['requires_semantic_review'] is True
    assert 'HIDDEN_COLUMNS_PRESENT' in evidence['semantic_review_reasons']
    assert 'AUTO_LAYOUT_REVIEW_REQUIRED' not in evidence['semantic_review_reasons']
    assert provenance['selection']['sheet']=='收入'


def test_conflicting_visibility_declarations_cannot_silently_guess(tmp_path):
    path=workbook(tmp_path);part='xl/worksheets/sheet1.xml'
    with zipfile.ZipFile(path) as archive:files={name:archive.read(name) for name in archive.namelist()}
    root=ET.fromstring(files[part]);ns='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    columns=root.find(ns+'cols');ET.SubElement(columns,ns+'col',{'min':'3','max':'3','hidden':'0'})
    files[part]=ET.tostring(root)
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,content in files.items():archive.writestr(name,content)
    with pytest.raises(WorkbookReadError,match='可见性声明冲突'):
        read_table(path,sheet='收入',cell_range='A1:D3',header_rows=[1])


def test_thousands_of_full_width_overlapping_groups_are_checked_without_expansion(tmp_path):
    path=workbook(tmp_path);part='xl/worksheets/sheet1.xml'
    with zipfile.ZipFile(path) as archive:files={name:archive.read(name) for name in archive.namelist()}
    root=ET.fromstring(files[part]);ns='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    columns=root.find(ns+'cols');columns.clear()
    # Many broad declarations are valid input for the scanner. The final
    # declaration conflicts on B and must be caught without iterating all
    # 16,384 columns once per range or losing the competing visibility.
    for _ in range(16000):ET.SubElement(columns,ns+'col',{'min':'1','max':'16384','hidden':'1'})
    ET.SubElement(columns,ns+'col',{'min':'2','max':'2','hidden':'0'})
    files[part]=ET.tostring(root)
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,content in files.items():archive.writestr(name,content)
    with pytest.raises(WorkbookReadError,match='可见性声明冲突'):
        read_table(path,sheet='收入',cell_range='A1:D3',header_rows=[1])


def test_hidden_column_result_cannot_be_adopted_without_bound_business_review(tmp_path):
    path=workbook(tmp_path);pid=project();source=file_source(pid,'income.xlsx',path.read_bytes())
    def col(name,kind='text',**extra):return {'name':name,'data_type':kind,'meaning':name,**extra}
    document={'version':1,'plan':{'name':'核对隐藏列','inputs':{'input_table':{
        'columns':[col('entity'),col('actual','decimal',unit='CNY',aggregation='additive'),
                   col('draft','decimal',unit='CNY',aggregation='additive'),col('note')],
        'grain':['entity'],'meaning':'来源记录'}},
        'stages':[{'id':'full','kind':'select','description':'保留完整字段核对',
                   'input':'input_table','columns':['entity','actual','draft','note']}], 'output':'full'},
        'bindings':{'input_table':{'source_id':source['id'],
             'options':{'sheet':'收入','cell_range':'A1:D3','header_rows':[1]},
             'column_map':{'c_1':'entity','c_2':'actual','c_3':'draft','c_4':'note'}}}}
    result=run(pid,registered(pid,document))
    assert result['status']=='validated'
    assert result['data_result']['requires_source_review'] is True
    assert result['data_result']['requires_semantic_review'] is True
    sha=result['data_result']['handoff_sha256']
    with pytest.raises(ValueError,match='工作台'):
        project_data.publish_result(pid,result['id'],result['commit'])
    with pytest.raises(ValueError,match='语义'):
        project_data.publish_result(pid,result['id'],result['commit'],review_actor='owner',review_handoff_sha256=sha)
    adopted=project_data.publish_result(pid,result['id'],result['commit'],review_actor='owner',
                                         semantic_review_actor='owner',review_handoff_sha256=sha)
    assert adopted['published_run']==result['id']
