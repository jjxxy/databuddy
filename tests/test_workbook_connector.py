"""Existing source ingestion retains enhanced XLSX semantics through adapters."""
import hashlib
from pathlib import Path
import shutil

import pytest
from openpyxl import Workbook

from agent_service.config import DATA, MAX_ROWS
from agent_service.connectors import SourceError, read_source
from agent_service.store import seal, uid


def source(tmp_path, rows, *, options=None, sheet=None, second_sheet=False):
    book=Workbook();ws=book.active;ws.title='数据'
    for row in rows:ws.append(row)
    if second_sheet:
        other=book.create_sheet('其他');other.append(['字段']);other.append(['值'])
    file=tmp_path/(uid()+'.xlsx');book.save(file)
    uploads=DATA/'uploads';uploads.mkdir(exist_ok=True)
    shutil.copy2(file,uploads/file.name)
    cfg={'stored_name':file.name}
    if options is not None:cfg['xlsx_options']=options
    if sheet is not None:cfg['sheet']=sheet
    return {'id':'s_'+uid(),'kind':'file','name':'source.xlsx','secret_config':seal(cfg),
            'content_hash':hashlib.sha256(file.read_bytes()).hexdigest()},file


def test_plain_excel_keeps_normalized_names_and_rich_field_context(tmp_path):
    s,file=source(tmp_path,[['客户','金额(万元)'],['000123',2]])
    rows,fields,evidence,truncated=read_source(s)
    assert not truncated and rows[0]['c_1']=='000123' and rows[0]['c_2']=='2'
    assert len(rows[0]['record_hash'])==64 and rows[0]['_source_index']==1
    assert fields[0]['name']=='c_1' and fields[0]['excel_column']=='A'
    assert fields[1]['unit']=='万元' and fields[1]['header_path']==['金额(万元)']
    assert evidence['table_id'] and evidence['sheet']=='数据' and evidence['sheets']==['数据']
    assert evidence['source_rows']==[2] and evidence['complete']
    assert evidence['provenance']['file_sha256']==hashlib.sha256(file.read_bytes()).hexdigest()
    assert evidence['provenance']['columns']['c_2']['excel_column']=='B'
    assert '_normalized_fields' not in evidence


def test_legacy_first_sheet_is_explicitly_disclosed(tmp_path):
    s,file=source(tmp_path,[['金额'],[10]],second_sheet=True)
    rows,fields,evidence,truncated=read_source(s,limit=500)
    assert evidence['sheet']=='数据'
    assert any(w['code']=='LEGACY_FIRST_SHEET_SELECTED' for w in evidence['warnings'])
    assert evidence['sheets']==['数据','其他'] and len(rows)==1


def test_nested_read_options_sheet_overrides_legacy_default(tmp_path):
    s,_=source(tmp_path,[['金额'],[10]],options={'sheet':'其他'},sheet='数据',second_sheet=True)
    rows,fields,evidence,truncated=read_source(s)
    assert evidence['sheet']=='其他' and fields[0]['label']=='字段' and rows[0]['c_1']=='值'


def test_connector_multilevel_header_paths_and_units_survive(tmp_path):
    book=Workbook();ws=book.active;ws.title='底稿'
    ws['A1']='区域';ws.merge_cells('A1:A2');ws['B1']='2026年';ws.merge_cells('B1:C1')
    ws['B2']='金额(万元)';ws['C2']='同比';ws.append(['华东',2,0.125]);ws['C3'].number_format='0.0%'
    file=tmp_path/(uid()+'.xlsx');book.save(file)
    uploads=DATA/'uploads';uploads.mkdir(exist_ok=True);shutil.copy2(file,uploads/file.name)
    s={'kind':'file','id':'s_'+uid(),'name':'bottom.xlsx','secret_config':seal({'stored_name':file.name,'xlsx_options':{'sheet':'底稿','header_rows':[1,2]}}),
       'content_hash':hashlib.sha256(file.read_bytes()).hexdigest()}
    rows,fields,evidence,_=read_source(s)
    assert fields[1]['label']=='2026年 / 金额(万元)' and fields[1]['unit']=='万元'
    assert fields[2]['stored_semantics']=='ratio' and rows[0]['c_3']=='0.125'
    assert fields[2]['header_coordinates'][0]['source_coordinate']=='B1'
    assert evidence['provenance']['source_rows']==[3]


def test_formula_probe_is_incomplete_and_full_read_fails(tmp_path):
    s,_=source(tmp_path,[['金额','两倍'],[10,'=A2*2']])
    rows,fields,evidence,truncated=read_source(s,limit=500)
    assert rows[0]['c_2'] is None and not evidence['complete']
    assert not evidence['executable_values_complete']
    assert evidence['special_cells']['2']['c_2']['cache_status']=='missing'
    assert evidence['provenance']['special_cells']['2']['c_2']['formula']['formula']=='=A2*2'
    with pytest.raises(SourceError) as exc:
        read_source(s)
    assert exc.value.code=='FORMULA_CACHE_MISSING'


def test_probe_metadata_matches_returned_rows_and_truncation_is_honest(tmp_path):
    s,_=source(tmp_path,[['金额'],[1],[2],[3],[4]])
    rows,fields,evidence,truncated=read_source(s,limit=2)
    assert truncated and len(rows)==2 and evidence['source_rows']==[2,3]
    assert evidence['provenance']['source_rows']==[2,3] and not evidence['complete']


def test_option_injection_cannot_disable_full_read_formula_gate(tmp_path):
    s,_=source(tmp_path,[['结果'],['=1+1']],options={'require_formula_cache':False})
    with pytest.raises(SourceError):read_source(s)


def test_csv_stays_on_original_normalization_path(tmp_path):
    uploads=DATA/'uploads';uploads.mkdir(exist_ok=True)
    name=uid()+'.csv';(uploads/name).write_text('客户,金额\n甲,10\n')
    s={'id':'s_'+uid(),'kind':'file','name':'source.csv','secret_config':seal({'stored_name':name}),
       'content_hash':hashlib.sha256((uploads/name).read_bytes()).hexdigest()}
    rows,fields,evidence,truncated=read_source(s)
    assert rows[0]['c_1']=='甲' and rows[0]['c_2']=='10'
    assert fields==[{'name':'c_1','label':'客户'},{'name':'c_2','label':'金额'}]
    assert evidence['parser']=='csv' and not truncated


def titled_workbook(tmp_path, *, later_table=False, footer=False):
    book=Workbook();ws=book.active;ws.title='季度预算'
    ws['A1']='季度预算报告';ws.merge_cells('A1:F1')
    ws.append([])
    ws.append(['部门','月份','预算','已用','剩余','备注'])
    ws.append(['销售','一月',100,40,60,'正常'])
    ws.append(['运营','一月',200,50,150,'正常'])
    if later_table:
        ws.append([]);ws.append(['其他部门','其他月份','其他预算','其他已用','其他剩余','其他备注'])
        ws.append(['研发','二月',300,60,240,'正常'])
    if footer:
        ws.append([]);ws.append(['备注：数据待核实'])
    file=tmp_path/(uid()+'.xlsx');book.save(file)
    uploads=DATA/'uploads';uploads.mkdir(exist_ok=True);shutil.copy2(file,uploads/file.name)
    s={'kind':'file','id':'s_'+uid(),'name':'quarterly_budget.xlsx',
       'secret_config':seal({'stored_name':file.name}),
       'content_hash':hashlib.sha256(file.read_bytes()).hexdigest()}
    return s,file


def test_merged_title_unique_candidate_is_read_with_review_evidence(tmp_path):
    s,_=titled_workbook(tmp_path)
    rows,fields,evidence,truncated=read_source(s)
    assert not truncated and [f['label'] for f in fields]==['部门','月份','预算','已用','剩余','备注']
    assert [r['c_1'] for r in rows]==['销售','运营'] and evidence['source_rows']==[4,5]
    assert evidence['selection']['header_rows']==[3]
    assert evidence['auto_layout_selection']['options']['cell_range']=='A3:F5'
    assert evidence['semantic_review_required'] is True
    assert any(w['code']=='XLSX_AUTO_LAYOUT_REVIEW_REQUIRED' for w in evidence['warnings'])


@pytest.mark.parametrize('case',['later_table','footer'])
def test_merged_title_never_silently_omits_other_content(tmp_path,case):
    s,_=titled_workbook(tmp_path,**{case:True})
    with pytest.raises(SourceError) as exc:read_source(s)
    assert exc.value.code=='HEADER_SELECTION_REQUIRED'


def test_auto_layout_preview_and_workflow_input_keep_review_requirement(tmp_path):
    from agent_service import project_data
    from agent_service.store import put, delete
    s,_=titled_workbook(tmp_path)
    pid='p_'+uid();s['project_id']=pid
    put('project',{'name':'layout review'},pid);put('source',s,s['id'])
    try:
        preview=project_data.preview_source(pid,{'source_id':s['id']})
        assert preview['total']==2 and preview['evidence']['requires_semantic_review']
        rows,fields,evidence,provenance=project_data.read_input(pid,{'source_id':s['id']})
        assert len(rows)==2 and fields[0]['label']=='部门'
        assert evidence['requires_source_review'] is True
        assert evidence['auto_layout_selection']['requires_confirmation'] is True
        assert provenance['selection']['header_rows']==[3]
    finally:
        delete('source',s['id']);delete('project',pid)


def test_auto_layout_never_drops_isolated_data_left_of_candidate(tmp_path):
    # A discovery candidate can omit a one-column block separated by an empty
    # column. Its right edge and last row still match the worksheet bounds.
    from agent_service.workbook_reader import inspect_workbook
    book=Workbook();ws=book.active;ws.title='数据'
    ws['A1']='标题';ws.merge_cells('A1:F1')
    for column,value in zip(('C','D','E','F'),('部门','预算','已用','备注')):ws[f'{column}3']=value
    for column,value in zip(('C','D','E','F'),('销售',100,40,'正常')):ws[f'{column}4']=value
    for column,value in zip(('C','D','E','F'),('运营',200,50,'正常')):ws[f'{column}5']=value
    ws['A5']='左侧独立数据'
    file=tmp_path/(uid()+'.xlsx');book.save(file)
    uploads=DATA/'uploads';uploads.mkdir(exist_ok=True);shutil.copy2(file,uploads/file.name)
    source={'kind':'file','id':'s_'+uid(),'name':'budget.xlsx',
            'secret_config':seal({'stored_name':file.name}),
            'content_hash':hashlib.sha256(file.read_bytes()).hexdigest()}
    info=inspect_workbook(file)
    assert info['candidate_regions'][0]['range']=='C3:F5'
    with pytest.raises(SourceError) as exc:read_source(source)
    assert exc.value.code=='HEADER_SELECTION_REQUIRED'
