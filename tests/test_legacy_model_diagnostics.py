"""Legacy routing respects the selected model and reports actual failures."""
import json

from agent_service import conversation, provider, context_compaction
from agent_service.store import get, patch, put, delete, uid, now
from test_model_plan_recovery import choice, model
from test_opencode_agent_bridge import turn


def test_catalog_model_used_without_environment_variables(turn, model, monkeypatch):
    config, requests, responses = model
    monkeypatch.setattr(conversation, 'LLM_BASE', '')
    monkeypatch.setattr(conversation, 'LLM_MODEL', '')
    responses.append(choice(json.dumps({'action':'reply','reply':'已理解需求'},ensure_ascii=False)))
    with provider.task_scope(turn['id']):
        result=conversation.decision(get('project',turn['project_id']),turn,'请解释数据处理',None,None)
    assert result.reply=='已理解需求'
    assert len(requests)==1 and requests[0]['model']==config['model']


def test_context_overflow_retries_without_old_history(turn, model, monkeypatch):
    _, requests, responses = model
    monkeypatch.setattr(conversation, 'LLM_BASE', '')
    monkeypatch.setattr(conversation, 'LLM_MODEL', '')
    old_ids=['t_'+uid() for _ in range(4)]
    for index,old_id in enumerate(old_ids):
        put('turn',{'project_id':turn['project_id'],'text':'必须使用不含税口径' if index==0 else '先前问题',
                    'answer':'先前答复','state':'done','created_at':f'2000-01-01T00:00:{index:02d}'},old_id)
    responses.extend([(400,{'error':{'code':'context_length_exceeded','message':'context window exceeded'}}),
                      choice(json.dumps({'action':'reply','reply':'继续'},ensure_ascii=False))])
    try:
        with provider.task_scope(turn['id']):
            result=conversation.decision(get('project',turn['project_id']),turn,'新的问题',None,None)
        assert result.reply=='继续' and len(requests)==2
        before=json.loads(requests[0]['messages'][1]['content'])
        after=json.loads(requests[1]['messages'][1]['content'])
        assert before['history']['recent_verbatim'] and after['history']['recent_verbatim'] and after['request']=='新的问题'
        assert len(before['history']['recent_verbatim'])==4 and after['history']['older_assistant_answers_omitted']
        assert after['history']['recent_verbatim'][0]['user']=='必须使用不含税口径'
        assert 'recover_with' not in json.dumps(after,ensure_ascii=False)
    finally:
        for old_id in old_ids:delete('turn',old_id)


def test_configured_window_prepares_self_contained_history_and_output_reserve(turn,model):
    config,requests,responses=model
    config.update(context_window_tokens=4000,max_output_tokens=500)
    old_ids=[]
    try:
        for i in range(4):
            old_ids.append('t_'+uid())
            put('turn',{'project_id':turn['project_id'],'text':f'第{i}轮用户口径',
                'answer':'历史回答'+('汉字'*3000),'state':'done',
                'created_at':f'2000-01-01T00:00:{i:02d}'},old_ids[-1])
        # Recent answers are retained in full; make them small enough to fit.
        for identifier in old_ids[-2:]:patch('turn',identifier,answer='最近答案')
        responses.append(choice(json.dumps({'action':'reply','reply':'继续'},ensure_ascii=False)))
        with provider.task_scope(turn['id']):
            result=conversation.decision(get('project',turn['project_id']),turn,'新的问题',None,None)
        assert result.reply=='继续' and len(requests)==1 and requests[0]['max_tokens']==500
        history=json.loads(requests[0]['messages'][1]['content'])['history']
        assert history['older_assistant_answers_omitted']
        assert [row['user'] for row in history['recent_verbatim']]==[f'第{i}轮用户口径' for i in range(4)]
        assert 'agent' not in history['recent_verbatim'][0]
        assert history['recent_verbatim'][-1]['agent']=='最近答案'
        assert 'recover_with' not in json.dumps(history,ensure_ascii=False)
    finally:
        for identifier in old_ids:delete('turn',identifier)


def test_compacted_routing_does_not_apply_vague_prior_rule(turn,model):
    config,requests,responses=model
    config.update(context_window_tokens=4000,max_output_tokens=500)
    old_ids=[]
    try:
        for i in range(4):
            old_ids.append('t_'+uid())
            put('turn',{'project_id':turn['project_id'],'text':f'第{i}轮用户口径',
                'answer':('汉字'*3000) if i<2 else '最近答案','state':'done',
                'created_at':f'2000-01-01T00:00:{i:02d}'},old_ids[-1])
        responses.append(choice(json.dumps({'action':'refresh'})))
        with provider.task_scope(turn['id']):
            result=conversation.decision(get('project',turn['project_id']),turn,'请按之前规则重新处理这份数据',None,None)
        assert len(requests)==1 and result.action=='reply' and '具体结果或规则' in result.reply
    finally:
        for identifier in old_ids:delete('turn',identifier)


def test_real_overflow_progressively_drops_large_recent_assistant_answers(turn,model):
    _,requests,responses=model
    ids=[]
    try:
        for i in range(3):
            ids.append('t_'+uid())
            put('turn',{'project_id':turn['project_id'],'text':f'用户要求{i}',
                'answer':'大型旧回答'*1000,'state':'done',
                'created_at':f'2000-01-01T00:00:{i:02d}'},ids[-1])
        overflow=(400,{'error':{'code':'context_length_exceeded','message':'context window exceeded'}})
        responses.extend([overflow,overflow,choice(json.dumps({'action':'reply','reply':'继续'},ensure_ascii=False))])
        with provider.task_scope(turn['id']):
            result=conversation.decision(get('project',turn['project_id']),turn,'新的问题',None,None)
        assert result.reply=='继续' and len(requests)==3
        history=json.loads(requests[-1]['messages'][1]['content'])['history']['recent_verbatim']
        assert [row['user'] for row in history]==[f'用户要求{i}' for i in range(3)]
        assert all('agent' not in row for row in history)
    finally:
        for identifier in ids:delete('turn',identifier)


def test_legacy_history_is_session_scoped_private_and_untruncated(turn, model, monkeypatch):
    _,requests,responses=model
    revoked_id='t_'+uid(); foreign_id='t_'+uid();other_session_id='t_'+uid(); ids=[]
    values=[(revoked_id,{'text':'撤销的秘密','answer':'秘密答复'}),
            (foreign_id,{'actor_id':'someone_else','text':'记住我的个人偏好 PRIVATE','answer':'好的'}),
            (other_session_id,{'session_id':'another_session','text':'另一对话 SECRET','answer':'另一个秘密'})]
    values += [('t_'+uid(),{'text':f'历史第{i}轮','answer':f'第{i}轮答复 '+('长内容'*1500)}) for i in range(12)]
    try:
        for i,(identifier,fields) in enumerate(values):
            put('turn',{'project_id':turn['project_id'],'state':'done','created_at':f'2000-01-01T00:00:{i:02d}',**fields},identifier)
            ids.append(identifier)
        monkeypatch.setattr(context_compaction,'revoked_turn_ids',lambda pid:{revoked_id})
        responses.append(choice(json.dumps({'action':'reply','reply':'继续'},ensure_ascii=False)))
        with provider.task_scope(turn['id']):conversation.decision(get('project',turn['project_id']),turn,'新的问题',None,None)
        context=json.loads(requests[0]['messages'][1]['content'])
        history=context['history']['recent_verbatim']
        assert len(history)==14 and history[-12]['user']=='历史第0轮'
        assert len(history[-1]['agent'])>3500
        serialized=json.dumps(history,ensure_ascii=False)
        assert '撤销的秘密' not in serialized and '秘密答复' not in serialized
        assert 'PRIVATE' not in serialized and '另一对话 SECRET' not in serialized
        assert any(item.get('revoked_memory') for item in history)
        assert any(item.get('personal_other_actor') for item in history)
    finally:
        for identifier in ids:delete('turn',identifier)


def test_legacy_history_honors_model_data_consent(turn, model):
    config,requests,responses=model
    old_id='t_'+uid()
    put('turn',{'project_id':turn['project_id'],'text':'不得泄露的历史','answer':'私人答复','state':'done','created_at':'2000-01-01'},old_id)
    try:
        config['share_data']=False
        responses.append(choice(json.dumps({'action':'reply','reply':'继续'},ensure_ascii=False)))
        source={'profile':{'fields':['secret_field']}}
        pipeline={'spec':{'private':'secret_spec'}}
        with provider.task_scope(turn['id']):conversation.decision(get('project',turn['project_id']),turn,'新的问题',source,pipeline)
        content=requests[0]['messages'][1]['content']
        assert '不得泄露的历史' not in content and '私人答复' not in content
        assert 'secret_field' not in content and 'secret_spec' not in content
        assert '模型业务内容读取未获批准' in content
    finally:delete('turn',old_id)


def test_legacy_history_hides_answer_with_revoked_company_evidence(turn, model, monkeypatch):
    from agent_service import company_search
    from agent_service.store import seal
    _,requests,responses=model
    old_id='t_'+uid()
    put('turn',{'project_id':turn['project_id'],'text':'查阅资料','answer':'原回答公开摘要',
                'secret_knowledge_answer':seal('已撤销的公司资料 SECRET'),
                'knowledge_citations':[{'evidence_id':'missing-evidence'}],
                'state':'done','created_at':'2000-01-01'},old_id)
    try:
        monkeypatch.setattr(company_search,'assert_model_allowed',lambda pid:None)
        monkeypatch.setattr(company_search,'_load',lambda *args:(_ for _ in ()).throw(KeyError('revoked')))
        responses.append(choice(json.dumps({'action':'reply','reply':'继续'},ensure_ascii=False)))
        with provider.task_scope(turn['id']):conversation.decision(get('project',turn['project_id']),turn,'新的问题',None,None)
        content=requests[0]['messages'][1]['content']
        assert '已撤销的公司资料 SECRET' not in content
        assert '相关回答已隐藏' in content
    finally:delete('turn',old_id)


def test_unauthorized_legacy_model_is_paused_without_leaking_response(turn, model, monkeypatch):
    _, requests, responses = model
    monkeypatch.setattr(conversation, 'LLM_BASE', '')
    monkeypatch.setattr(conversation, 'LLM_MODEL', '')
    responses.append(401)
    current=patch('turn',turn['id'],state='queued',text='请解释数据处理')
    with provider.task_scope(turn['id']):conversation.legacy_handle(current)
    saved=get('turn',turn['id'])
    assert len(requests)==1 and saved['outcome']=='paused' and saved['resume_available']
    assert 'HTTP 401' in saved['answer'] and 'controlled error' not in saved['answer']


def test_invalid_legacy_plan_is_not_marked_complete(turn, model, monkeypatch):
    _, _, responses = model
    monkeypatch.setattr(conversation, 'LLM_BASE', '')
    monkeypatch.setattr(conversation, 'LLM_MODEL', '')
    responses.append(choice('not JSON'))
    current=patch('turn',turn['id'],state='queued',text='请解释数据处理')
    with provider.task_scope(turn['id']):conversation.legacy_handle(current)
    saved=get('turn',turn['id'])
    assert saved['outcome']=='needs_input' and '没有执行' in saved['answer']
