"""Character caps are explicit; native token-window compaction stays enabled."""
import pytest
from agent_service import opencode_runtime as runtime


def test_no_default_character_cap_on_generation_or_compaction(monkeypatch):
    monkeypatch.delenv('ATLAS_OPENCODE_MODEL_INPUT_CHAR_LIMITS',raising=False)
    monkeypatch.delenv('ATLAS_OPENCODE_COMPACTION_MAX_INPUT_CHARACTERS',raising=False)
    for kind in (None,'generation','summary','COMPACTION'):
        assert runtime._input_character_limit({'kind':kind},{'model':'finance'}) is None
    assert runtime._input_character_limit({'kind':'compaction'},{'model':'finance'}) is None


def test_compaction_respects_explicit_operator_caps_without_a_hidden_hard_cap(monkeypatch):
    monkeypatch.setenv('ATLAS_OPENCODE_MODEL_INPUT_CHAR_LIMITS','{"finance":120000,"small":40000}')
    monkeypatch.setenv('ATLAS_OPENCODE_COMPACTION_MAX_INPUT_CHARACTERS','200000')
    assert runtime._input_character_limit({'kind':'compaction'},{'model':'finance'})==120000
    assert runtime._input_character_limit({'kind':'generation'},{'model':'small'})==40000
    monkeypatch.setenv('ATLAS_OPENCODE_COMPACTION_MAX_INPUT_CHARACTERS','320001')
    assert runtime._input_character_limit({'kind':'compaction'},{'model':'finance'})==120000
    monkeypatch.setenv('ATLAS_OPENCODE_MODEL_INPUT_CHAR_LIMITS','{}')
    assert runtime._input_character_limit({'kind':'compaction'},{'model':'finance'})==320001
    monkeypatch.setenv('ATLAS_OPENCODE_COMPACTION_MAX_INPUT_CHARACTERS','0')
    with pytest.raises(ValueError):runtime._input_character_limit({'kind':'compaction'},{'model':'finance'})


@pytest.mark.parametrize('value',['[]','{"finance":true}','{"finance":-1}','{"finance":"120000"}'])
def test_invalid_trusted_limit_configuration_fails_closed(monkeypatch,value):
    monkeypatch.setenv('ATLAS_OPENCODE_MODEL_INPUT_CHAR_LIMITS',value)
    with pytest.raises(ValueError):runtime._input_character_limit({'kind':'compaction'},{'model':'finance'})


def test_repeated_tool_schemas_are_omitted_but_new_and_changed_schemas_preserved():
    original={'name':'read_file','arguments_schema':{'type':'object'}}
    changed=dict(original,arguments_schema={'type':'string'})
    known={}
    first=runtime._new_tool_definitions({'observation':{'actual':'receipt'},'available_tools':[original]},known)
    repeated=runtime._new_tool_definitions(first,known)
    assert first['available_tools']==[original]
    assert repeated['available_tools']==[] and repeated['observation']=={'actual':'receipt'}
    assert runtime._new_tool_definitions({'available_tools':[changed]},known)['available_tools']==[changed]


def test_resume_uses_native_token_window_unless_a_character_cap_was_explicitly_set():
    history=[{'content':'x'*35000}]
    assert not runtime._needs_compaction(history,'y'*35000)
    assert runtime._needs_compaction(history,'y'*35000,character_cap=60000)
    assert not runtime._needs_compaction(history,'y'*1000)
    assert runtime._needs_compaction([],'small',True)
    # The trigger is an observation only; exact old constraints are retained.
    constraints=[{'content':'用户禁止：不要将期末余额求和。'}]
    original=list(constraints)
    runtime._needs_compaction(constraints,'new context')
    assert constraints==original
