"""Multiple inputs are one durable task, not a UI list over first-file processing."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_service import activity, api, conversation as chat, execution, provider
from agent_service.actor_context import actor_scope
from agent_service.config import DATA, TOKEN
from agent_service.store import all_items, delete, get, now, patch, put, seal, uid

H={'Authorization':'Bearer '+TOKEN}


@pytest.fixture
def project(monkeypatch):
    pid='p_'+uid();put('project',{'name':'多资料验收','created_at':now()},pid)
    queues=[]
    def enqueue(*args,**kwargs):queues.append(args);return {'id':'fixture-'+uid()}
    monkeypatch.setattr(api,'enqueue',enqueue)
    monkeypatch.setattr(execution,'enqueue',enqueue)
    monkeypatch.setattr(api,'_requests',{})
    monkeypatch.setenv('ATLAS_AUTH_MODE','token')
    monkeypatch.setattr('agent_service.model_catalog.select',lambda *a,**k:{})
    monkeypatch.setattr(provider,'status',lambda:{'configured':True})
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://fixture.test','model':'fixture','share_data':True})
    uploads_before=set((DATA/'uploads').glob('*'))
    yield pid,TestClient(api.app),queues
    source_ids={s['id'] for s in all_items('source') if s.get('project_id')==pid}
    for source in all_items('source'):
        if source['id'] in source_ids:
            from agent_service.store import unseal
            if source.get('secret_config'):
                raw=unseal(source['secret_config'])
                if raw.get('stored_name'):(DATA/'uploads'/raw['stored_name']).unlink(missing_ok=True)
    for kind in ('turn','conversation','source','source_version','browser_view','browser_device'):
        for record in all_items(kind):
            if record.get('project_id')==pid:delete(kind,record['id'])
    delete('project',pid)
    for path in set((DATA/'uploads').glob('*'))-uploads_before:
        if path.is_file():path.unlink()


def upload(project,name='detail.csv',content=b'id,amount\n1,5\n',request_id=None):
    pid,client,_=project
    response=client.post('/api/projects/'+pid+'/attachments',headers=H,
                         files={'file':(name,content,'application/octet-stream')},
                         data={'request_id':request_id or 'upload-'+uid()})
    assert response.status_code==200,response.text
    return response.json()


def submit(project,ids,**updates):
    pid,client,_=project
    response=client.post('/api/projects/'+pid+'/messages',headers=H,
        json={'text':'结合这些资料按办事处汇总收入并核对口径','source_ids':ids,'request_id':'message-'+uid(),**updates})
    assert response.status_code==200,response.text
    return get('turn',response.json()['id'])


def ready(identifiers):
    for identifier in identifiers:patch('source',identifier,status='ready')


def test_explicit_empty_attachments_do_not_mislabel_an_application_focus_as_upload():
    assert chat.attachment_ids({'source_id':'legacy-source'})==['legacy-source']
    assert chat.attachment_ids({'source_id':'application-source','source_ids':[]})==[]


def test_xlsx_and_policy_upload_survive_one_message_with_frozen_provenance(project):
    base=Path(__file__).resolve().parents[1]/'acceptance/finance_monthly/inputs/phase1'
    files=[upload(project,p.name,p.read_bytes()) for p in sorted(base.iterdir())]
    ids=[item['id'] for item in files]
    turn=submit(project,ids)
    assert turn['source_ids']==ids and turn['source_id']==ids[0]
    assert [row['name'] for row in turn['attached_sources']]==[item['name'] for item in files]
    assert all(row['revision']==1 and len(row['content_hash'])==64 for row in turn['attached_sources'])
    assert 'secret_config' not in json.dumps(chat.public_turn(turn))
    public=project[1].get('/api/projects/'+project[0]+'/conversation',headers=H).json()['turns'][-1]
    assert public['source_ids']==ids and len(public['attached_sources'])==2


def test_same_names_are_distinct_sources_and_upload_retry_keeps_identity(project):
    request='upload-'+uid()
    first=upload(project,request_id=request)
    duplicate=upload(project,request_id=request)
    second=upload(project,content=b'id,amount\n2,7\n')
    assert first['id']==duplicate['id'] and first['id']!=second['id']
    assert first['name']==second['name']
    turn=submit(project,[first['id'],second['id']])
    assert len(turn['attached_sources'])==2


def test_partial_upload_failure_does_not_delete_successful_files_or_fake_an_attachment(project):
    first=upload(project)
    pid,client,_=project
    bad=client.post('/api/projects/'+pid+'/attachments',headers=H,
        files={'file':('unsafe.exe',b'not-a-workbook','application/octet-stream')},data={'request_id':'upload-'+uid()})
    assert bad.status_code==400
    second=upload(project,'policy.txt','只统计已付款收入'.encode())
    sources=client.get('/api/projects/'+pid+'/conversation',headers=H).json()['sources']
    assert {source['id'] for source in sources}=={first['id'],second['id']}
    assert len(submit(project,[first['id'],second['id']])['attached_sources'])==2


def test_accepted_message_retry_cannot_change_file_set_or_order(project):
    ids=[upload(project)['id'],upload(project,'policy.txt',b'policy')['id']]
    key='message-'+uid()
    first=submit(project,ids,request_id=key)
    again=submit(project,list(reversed(ids)),request_id=key,text='different task')
    assert again['id']==first['id'] and again['source_ids']==ids
    assert again['attached_sources']==first['attached_sources']


@pytest.mark.parametrize('change',[
    lambda ids:{'source_ids':[ids[0],ids[0]]},
    lambda ids:{'source_ids':ids,'source_id':ids[1]},
    lambda ids:{'source_ids':ids*11},
    lambda ids:{'source_ids':['']},
])
def test_ambiguous_or_duplicate_attachment_inputs_are_rejected(project,change):
    ids=[upload(project)['id'],upload(project,'policy.txt',b'policy')['id']]
    response=project[1].post('/api/projects/'+project[0]+'/messages',headers=H,
        json={'text':'分析','request_id':'message-'+uid(),**change(ids)})
    assert response.status_code==422


def test_legacy_source_id_remains_supported_and_matching_alias_is_allowed(project):
    identifier=upload(project)['id'];client=project[1];path='/api/projects/'+project[0]+'/messages'
    for fields in ({'source_id':identifier},{'source_id':identifier,'source_ids':[identifier]}):
        response=client.post(path,headers=H,json={'text':'分析','request_id':'message-'+uid(),**fields})
        assert response.status_code==200 and response.json()['source_ids']==[identifier]


def test_second_cross_project_file_rejects_whole_turn(project):
    own=upload(project)['id'];other=put('source',{'project_id':'another-project','name':'private.csv','status':'ready'})
    try:
        key='message-'+uid()
        response=project[1].post('/api/projects/'+project[0]+'/messages',headers=H,
            json={'text':'合并','source_ids':[own,other['id']],'request_id':key})
        assert response.status_code==400
        with pytest.raises(KeyError):get('turn',chat.stable('t_',project[0],key))
    finally:delete('source',other['id'])


@pytest.mark.parametrize('injected',[{'attached_sources':[{'id':'invented','content_hash':'fake'}]},{'actor_id':'owner'}])
def test_client_cannot_supply_server_owned_attachment_provenance_or_actor(project,injected):
    response=project[1].post('/api/projects/'+project[0]+'/messages',headers=H,
        json={'text':'分析','source_ids':[upload(project)['id']],'request_id':'message-'+uid(),**injected})
    assert response.status_code==422


def test_all_inputs_are_probed_and_model_waits_for_last_one(project,monkeypatch):
    ids=[upload(project)['id'],upload(project,'policy.txt',b'policy')['id']]
    turn=submit(project,ids);seen=[]
    monkeypatch.setattr('agent_service.agent.advance',lambda value:seen.append(value))
    project[2].clear();chat.handle(turn)
    assert seen==[] and get('turn',turn['id'])['state']=='waiting_source'
    assert project[2]==[('probe',identifier) for identifier in ids]
    ready(ids[:1]);project[2].clear();chat.handle(get('turn',turn['id']))
    assert seen==[] and project[2]==[('probe',ids[1])]
    ready(ids[1:]);chat.handle(get('turn',turn['id']))
    assert len(seen)==1 and seen[0]['source_ids']==ids


def test_failed_second_input_cannot_be_silently_dropped(project,monkeypatch):
    ids=[upload(project)['id'],upload(project,'policy.txt',b'policy')['id']]
    turn=submit(project,ids);ready(ids[:1]);patch('source',ids[1],status='failed',error='invalid')
    monkeypatch.setattr('agent_service.agent.advance',lambda value:pytest.fail('failed attachment reached model'))
    chat.handle(turn);current=get('turn',turn['id'])
    assert current['outcome']=='needs_input' and 'policy.txt' in current['answer']
    assert current['source_ids']==ids


@pytest.mark.parametrize('change',[{'content_hash':'changed'},{'revision':2},{'archived':True}])
def test_changed_or_archived_attachment_blocks_task_without_substitution(project,change):
    ids=[upload(project)['id'],upload(project,'policy.txt',b'policy')['id']]
    turn=submit(project,ids);ready(ids);patch('source',ids[1],**change)
    with pytest.raises(ValueError,match='任务附件已变更'):chat.validate_attachment_versions(turn)
    chat.handle(turn)
    assert get('turn',turn['id'])['outcome']=='needs_input'
    assert chat.public_turn(get('turn',turn['id']))['attached_sources'][1]['version_current'] is False


def test_resume_preserves_all_frozen_inputs_and_rejects_new_files(project,monkeypatch):
    ids=[upload(project)['id'],upload(project,'policy.txt',b'policy')['id']]
    old=submit(project,ids);ready(ids)
    patch('turn',old['id'],state='done',outcome='paused',resume_available=True,finished_at=now())
    new=submit(project,[],text='继续',resume_turn_id=old['id'])
    monkeypatch.setattr('agent_service.agent.advance',lambda value:None)
    chat.handle(new);resumed=get('turn',new['id'])
    assert resumed['source_ids']==ids and resumed['attached_sources']==old['attached_sources']
    patch('turn',old['id'],resume_available=True)
    response=project[1].post('/api/projects/'+project[0]+'/messages',headers=H,
        json={'text':'继续','source_ids':list(reversed(ids)),'resume_turn_id':old['id'],'request_id':'message-'+uid()})
    assert response.status_code==400


def test_no_model_multi_input_request_never_falls_back_to_only_first_file(project,monkeypatch):
    ids=[upload(project)['id'],upload(project,'policy.txt',b'policy')['id']];ready(ids)
    turn=submit(project,ids)
    monkeypatch.setattr(provider,'status',lambda:{'configured':False})
    monkeypatch.setattr(chat,'legacy_handle',lambda value:pytest.fail('legacy single-input fallback invoked'))
    chat.handle(turn)
    assert get('turn',turn['id'])['outcome']=='needs_input'
    assert '全部2份' in get('turn',turn['id'])['answer']


def test_cancel_still_works_when_an_input_has_been_removed(project):
    ids=[upload(project)['id'],upload(project,'policy.txt',b'policy')['id']]
    turn=submit(project,ids);delete('source',ids[1])
    stopped=submit(project,[],text='停止')
    assert stopped['state']=='done' and get('turn',turn['id'])['outcome']=='cancelled'


def test_actor_scope_is_server_owned_and_another_actor_cannot_resume(project):
    old=submit(project,[upload(project)['id']])
    assert old['actor_id']=='owner'
    patch('turn',old['id'],state='done',resume_available=True)
    with actor_scope('u_'+'a'*64),pytest.raises(ValueError,match='自己发起'):
        chat.submit(project[0],chat.TurnInput(text='继续',resume_turn_id=old['id'],request_id='other-'+uid()))


def test_two_actors_can_use_same_request_id_without_reusing_personal_task_context(project):
    key='message-'+uid();body=chat.TurnInput(text='按我的个人技能分析',request_id=key)
    with actor_scope('u_'+'a'*64):
        first=chat.submit(project[0],body)
        assert api.message_status(project[0],key)['accepted'] is True
    with actor_scope('u_'+'b'*64):
        assert api.message_status(project[0],key)=={'accepted':False}
        second=chat.submit(project[0],body)
        assert api.message_status(project[0],key)['turn']['id']==second['id']
    assert first['id']!=second['id']
    assert get('turn',first['id'])['actor_id']!=get('turn',second['id'])['actor_id']
    assert chat.turn_identity(project[0],key,'owner')==chat.stable('t_',project[0],key)


def test_unsent_draft_keeps_every_uploaded_id_encrypted_and_pending_retry_immutable(project):
    pid=project[0];ids=[upload(project)['id'],upload(project,'policy.txt',b'policy')['id']]
    did='d_'+('a'*24)+uid()[:8];activity.register(did)
    try:
        pending={'text':'两份资料一起处理','source_ids':ids,'request_id':'message-'+uid(),'session_id':chat.default_session(pid)}
        activity.save_view(did,activity.ViewInput(project_id=pid,session_id=chat.default_session(pid),source_ids=ids,pending=pending))
        restored=activity.read_view(did,pid,chat.default_session(pid))
        assert restored['source_ids']==ids and restored['pending']['source_ids']==ids
        stored=get('browser_view',activity.view_key(did,pid,chat.default_session(pid)))
        assert ids[0] not in json.dumps(stored)
        with pytest.raises(ValueError):activity.save_view(did,activity.ViewInput(project_id=pid,session_id=chat.default_session(pid),source_ids=ids*2,revision=1))
    finally:
        delete('browser_view',activity.view_key(did,pid,chat.default_session(pid)));delete('browser_device',did)
