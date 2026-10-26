import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from agent_service import collaboration_budget as budget
from agent_service.store import put,get,patch,delete,all_items,now,uid
from agent_service.provider import BudgetExceeded


@pytest.fixture
def parent():
    identity='t_'+uid();pid='p_'+uid()
    put('project',{'name':'Budget tests','created_at':now()},pid)
    root=put('turn',{'project_id':pid,'actor_id':'owner','state':'agent','text':'核对资料','created_at':now(),
        'model_id':'finance','model_catalog_revision':'fixture-pinned',
        'task_contract':{'kind':'answer','budgets':{'model_calls':5,'input_characters':1000,'output_tokens':1000,'duration_ms':60000}},
        'model_usage':{'calls':1,'input_characters':100,'output_tokens_budgeted':100,'duration_ms':10}},identity)
    yield root
    for kind in ('turn','collab_budget'):
        for item in all_items(kind):
            if item.get('project_id')==pid:delete(kind,item['id'])
    delete('project',pid)


def child(parent):
    return put('turn',{'project_id':parent['project_id'],'actor_id':'owner','state':'agent',
        'collaboration_parent_turn_id':parent['id'],'collaboration_root_turn_id':parent['id'],
        'model_id':parent['model_id'],'model_catalog_revision':parent['model_catalog_revision']},'t_'+uid())


def test_baseline_counted_once_and_restart_never_resets(parent):
    first=budget.initialize(parent)
    assert first['used']=={'calls':1,'input_characters':100,'output_tokens_budgeted':100}
    budget.admit(parent,200,100)
    saved=budget.initialize(get('turn',parent['id']))
    assert saved['used']['calls']==2 and saved['used']['input_characters']==300
    assert saved['deadline_at']==first['deadline_at']


def test_children_and_parent_share_atomic_limit_across_threads(parent):
    budget.initialize(parent);children=[child(parent) for _ in range(2)]
    def reserve(i):
        try:budget.admit((children+[parent])[i%3],10,10);return True
        except BudgetExceeded:return False
    with ThreadPoolExecutor(max_workers=6) as pool:accepted=list(pool.map(reserve,range(12)))
    assert sum(accepted)==4
    totals=budget.current_totals(parent)
    assert totals['used']['calls']==5 and totals['remaining']['model_calls']==0


def test_parent_cancellation_stops_child_and_parent_requests(parent):
    budget.initialize(parent);sub=child(parent)
    patch('turn',parent['id'],state='done',outcome='cancelled')
    for turn in (parent,sub):
        with pytest.raises(BudgetExceeded,match='已结束或取消'):budget.admit(turn,1,1)
    assert budget.current_totals(parent)['used']['calls']==1


def test_pinned_model_project_and_actor_cannot_change(parent):
    budget.initialize(parent);sub=child(parent)
    for key,value in [('model_catalog_revision','changed'),('project_id','foreign'),('actor_id','u_'+'a'*64)]:
        original=get('turn',sub['id'])[key];patch('turn',sub['id'],**{key:value})
        with pytest.raises(BudgetExceeded):budget.admit(sub,1,1)
        patch('turn',sub['id'],**{key:original})
    assert budget.current_totals(parent)['used']['calls']==1


def test_noop_for_ordinary_turn_but_child_missing_ledger_fails_closed(parent):
    assert budget.admit(parent,10,10)=={'active':False}
    sub=child(parent)
    with pytest.raises(BudgetExceeded,match='记录不可用'):budget.admit(sub,1,1)


def test_deadline_and_combined_token_input_limits(parent,monkeypatch):
    budget.initialize(parent)
    with pytest.raises(BudgetExceeded):budget.admit(parent,901,1)
    with pytest.raises(BudgetExceeded):budget.admit(parent,1,901)
    record=get(budget.KIND,budget._identity(parent['id']))
    monkeypatch.setattr(budget.time,'time',lambda:record['deadline_at']+1)
    with pytest.raises(BudgetExceeded):budget.admit(parent,1,1)
    assert budget.current_totals(parent)['used']['calls']==1


def test_budget_cannot_be_expanded_or_invalid_counters_refund(parent):
    with pytest.raises(ValueError):budget.initialize(parent,{'model_calls':6})
    for args in [(-1,1),(1,-1),(True,1),(1,1.5)]:
        with pytest.raises(ValueError):budget.admit(parent,*args)


def test_child_cannot_initialize_nested_budget(parent):
    sub=child(parent)
    with pytest.raises(ValueError,match='不能再次委派'):budget.initialize(sub)


def unlimited(parent,usage=None):
    return patch('turn',parent['id'],task_contract={'kind':'answer','budget_policy':'unlimited',
        'budgets':dict.fromkeys(budget.DEFAULTS,None)},model_usage=usage or parent.get('model_usage',{}))


def test_unlimited_root_counts_concurrent_calls_without_legacy_caps_or_deadline(parent):
    root=unlimited(parent,{'calls':6000,'input_characters':2000000,'output_tokens_budgeted':20000000,'duration_ms':900000})
    first=budget.initialize(root)
    assert all(value is None for value in first['limits'].values())
    assert all(value is None for value in first['remaining'].values()) and first['deadline_at'] is None
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _:budget.admit(root,90000,64000),range(25)))
    saved=budget.initialize(get('turn',root['id']))
    assert saved['used']['calls']==6025 and saved['used']['input_characters']==4250000
    assert saved['used']['output_tokens_budgeted']==21600000 and saved['deadline_at'] is None


def test_historical_default_ledger_is_removed_without_resetting_usage(parent):
    legacy=dict(budget.DEFAULTS)
    root=patch('turn',parent['id'],task_contract={'kind':'answer','budgets':legacy})
    used={'calls':18,'input_characters':360000,'output_tokens_budgeted':24000}
    identity=budget._identity(root['id'])
    put(budget.KIND,{'root_turn_id':root['id'],'project_id':root['project_id'],'actor_id':'owner',
        'limits':legacy,'used':used,'baseline_usage':dict(used),'model_pin':{'model_id':root['model_id'],
        'model_catalog_revision':root['model_catalog_revision']},'deadline_at':time.time()-10,
        'per_turn':{},'policy':'historical defaults'},identity)
    view=budget.current_totals(root)
    assert view['used']==used and view['deadline_at'] is None and view['limits']['model_calls'] is None
    result=budget.admit(root,90000,3200)
    assert result['used']['calls']==19 and result['used']['input_characters']==450000
    stored=get(budget.KIND,identity)
    assert stored['used']['calls']==19 and stored['deadline_at'] is None


def test_explicit_narrowed_ledger_and_child_grant_stay_bounded_for_unlimited_root(parent):
    root=unlimited(parent)
    first=budget.initialize(root,{'model_calls':3})
    assert first['limits']['model_calls']==3 and first['limits']['duration_ms'] is None
    sub=patch('turn',child(root)['id'],task_contract={'kind':'answer','budget_policy':'bounded',
        'budgets':{'model_calls':1,'input_characters':1000,'output_tokens':1000,'duration_ms':60000}})
    budget.admit(sub,20,20)
    with pytest.raises(BudgetExceeded,match='子任务'):budget.admit(sub,20,20)
    budget.admit(root,20,20)
    with pytest.raises(BudgetExceeded,match='合计'):budget.admit(root,20,20)
    assert budget.current_totals(root)['used']['calls']==3


def test_unlimited_root_still_stops_on_cancellation_and_model_drift(parent):
    root=unlimited(parent);budget.initialize(root);sub=child(root)
    patch('turn',sub['id'],model_catalog_revision='changed')
    with pytest.raises(BudgetExceeded,match='模型版本'):budget.admit(sub,100000,64000)
    patch('turn',root['id'],state='done',outcome='cancelled')
    with pytest.raises(BudgetExceeded,match='已结束或取消'):budget.admit(root,100000,64000)
