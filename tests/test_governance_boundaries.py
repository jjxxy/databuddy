"""Lightweight boundary tests independent of the end-to-end pipeline fixture."""
import copy
import json
import pytest
from agent_service import data_governance as g,metric_registry as registry,agent_context
from agent_service.store import put,uid,delete
from agent_service.column_lineage import build


def test_source_page_does_not_open_any_application_artifacts(monkeypatch,request):
    pid='p_'+uid();put('project',{'name':'分页读取边界'},pid)
    put('source',{'project_id':pid,'name':'来源','status':'ready','profile':{'fields':[]}},uid())
    fake=put('pipeline',{'project_id':pid,'name':'应用','source_id':'s','published_run':'unavailable','active_commit':'commit'},uid())
    request.addfinalizer(lambda:delete('pipeline',fake['id']))
    def forbidden(*a,**k):raise AssertionError('source page opened pipeline artifacts')
    monkeypatch.setattr(g,'manifest_for',forbidden)
    monkeypatch.setattr(g,'get_job',forbidden)
    page=g.catalog(pid,asset_type='sources',limit=1)
    assert page['total']==1 and page['assets'][0]['asset_type']=='source'
    # Application descriptors sort before sources; page 2 is still metadata-only.
    assert g.catalog(pid,offset=1,limit=1,asset_type='all')['assets'][0]['asset_type']=='source'


def test_semantic_digest_changes_on_rule_unit_or_code_change():
    p={'id':'app','project_id':'project'};run={'id':'run','commit':'a'*40}
    spec={'amount_field':'amount','group_by':'client','currency':'CNY','metric_definition':'收入','filters':[],
          'computations':[],'joins':[],'trim_fields':[],'primary_key':'id'}
    m={'spec':spec,'metric_aggregation':'sum_decimal_38_12'}
    initial=registry.declaration(p,run,m)
    for key,value in [('currency','USD'),('metric_definition','净收入'),('filters',[{'field':'status','value':'paid'}])]:
        changed=copy.deepcopy(m);changed['spec'][key]=value
        assert registry.declaration(p,run,changed)['metric_sha256']!=initial['metric_sha256']
    assert registry.declaration(p,dict(run,commit='b'*40),m)['id']!=initial['id']


def test_quote_contains_exact_code_and_does_not_inherit_model_status():
    p={'id':'app','project_id':'project'}
    m=registry.declaration(p,{'id':'run','commit':'b'*40},{'spec':{'amount_field':'a','metric_definition':'退款后收入','currency':'CNY','metric_status':'confirmed'}})
    assert m['status']=='proposed'
    assert registry.confirmation_text(m)=='确认指标：退款后收入；单位：CNY；版本：bbbbbbbbbbbb'
    assert registry.status(p,{'id':'run','commit':'b'*40},{'spec':{'amount_field':'a'}})=='draft'


def test_chat_only_discovers_read_tools_for_governance():
    turn={'text':'查看指标口径、字段血缘和资产','interaction_mode':'chat'}
    tools=set(agent_context.selected(turn))
    assert {'data_asset_catalog','inspect_metric','inspect_column_lineage'}<=tools
    assert 'confirm_metric' not in tools
