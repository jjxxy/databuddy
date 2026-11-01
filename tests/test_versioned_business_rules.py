"""Only explicit user corrections replace a versioned business rule."""
import json
import pytest

from agent_service import knowledge,workspace
from agent_service.store import get,patch
from test_opencode_agent_bridge import turn


def test_explicit_correction_preserves_old_version_without_injecting_it(turn):
    first='请保存约定：金额单位为万元'
    value=patch('turn',turn['id'],text=first)
    record=knowledge.remember(value,'金额单位',first)
    assert record['version']==1
    correction='请更正金额单位，改为元'
    value=patch('turn',turn['id'],text=correction)
    updated=knowledge.remember(value,'金额单位',correction)
    assert updated['version']==2 and updated['quote']==correction
    current=knowledge.rules(turn['project_id'])
    assert len(current)==1 and current[0]['quote']==correction
    assert 'superseded_versions' not in current[0]
    stored=json.loads(workspace.read_file(turn['project_id'],'notes/business-rules.json'))
    assert stored[0]['superseded_versions'][0]['quote']==first
    assert stored[0]['superseded_versions'][0]['version']==1


def test_unrelated_user_statement_cannot_overwrite_existing_rule(turn):
    first='请保存约定：金额单位为万元'
    knowledge.remember(patch('turn',turn['id'],text=first),'金额单位',first)
    other='请保存约定：今年只统计已付款订单'
    with pytest.raises(ValueError,match='明确指出规则名称'):
        knowledge.remember(patch('turn',turn['id'],text=other),'金额单位',other)
    assert knowledge.rules(turn['project_id'])[0]['quote']==first


def test_model_cannot_invent_correction_not_in_user_request(turn):
    first='请保存约定：金额单位为万元'
    knowledge.remember(patch('turn',turn['id'],text=first),'金额单位',first)
    value=patch('turn',turn['id'],text='请更正金额单位，改为元')
    with pytest.raises(ValueError):
        knowledge.remember(value,'金额单位','请更正金额单位，改为千元')
    assert knowledge.rules(turn['project_id'])[0]['quote']==first
