"""The schema seen by the agent must advertise runtime financial constraints."""
import pytest
from jsonschema import Draft202012Validator, ValidationError as SchemaError
from pydantic import ValidationError
from agent_service.data_workflows import Column

@pytest.mark.parametrize('kind',['decimal','integer'])
def test_numeric_unit_is_required_in_advertised_and_runtime_contract(kind):
    value={'name':'amount','data_type':kind,'meaning':'Amount or count'}
    schema=Column.model_json_schema();Draft202012Validator.check_schema(schema)
    with pytest.raises(SchemaError):Draft202012Validator(schema).validate(value)
    with pytest.raises(ValidationError):Column.model_validate(value)
    valid=dict(value,unit='CNY' if kind=='decimal' else '1')
    Draft202012Validator(schema).validate(valid)
    assert Column.model_validate(valid).unit==valid['unit']

def test_text_unit_remains_optional_and_display_labels_remain_chinese():
    value={'name':'office','label':'办事处','data_type':'text','meaning':'有效办事处'}
    Draft202012Validator(Column.model_json_schema()).validate(value)
    assert Column.model_validate(value).label=='办事处'
