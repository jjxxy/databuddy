import copy
import json

import pytest

from agent_service import semantic_service as service, workspace
from agent_service.semantic_alignment import apply_value_mapping
from agent_service.store import get, patch, put, uid


def project():
    return put('project', {'name':'语义核对'}, 'p_'+uid())['id']


def field(identifier='revenue'):
    return {'id':identifier,'name':'净收入','meaning':'已付款订单扣除退款后的金额',
            'kind':'measure','grain':['office','month'],'unit':'CNY','value_format':'decimal',
            'temporal_kind':'interval','period':'2026-10','aggregation':'sum'}


def mapping(version=1):
    return {'id':'office-crosswalk','version':version,'source_field_id':'province','target_field_id':'office',
            'entries':[{'source':'安徽','target':'合肥办事处'},{'source':'江苏','target':'南京办事处'}]}


def test_proposals_are_unreviewed_owned_and_paginated():
    pid=project();other=project()
    alignment=service.propose(pid,field('source'),[field()])
    crosswalk=service.propose_mapping(pid,mapping())
    assert alignment['status']==crosswalk['status']=='proposed'
    page=service.list_proposals(pid,limit=1)
    assert page['total']==2 and page['next_offset']==1 and len(page['items'])==1
    assert 'payload' not in page['items'][0]
    assert service.list_proposals(other)['items']==[]
    for getter in (service.get_proposal,service.get_reviewed_mapping,service.get_reviewed_alignment):
        with pytest.raises(ValueError,match='当前项目'):getter(other,alignment['id'])
    with pytest.raises(ValueError):service.list_proposals(pid,limit=True)
    with pytest.raises(ValueError):service.get_reviewed_mapping(pid,crosswalk['id'])


def test_server_review_binds_digest_and_cannot_change_approved_target():
    pid=project()
    one=field('target-one');two=field('target-two')
    record=service.propose(pid,field('source'),[one,two])
    with pytest.raises(ValueError,match='版本已变化'):
        service.review(pid,record['id'],'0'*64,'target-one','owner')
    with pytest.raises(ValueError,match='核对人'):
        service.review(pid,record['id'],record['content_sha256'],'target-one',' ')
    approved=service.review(pid,record['id'],record['content_sha256'],'target-one','oidc-subject-hash')
    assert approved['review']['reviewer']=='oidc-subject-hash'
    assert service.get_reviewed_alignment(pid,record['id']).target_id=='target-one'
    repeated=service.review(pid,record['id'],record['content_sha256'],'target-one','another-owner')
    assert repeated['review']==approved['review']
    with pytest.raises(ValueError,match='不可变'):
        service.review(pid,record['id'],record['content_sha256'],'target-two','owner')


def test_client_cannot_self_submit_approved_status_or_replace_server_receipt():
    pid=project();record=service.propose_mapping(pid,mapping())
    external=copy.deepcopy(record);external['status']='approved'
    external['review']={'reviewer':'model'}
    with pytest.raises(ValueError):service.get_reviewed_mapping(pid,external['id'])
    assert service.get_proposal(pid,record['id'])['status']=='proposed'
    approved=service.review_mapping_record(pid,record['id'],record['content_sha256'],'owner')
    review=service.get_reviewed_mapping(pid,record['id'])
    assert review.mapping_sha256==record['content_sha256']
    assert apply_value_mapping([{'province':'安徽'}],'province',review)['rows'][0]['province']=='合肥办事处'
    assert service.review_mapping_record(pid,record['id'],record['content_sha256'],'different')['review']==approved['review']


def test_editing_definitions_creates_new_revision_and_preserves_prior_approval():
    pid=project();first=service.propose_mapping(pid,mapping())
    service.review_mapping_record(pid,first['id'],first['content_sha256'],'owner')
    changed=mapping(2);changed['entries'][0]['target']='新的合肥办事处'
    second=service.propose_mapping(pid,changed)
    assert second['id']!=first['id'] and second['version']==first['version']+1
    assert second['status']=='proposed'
    assert service.get_reviewed_mapping(pid,first['id']).mapping.version==1
    with pytest.raises(ValueError):service.get_reviewed_mapping(pid,second['id'])
    same=service.propose_mapping(pid,mapping())
    assert same['id']==first['id'] and same['status']=='approved'


def test_model_name_match_with_bad_units_cannot_be_approved():
    pid=project();source=field('source');source['unit']='万元'
    record=service.propose(pid,source,[field()])
    with pytest.raises(ValueError,match='不兼容'):
        service.review(pid,record['id'],record['content_sha256'],'revenue','owner')
    rejected=service.review(pid,record['id'],record['content_sha256'],'revenue','owner','rejected')
    assert rejected['status']=='rejected'
    with pytest.raises(ValueError):service.get_reviewed_alignment(pid,record['id'])


def test_workspace_definitions_do_not_contain_consent_and_file_edits_do_not_approve():
    pid=project();record=service.propose_mapping(pid,mapping())
    text=workspace.read_file(pid,record['definition_path'])
    assert 'reviewer' not in text and 'approved' not in text
    obj=json.loads(text);obj['status']='approved';obj['reviewer']='model'
    workspace.write_file(pid,record['definition_path'],json.dumps(obj))
    with pytest.raises(ValueError):service.get_reviewed_mapping(pid,record['id'])


def test_source_quotes_and_credentials_are_not_saved_as_definitions():
    pid=project();source=field('source')
    source['provenance']=[{'source_id':'report','locator':'sheet A cell B3','content_sha256':'a'*64,'quote':'SENSITIVE ORIGINAL ROW'}]
    record=service.propose(pid,source,[field()])
    assert 'SENSITIVE ORIGINAL ROW' not in json.dumps(service.get_proposal(pid,record['id']))
    assert 'SENSITIVE ORIGINAL ROW' not in workspace.read_file(pid,record['definition_path'])
    source['meaning']='api_key: a-private-key'
    with pytest.raises(ValueError,match='凭证'):
        service.propose(pid,source,[field()])


def test_large_definitions_use_metadata_without_claiming_file_storage():
    pid=project()
    large=mapping();large['entries']=[{'source':str(i).zfill(6)+'s'*50,'target':'t'*50} for i in range(3000)]
    record=service.propose_mapping(pid,large)
    assert record['definition_path'] is None and record['definition_storage']=='metadata_only'
    service.review_mapping_record(pid,record['id'],record['content_sha256'],'owner')
    assert len(service.get_reviewed_mapping(pid,record['id']).mapping.entries)==3000


def test_tampered_database_payload_is_rejected_before_review_or_execution():
    pid=project();record=service.propose_mapping(pid,mapping())
    changed=copy.deepcopy(record['payload']);changed['entries'][0]['target']='tampered'
    patch(service.RECORD_KIND,record['id'],payload=changed)
    with pytest.raises(ValueError,match='完整性'):
        service.review_mapping_record(pid,record['id'],record['content_sha256'],'owner')
    with pytest.raises(ValueError,match='完整性'):
        service.get_proposal(pid,record['id'])


def test_review_functions_do_not_accept_cross_project_records():
    pid=project();other=project()
    alignment=service.propose(pid,field('source'),[field()])
    value=service.propose_mapping(pid,mapping())
    with pytest.raises(ValueError,match='当前项目'):
        service.review(other,alignment['id'],alignment['content_sha256'],'revenue','owner')
    with pytest.raises(ValueError,match='当前项目'):
        service.review_mapping_record(other,value['id'],value['content_sha256'],'owner')
