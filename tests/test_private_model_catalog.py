"""Private data grants follow explicit model identities, never the catalog size."""
import copy
import json
import time

import pytest

from agent_service import company_search as kb, model_catalog, provider, work_tools
from agent_service.store import all_items, delete, get, now, patch, put, seal, uid


@pytest.fixture
def models(monkeypatch):
    entries=[{'id':mid,'name':mid,'base_url':'https://'+mid+'.test/v1',
              'model':'finance','api_key':'secret-'+mid,'share_data':True}
             for mid in ('a','b','c')]
    state={'selected':'a','revision':'catalog-original','models':entries}
    monkeypatch.setattr(model_catalog,'owner_configuration',lambda:{
        'models':copy.deepcopy(state['models']),'default_model_id':'a',
        'revision':state['revision']})
    monkeypatch.setattr(model_catalog,'active_selection',lambda:{
        'model_id':state['selected'],'model_catalog_revision':state['revision']})
    monkeypatch.setattr(provider,'settings',lambda:next(
        copy.deepcopy(entry) for entry in state['models'] if entry['id']==state['selected']))
    return state


@pytest.fixture
def project():
    pid=put('project',{'name':'Private catalog acceptance','created_at':now()},'p_'+uid())['id']
    yield pid
    # Lifecycle reconciliation scans the whole workspace. Synthetic private
    # grants and actions belong to this fixture, not to later test projects.
    # Keep the project until its encrypted children have been read and removed.
    for kind in ('work_action','work_connector','company_operation','company_evidence',
                 'company_search','turn','conversation'):
        for item in all_items(kind):
            if item.get('project_id')==pid:delete(kind,item['id'])
    delete('project',pid)


def company_config(ids):
    return kb.CompanySearchConfig(search_url='https://company.test/search',
        principal_label='operator-provisioned',user_scoped_ack=True,
        storage_approved=True,model_approved=True,approved_model_ids=ids)


def test_company_explicit_model_set_and_new_catalog_model_denied(models,project,monkeypatch):
    monkeypatch.setattr(kb,'_search_response',lambda *a,**k:{})
    kb.configure(project,company_config(['a','b']))
    assert kb.status(project)['model_allowed'] is True
    models['selected']='b'
    kb.assert_model_allowed(project)
    models['selected']='c'
    with pytest.raises(kb.CompanySearchError,match='管理员'):kb.assert_model_allowed(project)
    # Even an alias of the exact approved destination needs an explicit grant.
    models['models'][2].update(base_url=models['models'][0]['base_url'])
    assert kb.status(project)['model_allowed'] is False
    raw=kb._config(project)
    assert set(raw['approved_models'])=={'a','b'}
    assert all(v['catalog_revision']=='catalog-original' for v in raw['approved_models'].values())
    assert 'secret-' not in json.dumps(raw['approved_models'])
    assert 'approved_models' not in kb.configuration(project)


def test_same_model_id_cannot_inherit_grant_after_destination_change(models,project,monkeypatch):
    monkeypatch.setattr(kb,'_search_response',lambda *a,**k:{})
    kb.configure(project,company_config(['a']))
    # Unrelated catalog edits preserve the unchanged model's approval.
    models['revision']='catalog-new';models['models'][1]['model']='different'
    assert kb.status(project)['model_allowed'] is True
    models['models'][0]['base_url']='https://changed-destination.test/v1'
    assert kb.status(project)['model_allowed'] is False
    kb.configure(project,company_config(['a']))
    assert kb.status(project)['model_allowed'] is True


def test_incomplete_explicit_grant_never_falls_back_to_default(models):
    assert kb.model_approval_allowed({'model_approved':True,'approved_model_ids':['a'],
        'approved_models':{},'model_fingerprint':kb.model_fingerprint()}) is False


@pytest.mark.parametrize('ids',[['missing'],['a','a']])
def test_invalid_approved_ids_rejected_before_company_network(models,project,monkeypatch,ids):
    monkeypatch.setattr(kb,'_search_response',lambda *a,**k:pytest.fail('invalid grant reached network'))
    with pytest.raises(ValueError):kb.configure(project,company_config(ids))


def test_approved_ids_require_explicit_model_consent(models):
    with pytest.raises(ValueError):company_config(['a']).model_validate(
        dict(company_config(['a']).model_dump(),model_approved=False))


def _work(monkeypatch,project,ids,name):
    class Remote:
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setattr('agent_service.connectors.safe_api_url',lambda u:'203.0.113.1')
    monkeypatch.setattr(work_tools,'Session',Remote)
    monkeypatch.setattr(work_tools,'catalog',lambda s:{'search':{'inputSchema':{'type':'object'}}})
    return work_tools.configure(project,work_tools.ConnectorInput(name=name,
        url='https://'+name+'.test/mcp',read_tools=['search'],storage_approved=True,
        model_approved=True,approved_model_ids=ids))


def test_project_history_requires_intersection_of_system_grants(models,project,monkeypatch):
    first=_work(monkeypatch,project,['a','b'],'first')
    second=_work(monkeypatch,project,['b','c'],'second')
    assert 'approved_models' not in first
    # a can read first alone, but must not receive history from second.
    work_tools.model_allowed(get('work_connector',first['id']))
    with pytest.raises(kb.CompanySearchError):work_tools.assert_project_model_allowed(project)
    models['selected']='b'
    work_tools.assert_project_model_allowed(project)
    models['selected']='c'
    work_tools.model_allowed(get('work_connector',second['id']))
    with pytest.raises(kb.CompanySearchError):work_tools.assert_project_model_allowed(project)
    # Disabling a connector must not erase privacy on already retrieved data.
    patch('work_connector',first['id'],enabled=False)
    with pytest.raises(kb.CompanySearchError):work_tools.assert_project_model_allowed(project)


def test_connector_revocation_blocks_complete_history(models,project,monkeypatch):
    _work(monkeypatch,project,['a','b'],'system')
    work_tools.assert_project_model_allowed(project)
    patch('project',project,work_model_approval=None)
    with pytest.raises(kb.CompanySearchError):work_tools.assert_project_model_allowed(project)


def test_disabling_model_data_access_also_blocks_cached_work_history(models,project,monkeypatch):
    _work(monkeypatch,project,['a'],'system')
    work_tools.assert_project_model_allowed(project)
    models['models'][0]['share_data']=False
    with pytest.raises(kb.CompanySearchError):work_tools.assert_project_model_allowed(project)


def test_unapproved_work_model_set_cannot_enable_connection(models,project,monkeypatch):
    monkeypatch.setattr('agent_service.connectors.safe_api_url',lambda u:'203.0.113.1')
    with pytest.raises(ValueError,match='明确启用'):
        work_tools.configure(project,work_tools.ConnectorInput(name='system',
            url='https://system.test/mcp',read_tools=['search'],storage_approved=True,
            approved_model_ids=['a']))


def test_external_action_worker_checks_originating_turn_model(models,project,monkeypatch):
    from agent_service.conversation import default_session
    connector=_work(monkeypatch,project,['b'],'system')
    patch('work_connector',connector['id'],write_tools=['search'],read_tools=[])
    session_id=default_session(project)
    origin=put('turn',{'project_id':project,'session_id':session_id,
                      'text':'提交已确认的业务操作','interaction_mode':'work',
                      'state':'done','outcome':'needs_input','created_at':now(),
                      'finished_at':now(),'model_id':'b',
                      'model_catalog_revision':'catalog-original'},'t_'+uid())
    payload={'connector_id':connector['id'],'connector_revision':connector['revision'],
             'tool':'search','arguments':{},
             'tool_fingerprint':work_tools.fingerprint(connector['catalog']['search'])}
    action=put('work_action',{'project_id':project,'session_id':session_id,
        'turn_id':origin['id'],'state':'approved','created_at':now(),
        'digest':work_tools.fingerprint(payload),'expires':time.time()+60,
        'secret_payload':seal(payload)},'wa_'+uid())
    observed=[]
    def check_model(value):
        observed.append(provider.current_turn_id())
        assert provider.current_turn_id()==origin['id']
    monkeypatch.setattr(work_tools,'model_allowed',check_model)
    class Remote:
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def request(self,*args):return {'content':[{'type':'text','text':'saved'}]}
    monkeypatch.setattr(work_tools,'Session',Remote)
    assert work_tools.perform(action['id'])['state']=='succeeded'
    assert observed==[origin['id']]
    assert provider.current_turn_id() is None
