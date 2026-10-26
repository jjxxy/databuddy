"""Real loopback Agent protocols plus durable replay and trust-boundary checks."""
import base64,hashlib,io,json,threading,time,zipfile
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from decimal import Decimal

import pytest

from agent_service import agent_mesh as mesh,agent_mesh_transport as wire,provider,company_search,agent_handoff
from agent_service.actor_context import actor_scope
from agent_service.config import DATA,TOKEN
from agent_service.store import get,put,patch,delete,all_items,uid,now,unseal

A='u_'+'a'*64
B='u_'+'b'*64


@pytest.fixture
def project(tmp_path,monkeypatch):
    kinds=('project','turn','collab_peer','collab_task','collab_batch','collab_budget','agent_task_artifact','agent_handoff_import','agent_handoff_result_import','agent_handoff_artifact_import','agent_imported_result','workspace_artifact','workspace_artifact_export')
    previous={kind:{item['id'] for item in all_items(kind)} for kind in kinds}
    from agent_service.jobs import jobs,engine
    from sqlalchemy import select
    with engine.connect() as connection:previous_jobs=set(connection.execute(select(jobs.c.id)).scalars())
    monkeypatch.setenv('ATLAS_LOCAL_MODE','1');monkeypatch.setenv('ATLAS_LOCAL_WORKSPACE',str(tmp_path))
    monkeypatch.setenv('API_ALLOWED_HOSTS','127.0.0.1')
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://fixture-llm.test/v1','model':'fixture','api_key':'fixture-model-secret','share_data':True})
    monkeypatch.setattr(company_search,'capture_model_approvals',lambda ids:{})
    pid='p_'+uid();put('project',{'name':'Agent cooperation'},pid)
    yield pid
    for future in list(mesh._futures.values()):future.result(timeout=20)
    mesh._futures.clear()
    with engine.begin() as connection:
        query=jobs.delete()
        if previous_jobs:query=query.where(jobs.c.id.not_in(previous_jobs))
        connection.execute(query)
    for kind in kinds:
        for item in all_items(kind):
            if item['id'] not in previous[kind]:delete(kind,item['id'])


def parent(pid):
    return put('turn',{'project_id':pid,'actor_id':'owner','state':'agent','interaction_mode':'work',
        'text':'请让协作Agent核对数据，保留原始结果，不批准财务口径','created_at':now(),
        'agent_trace':[{'id':'step_'+uid(),'operation_id':'operation_'+uid(),'tool':'delegate_agents','status':'started','arguments':{}}],
        'task_contract':{'kind':'answer','requirements':[],'application_ids':[],'acceptance':'有依据的回答','budgets':{
            'model_calls':18,'input_characters':360000,'output_tokens':24000,'duration_ms':300000}}},'t_'+uid())


@contextmanager
def service(version='1.0',*,drop_submit=False,terminal_outcome='answered',output_bundle=None):
    observed=[];state={'submitted':0,'polled':0,'cancelled':0};url=None
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,value,status=200):
            raw=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(raw)
        def do_GET(self):
            observed.append({'method':'GET','authorization':self.headers.get('Authorization'),'version':self.headers.get('A2A-Version')})
            if version=='0.3.0':self.send({'protocolVersion':'0.3.0','url':url,'preferredTransport':'JSONRPC'})
            else:self.send({'supportedInterfaces':[{'url':url,'protocolBinding':'JSONRPC','protocolVersion':'1.0'}]})
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            observed.append({'method':'POST','authorization':self.headers.get('Authorization'),'body':body,'version':self.headers.get('A2A-Version')})
            method=body['method'];params=body.get('params',{})
            if method in ('SendMessage','message/send'):
                state['submitted']+=1
                if drop_submit:self.connection.shutdown(2);self.connection.close();return
                task=self.task(False)
                result={'task':task} if method=='SendMessage' else task
            elif method in ('GetTask','tasks/get'):
                state['polled']+=1;result=self.task(True)
            elif method in ('CancelTask','tasks/cancel'):
                state['cancelled']+=1;result=self.task(False,cancelled=True)
            elif method=='initialize':result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}}}
            elif method=='notifications/initialized':self.send_response(204);self.end_headers();return
            elif method=='tools/list':
                result={'tools':[{'name':name,'description':'Scoped Atlas agent','inputSchema':{'type':'object','additionalProperties':True}} for name in ('atlas_submit_task','atlas_get_task','atlas_cancel_task')]}
            elif method=='tools/call':
                tool=params['name']
                if tool=='atlas_submit_task':state['submitted']+=1;v={'id':'remote-task','status':'working'}
                elif tool=='atlas_get_task':state['polled']+=1;v={'id':'remote-task','status':'completed','outcome':terminal_outcome,'answer':'Reviewed actual source scope.','output_bundle':output_bundle}
                else:state['cancelled']+=1;v={'id':'remote-task','status':'cancelled'}
                result={'content':[],'structuredContent':v,'isError':False}
            else:raise AssertionError(method)
            self.send({'jsonrpc':'2.0','id':body['id'],'result':result})
        def task(self,done,cancelled=False):
            kind='CANCELED' if cancelled else 'COMPLETED' if done else 'WORKING'
            status='TASK_STATE_'+kind if version=='1.0' else {'CANCELED':'canceled','COMPLETED':'completed','WORKING':'working'}[kind]
            part={'data':{'atlas':{'outcome':terminal_outcome,'output_bundle':output_bundle}}}
            if version!='1.0':part['kind']='data'
            return {'id':'remote-task','status':{'state':status,'message':{'parts':[{'text':'Reviewed actual source scope.'}]}},'artifacts':[{'parts':[part]}]}
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);url='http://127.0.0.1:'+str(server.server_port)+'/a2a'
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield url,observed,state
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


def configured(pid,url,**extra):
    return mesh.configure_peer(pid,mesh.PeerInput(name='Independent reviewer',url=url,credential='fixture-peer-token',
        remote_project_id='remote-project',data_sharing_approved=True,submit_approved=True,model_approved=True,**extra))


def delegated(pid,url,**peer_options):
    p=parent(pid);peer=configured(pid,url,**peer_options)
    result=mesh.delegate(p,mesh.Delegation(assignments=[mesh.Assignment(peer_id=peer['id'],goal='核对输入范围并指出不确定事项')]))
    return p,peer,result['tasks'][0]


def test_unlimited_root_delegates_with_a_scoped_deadline_without_hidden_root_cap(project):
    from agent_service.collaboration_budget import current_totals
    with service() as (url,observed,state):
        root=parent(project)
        contract=dict(root['task_contract'],budget_policy='unlimited',
            budgets=dict.fromkeys(('model_calls','input_characters','output_tokens','duration_ms'),None))
        root=patch('turn',root['id'],task_contract=contract,
            model_usage={'calls':100,'input_characters':2000000,'output_tokens_budgeted':200000,'duration_ms':900000})
        peer=configured(project,url)
        request=mesh.Delegation(assignments=[mesh.Assignment(peer_id=peer['id'],goal='核对输入范围')])
        started=time.time();result=mesh.delegate(root,request);task=result['tasks'][0]
        seconds=datetime.fromisoformat(task['deadline_at']).timestamp()-started
        assert 599<=seconds<=601
        totals=current_totals(root)
        assert totals['deadline_at'] is None and all(value is None for value in totals['limits'].values())
        assert totals['used']['calls']==100
        repeated=mesh.delegate(get('turn',root['id']),request)
        assert repeated['tasks'][0]['id']==task['id']
        assert repeated['tasks'][0]['deadline_at']==task['deadline_at']


@pytest.mark.parametrize('version',['1.0','0.3.0'])
def test_actual_a2a_protocol_profiles_submit_once_poll_and_return_advice(project,version):
    with service(version) as (url,observed,state):
        p,peer,task=delegated(project,url)
        assert get('turn',p['id'])['state']=='waiting_agent_tasks'
        mesh._advance(task['id']);current=get('collab_task',task['id'])
        assert current['state']=='working' and current['remote_task_id']=='remote-task'
        patch('collab_task',task['id'],next_poll_at=0)
        mesh._advance(task['id']);current=get('collab_task',task['id'])
        assert current['state']=='succeeded' and current['verification_status']=='advisory_unverified'
        assert current['evidence']['financial_verified'] is False and not current['evidence']['integrity_verified']
        assert 'Reviewed actual' in mesh.public_task(current)['result_summary']
        mesh._wake_parents();assert get('turn',p['id'])['state']=='agent'
        assert state=={'submitted':1,'polled':1,'cancelled':0}
        body=next(item['body'] for item in observed if item.get('body',{}).get('method') in ('SendMessage','message/send'))
        if version=='1.0':
            assert body['params']['configuration']['returnImmediately'] is True
            assert body['params']['message']['role']=='ROLE_USER' and 'kind' not in body['params']['message']
        else:assert body['params']['configuration']['blocking'] is False
        assert all(item['authorization']=='Bearer fixture-peer-token' for item in observed)
        assert peer['protocol_version']==version


def test_mcp_actual_initialization_catalog_submit_poll_and_string_status(project):
    with service() as (url,observed,state):
        p,peer,task=delegated(project,url,transport='mcp')
        mesh._advance(task['id']);patch('collab_task',task['id'],next_poll_at=0);mesh._advance(task['id'])
        assert get('collab_task',task['id'])['state']=='succeeded'
        assert state['submitted']==state['polled']==1
        assert sum(v.get('body',{}).get('method')=='initialize' for v in observed)==3


def test_lost_submit_response_is_unknown_and_never_replayed_after_restart(project):
    with service(drop_submit=True) as (url,observed,state):
        p,peer,task=delegated(project,url)
        mesh._advance(task['id'])
        assert get('collab_task',task['id'])['state']=='unknown' and state['submitted']==1
        mesh._advance(task['id']);mesh._advance(task['id'])
        assert state['submitted']==1
        mesh._wake_parents();assert get('turn',p['id'])['state']=='agent'
        with pytest.raises(ValueError,match='未完成'):mesh.assert_parent_completion(get('turn',p['id']))


def test_crash_after_admission_before_response_never_resubmits(project,monkeypatch):
    with service() as (url,observed,state):
        _,_,task=delegated(project,url)
        patch('collab_task',task['id'],state='submitting',dispatch_started_at=now())
        monkeypatch.setattr(wire,'submit',lambda *args:pytest.fail('ambiguous request was replayed'))
        mesh._advance(task['id'])
        assert get('collab_task',task['id'])['state']=='unknown' and state['submitted']==0


def test_delegation_retry_reuses_frozen_batch_and_rejects_changed_body(project):
    with service() as (url,_,state):
        p,peer,task=delegated(project,url)
        original=mesh.Delegation(assignments=[mesh.Assignment(peer_id=peer['id'],goal='核对输入范围并指出不确定事项')])
        repeated=mesh.delegate(get('turn',p['id']),original)
        assert repeated['tasks'][0]['id']==task['id'] and len(all_items('collab_task'))==1
        with pytest.raises(ValueError,match='内容已变化'):
            mesh.delegate(get('turn',p['id']),mesh.Delegation(assignments=[mesh.Assignment(peer_id=peer['id'],goal='替换为另一项任务')]))
        assert state['submitted']==0


def test_cancel_before_submission_does_not_call_remote(project):
    with service() as (url,_,state):
        _,_,task=delegated(project,url)
        assert mesh.cancel_task(project,task['id'])['state']=='cancelled'
        mesh._advance(task['id']);assert state['submitted']==0


def test_cancel_after_submission_requires_actual_peer_receipt(project):
    with service() as (url,_,state):
        _,_,task=delegated(project,url);mesh._advance(task['id'])
        assert mesh.cancel_task(project,task['id'])['state']=='cancel_requested'
        mesh._advance(task['id']);assert get('collab_task',task['id'])['state']=='cancelled' and state['cancelled']==1


def test_peer_revision_disable_and_actor_boundary_stop_background_use(project):
    with service() as (url,_,state):
        p,peer,task=delegated(project,url)
        with actor_scope(B):
            with pytest.raises(ValueError):mesh._owned('collab_peer',peer['id'],project)
            with pytest.raises(ValueError):mesh.cancel_task(project,task['id'])
            assert mesh.list_tasks(project)['items']==[]
        mesh.disable_peer(project,peer['id'],peer['revision']);mesh._advance(task['id'])
        assert get('collab_task',task['id'])['state']=='auth_required' and state['submitted']==0


def test_private_project_requires_separate_operator_destination_grant(project):
    with service() as (url,_,_):
        p=parent(project);peer=configured(project,url);patch('project',project,company_private=True)
        with pytest.raises(ValueError,match='管理员'):
            mesh.delegate(p,mesh.Delegation(assignments=[mesh.Assignment(peer_id=peer['id'],goal='核对资料')]))
        assert not all_items('collab_task')
        value=mesh.PeerInput(name='Admin approved',url=url,credential='different-peer-token',remote_project_id='remote',
            data_sharing_approved=True,submit_approved=True,model_approved=True,private_data_approved=True)
        with pytest.raises(ValueError,match='管理员'):mesh.configure_peer(project,value)
        preset=mesh.configure_peer(project,value,operator=True)
        assert preset['private_data_approved'] and not preset['can_edit']


def test_task_freezes_peer_credentials_and_bundle_not_exposing_secrets(project):
    with service() as (url,_,_):
        _,peer,task=delegated(project,url)
        raw=get('collab_task',task['id'])
        assert unseal(raw['secret_peer_snapshot'])['credential']=='fixture-peer-token'
        assert 'fixture-peer-token' not in json.dumps(mesh.public_task(raw))
        assert 'fixture-peer-token' not in json.dumps(mesh.public_peer(get('collab_peer',peer['id'])))
        assert raw['input_manifest_sha256']==unseal(raw['secret_input_bundle'])['manifest_sha256']


@pytest.mark.parametrize('outcome',['needs_input','blocked','partial','paused','failed'])
def test_transport_completed_is_not_business_completion(project,outcome):
    with service(terminal_outcome=outcome) as (url,_,_):
        _,_,task=delegated(project,url);mesh._advance(task['id']);patch('collab_task',task['id'],next_poll_at=0);mesh._advance(task['id'])
        assert get('collab_task',task['id'])['state']!='succeeded'


def test_expected_artifact_requires_actual_complete_bytes_and_rejects_tamper(project):
    valid=agent_handoff._bundle([agent_handoff._item('report_1','artifact','report.txt',b'Full verified result, not preview.')])
    with service(output_bundle=valid) as (url,_,_):
        p=parent(project);peer=configured(project,url)
        result=mesh.delegate(p,mesh.Delegation(assignments=[mesh.Assignment(peer_id=peer['id'],goal='获取完整报告文件',expected_delivery='artifacts')]))
        task=result['tasks'][0];mesh._advance(task['id']);patch('collab_task',task['id'],next_poll_at=0);mesh._advance(task['id'])
        current=get('collab_task',task['id'])
        assert current['state']=='succeeded' and current['evidence']['integrity_verified']
        assert unseal(current['secret_output_bundle'])==valid
    valid['items'][0]['content_base64']=base64.b64encode(b'tampered').decode()
    with service(output_bundle=valid) as (url,_,_):
        p,peer,task=delegated(project,url);mesh._advance(task['id']);patch('collab_task',task['id'],next_poll_at=0);mesh._advance(task['id'])
        assert get('collab_task',task['id'])['state']=='rejected'


def test_missing_artifacts_do_not_complete_artifact_contract(project):
    with service() as (url,_,_):
        p=parent(project);peer=configured(project,url)
        result=mesh.delegate(p,mesh.Delegation(assignments=[mesh.Assignment(peer_id=peer['id'],goal='返回完整文件',expected_delivery='artifacts')]))
        task=result['tasks'][0];mesh._advance(task['id']);patch('collab_task',task['id'],next_poll_at=0);mesh._advance(task['id'])
        assert get('collab_task',task['id'])['state']=='input_required'


def test_no_recursive_service_task_or_child_delegation(project):
    p=parent(project)
    for field in ('gateway_client_id','inbound_client_id','collaboration_task_id'):
        with pytest.raises(ValueError,match='再次委派'):mesh._provenance(p|{field:'existing'})


@pytest.mark.parametrize('credential',[TOKEN,'fixture-model-secret'])
def test_local_system_and_model_tokens_never_forwarded_as_peer_credentials(project,credential):
    with pytest.raises(ValueError,match='专用凭据'):
        mesh._peer_config(mesh.PeerInput(name='Peer',url='https://agent.example/a2a',remote_project_id='p',credential=credential))


def test_project_model_policy_survives_disconnect_and_does_not_approve_new_model(project,monkeypatch):
    with service() as (url,_,_):
        p,peer,task=delegated(project,url)
        mesh.assert_project_model_allowed(project)
        mesh.disable_peer(project,peer['id'],peer['revision'])
        monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://other-model.test/v1','model':'different','share_data':True})
        with pytest.raises(ValueError,match='当前模型'):mesh.assert_project_model_allowed(project)


def test_explicit_artifact_mcp_response_larger_than_legacy_one_mb_is_fully_verified(project):
    content=b'complete-business-output\n'*45000
    bundle=agent_handoff._bundle([agent_handoff._item('large_report','artifact','large.txt',content)])
    assert len(json.dumps(bundle))>1024*1024
    with service(output_bundle=bundle) as (url,_,_):
        p=parent(project);peer=configured(project,url,transport='mcp')
        result=mesh.delegate(p,mesh.Delegation(assignments=[mesh.Assignment(peer_id=peer['id'],goal='交付完整文件',expected_delivery='artifacts')]))
        task=result['tasks'][0];mesh._advance(task['id']);patch('collab_task',task['id'],next_poll_at=0);mesh._advance(task['id'])
        actual=get('collab_task',task['id'])
        assert actual['state']=='succeeded' and actual['evidence']['bytes']==len(content)
        recovered=unseal(actual['secret_output_bundle'])
        assert base64.b64decode(recovered['items'][0]['content_base64'])==content


def test_all_assignments_validate_before_any_task_admission(project):
    with service() as (url,_,state):
        p=parent(project);peer=configured(project,url)
        value=mesh.Delegation(assignments=[mesh.Assignment(peer_id=peer['id'],goal='第一项'),
            mesh.Assignment(peer_id=peer['id'],goal='第二项',source_ids=['missing-source'])])
        with pytest.raises(KeyError):mesh.delegate(p,value)
        assert not all_items('collab_task') and not all_items('collab_batch') and state['submitted']==0


def test_batch_admission_is_atomic_when_record_encoding_fails(project,monkeypatch):
    from agent_service import store
    with service() as (url,_,state):
        p=parent(project);peer=configured(project,url);original=store._encode
        def encode(kind,value,connection):
            if kind=='collab_task':raise ValueError('synthetic storage failure')
            return original(kind,value,connection)
        monkeypatch.setattr(store,'_encode',encode)
        with pytest.raises(ValueError,match='synthetic'):
            mesh.delegate(p,mesh.Delegation(assignments=[mesh.Assignment(peer_id=peer['id'],goal='需要原子接收')]))
        assert not all_items('collab_task') and not all_items('collab_batch') and state['submitted']==0


def test_disabled_content_permission_redacts_all_model_access_paths(project,monkeypatch):
    with service() as (url,_,_):
        p,_,task=delegated(project,url)
        patch('collab_task',task['id'],state='succeeded',result_summary='PRIVATE-NUMBER-123.45')
        monkeypatch.setattr(provider,'settings',lambda:{'share_data':False})
        assert 'PRIVATE-NUMBER' not in json.dumps(mesh.context_for_turn(p))
        assert 'PRIVATE-NUMBER' not in json.dumps(mesh.execute(p,'get_agent_task',{'task_id':task['id']}))


def test_wrong_remote_task_receipt_never_replaces_bound_identifier(project):
    with service() as (url,_,_):
        _,_,task=delegated(project,url)
        current=patch('collab_task',task['id'],state='working',remote_task_id='expected-task')
        with pytest.raises(wire.RemoteAmbiguous,match='其他任务'):
            mesh._receive(current,{'id':'wrong-task','status':'completed','answer':'Fake success'})
        assert get('collab_task',task['id'])['remote_task_id']=='expected-task'


def test_deadline_and_failed_cancellation_do_not_leave_parent_waiting_forever(project,monkeypatch):
    with service() as (url,_,_):
        p,_,task=delegated(project,url);mesh._advance(task['id'])
        patch('collab_task',task['id'],deadline_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
        def failed(*args):raise wire.RemoteAmbiguous('synthetic lost cancellation')
        monkeypatch.setattr(wire,'cancel',failed)
        mesh._advance(task['id'])
        assert get('collab_task',task['id'])['state']=='unknown'
        mesh._wake_parents();assert get('turn',p['id'])['state']=='agent'


def test_increased_handoff_setting_cannot_bypass_wire_limit_before_admission(project,monkeypatch):
    with service() as (url,_,state):
        p=parent(project);peer=configured(project,url)
        monkeypatch.setattr(agent_handoff,'build_input',lambda *a,**k:{'manifest_sha256':'a'*64,'padding':'x'*(4*1024*1024)})
        with pytest.raises(ValueError,match='传输预算'):
            mesh.delegate(p,mesh.Delegation(assignments=[mesh.Assignment(peer_id=peer['id'],goal='不能截断')]))
        assert not all_items('collab_task') and state['submitted']==0


def test_source_echo_does_not_satisfy_artifact_delivery(project):
    bundle=agent_handoff._bundle([agent_handoff._item('source_1','source','input.txt',b'input only',source_version={'revision':1,'content_sha256':hashlib.sha256(b'input only').hexdigest()})])
    with service(output_bundle=bundle) as (url,_,_):
        p=parent(project);peer=configured(project,url)
        task=mesh.delegate(p,mesh.Delegation(assignments=[mesh.Assignment(peer_id=peer['id'],goal='交付计算成果',expected_delivery='artifacts')]))['tasks'][0]
        mesh._advance(task['id']);patch('collab_task',task['id'],next_poll_at=0);mesh._advance(task['id'])
        actual=get('collab_task',task['id'])
        assert actual['state']=='input_required' and actual['verification_status']=='missing_artifacts'


def test_empty_valid_bundle_can_complete_advisory_but_not_financial_verification(project):
    with service(output_bundle=agent_handoff._bundle([])) as (url,_,_):
        _,_,task=delegated(project,url);mesh._advance(task['id']);patch('collab_task',task['id'],next_poll_at=0);mesh._advance(task['id'])
        actual=get('collab_task',task['id'])
        assert actual['state']=='succeeded' and actual['verification_status']=='advisory_unverified'
        assert not actual['evidence']['financial_verified']


def test_peer_edit_rechecks_revision_after_network_probe(project,monkeypatch):
    with service() as (url,_,_):
        peer=configured(project,url);original=wire.probe
        def probe(config):
            result=original(config)
            patch('collab_peer',peer['id'],revision='concurrent-new-revision',name='Concurrent edit')
            return result
        monkeypatch.setattr(wire,'probe',probe)
        value=mesh.PeerInput(name='Stale edit',url=url,remote_project_id='remote-project',data_sharing_approved=True,submit_approved=True,model_approved=True)
        with pytest.raises(ValueError,match='验证期间已变化'):mesh.configure_peer(project,value,peer['id'],peer['revision'])
        assert get('collab_peer',peer['id'])['name']=='Concurrent edit'


def test_all_admin_model_secrets_are_protected_from_peer_forwarding(project,monkeypatch):
    from agent_service import model_catalog
    monkeypatch.setattr(model_catalog,'owner_configuration',lambda:{'models':[{'api_key':'other-catalog-model-secret'}]})
    with pytest.raises(ValueError,match='专用凭据'):
        mesh._peer_config(mesh.PeerInput(name='Peer',url='https://agent.example/a2a',remote_project_id='p',credential='other-catalog-model-secret'))


@pytest.mark.parametrize('transport',['a2a','mcp'])
def test_deleted_parent_cleanup_cancels_known_remote_without_model_or_resubmit(project,monkeypatch,transport):
    with service() as (url,_,state):
        p,_,task=delegated(project,url,transport=transport);mesh._advance(task['id'])
        patch('turn',p['id'],state='done',outcome='cancelled',lifecycle_cancelled_at=now())
        patch('collab_task',task['id'],state='unknown',cancel_requested=True,lifecycle_cancelled_at=now())
        monkeypatch.setattr(provider,'settings',lambda:{'share_data':False})
        mesh._advance(task['id']);actual=get('collab_task',task['id'])
        assert actual['state']=='cancelled' and actual['remote_cleanup_state'] in ('canceled','cancelled')
        assert actual['lifecycle_cleanup_finished_at'] and state=={'submitted':1,'polled':0,'cancelled':1}
        assert get('turn',p['id'])['state']=='done'
        mesh._advance(task['id']);assert state['cancelled']==1


def test_deleted_parent_cleanup_never_retries_ambiguous_cancel(project,monkeypatch):
    with service() as (url,_,state):
        _,_,task=delegated(project,url);mesh._advance(task['id'])
        patch('collab_task',task['id'],state='unknown',cancel_requested=True,lifecycle_cancelled_at=now())
        def ambiguous(*args):raise wire.RemoteAmbiguous('lost cancel receipt')
        monkeypatch.setattr(wire,'cancel',ambiguous)
        mesh._advance(task['id'])
        assert get('collab_task',task['id'])['cancel_dispatched_at']
        patch('collab_task',task['id'],lifecycle_cleanup_next_at=0)
        mesh._advance(task['id']);actual=get('collab_task',task['id'])
        assert state=={'submitted':1,'polled':1,'cancelled':0}
        assert actual['state']=='unknown' and actual['remote_cleanup_state']=='completed' and actual['lifecycle_cleanup_finished_at']
        assert 'secret_output_bundle' not in actual and 'secret_remote_response' in actual
        assert unseal(actual['secret_remote_response'])['status']['state']=='TASK_STATE_WORKING'


def test_inflight_receipt_after_deletion_only_preserves_remote_id_for_cleanup(project):
    with service() as (url,_,_):
        _,_,task=delegated(project,url)
        patch('collab_task',task['id'],state='unknown',cancel_requested=True,lifecycle_cancelled_at=now())
        mesh._receive(task,{'id':'remote-task','status':'completed','answer':'must not enter deleted work context'})
        actual=get('collab_task',task['id'])
        assert actual['state']=='unknown' and actual['remote_task_id']=='remote-task'
        assert 'result_summary' not in actual and 'secret_remote_response' not in actual


def test_verified_remote_files_have_actor_scoped_paged_exact_byte_access(project,monkeypatch):
    raw=b'Complete report content including final page.\n'*3000
    bundle=agent_handoff._bundle([agent_handoff._item('report1','artifact','report.txt',raw)])
    with service(output_bundle=bundle) as (url,_,_):
        p,_,task=delegated(project,url);mesh._advance(task['id']);patch('collab_task',task['id'],next_poll_at=0);mesh._advance(task['id'])
        task=get('collab_task',task['id']);assert mesh.public_task(task)['output_artifacts'][0]['id']=='report1'
        restored=b'';offset=0
        while True:
            receipt=mesh.read_task_artifact(project,task['id'],'report1',offset,65536)
            part=base64.b64decode(receipt['content_base64']);assert hashlib.sha256(part).hexdigest()==receipt['page_sha256']
            restored+=part;offset=receipt['next_offset']
            if receipt['eof']:break
        assert restored==raw and receipt['sha256']==hashlib.sha256(raw).hexdigest()
        with actor_scope(B):
            with pytest.raises(ValueError):mesh.read_task_artifact(project,task['id'],'report1')
        monkeypatch.setattr(provider,'settings',lambda:{'share_data':False})
        with pytest.raises(ValueError,match='未获准'):mesh.execute(p,'read_agent_task_artifact',{'task_id':task['id'],'artifact_id':'report1'})
        assert 'Complete report' not in json.dumps(mesh.execute(p,'cancel_agent_task',{'task_id':task['id']}))


def test_complete_remote_numerical_result_is_real_readable_unapproved_local_result(project,tmp_path):
    from agent_service.result_delivery import write_handoff,read_all_handoff
    from agent_service import project_data
    schema={'columns':[{'name':'office','data_type':'text','meaning':'Office'},
        {'name':'amount','data_type':'decimal','meaning':'Net revenue','unit':'CNY','period':'2026-10','aggregation':'additive'}],
        'grain':['office'],'meaning':'One office per row'}
    rows=[{'office':'O'+str(i),'amount':Decimal('7.500000000001')} for i in range(40)]
    folder=tmp_path/'complete-result';receipt=write_handoff(folder,rows,schema,context={'output_semantics':schema},provenance={})
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as archive:
        for path in folder.iterdir():archive.writestr(path.name,path.read_bytes())
    metadata={'records':40,'handoff_manifest_sha256':receipt['manifest_sha256'],'output_semantics':schema,'plan_sha256':'a'*64}
    bundle=agent_handoff._bundle([agent_handoff._item('result1','result','full-result.zip',buffer.getvalue(),result_metadata=metadata)])
    with service(output_bundle=bundle) as (url,_,_):
        _,_,task=delegated(project,url);mesh._advance(task['id']);patch('collab_task',task['id'],next_poll_at=0);mesh._advance(task['id'])
        actual=get('collab_task',task['id']);assert actual['state']=='succeeded' and len(actual['received_result_ids'])==1
        local,meta=project_data.result_folder(project,actual['received_result_ids'][0])
        data=read_all_handoff(local/'handoff',meta['handoff_sha256'],native_types=True)
        assert len(data['rows'])==40 and sum(row['amount'] for row in data['rows'])==Decimal('300.000000000040')
        assert meta['requires_semantic_review'] and actual['evidence']['financial_verified'] is False


def test_registered_api_contract_has_revision_queries_and_safe_paged_results(project):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app=FastAPI();mesh.install(app,lambda:None,lambda:None)
    with service(output_bundle=agent_handoff._bundle([agent_handoff._item('file1','artifact','result.txt',b'exact bytes')])) as (url,_,_):
        with TestClient(app) as client:
            value={'name':'API peer','url':url,'credential':'fixture-peer-token','remote_project_id':'remote-project',
                'data_sharing_approved':True,'submit_approved':True,'model_approved':True}
            response=client.post('/api/projects/'+project+'/agent-peers',json=value)
            assert response.status_code==200;peer=response.json()
            assert 'fixture-peer-token' not in response.text
            assert client.put('/api/projects/'+project+'/agent-peers/'+peer['id'],json=value).status_code==422
            p=parent(project);task=mesh.delegate(p,mesh.Delegation(assignments=[mesh.Assignment(peer_id=peer['id'],goal='验收')]))['tasks'][0]
            mesh._advance(task['id']);patch('collab_task',task['id'],next_poll_at=0);mesh._advance(task['id'])
            listing=client.get('/api/projects/'+project+'/agent-tasks').json()
            assert listing['items'][0]['output_artifacts'][0]['id']=='file1'
            base='/api/projects/'+project+'/agent-tasks/'+task['id']+'/artifacts/file1'
            page=client.get(base+'?limit=5').json();assert base64.b64decode(page['content_base64'])==b'exact' and not page['eof']
            assert client.get(base+'?limit=65537').status_code==422
            assert client.delete('/api/projects/'+project+'/agent-peers/'+peer['id']+'?expected_revision='+peer['revision']).json()['disabled']


def test_explicit_cancel_can_reconcile_known_unknown_without_resubmission(project):
    with service() as (url,_,state):
        _,_,task=delegated(project,url);mesh._advance(task['id'])
        patch('collab_task',task['id'],state='unknown',poll_failures=5)
        assert mesh.cancel_task(project,task['id'])['state']=='cancel_requested'
        mesh._advance(task['id'])
        assert get('collab_task',task['id'])['state']=='cancelled' and state=={'submitted':1,'polled':0,'cancelled':1}


def test_poll_scheduler_does_not_starve_new_work_behind_not_due_tasks(project,monkeypatch):
    observed=[]
    class Future:
        def done(self):return False
        def result(self,timeout=None):return None
    class Pool:
        def submit(self,function,identity):observed.append(identity);return Future()
    monkeypatch.setattr(mesh,'_pool',Pool())
    p=parent(project)
    for index in range(4):
        put('collab_task',{'project_id':project,'actor_id':'owner','parent_turn_id':p['id'],'transport':'a2a','state':'working',
            'created_at':'2026-01-01','next_poll_at':time.time()+60,'deadline_at':(datetime.now(timezone.utc)+timedelta(seconds=120)).isoformat()},'ct_wait_'+str(index))
    put('collab_task',{'project_id':project,'actor_id':'owner','parent_turn_id':p['id'],'transport':'a2a','state':'queued','created_at':'2026-02-01'},'ct_new')
    mesh.reconcile()
    assert observed==['ct_new']


def test_cleanup_transport_cannot_submit_a_new_task_even_with_internal_cleanup_flag(project):
    with service() as (url,_,state):
        _,_,task=delegated(project,url);mesh._advance(task['id'])
        patch('collab_task',task['id'],state='unknown',cancel_requested=True,lifecycle_cancelled_at=now())
        config=unseal(get('collab_task',task['id'])['secret_peer_snapshot']);config.update(mesh_task_id=task['id'],mesh_cleanup=True)
        with pytest.raises(wire.RemoteRejected,match='只允许取消或查询'):
            wire.request(config,'POST',{'jsonrpc':'2.0','id':'forbidden','method':'SendMessage','params':{}})
        assert state['submitted']==1


def test_builtin_readonly_advice_cannot_be_assigned_artifact_delivery(project):
    p=parent(project)
    with pytest.raises(ValueError,match='只读并返回复核意见'):
        mesh.delegate(p,mesh.Delegation(assignments=[mesh.Assignment(peer_id='local_data_checker',goal='写完整文件',expected_delivery='artifacts')]))
    assert not all_items('collab_task')


def test_existing_collaboration_deadline_is_not_extended_by_new_delegation(project):
    from agent_service import collaboration_budget
    with service() as (url,_,state):
        p=parent(project);peer=configured(project,url);collaboration_budget.initialize(p)
        ledger=get('turn',p['id'])['collaboration_budget_id'];patch('collab_budget',ledger,deadline_at=time.time()-1)
        with pytest.raises(ValueError,match='时间预算已耗尽'):
            mesh.delegate(p,mesh.Delegation(assignments=[mesh.Assignment(peer_id=peer['id'],goal='不能延长已有预算')]))
        assert not all_items('collab_task') and state['submitted']==0


def test_remote_report_and_workbook_land_in_unified_catalog_with_exact_bytes(project):
    from openpyxl import Workbook
    from agent_service import workspace_artifacts as assets
    workbook=Workbook();workbook.active.title='财务明细';workbook.active.append(['办事处','收入'])
    workbook.active.append(['华东','9007199254740993.0001']);buffer=io.BytesIO();workbook.save(buffer);workbook.close()
    report=b'# Remote report\nUntrusted opinion; numbers require review.\n'
    raw_workbook=buffer.getvalue()
    bundle=agent_handoff._bundle([agent_handoff._item('report1','artifact','report.md',report),
        agent_handoff._item('book1','artifact','finance.xlsx',raw_workbook)])
    with service(output_bundle=bundle) as (url,_,_):
        p,_,task=delegated(project,url);mesh._advance(task['id']);patch('collab_task',task['id'],next_poll_at=0);mesh._advance(task['id'])
        actual=get('collab_task',task['id']);assert actual['state']=='succeeded' and len(actual['received_artifact_ids'])==2
        assert len(set(actual['received_artifact_ids']))==2 and not actual.get('received_result_ids')
        records=[get('agent_task_artifact',identity) for identity in actual['received_artifact_ids']]
        assert all(record['project_id']==project and record['turn_id']==p['id'] for record in records)
        entries=assets.catalog(project,'agent')['items'];assert len(entries)==2
        other=put('project',{'name':'Other project'},'p_other_'+uid())
        by_name={entry['name']:entry for entry in entries}
        for name,raw in [('report.md',report),('finance.xlsx',raw_workbook)]:
            entry=by_name[name];path,receipt=assets.file(project,entry['id'])
            assert path.read_bytes()==raw and receipt['download_sha256']==hashlib.sha256(raw).hexdigest()
            assert receipt['integrity_verified'] and not receipt['financial_verified']
            assert receipt['semantic_status']=='requires_review' and receipt['status']=='needs_review'
            with pytest.raises(KeyError):assets.file(other['id'],entry['id'])
        assert assets.preview(project,by_name['report.md']['id'])['text']==report.decode()
        assert assets.preview(project,by_name['finance.xlsx']['id'])['rows'][1]['c_2']=='9007199254740993.0001'
        response=unseal(actual['secret_remote_response']);mesh._receive(actual,response)
        assert get('collab_task',task['id'])['received_artifact_ids']==actual['received_artifact_ids']
        assert len(assets.catalog(project,'agent')['items'])==2
        assert not [value for value in all_items('source') if value.get('project_id')==project]
        archived=next(record for record in records if record['name']=='report.md')
        patch('agent_task_artifact',archived['id'],archived=True)
        with pytest.raises(ValueError,match='已归档'):mesh.read_task_artifact(project,task['id'],'report1')


def test_catalog_import_failure_keeps_verified_bytes_but_does_not_claim_complete(project,monkeypatch):
    raw=b'Full report bytes remain available after catalog failure.'
    bundle=agent_handoff._bundle([agent_handoff._item('file1','artifact','report.txt',raw)])
    with service(output_bundle=bundle) as (url,_,_):
        _,_,task=delegated(project,url)
        def fail(*args,**kwargs):raise OSError('synthetic local artifact storage failure')
        monkeypatch.setattr(agent_handoff,'import_artifacts',fail)
        mesh._advance(task['id']);patch('collab_task',task['id'],next_poll_at=0);mesh._advance(task['id'])
        actual=get('collab_task',task['id'])
        assert actual['state']=='input_required' and actual['verification_status']=='integrity_verified'
        assert '统一成果目录尚未完成' in actual['error'] and not actual.get('received_artifact_ids')
        receipt=mesh.read_task_artifact(project,task['id'],'file1')
        assert base64.b64decode(receipt['content_base64'])==raw and not receipt['financial_verified']
