"""Recent user messages determine project-scoped conversation ordering."""
from agent_service import conversation as chat
from agent_service.store import get, patch, put, uid
from test_multi_file_conversation import H, project


def test_recent_message_reorders_old_session_and_retry_does_not_touch_it(project):
    pid,client,_=project
    first=chat.create_session(pid,chat.SessionInput(name='旧会话',request_id='old-'+uid()))
    second=chat.create_session(pid,chat.SessionInput(name='新会话',request_id='new-'+uid()))
    patch('conversation',first['id'],created_at='2001-01-01T00:00:00+00:00')
    patch('conversation',second['id'],created_at='2002-01-01T00:00:00+00:00')
    assert [x['id'] for x in chat.sessions(pid)].index(second['id']) < [x['id'] for x in chat.sessions(pid)].index(first['id'])
    body={'text':'整理这次需求','session_id':first['id'],'request_id':'activity-'+uid()}
    response=client.post(f'/api/projects/{pid}/messages',headers=H,json=body)
    assert response.status_code==200,response.text
    saved=get('conversation',first['id'])['last_activity_at']
    assert chat.sessions(pid)[0]['id']==first['id']
    again=client.post(f'/api/projects/{pid}/messages',headers=H,json=body)
    assert again.status_code==200 and again.json()['id']==response.json()['id']
    assert get('conversation',first['id'])['last_activity_at']==saved
    patch('conversation',first['id'],deleted_at='2025-01-01')
    assert first['id'] not in {x['id'] for x in chat.sessions(pid)}


def test_virtual_default_gets_activity_without_a_model_and_legacy_falls_back(project):
    pid,client,_=project
    default=chat.default_session(pid)
    old=put('conversation',{'project_id':pid,'name':'历史','created_at':'2001-01-01T00:00:00+00:00'},'c_'+uid())
    response=client.post(f'/api/projects/{pid}/messages',headers=H,
                         json={'text':'继续分析','request_id':'default-'+uid()})
    assert response.status_code==200,response.text
    persisted=get('conversation',default)
    assert persisted['name']=='项目对话' and persisted['last_mode']=='work'
    assert 'model_id' not in persisted and persisted['last_activity_at']==response.json()['created_at']
    result=chat.sessions(pid)
    assert result[0]['id']==default
    assert next(s for s in result if s['id']==old['id'])['created_at']==old['created_at']
