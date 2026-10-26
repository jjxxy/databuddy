"""Coercion failures carry safe coordinates and types, never cell contents."""
import json
from datetime import date,datetime
from decimal import Decimal

import pytest

from agent_service.data_workflows import execute,WorkflowError


def plan(kind='text',*,nullable=True):
    columns=[{'name':'id','data_type':'text','meaning':'Explicit row id'},
             {'name':'amount_raw','data_type':kind,'meaning':'Declared source value','nullable':nullable,
              **({'unit':'CNY'} if kind in ('decimal','integer') else {})}]
    return {'name':'Synthetic type-location check','inputs':{'income':{'columns':columns,'grain':['id'],'meaning':'Explicit input'}},
            'stages':[{'kind':'select','id':'selected','description':'Keep both declared fields',
                       'input':'income','columns':['id','amount_raw']}],'output':'selected'}


def test_input_decimal_declared_as_text_reports_exact_location_without_coercing():
    value=Decimal('100.000000000001')
    with pytest.raises(WorkflowError) as captured:
        execute(plan(),{'income':[{'id':'one','amount_raw':'already text'},{'id':'two','amount_raw':value}]})
    error=captured.value
    assert error.code=='TYPE_MISMATCH' and str(error)=='Text fields require string values'
    assert error.stage_id=='input:income'
    assert error.details=={'column':'amount_raw','row_index':2,'expected_data_type':'text','actual_type':'Decimal'}
    assert str(value) not in json.dumps(error.details)


@pytest.mark.parametrize('value,kind,actual,code',[
    (17,'text','int','TYPE_MISMATCH'),
    (True,'text','bool','TYPE_MISMATCH'),
    (0.1,'decimal','float','INVALID_DECIMAL'),
    ('https://private.invalid/?token=PRIVATE-SECRET','decimal','str','INVALID_DECIMAL'),
    ({'password':'PRIVATE-SECRET'},'text','dict','TYPE_MISMATCH'),
    (['PRIVATE-SECRET'],'text','list','TYPE_MISMATCH'),
    (b'PRIVATE-SECRET','text','bytes','TYPE_MISMATCH'),
    (date(2026,9,30),'text','date','TYPE_MISMATCH'),
    (datetime(2026,9,30,12,30),'text','datetime','TYPE_MISMATCH'),
    (None,'text','NoneType','NULL_VALUE'),
])
def test_actual_type_is_a_fixed_name_and_sensitive_contents_are_not_exposed(value,kind,actual,code):
    with pytest.raises(WorkflowError) as captured:
        execute(plan(kind,nullable=False),{'income':[{'id':'row','amount_raw':value}]})
    error=captured.value
    assert error.code==code and error.stage_id=='input:income'
    assert error.details=={'column':'amount_raw','row_index':1,'expected_data_type':kind,'actual_type':actual}
    encoded=str(error)+json.dumps(error.details)
    assert 'PRIVATE-SECRET' not in encoded and 'private.invalid' not in encoded and 'password' not in encoded


def test_untrusted_object_type_name_and_string_representation_are_not_exposed():
    class PRIVATE_SECRET_CLASS:
        def __repr__(self):raise AssertionError('Raw object representation must not be evaluated')
        def __str__(self):raise AssertionError('Raw object string must not be evaluated')
    with pytest.raises(WorkflowError) as captured:
        execute(plan(),{'income':[{'id':'row','amount_raw':PRIVATE_SECRET_CLASS()}]})
    assert captured.value.details['actual_type']=='other'
    assert 'PRIVATE_SECRET_CLASS' not in json.dumps(captured.value.details)


def test_stage_table_coercion_keeps_stage_identity_instead_of_input_prefix():
    value=plan();schema=value['inputs']['income']
    value['stages']=[{'id':'sql_output','kind':'sql','description':'Explicit synthetic output schema',
        'inputs':['income'],'sql':"SELECT id,CAST('1.23' AS DECIMAL(38,12)) AS amount_raw FROM income",
        'output_schema':schema}]
    value['output']='sql_output'
    with pytest.raises(WorkflowError) as captured:
        execute(value,{'income':[{'id':'row','amount_raw':'original text'}]})
    assert captured.value.code=='TYPE_MISMATCH' and captured.value.stage_id=='sql_output'
    assert captured.value.details=={'column':'amount_raw','row_index':1,'expected_data_type':'text','actual_type':'Decimal'}


def test_optional_details_are_backwards_compatible_and_only_accept_safe_fields():
    old=WorkflowError('OLD_CODE','Static message','saved_stage')
    assert old.code=='OLD_CODE' and old.stage_id=='saved_stage' and str(old)=='Static message' and old.details=={}
    supplied={'column':'../private/model.json','row_index':True,'expected_data_type':'PRIVATE-SECRET',
              'actual_type':'PRIVATE-SECRET','raw_value':'PRIVATE-SECRET'}
    assert WorkflowError('TYPE_MISMATCH','Static message',details=supplied).details=={}
    good={'column':'amount_raw','row_index':1,'expected_data_type':'text','actual_type':'Decimal'}
    assert WorkflowError('TYPE_MISMATCH','Static message',details={**good,'raw_value':'PRIVATE-SECRET'}).details==good
