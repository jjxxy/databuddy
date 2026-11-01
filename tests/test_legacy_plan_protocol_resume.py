"""Historical leaked planner decisions can be continued without replaying them."""
import json
import pytest
from agent_service import conversation as chat, agent, provider
from agent_service.actor_context import actor_scope
from agent_service.store import all_items, get, patch, uid
from test_multi_file_conversation import project, upload, submit, ready, H


def encoded():
    return json.dumps({'kind':'tool','tool':'set_task_plan','arguments':{'steps':['读取完整资料','核对并交付']}} ,ensure_ascii=False)


def damaged(project,**values):
    source=upload(project);ready([source['id']]);original=submit(project,[source['id']])
    patch('turn',original['id'],state='done',outcome='blocked',answer=encoded(),**values)
    return get('turn',original['id']),source


def test_real_historical_recovery_preserves_source_and_never_executes_pasted_plan(project,monkeypatch):
    pid,client,_=project;old,source=damaged(project)
    view=client.get(f'/api/projects/{pid}/conversation',headers=H).json()['turns'][-1]
    assert view['resume_available'] and '没有正常调用工作计划' in view['answer'] and '"kind"' not in view['answer']
    assert get('turn',old['id'])['answer']==encoded() and not get('turn',old['id']).get('task_plan')
    body={'text':'继续','request_id':'legacy-'+uid(),'session_id':old['session_id'],'resume_turn_id':old['id']}
    response=client.post(f'/api/projects/{pid}/messages',headers=H,json=body)
    assert response.status_code==200,response.text
    new=get('turn',response.json()['id']);assert new['source_ids']==[source['id']]
    conflict=client.post(f'/api/projects/{pid}/messages',headers=H,json={**body,'request_id':'other-'+uid()})
    assert conflict.status_code==400 and '已经在继续' in conflict.text
    monkeypatch.setattr(provider,'status',lambda:{'configured':True})
    monkeypatch.setattr(agent,'advance',lambda turn:None)
    chat.handle(new)
    new=get('turn',new['id']);assert new['resumed_from']==old['id'] and new['source_ids']==[source['id']]
    assert not new.get('task_plan') and get('turn',old['id'])['protocol_recovery_consumed'] is True
    history=chat.public_turn(get('turn',old['id']));assert not history.get('resume_available')
    assert '请查看后续继续记录' in history['answer'] and '"kind"' not in history['answer']
    assert not [x for x in new.get('agent_trace',[]) if x.get('tool')=='set_task_plan']
    second=client.post(f'/api/projects/{pid}/messages',headers=H,json={**body,'request_id':'last-'+uid()})
    assert second.status_code==400


def test_unknown_write_still_blocks_historical_continue(project):
    pid,client,_=project;old,_=damaged(project,agent_trace=[{'tool':'write_file','status':'started'}])
    r=client.post(f'/api/projects/{pid}/messages',headers=H,json={'text':'继续','request_id':'uncertain-'+uid(),
        'session_id':old['session_id'],'resume_turn_id':old['id']})
    assert r.status_code==400 and '尚未确认' in r.text
    assert len([x for x in all_items('turn') if x.get('project_id')==pid])==1


@pytest.mark.parametrize('changes',[
    {'interaction_mode':'chat'}, {'outcome':'verified_complete'}, {'outcome':'answered'},
    {'state':'agent'}, {'task_plan':{'steps':['已存在']}},
    {'answer':'{"kind":"tool","tool":"write_file","arguments":{}}'},
    {'answer':'{"kind":"tool","tool":"set_task_plan","arguments":{"steps":["一步"]}}'},
    {'text':'请给我一个 set_task_plan 工具调用的 JSON 示例'},
    {'text':'请输出 JSON 格式的工作计划'},
])
def test_not_a_historical_plan_leak(changes):
    turn={'state':'done','outcome':'blocked','answer':encoded(),'text':'请分析销售资料并生成报告','interaction_mode':'work',**changes}
    assert not chat._legacy_plan_protocol_error(turn)


def test_other_actor_and_generic_continue_not_granted(project):
    pid,client,_=project;old,_=damaged(project)
    with actor_scope('u_'+'a'*64):
        assert not chat.public_turn(old).get('resume_available')
    assert chat._can_resume_turn(old,explicit=True) and not chat._can_resume_turn(old)


@pytest.mark.parametrize('outcome',[None,'answered'])
def test_older_router_without_outcome_or_answered_is_recoverable_only_with_evidence(outcome):
    base={'state':'done','outcome':outcome,'answer':encoded(),'interaction_mode':'work',
          'text':'请读取 CSV 和 JSON营销资料，分析并生成 Excel 报告'}
    assert not chat._legacy_plan_protocol_error(base)
    assert chat._legacy_plan_protocol_error({**base,'source_ids':['s_example']})
    assert chat._legacy_plan_protocol_error({**base,'task_contract':{'kind':'office'}})
    assert not chat._legacy_plan_protocol_error({**base,'source_ids':['s_example'],'text':'请输出 JSON 示例'})


def test_cross_session_is_still_rejected(project):
    pid,client,_=project;old,_=damaged(project)
    session=client.post(f'/api/projects/{pid}/sessions',headers=H,json={'name':'另一对话','request_id':'other-session-'+uid()})
    assert session.status_code==200,session.text
    r=client.post(f'/api/projects/{pid}/messages',headers=H,json={'text':'继续','request_id':'other-'+uid(),
        'session_id':session.json()['id'],'resume_turn_id':old['id']})
    assert r.status_code==400 and '当前对话' in r.text
