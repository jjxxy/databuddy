"""Model/Skill flow nodes preserve identity, authorization and unverified output."""
import hashlib
import json
import pytest

from agent_service import extensions, model_catalog, provider
from agent_service.actor_context import actor_scope
from agent_service.store import put,patch,uid
from agent_service import workflow_model_skill as flow


@pytest.fixture
def task(monkeypatch,tmp_path):
    monkeypatch.setattr(extensions,'DATA',tmp_path)
    pid='p_'+uid();tid='t_'+uid()
    put('project',{'name':'业务项目','created_at':'2026-01-01'},pid)
    put('turn',{'project_id':pid,'actor_id':'owner','state':'agent','model_id':'m1',
        'model_catalog_revision':'mc_1','text':'生成草稿','created_at':'2026-01-01'},tid)
    monkeypatch.setattr(model_catalog,'settings_for_turn',lambda turn:{'base_url':'https://model.invalid/v1','model':'example','share_data':True})
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://model.invalid/v1','model':'example','share_data':True})
    return {'project_id':pid,'turn_id':tid,'actor_id':'owner'}


def config(task):return dict(flow.freeze_model(task),prompt='根据输入生成摘要')


def test_model_result_bound_to_frozen_identity_and_marked_unverified(task,monkeypatch):
    messages=[]
    def call(value,**kwargs):messages.extend(value);return '待核验结果'
    monkeypatch.setattr(provider,'complete',call)
    result=flow.run_model(task,config(task),{'content':{'amount':'123.45'}})
    assert result['text']=='待核验结果' and result['nondeterministic'] is True
    assert result['evidence_status']=='unverified' and result['model_id']=='m1'
    assert result['catalog_revision']=='mc_1' and len(result['request_sha256'])==64
    assert result['output_sha256']==hashlib.sha256('待核验结果'.encode()).hexdigest()
    assert '123.45' in messages[1]['content'] and '不得把推测' in messages[0]['content']
    assert provider.current_turn_id() is None


def test_scope_share_data_and_model_version_are_enforced(task,monkeypatch):
    calls=[];monkeypatch.setattr(provider,'complete',lambda *a,**k:calls.append(1) or 'out')
    bad=dict(config(task),catalog_revision='mc_other')
    with pytest.raises(ValueError,match='模型'):flow.run_model(task,bad,{'content':'secret'})
    with actor_scope('u_'+'a'*64):
        with pytest.raises(ValueError,match='用户'):flow.run_model(task,config(task),{'content':'secret'})
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':False})
    with pytest.raises(ValueError,match='未获准'):flow.run_model(task,config(task),{'content':'secret'})
    assert calls==[]


def test_unpinned_model_cannot_use_global_default(task):
    patch('turn',task['turn_id'],model_catalog_revision=None)
    with pytest.raises(ValueError,match='固定模型'):flow.freeze_model(task)


def test_skill_pin_uses_real_owner_and_durable_version_but_honors_disable(task,monkeypatch):
    old=b'---\nname: old\n---\nUse original reference.'
    skill=extensions.install_skill('SKILL.md',old)
    pinned=flow.freeze_skill(task,skill['id'])
    cfg={**config(task),**{k:pinned[k] for k in ('pin_id','skill_id','skill_digest')}}
    instructions=[]
    def call(messages,**kwargs):instructions.append(json.loads(messages[1]['content'])['skill_reference_untrusted']);return '草稿'
    monkeypatch.setattr(provider,'complete',call)
    # Updating the installation does not silently replace the frozen reference.
    new=extensions.install_skill('SKILL.md',b'---\nname: new\n---\nUse new reference.',identity=skill['id'])
    result=flow.run_skill(task,cfg,{'content':'data'})
    assert result['skill_digest']==skill['digest']!=new['digest']
    assert 'original reference' in instructions[0] and result['evidence_status']=='unverified'
    extensions.toggle_skill(skill['id'],extensions.Toggle(enabled=False,expected_digest=new['digest']))
    with pytest.raises(ValueError,match='停用'):flow.run_skill(task,cfg,{'content':'data'})


def test_skill_pin_cannot_cross_actor_project_or_use_untrusted_package(task,monkeypatch):
    skill=extensions.install_skill('SKILL.md',b'---\nname: one\n---\nReference.')
    pin=flow.freeze_skill(task,skill['id']);cfg={**config(task),**{k:pin[k] for k in ('pin_id','skill_id','skill_digest')}}
    monkeypatch.setattr(provider,'complete',lambda *a,**k:'out')
    fake=dict(cfg,skill_digest='0'*64)
    with pytest.raises(ValueError,match='不属于'):flow.run_skill(task,fake,{'content':'data'})
    with pytest.raises(ValueError,match='不属于'):flow.run_skill(dict(task,project_id='p_other'),cfg,{'content':'data'})
    with actor_scope('u_'+'a'*64):
        with pytest.raises(ValueError):flow.freeze_skill(dict(task,actor_id='u_'+'a'*64),skill['id'])


def test_unexpected_configuration_and_unauthorized_model_no_execution(task,monkeypatch):
    calls=[];monkeypatch.setattr(provider,'complete',lambda *a,**k:calls.append(1) or 'out')
    with pytest.raises(ValueError,match='模型'):flow.run_model(task,dict(config(task),base_url='https://evil.test'),{'content':'x'})
    with pytest.raises(ValueError,match='输入'):flow.run_model(task,config(task),{'content':'x','other':'y'})
    with pytest.raises(ValueError,match='密钥'):flow.run_model(task,dict(config(task),prompt='api_key = secret-value'),{'content':'x'})
    patch('turn',task['turn_id'],outcome='cancelled')
    with pytest.raises(ValueError,match='停止'):flow.run_model(task,{'model_id':'m1','catalog_revision':'mc_1','prompt':'生成'}, {'content':'x'})
    assert not calls


def test_flow_reuses_recorded_model_output_without_new_model_call(task,monkeypatch):
    import copy
    from agent_service.business_flow_engine import run
    calls=[];monkeypatch.setattr(provider,'complete',lambda *a,**k:calls.append(1) or '候选解释')
    doc={'name':'受控模型步骤','inputs':['data'],'nodes':[{'id':'draft','kind':'model',
        'title':'生成说明','inputs':{'content':{'source':'input','name':'data'}},'config':config(task)}],
        'outputs':['draft']}
    saved=[]; result=run(doc,{'data':{'amount':'10.00'}},flow.handlers(),task,persist=lambda state:saved.append(copy.deepcopy(state)))
    assert result['status']=='succeeded' and len(calls)==1
    assert result['steps'][0]['output']['evidence_status']=='unverified'
    again=run(doc,{'data':{'amount':'10.00'}},flow.handlers(),task,checkpoint=result,persist=lambda state:None)
    assert again['status']=='succeeded' and len(calls)==1
