"""Typed-plan failures give repairable schema feedback without rejected values."""
import copy,json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_service import agent,provider,tasks,workspace,project_data as data
from agent_service.store import get,put,patch,delete,uid,now,all_items
from test_project_data import project,source,document


@pytest.fixture(autouse=True)
def isolated_records():
    kinds=('turn','data_workflow','source_version','source','project')
    before={kind:{value['id'] for value in all_items(kind)} for kind in kinds}
    yield
    for kind in kinds:
        for value in all_items(kind):
            if value['id'] not in before[kind]:delete(kind,value['id'])


def task(pid):
    value=put('turn',{'project_id':pid,'text':'请创建加工方案文件并登记，先不要运行或发布。',
        'interaction_mode':'work','state':'agent','created_at':now()},'t_'+uid())
    tasks.ensure(value)
    return get('turn',value['id'])


def run_tool(turn,name,arguments):
    agent._execute_node({'turn_id':turn['id'],'step':{'kind':'tool','tool':name,'arguments':arguments}})
    return get('turn',turn['id'])['agent_trace'][-1]


def test_actual_failed_registration_can_be_repaired_and_registered_without_running(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    pid=project();value=source(pid,b'id,office,amount,note\nA,East,1.00,test\n');turn=task(pid)
    correct=document(value['id']);correct.pop('contract')
    invalid=copy.deepcopy(correct);invalid['plan']['inputs']['orders']['columns'][2]['unit']=''
    invalid['plan']['stages'][0].pop('description')
    path='workflows/repairable.json'
    workspace.write_file(pid,path,json.dumps(invalid))
    before_jobs={job['id'] for job in all_items('run')}
    failed=run_tool(turn,'register_data_workflow',{'path':path})
    assert failed['status']=='failed'
    observation=failed['observation']
    assert observation['code']=='document_validation_error' and observation['repairable']
    assert any(v.get('rule')=='numeric_unit_required' for v in observation['validation'])
    missing=next(v for v in observation['validation'] if v['type']=='missing')
    assert missing['loc']==['plan','stages',0,'select','description']
    assert missing['expected']=={'type':'string','minLength':1,'maxLength':1000}
    assert '无需重复discover_tools' in observation['next_step']
    assert not [w for w in all_items('data_workflow') if w['project_id']==pid]
    # A minimal synthetic schema repair test, not a hand-authored monthly solution.
    saved=run_tool(turn,'write_file',{'path':path,'content':json.dumps(correct)})
    assert saved['status']=='done'
    accepted=run_tool(turn,'register_data_workflow',{'path':path})
    assert accepted['status']=='done' and accepted['observation']['validation']=='typed_plan_valid; not_executed'
    assert accepted['observation']['workflow_id']
    assert {job['id'] for job in all_items('run')}==before_jobs


def test_validation_feedback_never_reflects_values_dynamic_keys_or_error_context(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    pid=project();value=source(pid);turn=task(pid);bad=document(value['id'])
    secret='sk-FIXTURE-PRIVATE-0123456789'
    url='https://private.invalid/billing?token='+secret
    bad['plan']['inputs']['orders']['columns'][0]['data_type']=url
    bad['plan']['inputs']['orders']['columns'][1][url]=secret
    bad['plan']['inputs'][secret]=copy.deepcopy(bad['plan']['inputs']['orders'])
    # The file writer correctly refuses credential-shaped content. Exercise the
    # exception boundary directly without persisting these rejected test values.
    with pytest.raises(ValidationError) as captured:data.WorkflowDocument.model_validate(bad)
    def reject(*args):raise captured.value
    monkeypatch.setattr(agent,'execute',reject)
    failed=run_tool(turn,'register_data_workflow',{'path':'workflows/sensitive.json'})
    assert failed['status']=='failed' and failed['observation']['code']=='document_validation_error'
    encoded=json.dumps(failed['observation'],ensure_ascii=False)
    assert secret not in encoded and url not in encoded and 'private.invalid' not in encoded
    assert 'input_value' not in encoded and 'input_type' not in encoded
    assert any('*' in v['loc'] for v in failed['observation']['validation'])
    def audit(node):
        if isinstance(node,dict):
            assert not {'input','ctx','url','msg'}&set(node)
            for value in node.values():audit(value)
        elif isinstance(node,list):
            for value in node:audit(value)
    audit(failed['observation'])


def test_explicit_date_and_multiplier_diagnostics_come_from_schema_not_source_values():
    pid=project();value=source(pid);bad=document(value['id'])
    bad['plan']['stages']=[{'id':'dates','kind':'normalize','description':'Declared synthetic date format',
        'input':'orders','rules':[{'column':'note','kind':'date','output':{
            'name':'date','data_type':'date','meaning':'Declared date'},'date_format':'%m/%d'}]}]
    bad['plan']['output']='dates'
    with pytest.raises(ValidationError) as captured:data.WorkflowDocument.model_validate(bad)
    feedback=agent.validation_observation(captured.value,'register_data_workflow')
    detail=next(v for v in feedback['validation'] if v.get('rule')=='explicit_date_format_required')
    assert detail['loc']==['plan','stages',0,'normalize','rules',0]
    assert '%Y-%m-%d' in detail['expected']['date_format'] and '%m/%d' not in detail['expected']['date_format']
    bad['plan']['stages'][0]['rules'][0].update(date_format='%Y-%m-%d',unit_multiplier='1e4')
    with pytest.raises(ValidationError) as captured:data.WorkflowDocument.model_validate(bad)
    feedback=agent.validation_observation(captured.value,'register_data_workflow')
    detail=next(v for v in feedback['validation'] if v['type']=='string_pattern_mismatch')
    assert detail['expected']['pattern']==r'^[0-9]{1,26}(?:\.[0-9]{1,12})?$'


def test_unknown_exception_remains_generic_and_does_not_reflect_private_path(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    pid=project();turn=task(pid)
    def explode(*args):raise RuntimeError('C:/private/model.json api_key=sk-FIXTURE-PRIVATE https://private.invalid/')
    monkeypatch.setattr(agent,'execute',explode)
    failed=run_tool(turn,'register_data_workflow',{'path':'workflows/example.json'})
    assert failed['status']=='failed'
    assert failed['observation']=={'error':'工具执行没有成功；请检查资料、参数或服务状态'}
