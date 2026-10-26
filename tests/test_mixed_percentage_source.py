"""Explicit mixed-format percentage normalization keeps cell evidence intact."""
from decimal import Decimal
import hashlib

from openpyxl import Workbook
import pytest

from agent_service import project_data as data
from agent_service.config import DATA
from agent_service.data_workflows import execute
from agent_service.store import now, put, seal, uid
from agent_service.workbook_reader import WorkbookReadError, read_table
from test_project_data import project
from test_workbook_reader import patch_cells


def workbook(tmp_path, *, values=None, formats=None, header='税率'):
    book=Workbook();sheet=book.active
    sheet.append(['编号',header])
    for index,value in enumerate(values if values is not None else [0.13,'13%',' １３％ ',13],2):
        sheet.append([str(index-1),value])
    for coordinate,fmt in (formats if formats is not None else {'B2':'0.00%','B5':'0"%"'}).items():
        sheet[coordinate].number_format=fmt
    path=tmp_path/'mixed.xlsx';book.save(path);return path


@pytest.mark.parametrize('target,expected,semantics',[
    ('fraction',Decimal('0.13'),'ratio'),('points',Decimal('13'),'percent_points')])
def test_only_explicit_policy_unifies_percent_sign_text_and_excel_percentage_formats(tmp_path,target,expected,semantics):
    path=workbook(tmp_path)
    with pytest.raises(WorkbookReadError) as caught:read_table(path)
    assert caught.value.code=='MIXED_PERCENTAGE_REPRESENTATION'
    result=read_table(path,percentage_columns={'c_2':target})
    assert all(row['c_2']==expected for row in result['rows'])
    assert result['executable_values_complete'] is True and result['provenance']['complete'] is True
    field=result['fields'][1]
    assert field['numeric'] and field['stored_semantics']==semantics and field['unit']=='%'
    assert result['selection']['percentage_columns']=={'c_2':target}
    assert result['special_cells']['2']['c_2']['percentage_normalization']['raw_value']=='0.13'
    assert result['special_cells']['3']['c_2']['percentage_normalization']['raw_value']=='13%'
    assert result['special_cells']['4']['c_2']['percentage_normalization']['raw_value']==' １３％ '
    last=result['special_cells']['5']['c_2']['percentage_normalization']
    assert last['raw_kind']=='percentage_points' and last['number_format']=='0"%"'
    assert last['output_representation']==target
    other=read_table(path,percentage_columns={'c_2':'points' if target=='fraction' else 'fraction'})
    assert result['table_id']!=other['table_id']


@pytest.mark.parametrize('bare',[13,0.13,True,'13','0.13','13%%'])
def test_explicit_percentage_target_never_guesses_bare_numbers_or_unsigned_text(tmp_path,bare):
    path=workbook(tmp_path,values=[0.13,bare],formats={'B2':'0.00%'})
    with pytest.raises(WorkbookReadError) as caught:
        read_table(path,percentage_columns={'c_2':'fraction'})
    assert caught.value.code in {'PERCENTAGE_NUMERIC_UNDECLARED','LOCALIZED_PERCENT_SIGN_REQUIRED','LOCALIZED_VALUE_INVALID'}


@pytest.mark.parametrize('header,fmt,code',[
    ('金额（元）','0%','UNIT_FORMAT_CONFLICT'),
    ('税率','0%"USD"','UNIT_FORMAT_CONFLICT'),
    ('税率','0,,%','NUMERIC_DISPLAY_SCALE_UNVERIFIED'),
    ('税率','0%%','PERCENTAGE_FORMAT_UNSUPPORTED')])
def test_percentage_policy_does_not_erase_currency_or_display_scale_conflicts(tmp_path,header,fmt,code):
    path=workbook(tmp_path,values=[0.13,'13%'],formats={'B2':fmt},header=header)
    with pytest.raises(WorkbookReadError) as caught:
        read_table(path,percentage_columns={'c_2':'fraction'})
    assert caught.value.code==code


def test_percentage_policy_keeps_formula_cache_gate_and_freshness_evidence(tmp_path):
    path=workbook(tmp_path,values=['=13/100','13%'],formats={'B2':'0.00%'})
    with pytest.raises(WorkbookReadError) as caught:
        read_table(path,percentage_columns={'c_2':'fraction'})
    assert caught.value.code=='FORMULA_CACHE_MISSING'
    patch_cells(path,{'B2':{'value':'0.13'}})
    result=read_table(path,percentage_columns={'c_2':'fraction'})
    cell=result['special_cells']['2']['c_2']
    assert cell['formula']['formula']=='=13/100' and cell['cache_freshness']=='unknown'
    assert cell['percentage_normalization']['normalized_value']=='0.13'


def test_complete_execution_cannot_drop_percentage_conversion_receipts(tmp_path):
    path=workbook(tmp_path)
    with pytest.raises(WorkbookReadError) as caught:
        read_table(path,percentage_columns={'c_2':'fraction'},provenance_limit=1)
    assert caught.value.code=='WORKBOOK_METADATA_LIMIT'


@pytest.mark.parametrize('options',[{'c_999':'fraction'},{'B':'fraction'},{'c_2':'guess'},['c_2']])
def test_normalization_option_requires_selected_canonical_column_and_known_target(tmp_path,options):
    with pytest.raises(WorkbookReadError) as caught:
        read_table(workbook(tmp_path),percentage_columns=options)
    assert caught.value.code=='WORKBOOK_SELECTION_INVALID'


def test_explicit_reader_policy_is_available_through_real_project_binding_and_typed_execution(tmp_path):
    path=workbook(tmp_path);pid=project();stored_name=uid()+'.xlsx'
    target=DATA/'uploads'/stored_name;target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(path.read_bytes())
    source=put('source',dict(project_id=pid,kind='file',name='税率底稿.xlsx',
        secret_config=seal({'stored_name':stored_name}),content_hash=hashlib.sha256(target.read_bytes()).hexdigest(),
        bytes=target.stat().st_size,status='ready',created_at=now()),'s_'+uid())
    rows,fields,proof,provenance=data.read_input(pid,{'source_id':source['id'],
        'options':{'percentage_columns':{'c_2':'fraction'}},'column_map':{'c_1':'id','c_2':'rate'}})
    assert fields[1]['stored_semantics']=='ratio' and proof['executable_values_complete']
    assert provenance['selection']['percentage_columns']=={'c_2':'fraction'}
    plan={'name':'Preserve exact mixed-source percentage','inputs':{'raw':{
        'meaning':'One rate per observation','grain':['id'],'columns':[
            {'name':'id','data_type':'text','meaning':'observation id','unit':''},
            {'name':'rate','data_type':'decimal','meaning':'VAT rate','unit':'%',
             'value_format':'percent_fraction','aggregation':'non_additive'}]}},
        'stages':[{'kind':'select','id':'selected','input':'raw','description':'Select validated rates','columns':['id','rate']}],
        'output':'selected'}
    result=execute(plan,{'raw':rows})
    assert [Decimal(row['rate']) for row in result['output']['rows']]==[Decimal('0.13')]*4
