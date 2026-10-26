import json
import pytest
from agent_service import data_governance as g,workspace
from agent_service.data_contracts import select_contract
from agent_service.store import put,uid
from agent_service.conversation import create_project,ProjectInput

def project():return create_project(ProjectInput(name='Governance',request_id=uid()))['id']
def pipeline(pid):
    return put('pipeline',{'project_id':pid,'name':'数据应用','source_id':'source-fixture','source_ids':['source-fixture'],'commit':'draft-code','spec':{'amount_field':'amount','metric_definition':'DRAFT MUST NOT LEAK'}},uid())

def test_scoped_contracts_do_not_affect_other_application():
    pid=project();a=pipeline(pid);b=pipeline(pid)
    workspace.write_file(pid,'data-contract.json',json.dumps({'version':1,'min_rows':3}))
    workspace.write_file(pid,'contracts/'+a['id']+'.json',json.dumps({'version':1,'max_rows':1}))
    assert select_contract(pid,a['id'])=={'version':1,'max_rows':1}
    assert select_contract(pid,b['id'])=={'version':1,'min_rows':3}
    with pytest.raises(ValueError):select_contract(pid,'../other')

def test_catalog_scope_pagination_and_unpublished_semantics():
    pid=project();other=project();a=pipeline(pid);b=pipeline(pid);foreign=pipeline(other)
    page=g.catalog(pid,limit=1)
    assert len(page['assets'])==1 and page['next_offset']==1
    rest=g.catalog(pid,offset=1,limit=1)
    assert {page['assets'][0]['application_id'],rest['assets'][0]['application_id']}=={a['id'],b['id']}
    assert all(x['published_metric'] is None for x in page['assets']+rest['assets'])
    assert 'DRAFT MUST NOT LEAK' not in str(page)
    with pytest.raises(ValueError):g.quality(pid,foreign['id'])

def test_catalog_uses_published_snapshot_not_draft(monkeypatch,request):
    pid=project();p=pipeline(pid)
    from agent_service.store import patch
    patch('pipeline',p['id'],published_run='published-run')
    request.addfinalizer(lambda:patch('pipeline',p['id'],published_run=None))
    monkeypatch.setattr(g,'get_job',lambda rid:{'id':rid,'commit':'published-code'})
    monkeypatch.setattr(g,'manifest_for',lambda r:(None,{'inputs':[{'source_id':'original-source'}],'spec':{'amount_field':'amount','metric_status':'confirmed','metric_definition':'退款后收入','currency':'CNY'},'records':2}))
    row=g.catalog(pid)['assets'][0]
    assert row['published_metric']['definition']=='退款后收入'
    assert row['published_metric']['commit']=='published-code'
    assert row['published_sources']==['original-source']
