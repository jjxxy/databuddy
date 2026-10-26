"""A broken continue link must be rejected clearly without accepting a task."""
import pytest
from agent_service import conversation as chat
from agent_service.store import all_items,get,patch,uid
from test_multi_file_conversation import project,upload,submit,H


@pytest.mark.parametrize('target',['${esc(t.id)}','t_missing_task'])
def test_missing_resume_target_is_a_definite_rejection_with_recoverable_message(project,target):
    pid,client,_=project
    request_id='missing-resume-'+uid()
    before=[t['id'] for t in all_items('turn') if t.get('project_id')==pid]
    response=client.post(f'/api/projects/{pid}/messages',headers=H,
        json={'text':'继续','request_id':request_id,'resume_turn_id':target})
    assert response.status_code==400
    assert response.json()['detail'].startswith('待继续的任务不存在')
    assert '输入和附件已保留' in response.json()['detail']
    assert client.get(f'/api/projects/{pid}/message-status?request_id={request_id}',headers=H).json()['accepted'] is False
    assert [t['id'] for t in all_items('turn') if t.get('project_id')==pid]==before


def test_real_resume_keeps_original_attachments_and_one_idempotent_turn(project):
    pid,client,_=project
    source=upload(project)
    original=submit(project,[source['id']])
    patch('turn',original['id'],state='failed',resume_available=True,outcome='paused')
    request_id='real-resume-'+uid()
    body={'text':'继续','request_id':request_id,'session_id':original['session_id'],
          'resume_turn_id':original['id'],'source_ids':[]}
    response=client.post(f'/api/projects/{pid}/messages',headers=H,json=body)
    assert response.status_code==200,response.text
    accepted=get('turn',response.json()['id'])
    assert accepted['source_ids']==[source['id']]
    assert accepted['attached_sources']==original['attached_sources']
    patch('turn',original['id'],resume_available=False)
    duplicate=client.post(f'/api/projects/{pid}/messages',headers=H,json=body)
    assert duplicate.status_code==200 and duplicate.json()['id']==accepted['id']
    assert len([t for t in all_items('turn') if t.get('project_id')==pid])==2
    assert client.get(f'/api/projects/{pid}/message-status?request_id={request_id}',headers=H).json()['accepted'] is True
