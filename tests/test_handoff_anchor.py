"""Local upload evidence never treats a self-reported handoff hash as ancestry."""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from agent_service import agent_handoff as handoff, connectors
from agent_service.actor_context import actor_scope
from agent_service.handoff_anchor import AnchorError, check_anchor
from agent_service.store import all_items, delete, get, patch, put, seal, unseal, uid
from test_multi_file_conversation import project, upload, H


@pytest.fixture(autouse=True)
def cleanup_records():
    kinds=('agent_client','agent_handoff_import')
    previous={kind:{item['id'] for item in all_items(kind)} for kind in kinds}
    yield
    for kind in kinds:
        for item in all_items(kind):
            if item['id'] not in previous[kind]:delete(kind,item['id'])


def pair(project, raw=b'id,amount\n1,5\n'):
    pid,client,_=project
    c=put('agent_client',{'enabled':True,'expires_at':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
          'revision':uid(),'owner_id':'owner','actor_id':'u_'+('1'*64),'project_ids':[pid]},'agc_'+uid())
    item=handoff._item('external','source','incoming.csv',raw,source_version={
        'source_id':'untrusted','revision':1,'revision_id':'untrusted-version',
        'content_sha256':hashlib.sha256(raw).hexdigest()})
    with actor_scope(c['actor_id']):
        imported=handoff.import_bundle(pid,handoff._bundle([item]),scope_id='gateway:'+c['id'],request_id=uid(),
                                       gateway_grant={'client_id':c['id'],'revision':c['revision']})
    external=imported['source_ids'][0]
    anchor=upload(project,content=raw)['id']
    for sid in [external,anchor]:
        rows,evidence=connectors.file_rows(get('source',sid),100)
        assert rows and evidence['parser']=='csv'
        patch('source',sid,status='ready',profile={'evidence':evidence})
    return external,anchor,c


def test_authenticated_http_upload_and_exact_current_bytes(project):
    pid=project[0];external,anchor,client=pair(project)
    result=check_anchor(pid,external,anchor)
    assert result['actor_id']=='owner' and result['proof_scope']=='recipient_authenticated_subject_uploaded_identical_bytes'
    assert result['gateway_client_id']==client['id']
    assert result['remote_lineage_verified'] is False and result['financial_verified'] is False
    assert result['external_content_sha256']==result['anchor_content_sha256']
    with actor_scope('u_'+('2'*64)):
        with pytest.raises(AnchorError):check_anchor(pid,external,anchor)
    response=project[1].post('/api/projects/'+pid+'/attachments',files={'file':('x.csv',b'id\n1\n')},data={'request_id':'unauthorized-upload'})
    assert response.status_code in (401,403)


@pytest.mark.parametrize('change',[
    lambda p,e,a,c:patch('agent_client',c['id'],enabled=False),
    lambda p,e,a,c:patch('agent_client',c['id'],revision=uid()),
    lambda p,e,a,c:patch('agent_client',c['id'],project_ids=[]),
    lambda p,e,a,c:patch('source',a,archived=True),
    lambda p,e,a,c:patch('source',e,status='processing'),
    lambda p,e,a,c:patch('source',a,origin_source_id='derived'),
    lambda p,e,a,c:patch('source',a,fact_review_id='fact'),
    lambda p,e,a,c:patch('source',a,browser_download_id='browser'),
    lambda p,e,a,c:patch('source',a,profile={'evidence':{'parser':'csv','semantic_alignment_required':True}}),
    lambda p,e,a,c:patch('source',a,revision_id='missing'),
    lambda p,e,a,c:patch('source',e,external_provenance={'content_sha256':'0'*64}),
])
def test_stale_grants_and_unsafe_or_changed_sources_fail_closed(project,change):
    pid=project[0];external,anchor,client=pair(project)
    change(pid,external,anchor,client)
    with pytest.raises(AnchorError):check_anchor(pid,external,anchor)


def test_changed_or_missing_real_file_is_never_accepted(project):
    pid=project[0];external,anchor,_=pair(project)
    path,_,_=connectors.verified_file_source(get('source',anchor))
    path.write_bytes(b'id,amount\n1,999\n')
    with pytest.raises(AnchorError):check_anchor(pid,external,anchor)


def test_old_or_forged_sources_cannot_become_user_upload_by_retry(project):
    pid=project[0];external,anchor,_=pair(project)
    s=get('source',anchor);cfg=unseal(s['secret_config']);cfg.pop('user_upload_proof')
    old_config=seal(cfg)
    patch('source',anchor,secret_config=old_config)
    patch('source_version',s['revision_id'],secret_config=old_config)
    with pytest.raises(AnchorError,match='重新上传'):check_anchor(pid,external,anchor)


def test_different_bytes_and_cross_project_are_rejected(project):
    pid=project[0];external,anchor,_=pair(project)
    other=upload(project,content=b'id,amount\n1,9\n')['id']
    _,ev=connectors.file_rows(get('source',other),100)
    patch('source',other,status='ready',profile={'evidence':ev})
    with pytest.raises(AnchorError):check_anchor(pid,external,other)
    with pytest.raises(AnchorError):check_anchor('p_wrong',external,anchor)
    with pytest.raises(AnchorError):check_anchor(pid,external,external)


def test_upload_by_machine_token_does_not_create_user_proof(project):
    from agent_service.agent_gateway import _hash
    pid=project[0];_,_,client=pair(project)
    token='nexus_ag_'+uid();patch('agent_client',client['id'],token_hash=_hash(token))
    response=project[1].post('/api/projects/'+pid+'/attachments',
        headers={'Authorization':'Bearer '+token},
        files={'file':('x.csv',b'id\n1\n')},data={'request_id':'machine-attempt'})
    assert response.status_code in (401,403)


def test_old_gateway_handoff_without_server_grant_is_not_trusted(project):
    pid=project[0];external,anchor,_=pair(project)
    s=get('source',external);cfg=unseal(s['secret_config']);cfg['agent_handoff'].pop('gateway_grant')
    config=seal(cfg)
    patch('source',external,secret_config=config)
    patch('source_version',s['revision_id'],secret_config=config)
    with pytest.raises(AnchorError,match='固定记录'):check_anchor(pid,external,anchor)


def test_concurrent_source_change_is_detected(project,monkeypatch):
    from agent_service import handoff_anchor
    pid=project[0];external,anchor,_=pair(project)
    actual=handoff_anchor.verified_file_source
    def change_after_read(source):
        value=actual(source)
        if source['id']==anchor:patch('source',external,archived=True)
        return value
    monkeypatch.setattr(handoff_anchor,'verified_file_source',change_after_read)
    with pytest.raises(AnchorError,match='发生变化'):check_anchor(pid,external,anchor)
