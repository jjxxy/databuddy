"""Frozen historic runs cannot erase business-semantic review obligations."""
import copy

import pytest

from agent_service import project_data as data, semantic_service
from agent_service.result_delivery import read_handoff
from test_project_data import column, document, project, registered, run, source


def _stage(kind):
    if kind=='lookup':
        return {'kind':'lookup','id':'matched','description':'按单据匹配补充归属',
                'input':'orders','right':'reference','left_key':'id','right_key':'id',
                'outputs':{'office':column('other_office')}}
    return {'kind':'reclassify','id':'regions','description':'按映射归类办事处',
            'input':'orders','column':'office','output':column('region'),
            'mapping':{'East':'East-region','West':'West-region'}}


@pytest.mark.parametrize('kind',['lookup','reclassify'])
@pytest.mark.parametrize('marker',[False,None])
def test_frozen_stage_cannot_launder_missing_historical_flags(monkeypatch,kind,marker):
    import agent_service.jobs as jobs
    pid=project();src=source(pid);doc=document(src['id']);doc['contract']=None
    if kind=='lookup':
        doc['plan']['inputs']['reference']=copy.deepcopy(doc['plan']['inputs']['orders'])
        doc['bindings']['reference']=copy.deepcopy(doc['bindings']['orders'])
    doc['plan']['stages']=[_stage(kind)];doc['plan']['output']=doc['plan']['stages'][0]['id']
    result=run(pid,registered(pid,doc));folder,meta=data.result_folder(pid,result['id'])
    assert meta['requires_semantic_review'] is True
    page=read_handoff(folder/'handoff',limit=1,expected_manifest_sha256=meta['handoff_sha256'])
    meta=copy.deepcopy(meta);page=copy.deepcopy(page)
    if marker is None:
        meta.pop('requires_semantic_review');page['context'].pop('requires_semantic_review')
    else:
        meta['requires_semantic_review']=False;page['context']['requires_semantic_review']=False
    original=jobs.get_job
    def altered(ident):
        value=original(ident)
        if ident==result['id']:
            value=copy.deepcopy(value);value['data_result']=meta;value['status']='succeeded'
        return value
    monkeypatch.setattr(jobs,'get_job',altered)
    with pytest.raises(ValueError,match='语义复核依据不完整'):
        data.require_safe_numeric_lineage(pid,result['id'],root_meta=meta,root_page=page)


def test_exact_authenticated_mapping_is_valid_without_extra_generic_review():
    pid=project();src=source(pid);doc=document(src['id']);doc['contract']=None
    mapping={'id':'office-region-map','version':1,'source_field_id':'office','target_field_id':'region',
             'entries':[{'source':'East','target':'East-region'},{'source':'West','target':'West-region'}],
             'unmapped_policy':'error'}
    proposal=semantic_service.propose_mapping(pid,mapping)
    semantic_service.review_mapping_record(pid,proposal['id'],proposal['content_sha256'],actor='authenticated-test-owner')
    doc['reviewed_mapping_ids']=[proposal['id']];doc['plan']['stages']=[_stage('reclassify')]
    doc['plan']['output']='regions'
    result=run(pid,registered(pid,doc))
    assert result['data_result']['requires_semantic_review'] is False
    data.require_safe_numeric_lineage(pid,result['id'])
