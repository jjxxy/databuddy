"""Opt-in exact Excel scalar projection makes declared normalization reachable."""
import builtins,copy,hashlib,json
from decimal import Decimal

from openpyxl import Workbook
import pytest
from pydantic import ValidationError

from agent_service import project_data as data,workspace
from agent_service.data_workflows import digest
from agent_service.execution import process_once
from agent_service.jobs import get_job
from agent_service.store import get
from agent_service.workbook_reader import WorkbookReadError,read_table
from test_project_data import project,source as csv_source,document as csv_document
from test_workflow_run_diagnostics import source


def options():return data.ReadOptions(text_columns=['c_1'])


@pytest.mark.parametrize('columns',[['c_1','c_1'],['amount'],['c_0'],['c_257'],['c_01'],['c_1;sql'],[True],'c_1'])
def test_explicit_projection_requires_unique_bounded_source_identifiers(columns):
    with pytest.raises(ValidationError):data.ReadOptions(text_columns=columns)


def test_exact_numeric_projection_preserves_strings_nulls_metadata_and_original_objects():
    rows=[{'c_1':value,'c_2':False} for value in (Decimal('9007199254740993.000000000001'),
        Decimal('1E+40'),9007199254740993,'¥1,234.50','(2.00)',None)]
    fields=[{'name':'c_1','unit':'CNY','observed_kinds':['number','text'],'stored_semantics':'number'},
            {'name':'c_2','unit':None}];frozen=copy.deepcopy((rows,fields))
    projected,receipt=data._project_text_columns(rows,fields,options())
    assert [row['c_1'] for row in projected]==['9007199254740993.000000000001','1'+'0'*40,
        '9007199254740993','¥1,234.50','(2.00)',None]
    assert all(row['c_2'] is False for row in projected)
    assert (rows,fields)==frozen and receipt['lossless'] and receipt['columns']==['c_1']
    assert receipt['type_counts']['c_1']=={'Decimal':2,'int':1,'str':2,'NoneType':1}
    untouched,policy=data._project_text_columns(rows,fields,data.ReadOptions())
    assert untouched is rows and policy is None


@pytest.mark.parametrize('value',[1.25,True,Decimal('NaN'),Decimal('Infinity'),{'raw_private_value':'never-echo'}])
def test_projection_refuses_float_boolean_nonfinite_and_unknown_objects(value):
    with pytest.raises(WorkbookReadError) as error:
        data._project_text_columns([{'c_1':value}],[{'name':'c_1'}],options())
    assert error.value.code=='TEXT_PROJECTION_UNSUPPORTED_VALUE'
    assert error.value.details['row_index']==1 and error.value.details['column']=='c_1'
    assert 'never-echo' not in str(error.value)+json.dumps(error.value.details)


@pytest.mark.parametrize('value',[Decimal('1e1000000000'),Decimal('1e-1000000000'),1<<20000],
                         ids=['huge_positive_exponent','huge_negative_exponent','huge_integer'])
def test_projection_checks_cell_allocation_before_fixed_point_formatting(monkeypatch,value):
    original=builtins.format
    def checked(item,*args):
        if item is value:pytest.fail('Huge numeric value reached the formatter')
        return original(item,*args)
    monkeypatch.setattr(builtins,'format',checked)
    with pytest.raises(WorkbookReadError) as error:
        data._project_text_columns([{'c_1':value}],[{'name':'c_1'}],options())
    assert error.value.code=='TEXT_PROJECTION_LIMIT'


def test_projection_checks_aggregate_expansion_before_next_cell_format(monkeypatch):
    monkeypatch.setattr(data,'_TEXT_PROJECTION_MAX_BYTES',42)
    original=builtins.format;formatted=[]
    def checked(value,*args):formatted.append(value);return original(value,*args)
    monkeypatch.setattr(builtins,'format',checked)
    with pytest.raises(WorkbookReadError) as error:
        data._project_text_columns([{'c_1':Decimal('1E+20')} for _ in range(3)],[{'name':'c_1'}],options())
    assert error.value.code=='TEXT_PROJECTION_LIMIT' and error.value.details['row_index']==3
    assert len(formatted)==2


def test_unknown_and_percentage_columns_are_never_projected_as_plain_text():
    with pytest.raises(WorkbookReadError) as error:
        data._project_text_columns([{'c_1':1}],[{'name':'c_1'}],data.ReadOptions(text_columns=['c_2']))
    assert error.value.code=='TEXT_PROJECTION_INVALID'
    for semantics,policy in [('ratio',{}),('percent_points',{}),('ambiguous_percentage_representation',{}),('number',{'c_1':'fraction'})]:
        with pytest.raises(WorkbookReadError) as error:
            data._project_text_columns([{'c_1':Decimal('0.13')}],[{'name':'c_1','stored_semantics':semantics}],
                data.ReadOptions(text_columns=['c_1'],percentage_columns=policy))
        assert error.value.code=='TEXT_PROJECTION_PERCENTAGE_CONFLICT'


def test_old_empty_read_options_and_frozen_document_hashes_remain_compatible():
    legacy={'sheet':None,'table':None,'header_rows':None,'cell_range':None,'data_start_row':None,'data_end_row':None,
            'merged_label_columns':[],'percentage_columns':{},'html_table':0,'hidden_rows':'error','subtotal_policy':'error'}
    assert data.ReadOptions().model_dump()==legacy
    assert data.ReadOptions(text_columns=[]).model_dump()==legacy
    document=data.WorkflowDocument.model_validate(csv_document('s_synthetic')).model_dump()
    expected=copy.deepcopy(document);expected['bindings']['orders']['options']=legacy
    assert document==expected and digest(document)==digest(expected)
    projected=data.WorkflowDocument.model_validate({**document,'bindings':{'orders':{
        **document['bindings']['orders'],'options':{**legacy,'text_columns':['c_3']}}}}).model_dump()
    assert projected['bindings']['orders']['options']['text_columns']==['c_3']
    assert digest(projected)!=digest(document)
    schema=data.ReadOptions.model_json_schema()['properties']['text_columns']
    assert schema['uniqueItems'] and 'XLSX' in schema['description']


def mixed_workbook(tmp_path):
    book=Workbook();sheet=book.active;sheet.append(['编号','金额（元）'])
    for ident,value in zip(('A','B','C','D'),(1.25,'¥1,234.50','(2.00)',None)):sheet.append([ident,value])
    path=tmp_path/'mixed-input.xlsx';book.save(path);book.close();return path


def workflow(sid,projected):
    return {'version':1,'plan':{'name':'Synthetic explicit numeric-to-text intake','inputs':{'raw':{
        'meaning':'One original mixed amount per id','grain':['id'],'columns':[
            {'name':'id','data_type':'text','meaning':'original id'},
            {'name':'amount_raw','data_type':'text','meaning':'source amount representation','unit':'元'}]}},
        'stages':[{'id':'normalized','kind':'normalize','description':'Apply declared localized amount rule',
            'input':'raw','rules':[{'column':'amount_raw','kind':'decimal','currency':'CNY','source_unit':'元','missing_policy':'null',
                'output':{'name':'amount','data_type':'decimal','unit':'CNY','meaning':'declared exact amount','aggregation':'additive'}}]}],
        'output':'normalized'},'bindings':{'raw':{'source_id':sid,'options':{'cell_range':'A1:B5','header_rows':[1],
            **({'text_columns':['c_2']} if projected else {})},'column_map':{'c_1':'id','c_2':'amount_raw'}}}}


def test_real_xlsx_explicit_projection_preview_and_queued_normalization_preserve_original(tmp_path):
    pid=project();path=mixed_workbook(tmp_path);value=source(pid,path);original_bytes=path.read_bytes()
    raw=read_table(path,cell_range='A1:B5',header_rows=[1]);frozen=copy.deepcopy(raw)
    default_rows,default_fields,_,_=data.read_input(pid,workflow(value['id'],False)['bindings']['raw'])
    assert isinstance(default_rows[0]['amount_raw'],Decimal),'The original default must remain unchanged'
    binding=workflow(value['id'],True)['bindings']['raw']
    rows,fields,evidence,provenance=data.read_input(pid,binding)
    assert [row['amount_raw'] for row in rows]==['1.25','¥1,234.50','(2.00)',None]
    assert fields==default_fields and raw==frozen
    assert fields[1]['unit']=='元' and fields[1]['observed_kinds']==raw['fields'][1]['observed_kinds']
    assert provenance['source_rows']==raw['source_rows'] and provenance['special_cells']==raw['special_cells']
    assert evidence['text_projection']['columns']==['c_2'] and provenance['text_projection']==evidence['text_projection']
    preview=data.preview_source(pid,{'source_id':value['id'],'options':binding['options']})
    assert preview['rows'][0]['c_2']=='1.25' and preview['fields']==raw['fields']
    assert preview['evidence']['text_projection']['columns']==['c_2']
    path_in_project='workflows/explicit-intake.json'
    workspace.write_file(pid,path_in_project,json.dumps(workflow(value['id'],False)));wid=data.register(pid,path_in_project)['workflow_id']
    failed=data.enqueue_workflow(pid,wid);assert process_once();failed=get_job(failed['id'])
    assert failed['error_code']=='TYPE_MISMATCH' and failed['workflow_failure']['stage_id']=='input:raw'
    diagnostic=data.workflow_run_diagnostic(pid,wid,failed['id'])
    assert diagnostic['workflow_failure']['details']=={'column':'amount_raw','source_column':'c_2','row_index':1,
        'expected_data_type':'text','actual_type':'Decimal','input_alias':'raw'}
    assert 'bindings.raw.options.text_columns' in diagnostic['next_step'] and not diagnostic['result_available']
    workspace.write_file(pid,path_in_project,json.dumps(workflow(value['id'],True)));data.register(pid,path_in_project)
    queued=data.enqueue_workflow(pid,wid);assert process_once();completed=get_job(queued['id'])
    assert completed['status']=='validated',{key:completed.get(key) for key in ('error_code','error','workflow_failure')}
    result=data.read_result(pid,completed['id'])
    assert [None if row['amount'] is None else Decimal(row['amount']) for row in result['rows']]==[
        Decimal('1.25'),Decimal('1234.50'),Decimal('-2.00'),None]
    assert get('data_workflow',wid)['published_run'] is None
    assert path.read_bytes()==original_bytes and data.source_path(get('source',value['id'])).read_bytes()==original_bytes


def test_text_projection_is_rejected_for_non_xlsx_and_normalized_percentages(tmp_path):
    pid=project();plain=csv_source(pid)
    with pytest.raises(WorkbookReadError) as error:
        data.read_input(pid,{'source_id':plain['id'],'options':{'text_columns':['c_1']}})
    assert error.value.code=='TEXT_PROJECTION_NOT_XLSX'
    from test_mixed_percentage_source import workbook
    percent=source(pid,workbook(tmp_path))
    with pytest.raises(WorkbookReadError) as error:
        data.read_input(pid,{'source_id':percent['id'],'options':{'percentage_columns':{'c_2':'fraction'},'text_columns':['c_2']}})
    assert error.value.code=='TEXT_PROJECTION_PERCENTAGE_CONFLICT'
