import hashlib
import pytest
from agent_service.connectors import exact_json,read_source,SourceError
from agent_service.config import DATA
from agent_service.store import seal,uid


@pytest.mark.parametrize('extension,raw',[
    ('.json',b'[{"id":"A","amount":9007199254740.000001}]'),
    ('.jsonl',b'{"id":"A","amount":9007199254740.000001}\n'),
])
def test_source_json_decimal_token_survives_full_read_without_float(extension,raw):
    name=uid()+extension;(DATA/'uploads').mkdir(exist_ok=True);(DATA/'uploads'/name).write_bytes(raw)
    rows,fields,_,truncated=read_source({'id':'precision-test','kind':'file','name':'values'+extension,
                                       'secret_config':seal({'stored_name':name}),
                                       'content_hash':hashlib.sha256(raw).hexdigest()})
    assert rows[0]['c_2']=='9007199254740.000001' and not truncated


@pytest.mark.parametrize('raw,code',[
    ('{"amount":1,"amount":2}','DUPLICATE_JSON_FIELD'),
    ('{"amount":NaN}','INVALID_JSON_NUMBER'),
    ('{"amount":Infinity}','INVALID_JSON_NUMBER'),
])
def test_ambiguous_or_nonfinite_json_never_silently_changes_a_value(raw,code):
    with pytest.raises(SourceError) as caught:exact_json(raw)
    assert caught.value.code==code
