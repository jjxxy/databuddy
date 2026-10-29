"""Inspect actual planner/native bridge payloads, not a UI loading label."""
import copy,json

import pytest
from agent_service import agent,agent_context as ctx,provider,opencode_runtime as runtime,office
from agent_service import capability_guidance as guidance
from agent_service.store import get,patch
from test_opencode_agent_bridge import turn
from test_bounded_planning_context import turn as make_turn


def tools(info):return {item['name'] for item in info['tools']}


def test_plain_conversation_loads_only_core_even_when_all_services_are_enabled(turn,monkeypatch):
    monkeypatch.setattr('agent_service.work_runtime.status',lambda:{'configured':True})
    monkeypatch.setattr('agent_service.work_runtime.vision_status',lambda:{'enabled':True})
    monkeypatch.setattr('agent_service.web_research.status',lambda:{'enabled':True,'search_configured':True})
    monkeypatch.setattr('agent_service.browser_tools.status',lambda:{'configured':True})
    monkeypatch.setattr('agent_service.media_tools.status',lambda:{'images':True,'speech':True,'realtime':True})
    monkeypatch.setattr('agent_service.company_search.candidates_for_model',lambda *_:pytest.fail('Unrelated company search must not load'))
    current=patch('turn',turn['id'],text='你好，帮我想一句欢迎语')
    messages,_=agent.planning_context(current,native=False);info=json.loads(messages[1]['content'])
    assert tools(info)==ctx.ESSENTIAL
    assert info['loaded_capabilities']==[]
    assert len(messages[0]['content'])<2000
    for field in ('office_spec_schema','work_environment','company_search','company_candidates','agent_collaboration','specialist_assignment'):
        assert field not in info
    for method in ('WorkflowDocument','run_work_command.artifact_ids','request_browser_takeover','/manifest.json','导出或报表接口'):
        assert method not in messages[0]['content']
    assert set(info['tool_catalog'])==ctx.allowed(current),'catalog is only authorized names, not all parameter schemas'


def test_discovery_replaces_old_frontier_without_losing_original_tools_or_permissions(turn):
    current=patch('turn',turn['id'],text='生成Excel、Word和PPT，先核对来源再汇总。')
    initial,_=agent.planning_context(current,native=False);initial=json.loads(initial[1]['content'])
    assert 'office_spec_schema' in initial
    ctx.discover(current,ctx.DiscoverTools(query='build_transform',limit=1))
    active=get('turn',turn['id']);payload,_=agent.planning_context(active,native=False);info=json.loads(payload[1]['content'])
    assert tools(info)==ctx.ESSENTIAL|{'build_transform'}
    assert 'office_spec_schema' not in info and 'office' not in info['loaded_capabilities']
    ctx.discover(active,ctx.DiscoverTools(query='read_file',limit=1))
    newest=get('turn',turn['id']);messages,_=agent.planning_context(newest,native=False);focused=json.loads(messages[1]['content'])
    assert tools(focused)==ctx.ESSENTIAL|{'read_file'}
    assert 'build_transform' in ctx.allowed(newest) and 'build_transform' not in ctx.selected(newest)
    assert {'build_transform','read_file'}<=set(newest['discovered_tools']),'discovery audit is retained separately from loaded frontier'


def test_native_late_office_discovery_supplies_real_spec_and_only_changed_context(turn):
    current=patch('turn',turn['id'],text='先规划一份项目进展小结，按需要选择交付格式。')
    messages,_=agent.planning_context(current,native=False);initial=json.loads(messages[1]['content'])
    assert 'office_spec_schema' not in initial
    known={tool['name']:runtime.hashlib.sha256(json.dumps(tool,sort_keys=True,ensure_ascii=False).encode()).hexdigest() for tool in initial['tools']}
    known['__capability_context__']=initial['capability_context_version']
    reply=agent.opencode_step(turn['id'],{'kind':'tool','tool':'discover_tools','arguments':{'query':'render_office_artifact','limit':1}},'session:discover-office')
    outgoing=runtime._new_tool_definitions(reply,known)
    assert outgoing['capability_context']['office_spec_schema']==office.Spec.model_json_schema()
    assert 'office' in outgoing['capability_context']['guidance']
    assert not {'browser','media','collaboration','company','research'} & set(outgoing['capability_context']['guidance'])
    assert [tool['name'] for tool in outgoing['available_tools']]==['render_office_artifact']
    assert 'capability_context' not in runtime._new_tool_definitions(reply,known),'identical method instructions/spec must not be appended again'
    assert runtime._new_tool_definitions(reply,known)['available_tools']==[]


def test_focus_can_be_restored_from_saved_discovery_and_never_grants_chat_writes(turn):
    current=patch('turn',turn['id'],interaction_mode='chat',text='帮我理解这段话',discovered_tools=['render_office_artifact','build_transform'],
        agent_trace=[{'id':'saved','tool':'discover_tools','status':'done','arguments':{},'observation':{'tools':[{'name':'read_file'}]}}])
    selected=ctx.selected(current)
    assert 'read_file' in selected and not {'write_file','build_transform','render_office_artifact'}&set(selected)
    assert 'office_spec_schema' not in guidance.context_for(current,selected)


def test_discovery_focus_is_not_a_model_supplied_authorization_grant(turn,monkeypatch):
    current=patch('turn',turn['id'],tool_focus=['write_file','read_file'])
    monkeypatch.setattr('agent_service.agent_gateway.allowed_tools',lambda t,names:names-{'write_file'})
    assert 'write_file' not in ctx.selected(current)
    assert 'read_file' in ctx.selected(current)


def test_financial_accuracy_policy_survives_switch_to_office_authoring(turn):
    current=patch('turn',turn['id'],text='根据完整财务明细生成Word报告，不要将前25条当成全部数据。')
    ctx.discover(current,ctx.DiscoverTools(query='render_office_artifact',limit=1))
    current=get('turn',turn['id']);messages,_=agent.planning_context(current,native=False)
    info=json.loads(messages[1]['content'])
    assert 'finance' in info['loaded_capabilities']
    assert '完整输入' in messages[0]['content'] and '不能据此前25条' in messages[0]['content']
    assert 'office_spec_schema' in info


def test_existing_company_evidence_keeps_citation_policy_during_authoring(turn):
    current={**turn,'knowledge_read_refs':['already-read-reference']}
    context=guidance.context_for(current,{'read_file','render_office_artifact'})
    assert 'company' in context['guidance'] and 'citations' in context['guidance']['company']
    assert not {'browser','research','execution'} & set(context['guidance'])
