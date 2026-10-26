"""Exact working facts survive the old 50k/latest-two cliff and lossless sharing."""
import copy,json

import pytest

from agent_service import agent,agent_context as ctx,provider
from agent_service.store import get,put
from test_bounded_planning_context import turn


def layout():
    sheets=[{'name':'收入','candidate_regions':[]},{'name':'成本','candidate_regions':[]}]
    candidates=[]
    for index in range(5):
        candidate={'candidate_id':'region-'+str(index),'sheet':sheets[index%2]['name'],
                   'proposed_options':{'sheet':sheets[index%2]['name'],'header_rows':[2,3],'cell_range':'A2:H9'},
                   'header_preview':[{'coordinate':'A2','text':'待核对原始表头'+('名'*700)}],
                   'unit_basis':'explicit','percentage_fraction':True}
        sheets[index%2]['candidate_regions'].append(candidate)
    candidates=[copy.deepcopy(candidate) for sheet in sheets for candidate in sheet['candidate_regions']]
    return {'source_id':'s_layout','sheets':sheets,'candidate_regions':candidates,
            'region_discovery_truncated':False,'formula_cache_freshness':'unknown'}


def test_exact_layout_candidates_are_one_reconstructable_copy_without_mutating_original():
    original=layout();frozen=copy.deepcopy(original)
    view=ctx.observation_view(original,'inspect_source_tables')
    assert original==frozen and 'candidate_regions' not in view
    ref=view['candidate_regions_reference']
    assert ref['exact_values_already_present'] and ref['order']=='sheet_order'
    restored=[candidate for path in ref['concatenate'] for candidate in view['sheets'][int(path.split('/')[2])]['candidate_regions']]
    rebuilt=dict(view,candidate_regions=restored);rebuilt.pop('candidate_regions_reference')
    assert rebuilt==original
    assert len(json.dumps(original,ensure_ascii=False))-len(json.dumps(view,ensure_ascii=False))>3000
    assert ctx.observation_view(original,'read_work_tool') is original


@pytest.mark.parametrize('difference',['missing','changed','order','type'])
def test_layout_candidates_with_any_ordered_value_or_json_type_difference_are_not_shared(difference):
    original=layout()
    if difference=='missing':original['candidate_regions'].pop()
    elif difference=='changed':original['candidate_regions'][0]['unit_basis']='unknown'
    elif difference=='order':original['candidate_regions'].reverse()
    else:original['candidate_regions'][0]['percentage_fraction']=1  # True == 1 in Python, but JSON values differ.
    assert ctx.observation_view(original,'inspect_source_tables') is original


def table(index):
    fields=[{'name':'c_'+str(i),'label':'原始字段'+str(i),'unit':'CNY','unit_multiplier':'10000',
             'header_path':['月结底稿','金额或口径字段'+str(i)],'header_coordinates':[{'coordinate':chr(65+i)+'3'}],
             'metadata':'需保留的原始字段信息'*65} for i in range(1,8)]
    rows=[{'c_'+str(i):'9007199254740993.000000000001' if i==1 else '-0.000000000001' if i==2
                     else '2026/09/01' if i==3 else '13%' if i==4 else '2026-09' if i==5 else '含税' if i==6 else '预算v1'
           for i in range(1,8)} for _ in range(3)]
    return {'source_id':'s_'+str(index),'fields':fields,'rows':rows,'total':3,'offset':0,'next_offset':None,
            'complete_source_read':False,'available_rows_only':True,
            'evidence':{'fields':copy.deepcopy(fields),'source_revision':index+1,
                'selection':{'sheet':'已读页签'+str(index),'header_rows':[2,3],'cell_range':'A2:H6'},
                'source_rows':[4,5,6],'special_cells':[{'coordinate':'E4','formula':'=B4-C4','cached_value':'0.13'}],
                'warnings':[{'code':'PERCENTAGE_SEMANTICS_REQUIRED','coordinate':'F4'}],
                'executable_values_complete':False}}


def test_five_financial_tables_then_plan_and_guide_survive_actual_planning_payload_over_50k(monkeypatch):
    from agent_service.workflow_guidance import guide
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True,'engine':'atlas','tool_protocol':'json'})
    value=turn('请根据全部底稿核对单位、期间、预算版本与含税口径；先生成候选，不要发布。')
    originals=[table(i) for i in range(5)];canonical=guide()
    trace=[{'id':'financial-table-'+str(i),'tool':'read_source_table','status':'done',
            'arguments':{'source_id':'s_'+str(i),'options':{'sheet':'已读页签'+str(i)}},'observation':data}
           for i,data in enumerate(originals)]
    trace.extend([{'id':'plan','tool':'set_task_plan','status':'done','arguments':{},'observation':{'steps':['按已读表编写加工方案']}},
                  {'id':'guide','tool':'data_workflow_guide','status':'done','arguments':{},'observation':canonical}])
    value=put('turn',dict(value,agent_trace=trace),value['id']);frozen=copy.deepcopy(get('turn',value['id'])['agent_trace'])
    messages,_=agent.planning_context(value,native=False);info=json.loads(messages[1]['content'])
    assert len(messages[1]['content'])>50000, 'Regression must cross the former payload cliff'
    for index,original in enumerate(originals):
        observation=info['completed_steps'][index]['observation']
        assert observation==ctx.observation_view(original,'read_source_table')
        assert observation['rows']==original['rows']
        assert observation['evidence']['selection']==original['evidence']['selection']
        assert observation['evidence']['warnings']==original['evidence']['warnings']
        assert observation['evidence']['special_cells']==original['evidence']['special_cells']
        assert 'recover_with' not in observation
    assert info['completed_steps'][-1]['observation']==canonical
    assert get('turn',value['id'])['agent_trace']==frozen


def test_unconsumed_authoring_guide_does_not_expire_after_two_nonwriting_steps():
    from agent_service.workflow_guidance import guide
    canonical=guide();value=turn()
    value['agent_trace']=[{'id':'guide','tool':'data_workflow_guide','status':'done','arguments':{},'observation':canonical}]
    value['agent_trace'] += [{'id':'read-'+str(i),'tool':'read_source_table','status':'done','arguments':{},'observation':table(i)} for i in range(5)]
    result=ctx.trace_context(value,lambda value:value)
    assert result[0]['observation']==canonical


def test_legacy_owner_contract_model_view_shows_effective_policy_without_rewriting_audit(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True,'engine':'atlas','tool_protocol':'json'})
    value=turn('继续核对底稿，不要发布。')
    legacy=copy.deepcopy(value['task_contract']);legacy.pop('budget_policy',None)
    legacy['budgets']={'model_calls':18,'input_characters':360000,'output_tokens':24000,'duration_ms':300000}
    value=put('turn',dict(value,task_contract=legacy),value['id'])
    messages,_=agent.planning_context(value,native=False)
    contract=json.loads(messages[1]['content'])['task_contract']
    assert contract['budget_policy']=='unlimited' and all(limit is None for limit in contract['budgets'].values())
    for key in ('acceptance','goal','requirements'):
        assert contract[key]==legacy[key]
    assert get('turn',value['id'])['task_contract']==legacy


@pytest.mark.parametrize('extra_reference_characters',[0,30000])
def test_requested_guide_survives_five_tables_plan_file_and_followup_tools(monkeypatch,extra_reference_characters):
    from agent_service.workflow_guidance import guide
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True,'engine':'atlas','tool_protocol':'json'})
    value=turn('先核对完整底稿，再保存可复用加工方案，候选等待确认。')
    canonical=guide()
    if extra_reference_characters:
        canonical['instructions'].append('完整规范扩展说明：'+'x'*extra_reference_characters)
    trace=[{'id':'table-'+str(i),'tool':'read_source_table','status':'done','arguments':{},'observation':table(i)} for i in range(5)]
    trace.extend([
        {'id':'requested-guide','tool':'data_workflow_guide','status':'done','arguments':{},'observation':canonical},
        {'id':'saved-plan','tool':'write_file','status':'done','arguments':{'path':'notes/plan.md','content':'先查表，后编写加工方案'},
         'observation':{'path':'notes/plan.md','sha256':'a'*64}},
        {'id':'plan-state','tool':'set_task_plan','status':'done','arguments':{},'observation':{'steps':['编写并核验加工方案']}},
        {'id':'read-plan','tool':'read_file','status':'done','arguments':{'path':'notes/plan.md'},'observation':{'content':'先查表，后编写加工方案'}}])
    value=put('turn',dict(value,agent_trace=trace),value['id']);frozen=copy.deepcopy(get('turn',value['id'])['agent_trace'])
    receipts=ctx.trace_context(value,lambda value:value)
    assert receipts[5]['observation']==canonical
    messages,_=agent.planning_context(value,native=False)
    info=json.loads(messages[1]['content']);kept=info['completed_steps'][5]['observation']
    assert kept==canonical and 'schema' in kept
    assert 'recover_with' not in kept and 'context_excerpt' not in kept and 'not_full_result' not in kept
    assert get('turn',value['id'])['agent_trace']==frozen
