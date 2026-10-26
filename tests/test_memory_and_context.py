import json
from datetime import datetime,timedelta,timezone
import pytest
from fastapi.testclient import TestClient
from agent_service import memory_service as memory,context_compaction as context,provider,memory_api
from agent_service.actor_context import actor_scope
from agent_service.store import put,get,delete,all_items,now,uid,unseal,patch
from agent_service.config import TOKEN

A='u_'+'a'*64
B='u_'+'b'*64

@pytest.fixture
def project(monkeypatch):
    pid='p_'+uid();put('project',{'name':'Context acceptance','created_at':now()},pid)
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    kinds=('turn','conversation','context_checkpoint','user_memory','memory_revision')
    before={kind:{item['id'] for item in all_items(kind)} for kind in kinds}
    yield pid
    for kind in kinds:
        for item in all_items(kind):
            if item['id'] not in before[kind]:delete(kind,item['id'])
    delete('project',pid)

removed=set()

def create(pid,title='格式偏好',content='报告使用中文',scope='personal',**extra):
    value=memory.save(pid,memory.MemoryInput(title=title,content=content,scope=scope,**extra))
    removed.add(value['id']);return value


def history(pid,count=15):
    sid='s_'+uid();records=[]
    start=datetime(2026,10,1,tzinfo=timezone.utc)
    put('conversation',{'project_id':pid,'name':'上下文回归对话','created_at':start.isoformat()},sid)
    for i in range(count):
        record=put('turn',{'project_id':pid,'session_id':sid,'actor_id':A,'created_at':(start+timedelta(minutes=i)).isoformat(),
            'text':('Do not aggregate balances. 只统计已付款，金额单位元。' if i==0 else '本轮业务目标'+str(i))+'原始细节'+str(i)*1200,
            'answer':'工具已完成并保存结果'+str(i)*1200,'state':'done','outcome':'answered',
            'source_ids':['src_'+str(i)],'agent_trace':[{'id':'step'+str(i),'tool':'query_sql','status':'done','arguments':{},
                'observation':{'run_id':'run_'+str(i),'manifest_sha256':'a'*64,'rows':[[str(i)]]}}]},'t_'+uid())
        records.append(record)
    current=put('turn',{'project_id':pid,'session_id':sid,'actor_id':A,'created_at':(start+timedelta(minutes=count+1)).isoformat(),
        'text':'继续出报表','state':'agent','model_id':'finance','model_catalog_revision':'frozen-model'},'t_'+uid())
    return records,current


def test_personal_memory_scope_project_shared_and_encrypted(project):
    with actor_scope(A):
        first=create(project);shared=create(project,'项目口径','收入采用不含税金额','project',category='decision')
        assert not first['trusted_financial_rule'] and not shared['trusted_financial_rule']
        assert '中文' not in json.dumps(get('user_memory',first['id']),ensure_ascii=False)
        assert len(memory.list_memories(project))==2
    with actor_scope(B):
        assert [v['id'] for v in memory.list_memories(project)]==[shared['id']]
        with pytest.raises(KeyError):memory.forget(project,first['id'],1)
        create(project,'格式偏好','报告使用英文')
    with actor_scope(A):assert memory.list_memories(project,'personal')[0]['content']=='报告使用中文'


def test_memory_conflict_versions_and_forget_purge_revisions(project):
    with actor_scope(A):
        first=create(project)
        with pytest.raises(ValueError,match='同名'):create(project)
        edited=memory.save(project,memory.MemoryInput(title='格式偏好',content='报告用简体中文',scope='personal'),first['id'],1)
        assert edited['version']==2
        with pytest.raises(ValueError,match='已变化'):memory.forget(project,first['id'],1)
        memory.forget(project,first['id'],2)
        assert memory.list_memories(project)==[]
        assert 'secret_content' not in get('user_memory',first['id'])
        assert not [r for r in all_items('memory_revision') if r.get('memory_id')==first['id']]


@pytest.mark.parametrize('content',['api_key=REAL-PRIVATE','Bearer private-value','password: private-value','sk-abcdefghijklmnop'])
def test_secret_memory_rejected(project,content):
    with pytest.raises(ValueError):create(project,content=content)


def test_agent_only_remembers_explicit_current_quote_and_no_metric_approval(project):
    with actor_scope(A):
        turn=put('turn',{'project_id':project,'text':'请记住：我偏好先看表格，再看结论。','created_at':now()},'t_'+uid())
        with pytest.raises(ValueError):memory.tool(turn,'remember_memory',{'title':'格式','quote':'AI猜测用户喜欢图表'})
        value=memory.tool(turn,'remember_memory',{'title':'格式','quote':'我偏好先看表格，再看结论。','scope':'personal'})
        removed.add(value['id']);assert value['provenance']['source_turn_id']==turn['id']
        assert value['trusted_financial_rule'] is False
        with pytest.raises(ValueError):memory.tool(turn,'forget_memory',{'memory_id':value['id'],'expected_version':1})


def test_resumed_task_cannot_reuse_old_memory_consent_as_current_instruction(project):
    with actor_scope(A):
        turn={'id':'current','project_id':project,'text':'继续，记住以后使用中文表格','work_request':'记住以前使用英文表格'}
        with pytest.raises(ValueError):memory.tool(turn,'remember_memory',{'title':'Language','quote':'以前使用英文表格'})
        saved=memory.tool(turn,'remember_memory',{'title':'Language','quote':'以后使用中文表格'})
        assert saved['content']=='以后使用中文表格'
        turn['work_request']='删除Language记忆';turn['text']='继续做报告'
        with pytest.raises(ValueError):memory.tool(turn,'forget_memory',{'memory_id':saved['id'],'expected_version':1})


def test_memory_share_policy_expiry_and_generation(project,monkeypatch):
    with actor_scope(A):
        before=memory.generation(project);value=create(project,expires_at=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat())
        assert memory.generation(project)!=before and memory.context_for_turn({'project_id':project,'text':'中文'})['included']==1
        monkeypatch.setattr(provider,'settings',lambda:{'share_data':False})
        assert memory.context_for_turn({'project_id':project,'text':'中文'})['items']==[]
        record=get('user_memory',value['id']);record['expires_at']='2020-01-01T00:00:00+00:00';put('user_memory',record)
        assert memory.list_memories(project,include_expired=False)==[]
        assert memory.list_memories(project)[0]['expired']


def test_time_crossing_expiry_invalidates_generation_and_original_preference(project,monkeypatch):
    with actor_scope(A):
        records,current=history(project)
        patch('turn',records[0]['id'],text='Remember I prefer violet charts.')
        expires=datetime.now(timezone.utc)+timedelta(minutes=1)
        remembered=memory.tool(get('turn',records[0]['id']),'remember_memory',
            {'title':'Temporary color','quote':'I prefer violet charts.','scope':'personal','expires_at':expires.isoformat()})
        before=memory.generation(project)
        assert 'violet' in json.dumps(context.context_for_turn(current))
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return expires+timedelta(seconds=1)
        monkeypatch.setattr(memory,'datetime',Clock)
        assert memory.generation(project)!=before
        assert not memory.list_memories(project,include_expired=False)
        assert records[0]['id'] in memory.revoked_turn_ids(project)
        assert 'violet' not in json.dumps(context.context_for_turn(current))


def test_no_share_model_cannot_receive_active_memory_through_project_history(project,monkeypatch):
    from agent_service import agent
    with actor_scope(A):
        records,current=history(project,8)
        original=patch('turn',records[0]['id'],
            text='Continue finance analysis. Remember I prefer PRIVATE_HISTORY_STYLE.',
            answer='Prior confidential result: PRIVATE_BUSINESS_VALUE_5938')
        memory.tool(original,'remember_memory',
            {'title':'Private history preference','quote':'I prefer PRIVATE_HISTORY_STYLE.','scope':'personal'})
        current=patch('turn',current['id'],text='Continue finance analysis; explain this current user request.')
        allowed=agent.search_history(project,current['text'],5)
        assert any(item['turn_id']==original['id'] for item in allowed)
        monkeypatch.setattr(provider,'settings',lambda:{'share_data':False})
        # Both automatic retrieval and the explicit Agent search tool use this
        # same filtered search function, so an active preference cannot bypass
        # the memory/context content grant through an ordinary prior answer.
        assert agent.search_history(project,current['text'],5)==[]
        messages,_=agent.planning_context(current,native=False)
        info=json.loads(messages[1]['content'])
        assert info['project_history_matches']==[] and info['memory_context']['items']==[]
        assert info['request']==current['text']
        encoded=json.dumps(info,ensure_ascii=False)
        assert 'PRIVATE_HISTORY_STYLE' not in encoded and 'PRIVATE_BUSINESS_VALUE_5938' not in encoded
        assert 'explain this current user request' in encoded
        with pytest.raises(ValueError,match='读取未获批准'):context.read_original(current,original['id'])


def test_long_context_checkpoint_is_durable_smaller_and_recovers_original(project):
    with actor_scope(A):
        records,current=history(project)
        first=context.context_for_turn(current)
        assert first['compression']['active'] and len(first['recent_verbatim'])==6
        assert first['compression']['context_characters']<first['compression']['source_characters']
        assert first['summary']['summary_is_financial_evidence'] is False
        assert first['summary']['coverage'][0]['turn_id']==records[0]['id']
        quotes='\n'.join(item.get('quote','') for item in first['critical_constraints'])
        assert 'Do not aggregate balances' in quotes and '金额单位元' in quotes
        assert first['frozen_task']['model_catalog_revision']=='frozen-model'
        assert first==context.context_for_turn(current)
        recovered=context.read_original(current,records[0]['id'],limit=500)
        assert '原始细节' in recovered['content'] and recovered['next_offset'] is not None
        assert len(unseal(get('context_checkpoint',first['summary_id'])['secret_summary'])['coverage'])==9
        assert len(context.read_checkpoint(current,first['summary_id'])['items'])==9


def test_forget_invalidates_summary_history_matches_trace_and_source_quote(project):
    with actor_scope(A):
        records,current=history(project)
        patch('turn',records[0]['id'],text='记住我的个人偏好：只输出蓝色图表')
        value=memory.tool(get('turn',records[0]['id']),'remember_memory',{'title':'图表','quote':'只输出蓝色图表','scope':'personal'})
        removed.add(value['id'])
        before=context.context_for_turn(current)
        assert '蓝色' in json.dumps(before,ensure_ascii=False)
        memory.forget(project,value['id'],1)
        after=context.context_for_turn(current)
        assert after['memory_generation']!=before['memory_generation'] and '蓝色' not in json.dumps(after,ensure_ascii=False)
        assert context.filter_history_matches(project,[{'turn_id':records[0]['id'],'user':'蓝色'}])==[]
        sanitized=context.trace_for_model(current,[{'tool':'recall_memories','arguments':{'quote':'蓝色'},'observation':{'content':'蓝色'}}])
        assert '蓝色' not in json.dumps(sanitized,ensure_ascii=False)
        recovered=context.read_original(current,records[0]['id'],limit=10000)
        assert '蓝色' not in recovered['content'] and recovered['memory_withdrawal_applied']


def test_cross_project_and_other_actor_personal_context_do_not_leak(project):
    with actor_scope(A):records,current=history(project)
    with actor_scope(B):
        patch('turn',records[0]['id'],text='记住我的个人偏好：只输出蓝色图表')
        value=context.context_for_turn(current)
        assert '蓝色' not in json.dumps(value,ensure_ascii=False)
        foreign=put('project',{'name':'foreign'},'p_'+uid())
        try:
            with pytest.raises(ValueError,match='其他项目'):context.read_original({'project_id':foreign['id']},records[0]['id'])
        finally:delete('project',foreign['id'])


def test_foreign_personal_provenance_is_suppressed_in_any_language(project):
    with actor_scope(A):
        records,current=history(project)
        patch('turn',records[0]['id'],text='Remember my preference: I prefer English tables.')
        remembered=memory.tool(get('turn',records[0]['id']),'remember_memory',
            {'title':'Language','quote':'I prefer English tables.','scope':'personal'})
        # The durable source binding still applies if imported/edited text no
        # longer contains an English or Chinese explicit-memory keyword.
        patch('turn',records[0]['id'],text='Mes tableaux doivent être en anglais.')
    with actor_scope(B):
        value=context.context_for_turn(current)
        assert 'Mes tableaux' not in json.dumps(value,ensure_ascii=False)
        assert context.filter_history_matches(project,[{'turn_id':records[0]['id'],'user':'Mes tableaux'}])==[]


def test_forgetting_memory_does_not_reinject_derived_answer_or_old_receipt(project):
    with actor_scope(A):
        records,current=history(project)
        remembered=create(project,title='Color',content='Prefer blue tables')
        patch('turn',records[1]['id'],text='Continue current work',answer='Using blue tables as preferred',
              memory_context_versions={remembered['id']:1})
        patch('turn',records[2]['id'],agent_trace=[{'id':'read_old_memory','tool':'recall_memories','status':'done',
            'arguments':{},'observation':{'content':'Prefer blue tables'}}])
        memory.forget(project,remembered['id'],1)
        value=context.context_for_turn(current)
        assert 'Using blue tables' not in json.dumps(value,ensure_ascii=False)
        assert context.filter_history_matches(project,[{'turn_id':records[1]['id'],'answer':'Using blue tables'}])==[]
        recovered=context.read_original(current,records[2]['id'],limit=10000)
        assert 'Prefer blue tables' not in recovered['content']


def test_memory_routes_require_auth_and_support_conflict_safe_crud(project):
    from agent_service.api import app
    client=TestClient(app)
    route=f'/api/projects/{project}/memories'
    assert client.get(route).status_code==401
    headers={'Authorization':'Bearer '+TOKEN}
    value={'scope':'personal','category':'preference','title':'Route preference','content':'Use Chinese tables'}
    first=client.post(route,headers=headers,json=value)
    assert first.status_code==200
    saved=first.json();identity=saved['id']
    assert saved['version']==1 and not saved['trusted_financial_rule']
    assert client.post(route,headers=headers,json=value).status_code==409
    changed=client.put(route+'/'+identity,headers=headers,json=dict(value,content='Use concise Chinese tables',expected_version=1))
    assert changed.status_code==200 and changed.json()['version']==2
    assert client.delete(route+'/'+identity+'?expected_version=1',headers=headers).status_code==409
    assert client.get(route,headers=headers).json()['items'][0]['content']=='Use concise Chinese tables'
    assert client.delete(route+'/'+identity+'?expected_version=2',headers=headers).status_code==200
    assert client.get(route,headers=headers).json()['items']==[]


def test_api_identity_is_server_derived_and_separate_from_shared_project_rules(project,monkeypatch):
    from agent_service.api import app
    from agent_service import identity
    client=TestClient(app)
    route=f'/api/projects/{project}/memories'
    monkeypatch.setattr(identity,'mode',lambda:'oidc')
    # Each verified principal is provided by the authentication dependency;
    # the mutation body has no actor or owner field.
    monkeypatch.setattr(identity,'principal',lambda request:{'authenticated':True,'mode':'oidc','subject':'alice','issuer':'https://issuer.invalid'})
    value={'scope':'personal','category':'preference','title':'Language','content':'Use English'}
    assert client.post(route,json=value).status_code==200
    shared=client.post(route,json=dict(value,scope='project',title='Approved source selection')).json()
    monkeypatch.setattr(identity,'principal',lambda request:{'authenticated':True,'mode':'oidc','subject':'bob','issuer':'https://issuer.invalid'})
    assert [x['id'] for x in client.get(route).json()['items']]==[shared['id']]
    assert client.post(route,json=dict(value,owner_id=A)).status_code==422


def test_thousand_turns_have_bounded_model_projection_and_complete_paged_coverage(project):
    with actor_scope(A):
        records,current=history(project,1000)
        value=context.context_for_turn(current)
        assert len(json.dumps(value,ensure_ascii=False))<40000
        assert value['summary']['covered_turns']==994 and not value['summary']['coverage_is_complete']
        seen=[];offset=0
        while offset is not None:
            page=context.read_checkpoint(current,value['summary_id'],offset,50)
            seen.extend(item['turn_id'] for item in page['items']);offset=page['next_offset']
        assert seen==[item['id'] for item in records[:-6]]


def test_critical_constraints_cannot_be_silently_dropped(project):
    with actor_scope(A):
        records,current=history(project,8)
        patch('turn',records[0]['id'],text='Do not send these records. '+('only this currency. '*1500))
        with pytest.raises(ValueError,match='关键约束'):context.context_for_turn(current)


def test_large_unresolved_task_index_also_obeys_critical_budget(project):
    with actor_scope(A):
        records,current=history(project,100)
        for record in records:patch('turn',record['id'],text='Continue source review',outcome='needs_input')
        with pytest.raises(ValueError,match='关键约束'):context.context_for_turn(current)


def test_deleted_session_invalidates_context_and_hides_originals(project):
    from agent_service import workspace_lifecycle as life
    with actor_scope(A):
        records,current=history(project)
        _,survivor=history(project,0)
        before=context.context_for_turn(current);generation=memory.generation(project)
        assert context.read_original(survivor,records[0]['id'])['turn_id']==records[0]['id']
        life.delete_session(project,current['session_id'])
        assert memory.generation(project)!=generation
        assert context.filter_history_matches(project,[{'turn_id':records[0]['id'],'user':'旧对话内容'}])==[]
        with pytest.raises(life.WorkspaceDeleted):context.context_for_turn(current)
        with pytest.raises(life.WorkspaceDeleted):context.read_original(survivor,records[0]['id'])
        with pytest.raises(ValueError,match='已变更'):context.read_checkpoint(survivor,before['summary_id'])
        deleted_generation=memory.generation(project)
        life.restore_session(project,current['session_id'])
        assert memory.generation(project)!=deleted_generation
        assert context.read_original(survivor,records[0]['id'])['turn_id']==records[0]['id']
        assert get('turn',current['id'])['outcome']=='cancelled'


def test_deleted_session_native_history_is_retired_before_next_prompt(project,monkeypatch):
    import httpx
    from agent_service import opencode_runtime as native,workspace_lifecycle as life
    monkeypatch.setattr(native,'ready',lambda:True)
    monkeypatch.setattr(native,'connection',lambda value=None:{'base_url':'http://127.0.0.1:1','api_key':'fixture',
        'model_ref':{'providerID':'fixture','id':'fixture'}})
    metadata={};requests=[]
    def request(value,method,path,body=None,**kwargs):
        requests.append((method,path,body))
        if path=='/api/session':metadata.update(body['metadata']);return {'data':{'id':'native-after-trash'}}
        if path.endswith('/prompt'):
            bridge=metadata['atlas_bridge']
            with httpx.Client(trust_env=False,timeout=5) as client:
                reply=client.post(bridge['url'],headers={'Authorization':'Bearer '+bridge['token']},json={
                    'session_id':'native-after-trash','op':'step','call_id':'final','message_id':'new-message',
                    'step':{'kind':'final','answer':'只读取新的上下文'}})
                assert reply.status_code==200 and reply.json()['terminal']
            return {'data':{}}
        return {'data':[]}
    monkeypatch.setattr(native,'_request',request)
    with actor_scope(A):
        _,deleted=history(project,0);_,survivor=history(project,0)
        survivor=patch('turn',survivor['id'],opencode_session_id='native-containing-deleted-chat',
            opencode_memory_generation=memory.generation(project))
        life.delete_session(project,deleted['session_id'])
        def callback(step,call_id):
            patch('turn',survivor['id'],state='done',outcome='answered',answer=step['answer'])
            return {'answered':True}
        receipt=native.run_session(get('turn',survivor['id']),'Use current Atlas context.',{'project_history_matches':[]},callback)
        final=get('turn',survivor['id'])
        assert receipt['session_id']=='native-after-trash'
        assert final['opencode_retired_sessions']==['native-containing-deleted-chat']
        assert final['opencode_memory_generation']==memory.generation(project)
        assert not any('native-containing-deleted-chat' in path for _,path,_ in requests)
        assert sum(path=='/api/session' for _,path,_ in requests)==1


def test_native_own_memory_write_rebuilds_context_without_user_pause(project,monkeypatch):
    import httpx
    from agent_service import opencode_runtime as native
    monkeypatch.setattr(native,'ready',lambda:True)
    monkeypatch.setattr(native,'connection',lambda value=None:{'base_url':'http://127.0.0.1:1','api_key':'fixture','model_ref':{'providerID':'fixture','id':'fixture'}})
    sessions=[];metadata={};bridge_replies=[]
    def request(value,method,path,body=None,**kwargs):
        if path=='/api/session':
            identity='native-'+str(len(sessions));sessions.append(identity);metadata[identity]=body['metadata'];return {'data':{'id':identity}}
        if path.endswith('/prompt'):
            identity=path.split('/')[3];bridge=metadata[identity]['atlas_bridge']
            with httpx.Client(trust_env=False,timeout=5) as client:
                response=client.post(bridge['url'],headers={'Authorization':'Bearer '+bridge['token']},
                    json={'session_id':identity,'op':'step','call_id':'call-1','message_id':'msg-1',
                          'step':{'kind':'tool','tool':'remember_memory','arguments':{}}})
                bridge_replies.append(response.json())
                # Even if OpenCode races its next request, it is stopped before
                # another upstream model call receives the old memory context.
                bridge_replies.append(client.post(bridge['url'],headers={'Authorization':'Bearer '+bridge['token']},
                    json={'session_id':identity,'op':'model_request'}).json())
            return {'data':{}}
        return {'data':[]}
    monkeypatch.setattr(native,'_request',request)
    with actor_scope(A):
        turn=put('turn',{'project_id':project,'text':'请记住：先看表格','state':'agent','created_at':now()},'t_'+uid())
        def callback(step,call_id):
            remembered=create(project,title='View order',content='先看表格')
            patch('turn',turn['id'],agent_trace=[{'id':'step','opencode_call_id':call_id,'tool':'remember_memory','status':'done','arguments':{},'observation':remembered}])
            return {'saved':True}
        receipt=native.run_session(turn,'Use Atlas tools.',{},callback)
        final=get('turn',turn['id'])
        assert receipt['context_rebuild_required'] and final['state']=='agent'
        assert final['opencode_session_id'] is None and final['opencode_retired_sessions']==sessions
        assert len(final['agent_trace'])==1 and len(memory.list_memories(project))==1
        assert bridge_replies[0]['context_rebuild_required'] and bridge_replies[1]['terminal']


def test_native_external_memory_change_stops_next_old_context_request(project,monkeypatch):
    import httpx
    from agent_service import opencode_runtime as native
    monkeypatch.setattr(native,'ready',lambda:True)
    monkeypatch.setattr(native,'connection',lambda value=None:{'base_url':'http://127.0.0.1:1','api_key':'fixture','model_ref':{'providerID':'fixture','id':'fixture'}})
    metadata={};responses=[]
    def request(value,method,path,body=None,**kwargs):
        if path=='/api/session':metadata.update(body['metadata']);return {'data':{'id':'native-external'}}
        if path.endswith('/prompt'):
            bridge=metadata['atlas_bridge']
            with httpx.Client(trust_env=False,timeout=5) as client:
                for payload in ({'op':'step','call_id':'call','message_id':'msg','step':{'kind':'tool','tool':'project_context','arguments':{}}},
                                {'op':'model_request'}):
                    responses.append(client.post(bridge['url'],headers={'Authorization':'Bearer '+bridge['token']},
                        json=dict(payload,session_id='native-external')).json())
            return {'data':{}}
        return {'data':[]}
    monkeypatch.setattr(native,'_request',request)
    with actor_scope(A):
        remembered=create(project)
        turn=put('turn',{'project_id':project,'text':'Continue finance work','state':'agent','created_at':now()},'t_'+uid())
        def callback(step,call_id):
            # A UI deletion can arrive while this unrelated business tool runs.
            memory.forget(project,remembered['id'],1)
            return {'project_available':True}
        with pytest.raises(provider.BudgetExceeded,match='记忆已被修改或撤回'):
            native.run_session(turn,'Use Atlas tools.',{},callback)
        assert '记忆已被修改或撤回' in responses[-1]['error']
