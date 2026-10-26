"""A broad Office request must not spend its input budget on unrelated schemas."""
import json
import pytest
from agent_service import agent,agent_context as ctx,provider,tasks
from agent_service.store import get,put,uid,now

GOAL=('请读取附件中的全部订单，仅统计status等于paid的记录，金额扣除退款。'
      '生成Excel工作簿、Word报告和PPT简报，保留完整明细与合计，写清来源和口径。'
      '只生成候选和报告，不要发布或确认正式结果。')


def turn(text=GOAL,mode='work'):
    pid='p_'+uid();put('project',{'name':'Bounded context regression','created_at':now()},pid)
    value=put('turn',{'project_id':pid,'text':text,'interaction_mode':mode,
                     'state':'done','created_at':now()},'t_'+uid())
    tasks.ensure(value)
    return get('turn',value['id'])


def test_actual_planner_payload_for_three_office_goal_is_bounded(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True,'engine':'atlas','tool_protocol':'json'})
    value=turn();captured=[]
    def respond(messages,**kwargs):
        captured.append(messages)
        return json.dumps({'kind':'final','answer':'需要实际执行后核验','status':'needs_input'})
    monkeypatch.setattr(provider,'complete',respond)
    agent.plan(value)
    messages=captured[0];info=json.loads(messages[1]['content'])
    assert sum(len(message['content']) for message in messages)<22000
    names={tool['name'] for tool in info['tools']}
    assert {'source_schema','read_source_table','write_file','render_office_artifact',
            'discover_tools','read_tool_result'}<=names
    assert not {'create_data_application','build_transform','revise_data_application'}&names
    assert set(info['tool_catalog'])==ctx.allowed(value)
    from agent_service import office
    assert info['office_spec_schema']==office.Spec.model_json_schema()
    assert get('turn',value['id'])['task_contract']['budgets']['input_characters'] is None
    assert '预览' in messages[0]['content'] and '完整' in messages[0]['content']


def test_heavy_schema_is_recovered_in_full_on_demand():
    value=turn();assert 'build_transform' not in ctx.selected(value)
    recovered=ctx.discover(value,ctx.DiscoverTools(query='build_transform',limit=1))
    assert recovered['tools']==[ctx.definition('build_transform')]
    schema=recovered['tools'][0]['arguments_schema']
    assert schema['additionalProperties'] is False
    assert '$defs' in schema and 'spec' in schema['properties']
    assert 'build_transform' in ctx.selected(get('turn',value['id']))
    with pytest.raises(ValueError):
        agent.validate_step(get('turn',value['id']),{'kind':'tool','tool':'build_transform',
                            'arguments':{'source_id':'not-present','name':'bad','spec':{'group_by':'c_1'}}})


def test_bounded_office_selection_does_not_grant_chat_writes_or_denied_tools(monkeypatch):
    value=turn(mode='chat')
    assert not {'write_file','render_office_artifact','build_transform'}&set(ctx.selected(value))
    recovered=ctx.discover(value,ctx.DiscoverTools(query='write_file render_office_artifact',limit=8))
    assert all(not agent.TOOLS[tool['name']][1] for tool in recovered['tools'])
    work=turn()
    monkeypatch.setattr('agent_service.agent_gateway.allowed_tools',lambda turn,names:names-{'render_office_artifact'})
    assert 'render_office_artifact' not in ctx.selected(work)
    recovered=ctx.discover(work,ctx.DiscoverTools(query='render_office_artifact',limit=1))
    assert all(tool['name']!='render_office_artifact' for tool in recovered['tools'])


def test_office_file_name_schema_and_safe_spec_error_match_the_renderer():
    from agent_service import office,workspace
    value=turn()
    arguments=office.OfficeArgs.model_json_schema()
    assert '不含路径和扩展名' in arguments['properties']['name']['description']
    with pytest.raises(ValueError):office.OfficeArgs(spec_path='spec.json',name='report.xlsx')
    secret='DO_NOT_ECHO_UNTRUSTED_SPEC_VALUE'
    workspace.write_file(value['project_id'],'bad-spec.json',json.dumps({
        'kind':'spreadsheet','title':'bad','sheets':[{'name':'Data','columns':['amount'],
        'rows':[{'private':secret}]}]}))
    with pytest.raises(ValueError,match='office_spec_schema') as error:
        office.render(value,'bad-spec.json','report')
    assert 'sheets.0.rows.0' in str(error.value)
    assert secret not in str(error.value)


def test_actual_data_workflow_planner_payload_has_a_bounded_generic_frontier(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True,'engine':'atlas','tool_protocol':'json'})
    value=turn('请用全部附件按办事处和月份做经营汇总对账，处理文本金额、退款和多表关联。'
               '保存可复用数据工作流、完整结果和口径说明，供我核对，不要发布。')
    captured=[]
    monkeypatch.setattr(provider,'complete',lambda messages,**kwargs:
        captured.append(messages) or json.dumps({'kind':'final','answer':'尚需执行','status':'needs_input'}))
    agent.plan(value)
    info=json.loads(captured[0][1]['content'])
    assert sum(len(message['content']) for message in captured[0])<22000
    assert len(info['tools'])<=18
    assert sum(len(json.dumps(tool,ensure_ascii=False)) for tool in info['tools'])<=ctx.AUTO_SCHEMA_CHARACTERS
    names={tool['name'] for tool in info['tools']}
    assert {'data_workflow_guide','write_file','register_data_workflow','run_data_workflow',
            'inspect_source_tables','read_source_table','read_data_workflow_result'}<=names
    assert set(info['tool_catalog'])==ctx.allowed(value)
    assert 'delegate_agents' not in names and 'confirm_metric' not in names
    assert get('turn',value['id'])['task_contract']['budgets']['input_characters'] is None
    recovered=ctx.discover(value,ctx.DiscoverTools(query='build_transform',limit=1))
    assert recovered['tools']==[ctx.definition('build_transform')]


def test_requested_canonical_guide_is_not_split_into_repeated_string_pages():
    from agent_service.workflow_guidance import guide
    canonical=guide()
    assert 5000<len(json.dumps(canonical,ensure_ascii=False))<24000
    value=turn()
    value['agent_trace']=[{'id':'schema-guide','tool':'data_workflow_guide','status':'done',
                           'arguments':{},'observation':canonical}]
    receipt=ctx.trace_context(value,agent.public_observation)[0]
    assert receipt['observation']==canonical
    assert receipt['observation']['schema']['$defs']['TableSchema']['additionalProperties'] is False
    # An external tool cannot label its own invented row count authoritative.
    assert ctx.row_scope({'source_id':'s','total':999,'complete_source_read':True},'read_work_tool') is None


def test_source_total_is_distinct_from_profile_and_survives_row_truncation(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True,'engine':'atlas','tool_protocol':'json'})
    value=turn('先核对全部人员资料；统计范围尚未确认，请不要计算。')
    source='s_'+uid()
    put('source',{'project_id':value['project_id'],'name':'人员资料.csv','status':'ready'},source)
    steps=[{'id':'sample','tool':'source_schema','status':'done','arguments':{},
            'observation':{'source_id':source,'profile':{'sample_rows':500,'sample_only':True,'truncated':True}}},
           {'id':'full-read','tool':'read_source_table','status':'done','arguments':{},
            'observation':{'source_id':source,'total':2000,'complete_source_read':True,'sample_only':True,
                           'rows':[{'note':'x'*200} for _ in range(60)],'next_offset':60}}]
    patch_value=put('turn',dict(value,agent_trace=steps),value['id'])
    trace=ctx.trace_context(patch_value,agent.public_observation)
    assert trace[1]['observation']['not_full_result'] is True
    assert trace[0]['row_scope']['profile_sample_rows']==500
    assert trace[1]['row_scope']['source_total_rows']==2000
    assert trace[1]['row_scope']['preview_rows_returned']==60
    assert trace[1]['row_scope']['preview_contains_all_rows'] is False
    messages,_=agent.planning_context(patch_value,native=False)
    evidence=json.loads(messages[1]['content'])['source_context'][0]
    assert evidence['profile_sample_rows']==500 and evidence['source_total_rows']==2000
    assert '画像样本量，不能当总行数' in messages[0]['content']


@pytest.mark.parametrize('code',['HEADER_SELECTION_REQUIRED','SHEET_SELECTION_REQUIRED','WORKBOOK_MERGE_AMBIGUOUS'])
def test_failed_header_boundary_source_is_not_presented_as_empty_data(code):
    value=turn();source='s_'+uid()
    put('source',{'project_id':value['project_id'],'name':'复杂页签.xlsx','status':'failed',
                  'error_code':code},source)
    value.update(source_ids=[source])
    contexts=ctx.source_context(value,agent.public_observation)
    assert contexts==[{'source_id':source,'status':'failed','error_code':code,
                       'reading_boundary_required':True}]
    assert 'source_total_rows' not in contexts[0]


def test_financial_excel_export_starts_with_workflow_not_office_dto(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True,'engine':'atlas','tool_protocol':'json'})
    value=turn('按月份和办事处汇总收入、成本、预算，请生成完整结果并提供CSV和Excel下载，保留加工规则供核对。')
    messages,_=agent.planning_context(value,native=False)
    info=json.loads(messages[1]['content']);names={tool['name'] for tool in info['tools']}
    assert 'data_workflow_guide' in names and 'render_office_artifact' not in names
    assert 'office_spec_schema' not in info
    assert sum(len(message['content']) for message in messages)<21000
    ctx.discover(value,ctx.DiscoverTools(query='render_office_artifact',limit=1))
    messages,_=agent.planning_context(get('turn',value['id']),native=False)
    from agent_service import office
    assert json.loads(messages[1]['content'])['office_spec_schema']==office.Spec.model_json_schema()


def test_complete_structural_catalog_and_bounded_page_keep_original_recovery_identity(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    value=turn();original={'source_id':'s_layout','sheets':[{'name':str(i),'metadata':'x'*1000} for i in range(10)]}
    first={'id':'layout-original','tool':'inspect_source_tables','status':'done','arguments':{},'observation':original}
    value=put('turn',dict(value,agent_trace=[first]),value['id'])
    page=ctx.read_result(value,ctx.ToolResult(step_id='layout-original',offset=0,limit=10000))
    second={'id':'layout-page','tool':'read_tool_result','status':'done',
            'arguments':{'step_id':'layout-original','offset':0,'limit':10000},'observation':page}
    value=put('turn',dict(value,agent_trace=[first,second]),value['id'])
    receipts=ctx.trace_context(value,agent.public_observation)
    assert receipts[0]['observation']==original
    assert receipts[1]['observation']==page
    assert receipts[1]['original_result']['step_id']=='layout-original'
    assert receipts[1]['original_result']['next_offset']==page['next_offset']


def test_page_alias_reads_absolute_original_offsets_and_keeps_task_boundaries(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    value=turn();original={'rows':['x'*200]*60,'tail':'ORIGINAL-RESULT-TAIL'}
    steps=[{'id':'original','tool':'query_sql','status':'done','arguments':{},'observation':original},
           {'id':'first-page','tool':'read_tool_result','status':'done',
            'arguments':{'step_id':'original','offset':6000,'limit':6000},'observation':{}},
           {'id':'second-page','tool':'read_tool_result','status':'done',
            'arguments':{'step_id':'first-page','offset':0,'limit':6000},'observation':{}}]
    value=put('turn',dict(value,agent_trace=steps),value['id'])
    raw=json.dumps(original,ensure_ascii=False);offset=len(raw)-100
    page=ctx.read_result(value,ctx.ToolResult(step_id='second-page',offset=offset,limit=100))
    assert page['step_id']=='original' and page['content']==raw[offset:]
    assert page['resolved_page_steps']==['second-page','first-page']
    assert page['offset']==offset and page['next_offset'] is None
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':False})
    guarded_original=ctx.read_result(value,ctx.ToolResult(step_id='original',limit=10000))
    guarded_alias=ctx.read_result(value,ctx.ToolResult(step_id='second-page',limit=10000))
    assert guarded_original['content']==guarded_alias['content']
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    other=turn()
    with pytest.raises(ValueError,match='不能读取其他任务'):ctx.read_result(other,ctx.ToolResult(step_id='first-page'))
    cycle={'id':'cycle','tool':'read_tool_result','status':'done','arguments':{'step_id':'cycle'},'observation':{}}
    value=put('turn',dict(value,agent_trace=steps+[cycle]),value['id'])
    with pytest.raises(ValueError,match='循环'):ctx.read_result(value,ctx.ToolResult(step_id='cycle'))


def test_small_financial_tables_share_duplicate_schema_without_losing_values_or_coordinate_evidence():
    fields=[{'name':'c_'+str(i),'label':'字段'+str(i),'header_path':['上级','字段'+str(i)],
             'header_coordinates':[{'coordinate':chr(65+i)+'5','source_coordinate':chr(65+i)+'5','merged_range':None}],
             'number_formats':['0.00000000'],'unit':'CNY','unit_basis':'explicit','metadata':'x'*250} for i in range(1,8)]
    rows=[{'c_'+str(i):f'{row}.{i:012d}' for i in range(1,8)} for row in range(9)]
    original={'source_id':'s_facts','fields':fields,'rows':rows,'total':9,'offset':0,'next_offset':None,
        'complete_source_read':False,'available_rows_only':True,'evidence':{'fields':fields,'source_revision':4,
        'selection':{'sheet':'收入','header_rows':[5,6],'cell_range':'B5:I15'},'source_rows':list(range(7,16)),
        'special_cells':[{'coordinate':'E7','formula':'=B7-C7','cached_value':'12.125','number_format':'0.00000000'}],
        'warnings':[{'code':'PERCENTAGE_SEMANTICS_REQUIRED','coordinate':'I7'}],'executable_values_complete':False}}
    frozen=json.dumps(original,ensure_ascii=False)
    view=ctx.observation_view(original,'read_source_table')
    assert view['evidence']['fields_reference']=='#/fields'
    reconstructed=dict(view,evidence=dict(view['evidence']));reconstructed['evidence'].pop('fields_reference')
    reconstructed['evidence']['fields']=view['fields']
    assert reconstructed==original and json.dumps(original,ensure_ascii=False)==frozen
    value=turn();value['agent_trace']=[{'id':'table-'+str(i),'tool':'read_source_table','status':'done',
        'arguments':{'source_id':'s_facts'},'observation':original} for i in range(4)]
    receipts=ctx.trace_context(value,lambda v:v)
    for receipt in receipts:
        assert receipt['observation']==view
        assert receipt['observation']['rows']==rows
        assert receipt['observation']['evidence']['warnings']==original['evidence']['warnings']


def test_exact_discovery_returns_only_explicit_authorized_names_and_partial_search_remains_broad(monkeypatch):
    value=turn('对齐字段')
    exact=ctx.discover(value,ctx.DiscoverTools(query='请先 propose_value_mapping，再 inspect_semantic_proposal',limit=6))
    assert [tool['name'] for tool in exact['tools']]==['propose_value_mapping','inspect_semantic_proposal']
    assert exact['tools'][0]==ctx.definition('propose_value_mapping')
    one=ctx.discover(value,ctx.DiscoverTools(query='propose_value_mapping',limit=6))
    assert len(one['tools'])==1
    monkeypatch.setattr('agent_service.agent_gateway.allowed_tools',lambda turn,names:names-{'propose_value_mapping'})
    filtered=ctx.discover(value,ctx.DiscoverTools(query='propose_value_mapping inspect_semantic_proposal',limit=6))
    assert [tool['name'] for tool in filtered['tools']]==['inspect_semantic_proposal']
    partial=ctx.discover(value,ctx.DiscoverTools(query='source',limit=6))
    assert len(partial['tools'])>1
    assert all(tool['name']!='propose_value_mapping' for tool in partial['tools'])
