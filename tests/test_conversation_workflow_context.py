import pytest
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service import conversation,project_data,workspace
from agent_service.store import all_items, delete, get,uid
from test_project_data import project,source,document,registered


@pytest.fixture(autouse=True)
def cleanup_submitted_turns():
    # These tests inspect submissions without executing them. Leaving their
    # pending rows behind lets the real global scheduler process them in later
    # tests and consume another test's mocked model response.
    previous={item['id'] for item in all_items('turn')}
    yield
    for item in all_items('turn'):
        if item['id'] not in previous:delete('turn',item['id'])


def test_selected_flow_is_project_scoped_versioned_and_preserves_text():
    pid=project();other=project();s=source(pid);doc=document(s['id']);wid=registered(pid,doc)
    info=project_data.inspect_workflow(pid,wid);ref={'kind':'data','id':wid,'digest':info['draft_sha256']}
    text='把第二步按去年同期比较，不要发布'
    first=conversation.submit(pid,conversation.TurnInput(text=text,request_id=uid(),workflow_context=ref))
    stored=get('turn',first['id']);assert stored['text']==text and stored['selected_workflow']['id']==wid
    assert stored['selected_workflow']['digest']==ref['digest']
    with pytest.raises((ValueError,KeyError)):
        conversation.submit(other,conversation.TurnInput(text=text,request_id=uid(),workflow_context=ref))
    changed=document(s['id']);changed['plan']['name']='变更后';import json
    workspace.write_file(pid,'workflows/orders.json',json.dumps(changed))
    with pytest.raises(ValueError,match='流程已变化'):
        conversation.submit(pid,conversation.TurnInput(text=text,request_id=uid(),workflow_context=ref))


def test_replayed_message_cannot_switch_flow_or_session():
    pid=project();s=source(pid);a=registered(pid,document(s['id']),'workflows/a.json');b=registered(pid,document(s['id']),'workflows/b.json')
    ref=lambda wid:{'kind':'data','id':wid,'digest':project_data.inspect_workflow(pid,wid)['draft_sha256']}
    rid=uid();turn=conversation.submit(pid,conversation.TurnInput(text='调整第二步',request_id=rid,workflow_context=ref(a)))
    assert conversation.submit(pid,conversation.TurnInput(text='调整第二步',request_id=rid,workflow_context=ref(a)))['id']==turn['id']
    with pytest.raises(ValueError,match='不能更换'):
        conversation.submit(pid,conversation.TurnInput(text='调整第二步',request_id=rid,workflow_context=ref(b)))
    sid=conversation.create_session(pid,conversation.SessionInput(name='另一对话',request_id=uid()))['id']
    with pytest.raises(ValueError,match='其他对话'):
        conversation.submit(pid,conversation.TurnInput(text='调整第二步',request_id=rid,session_id=sid,workflow_context=ref(a)))


def test_wrong_workflow_kind_rejected():
    with pytest.raises(ValueError):
        conversation.TurnInput(text='修改',request_id=uid(),workflow_context={'kind':'business','id':'wf_'+'a'*40,'digest':'b'*64})


def test_two_sessions_keep_their_selected_flows_separate():
    pid=project();s=source(pid);a=registered(pid,document(s['id']),'workflows/one.json');b=registered(pid,document(s['id']),'workflows/two.json')
    sid=conversation.create_session(pid,conversation.SessionInput(name='第二对话',request_id=uid()))['id']
    def ref(wid):return {'kind':'data','id':wid,'digest':project_data.inspect_workflow(pid,wid)['draft_sha256']}
    one=conversation.submit(pid,conversation.TurnInput(text='修改第二步',session_id=conversation.default_session(pid),request_id=uid(),workflow_context=ref(a)))
    two=conversation.submit(pid,conversation.TurnInput(text='修改第二步',session_id=sid,request_id=uid(),workflow_context=ref(b)))
    assert get('turn',one['id'])['selected_workflow']['id']==a
    assert get('turn',two['id'])['selected_workflow']['id']==b


def test_pending_view_preserves_selected_flow_and_conflict_is_definitive():
    from agent_service import activity
    import json
    pid=project();s=source(pid);doc=document(s['id']);wid=registered(pid,doc)
    ref={'kind':'data','id':wid,'digest':project_data.inspect_workflow(pid,wid)['draft_sha256']}
    cid=conversation.default_session(pid);device='d_'+('a'*24)+uid()[:8];activity.register(device)
    pending={'session_id':cid,'request_id':uid(),'text':'请改第二步','workflow_context':ref}
    activity.save_view(device,activity.ViewInput(project_id=pid,session_id=cid,pending=pending))
    assert activity.read_view(device,pid,cid)['pending']==pending
    changed=document(s['id']);changed['plan']['name']='更新';workspace.write_file(pid,'workflows/orders.json',json.dumps(changed))
    response=TestClient(app).post('/api/projects/'+pid+'/messages',headers={'Authorization':'Bearer '+TOKEN},json=pending)
    assert response.status_code==409 and response.json()['code']=='WORKFLOW_CONTEXT_CONFLICT'
    assert conversation.turn_identity(pid,pending['request_id']) not in [x['id'] for x in __import__('agent_service.store',fromlist=['all_items']).all_items('turn')]
