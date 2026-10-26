import hashlib
import json
from datetime import datetime,timedelta,timezone
import pytest

from agent_service import agent_team as team,model_catalog,provider,conversation,workspace
from agent_service.config import DATA
from agent_service.store import put,get,patch,all_items,delete,seal,unseal,now,uid
from agent_service.actor_context import actor_scope


def configuration(suffix='v1'):
    return {'default_model_id':'finance','models':[{'id':'finance','name':'财经模型',
        'base_url':'https://finance.example.test/v1','model':'finance-'+suffix,'api_key':'fixture-not-real-key',
        'share_data':True}]}


@pytest.fixture
def parent(monkeypatch):
    kinds=('turn','project','conversation','source','model_catalog','collab_task','collab_budget','context_checkpoint')
    previous={kind:{item['id'] for item in all_items(kind)} for kind in kinds}
    saved={}
    for identity in ('model-catalog','model-provider'):
        try:saved[identity]=get('system',identity)
        except KeyError:pass
        delete('system',identity)
    model_catalog.provision(configuration(),verify=False)
    pid=conversation.create_project(conversation.ProjectInput(name='协同复核测试',request_id='team-project-'+uid()))['id']
    raw=b'id,amount\nA,100.25\nB,20.05\n';name=uid()+'.csv'
    (DATA/'uploads').mkdir(exist_ok=True);(DATA/'uploads'/name).write_bytes(raw)
    source=put('source',{'project_id':pid,'kind':'file','name':'finance.csv','status':'ready','created_at':now(),
        'revision':1,'content_hash':hashlib.sha256(raw).hexdigest(),'secret_config':seal({'stored_name':name}),
        'profile':{'fields':[{'name':'c_1','label':'id'},{'name':'c_2','label':'amount'}],'sample_rows':2}},'s_'+uid())
    turn=conversation.submit(pid,conversation.TurnInput(text='请复核收入。不要把预算与实际相加；金额单位元。',
        request_id='team-parent-'+uid(),source_ids=[source['id']]))
    turn=patch('turn',turn['id'],state='agent',task_contract={'version':1,'kind':'answer','requirements':[],
        'application_ids':[],'goal':'核对收入','acceptance':'核对来源后回答','budgets':{'model_calls':20,'input_characters':1000000,'output_tokens':64000,'duration_ms':120000}})
    monkeypatch.setenv('ATLAS_OPENCODE_RUNTIME','0')
    yield turn,source
    for kind in reversed(kinds):
        for item in all_items(kind):
            if item['id'] not in previous[kind]:delete(kind,item['id'])
    for identity in ('model-catalog','model-provider'):
        delete('system',identity)
        if identity in saved:put('system',saved[identity],identity)
    (DATA/'uploads'/name).unlink(missing_ok=True)


def task(parent,peer='local_data_checker',sources=None,results=None,goal='核对收入字段、单位及范围'):
    root,source=parent
    assignment={'goal':goal,'source_ids':sources if sources is not None else [source['id']],'result_ids':results or []}
    record=put('collab_task',{'project_id':root['project_id'],'actor_id':'owner','parent_turn_id':root['id'],
        'peer_id':peer,'transport':'local','state':'queued','created_at':now(),
        'deadline_at':(datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat(),
        'assignment':assignment},'ct_'+uid())
    return record,assignment


def create(parent,**kwargs):
    record,assignment=task(parent,**kwargs)
    return team.create_local(parent[0],record,assignment)


def test_builtins_have_specific_readonly_roles_and_no_remote_configuration():
    peers=team.builtin_peers()
    assert {p['id'] for p in peers}==set(team.ROLES)
    assert all(p['readonly'] and not p['financial_approval'] and not p['requires_configuration'] for p in peers)


def test_child_is_real_durable_turn_with_separate_session_and_original_model(parent):
    root,source=parent;old_revision=root['model_catalog_revision']
    model_catalog.provision(configuration('v2'),verify=False)
    record=create(parent);child=get('turn',record['child_turn_id'])
    assert child['interaction_mode']=='chat' and child['collaboration_readonly']
    assert child['session_id']!=root['session_id'] and child['collaboration_parent_turn_id']==root['id']
    assert get('conversation',child['session_id'])['internal'] is True
    assert child['session_id'] not in {item['id'] for item in conversation.sessions(root['project_id'])}
    assert child['model_catalog_revision']==old_revision
    assert child['source_ids']==[source['id']]
    assert child['attached_sources'][0]['content_hash']==source['content_hash']
    with provider.task_scope(child['id']):assert provider.settings()['model']=='finance-v1'
    assert '不要把预算与实际相加' in team.context_for_turn(child)['parent_request_verbatim']
    assert not child.get('before_version') and not child.get('work_version')


def test_unlimited_parent_can_delegate_without_creating_a_hidden_parent_cap(parent):
    from agent_service.collaboration_budget import current_totals
    from agent_service.task_budget import effective_limits
    root,source=parent
    contract=dict(root['task_contract'],budget_policy='unlimited',
                  budgets=dict.fromkeys(('model_calls','input_characters','output_tokens','duration_ms'),None))
    root=patch('turn',root['id'],task_contract=contract,
        model_usage={'calls':100,'input_characters':2000000,'output_tokens_budgeted':200000,'duration_ms':900000})
    record=create((root,source));child=get('turn',record['child_turn_id'])
    assert all(value is None for value in current_totals(root)['remaining'].values())
    assert current_totals(root)['used']['calls']==100
    assert child['task_contract']['budgets']=={'model_calls':5,'input_characters':120000,
        'output_tokens':16000,'duration_ms':120000}
    assert effective_limits(child)['model_calls']==5 and child['collaboration_readonly']
    from agent_service.agent_context import selected
    assert 'source_schema' in selected(child)


def test_child_creation_retries_keep_same_turn_and_session(parent):
    root,_=parent;record,assignment=task(parent)
    first=team.create_local(root,record,assignment)
    second=team.create_local(root,get('collab_task',record['id']),assignment)
    assert first['child_turn_id']==second['child_turn_id']
    children=[t for t in all_items('turn') if t.get('collaboration_task_id')==record['id']]
    assert len(children)==1


def test_only_explicit_sources_and_results_are_readable(parent):
    record=create(parent);child=get('turn',record['child_turn_id'])
    allowed=team.allowed_tools(child,{'source_schema','read_file','write_file','run_python_file','read_work_tool','delegate_agents','remember_memory'})
    assert allowed=={'source_schema','read_file'}
    team.assert_turn_allowed(child,'source_schema',{'source_id':parent[1]['id']})
    for name,args in [('write_file',{}),('delegate_agents',{}),('source_schema',{'source_id':'other'}),
                      ('read_data_workflow_result',{'run_id':'other'}),('query_sql',{'sql':'select 1'})]:
        with pytest.raises(ValueError):team.assert_turn_allowed(child,name,args)


def test_changed_source_version_and_parent_cancel_stop_running_child(parent):
    root,source=parent;record=create(parent);child=get('turn',record['child_turn_id'])
    patch('source',source['id'],revision=2)
    with pytest.raises(ValueError,match='附件已变更'):team.assert_turn_allowed(child)
    patch('source',source['id'],revision=1)
    patch('turn',root['id'],state='done',outcome='cancelled')
    with pytest.raises(ValueError,match='已停止'):team.assert_turn_allowed(child)
    team.cancel_children(root)
    assert get('collab_task',record['id'])['state']=='cancelled'
    assert get('turn',child['id'])['outcome']=='cancelled'


def test_cannot_delegate_cross_identity_nested_foreign_source_or_excess_children(parent):
    root,_=parent
    with actor_scope('u_'+'a'*64):
        record,assignment=task(parent)
        with pytest.raises(ValueError,match='自己'):team.create_local(root,record,assignment)
    foreign=put('source',{'project_id':'other','status':'ready','name':'foreign.csv'},'s_'+uid())
    record,assignment=task(parent,sources=[foreign['id']])
    with pytest.raises(ValueError,match='不属于当前项目'):team.create_local(root,record,assignment)
    first=create(parent);child=get('turn',first['child_turn_id'])
    record,assignment=task(parent)
    with pytest.raises(ValueError,match='不能继续委派'):team.create_local(child,record,assignment)
    for _ in range(3):create(parent)
    with pytest.raises(ValueError,match='最多创建4'):create(parent)


def test_run_child_executes_existing_agent_tools_without_project_write(parent,monkeypatch):
    root,source=parent;record=create(parent);child_id=record['child_turn_id']
    versions=len(workspace.history(root['project_id']))
    calls=[]
    answers=[{'kind':'tool','tool':'source_schema','arguments':{'source_id':source['id']}},
             {'kind':'final','answer':'已读取真实字段和来源版本；尚需完整控制数核对。'}]
    def model(messages,**kwargs):
        info=json.loads(messages[1]['content']);calls.append({'context':info,
            'tool_names':[tool.get('function',{}).get('name') for tool in kwargs.get('tools') or []]})
        return json.dumps(answers.pop(0),ensure_ascii=False)
    monkeypatch.setattr(provider,'complete',model)
    for _ in range(4):
        team.run_child(child_id)
        if get('turn',child_id)['state'] in ('done','failed'):break
    child=get('turn',child_id);finished=get('collab_task',record['id'])
    assert child['state']=='done' and child['outcome']=='answered',child
    assert finished['state']=='succeeded' and finished['evidence_count']==1
    assert unseal(finished['secret_result'])['opinion_is_financial_evidence'] is False
    assert calls and all('write_file' not in c['tool_names'] for c in calls)
    assert 'source_schema' in calls[0]['tool_names']
    assert len(workspace.history(root['project_id']))==versions and not child.get('work_version')


def test_model_prose_without_read_receipt_is_not_successful_review(parent):
    record=create(parent);child_id=record['child_turn_id']
    patch('turn',child_id,state='done',outcome='answered',answer='一切正确，已经核对')
    result=team.reconcile_local(record)
    assert result['state']=='input_required' and result['evidence_count']==0
    assert '缺少' in result['error']


def test_review_receipts_must_cover_every_assigned_source(parent):
    original=parent[1]
    second=put('source',{key:value for key,value in original.items() if key!='id'},'s_'+uid())
    record=create(parent,sources=[original['id'],second['id']]);child_id=record['child_turn_id']
    patch('turn',child_id,state='done',outcome='answered',answer='只读了一张表',agent_trace=[
        {'id':'one-source','tool':'source_schema','status':'done','arguments':{'source_id':original['id']},'observation':{'source_id':original['id']}}])
    result=team.reconcile_local(record);payload=unseal(result['secret_result'])
    assert result['state']=='input_required'
    assert payload['input_read_coverage']['missing_source_ids']==[second['id']]


def test_metadata_receipt_does_not_count_as_reading_result_and_other_version_is_blocked(parent):
    from agent_service import jobs
    root,source=parent
    app=put('pipeline',{'project_id':root['project_id'],'name':'收入合计','published_run':'published',
        'candidate_run':'candidate','source_id':source['id']},'pl_'+uid())
    run=jobs.save({'kind':'pipeline','project_id':root['project_id'],'target':app['id'],'validated':True,
        'status':'succeeded'},'j_'+uid())
    patch('pipeline',app['id'],published_run=run['id'])
    try:
        record=create(parent,results=[run['id']]);child=get('turn',record['child_turn_id'])
        team.assert_turn_allowed(child,'query_sql',{'application_id':app['id'],'sql':'select 1'})
        with pytest.raises(ValueError,match='未分配的结果版本'):
            team.assert_turn_allowed(child,'view_result',{'application_id':app['id']})
        patch('turn',child['id'],state='done',outcome='answered',answer='仅查了指标状态',agent_trace=[
            {'id':'source','tool':'source_schema','status':'done','arguments':{'source_id':source['id']},'observation':{}},
            {'id':'metadata','tool':'inspect_metric','status':'done','arguments':{'application_id':app['id']},'observation':{'run_id':run['id']}}])
        result=team.reconcile_local(record)
        assert result['state']=='input_required'
        assert unseal(result['secret_result'])['input_read_coverage']['missing_result_ids']==[run['id']]
    finally:
        from sqlalchemy import delete as sql_delete
        from agent_service.store import engine
        with engine.begin() as connection:connection.execute(sql_delete(jobs.jobs).where(jobs.jobs.c.id==run['id']))
        delete('pipeline',app['id'])


def test_read_receipt_hash_and_child_artifact_references_are_retained(parent):
    record=create(parent);child_id=record['child_turn_id']
    patch('turn',child_id,state='done',outcome='answered',answer='发现来源合计待核对',agent_trace=[
        {'id':'evidence-1','tool':'source_schema','status':'done','arguments':{'source_id':parent[1]['id']},'observation':{'field':'amount'}}])
    result=team.reconcile_local(record);payload=unseal(result['secret_result'])
    assert len(payload['evidence'][0]['observation_sha256'])==64 and payload['evidence'][0]['step_id']=='evidence-1'
    assert payload['source_ids']==[parent[1]['id']] and payload['requires_coordinator_verification']


def test_deadline_expires_and_runnable_lane_excludes_regular_turn(parent):
    record=create(parent);child_id=record['child_turn_id']
    assert child_id in team.runnable_children() and parent[0]['id'] not in team.runnable_children()
    patch('collab_task',record['id'],deadline_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
    with pytest.raises(ValueError,match='截止时间'):team.assert_turn_allowed(get('turn',child_id))
    assert team.reconcile_local(record)['state']=='failed'
    assert child_id not in team.runnable_children()


def test_missing_child_insert_is_safe_to_retry_after_crash(parent,monkeypatch):
    record,assignment=task(parent);original=conversation.submit
    monkeypatch.setattr(conversation,'submit',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('crash before insert')))
    with pytest.raises(RuntimeError):team.create_local(parent[0],record,assignment)
    waiting=get('collab_task',record['id']);assert waiting['child_turn_id']
    assert team.reconcile_local(waiting)['state']=='queued'
    monkeypatch.setattr(conversation,'submit',original)
    completed=team.create_local(parent[0],waiting,assignment)
    assert completed['child_turn_id']==waiting['child_turn_id']
    assert get('turn',completed['child_turn_id'])['collaboration_task_id']==record['id']
