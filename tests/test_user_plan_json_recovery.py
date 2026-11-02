"""Regression for a tool call emitted as visible model text, not a real tool call."""
import json
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from agent_service import agent, opencode_runtime as runtime
from agent_service.store import get, patch
from test_opencode_agent_bridge import turn
from test_model_plan_recovery import model, choice, call
from test_opencode_idle_recovery import session

STEPS = [
    '探查并读取所有数据源（CSV交易/客户/产品、Excel预算/门店、Word回顾、PDF协议、JSON营销）的schema和预览',
    '构建数据工作流：将8个源数据加工为11个分析需求的完整输出表',
    '运行数据工作流，获取完整结果',
    '根据结果生成Excel工作簿（需求1-5，每需求一个工作表）',
    '根据结果生成Word文档（需求6-10，含表格和文字说明）',
    '根据结果生成PPT演示文稿（需求11，关键发现与建议，8-12页）',
    '提供200字季度经营总结并交付最终结果',
]
PLAN = {'kind': 'tool', 'tool': 'set_task_plan', 'arguments': {'steps': STEPS}}
PLAN_TEXT = json.dumps(PLAN, ensure_ascii=False)


def _assert_plan_then_context(turn):
    saved = get('turn', turn['id'])
    assert saved['state'] == 'agent' and saved.get('answer') != PLAN_TEXT
    assert saved['task_plan']['steps'] == STEPS
    assert [entry['tool'] for entry in saved['agent_trace']] == ['set_task_plan']


def test_native_missing_tool_call_recovers_exact_user_plan_and_continues(turn, model):
    _, requests, responses = model
    responses.extend([
        choice(PLAN_TEXT),
        choice(calls=[call('project_context', {})]),
    ])
    agent.advance(get('turn', turn['id']))
    _assert_plan_then_context(turn)
    agent.advance(get('turn', turn['id']))
    saved = get('turn', turn['id'])
    assert [entry['tool'] for entry in saved['agent_trace']] == ['set_task_plan', 'project_context']
    assert saved['agent_trace'][-1]['status'] == 'done' and saved['state'] == 'agent'
    assert len(requests) == 2


def test_native_finish_task_with_nested_user_plan_continues(turn, model):
    _, requests, responses = model
    responses.extend([
        choice(calls=[call('finish_task', {'answer': PLAN_TEXT, 'status': 'blocked'})]),
        choice(calls=[call('project_context', {})]),
    ])
    agent.advance(get('turn', turn['id']))
    _assert_plan_then_context(turn)
    agent.advance(get('turn', turn['id']))
    assert get('turn', turn['id'])['agent_trace'][-1]['tool'] == 'project_context'
    assert len(requests) == 2


def test_opencode_bridge_nested_plan_replays_once_and_executes_next_tool(turn):
    result = agent.opencode_step(turn['id'], {'kind': 'final', 'answer': PLAN_TEXT, 'status': 'blocked'}, 'session:plan')
    _assert_plan_then_context(turn)
    replay = agent.opencode_step(turn['id'], {'kind': 'final', 'answer': PLAN_TEXT, 'status': 'blocked'}, 'session:plan')
    assert replay.get('replayed') is True
    assert len(get('turn', turn['id'])['agent_trace']) == 1
    agent.opencode_step(turn['id'], {'kind': 'tool', 'tool': 'project_context', 'arguments': {}}, 'session:context')
    assert [entry['tool'] for entry in get('turn', turn['id'])['agent_trace']] == ['set_task_plan', 'project_context']
    assert result.get('terminal') is not True


def test_opencode_idle_text_plan_then_runs_next_tool(turn, monkeypatch):
    contexts = [
        [{'id':'msg_prompt_1', 'type':'user'},
         {'id':'msg_plan', 'type':'assistant', 'finish':'stop', 'content':[{'type':'text','text':PLAN_TEXT}]},
         {'id':'msg_idle_1', 'type':'idle', 'outcome':'succeeded'}],
        [{'id':'msg_prompt_1', 'type':'user'}, {'id':'msg_idle_1', 'type':'idle', 'outcome':'succeeded'},
         {'id':'msg_prompt_2', 'type':'user'}, {'id':'msg_idle_2', 'type':'idle', 'outcome':'succeeded'}],
    ]
    def on_context(metadata, number):
        if number == 2:
            bridge=metadata['nexus_bridge']
            for call_id,step in [('context',{'kind':'tool','tool':'project_context','arguments':{}}),
                                 ('finish',{'kind':'final','answer':'请确认后续资料。','status':'needs_input'})]:
                data=json.dumps({'session_id':'ses_idle','op':'step','message_id':'msg_work','call_id':call_id,'step':step}).encode()
                request=Request(bridge['url'],data=data,headers={'Content-Type':'application/json','Authorization':'Bearer '+bridge['token']},method='POST')
                with build_opener(ProxyHandler({})).open(request,timeout=10) as response:
                    assert not json.loads(response.read()).get('error')
    requests, _ = session(monkeypatch, contexts, on_context=on_context)
    runtime.run_session(get('turn', turn['id']), 'work', {}, lambda step, call_id: agent.opencode_step(turn['id'], step, call_id))
    saved = get('turn', turn['id'])
    assert saved['task_plan']['steps'] == STEPS
    assert [entry['tool'] for entry in saved['agent_trace']] == ['set_task_plan', 'project_context']
    assert saved['answer'] == '请确认后续资料。' and len([r for r in requests if r[1].endswith('/prompt')]) == 2


@pytest.mark.parametrize('payload', [
    {'kind':'tool','tool':'write_file','arguments':{'path':'reports/untrusted.md','content':'must not run'}},
    {'kind':'tool','tool':'set_task_plan','arguments':{'steps':['bad'], 'unexpected':'must not run'}},
])
def test_nested_non_plan_or_invalid_tool_not_executed(turn, payload):
    agent.opencode_step(turn['id'], {'kind':'final','answer':json.dumps(payload), 'status':'blocked'}, 'session:bad')
    saved = get('turn', turn['id'])
    assert not saved.get('task_plan') and not saved.get('agent_trace')
    from agent_service import workspace
    assert not workspace.path_for(turn['project_id'],'reports/untrusted.md').exists()


def test_markdown_fenced_plan_is_processed_not_displayed(turn, model):
    _, _, responses = model
    responses.extend([choice('```json\n'+PLAN_TEXT+'\n```'), choice(calls=[call('project_context', {})])])
    agent.advance(get('turn', turn['id']))
    _assert_plan_then_context(turn)
    agent.advance(get('turn', turn['id']))
    assert get('turn', turn['id'])['agent_trace'][-1]['tool'] == 'project_context'


def test_chat_explicitly_requests_json_display_not_execution(turn, model):
    _, _, responses = model
    current = patch('turn', turn['id'], interaction_mode='chat',
                    text='请原样展示这段 JSON 的格式，不要执行任何操作。', task_contract=None)
    responses.append(choice(calls=[call('finish_task', {'answer':PLAN_TEXT, 'status':'complete'})]))
    agent.advance(current)
    saved = get('turn', turn['id'])
    assert saved['answer'] == PLAN_TEXT and not saved.get('task_plan') and not saved.get('agent_trace')


def test_nested_unknown_tool_is_not_executed(turn):
    bad = {'kind':'tool', 'tool':'invented_unloaded_tool', 'arguments':{'private':'not-a-command'}}
    agent.opencode_step(turn['id'], {'kind':'final','answer':json.dumps(bad),'status':'blocked'}, 'session:unknown')
    assert not get('turn', turn['id']).get('agent_trace')


def test_compatible_envelope_cannot_bypass_unloaded_tool(turn, monkeypatch):
    monkeypatch.setattr(agent.agent_context, 'selected', lambda turn: ['project_context'])
    assert agent._recover_plan_envelope(turn, PLAN_TEXT, selected_only=True) is None
    result = agent.opencode_step(turn['id'], {'kind':'final','answer':PLAN_TEXT,'status':'blocked'}, 'session:not-loaded')
    assert result.get('observation',{}).get('error') and not get('turn',turn['id']).get('agent_trace')


def test_compatible_envelope_cannot_bypass_denied_tool(turn, monkeypatch):
    monkeypatch.setattr(agent.agent_context, 'allowed', lambda turn: {'project_context'})
    result = agent.opencode_step(turn['id'], {'kind':'final','answer':PLAN_TEXT,'status':'blocked'}, 'session:not-authorized')
    assert result.get('observation',{}).get('error') and not get('turn',turn['id']).get('agent_trace')


@pytest.mark.parametrize('raw', [
    '前面的用户资料：'+PLAN_TEXT,
    PLAN_TEXT+'\n其他内容',
    PLAN_TEXT.replace('"kind": "tool"', '"kind": "tool", "kind": "tool"'),
    '```json\n'+PLAN_TEXT+'\n```\n额外内容',
])
def test_never_execute_embedded_partial_or_ambiguous_json(turn, raw):
    assert agent._recover_plan_envelope(turn, raw) is None
    assert not get('turn',turn['id']).get('agent_trace')


def test_opencode_idle_does_not_replay_prior_side_effect(turn, monkeypatch):
    patch('turn',turn['id'], agent_trace=[{'id':'prior','tool':'write_file','status':'done',
        'arguments':{'path':'reports/native.md','content':'already done'},'observation':{'path':'reports/native.md'}}])
    requests,_=session(monkeypatch, [[{'id':'msg_prompt_1','type':'user'},
        {'id':'msg_plan','type':'assistant','finish':'stop','content':[{'type':'text','text':PLAN_TEXT}]},
        {'id':'msg_idle','type':'idle','outcome':'succeeded'}]])
    from agent_service import provider
    with pytest.raises(provider.ModelOutputError):
        runtime.run_session(get('turn',turn['id']),'work',{},lambda *_:pytest.fail('must not repeat effects'))
    saved=get('turn',turn['id'])
    assert len(saved['agent_trace']) == 1 and not saved.get('task_plan')
    assert len([r for r in requests if r[1].endswith('/prompt')]) == 1


def test_streaming_sse_plan_text_still_records_and_continues(turn, model):
    _,requests,responses = model
    pieces=[PLAN_TEXT[:len(PLAN_TEXT)//2],PLAN_TEXT[len(PLAN_TEXT)//2:]]
    events=[{'choices':[{'index':0,'delta':{'content':piece},'finish_reason':None}]} for piece in pieces]
    events.append({'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})
    sse=''.join('data: '+json.dumps(event,ensure_ascii=False)+'\n\n' for event in events)+'data: [DONE]\n\n'
    responses.extend([sse.encode(),choice(calls=[call('project_context',{})])])
    agent.advance(get('turn',turn['id']))
    _assert_plan_then_context(turn)
    agent.advance(get('turn',turn['id']))
    assert get('turn',turn['id'])['agent_trace'][-1]['tool']=='project_context' and len(requests)==2


def test_existing_plan_is_not_replaced_by_textual_envelope(turn):
    existing={'steps':['确认口径','核对实际文件'], 'missing_information':[], 'completed_steps':[0], 'current_step':1}
    patch('turn',turn['id'],task_plan=existing)
    result=agent.opencode_step(turn['id'],{'kind':'final','answer':PLAN_TEXT,'status':'blocked'},'session:stale-plan')
    saved=get('turn',turn['id'])
    assert saved['task_plan']==existing and not saved.get('agent_trace')
    assert saved['state']=='agent' and result.get('observation',{}).get('error')


def test_textual_plan_cannot_fabricate_completed_steps_after_a_real_read(turn):
    agent.opencode_step(turn['id'],{'kind':'tool','tool':'project_context','arguments':{}},'session:read')
    fake={'kind':'tool','tool':'set_task_plan','arguments':{'steps':STEPS,'completed_steps':[0,1]}}
    result=agent.opencode_step(turn['id'],{'kind':'final','answer':json.dumps(fake,ensure_ascii=False),'status':'blocked'},'session:fake-done')
    saved=get('turn',turn['id'])
    assert saved['state']=='agent' and not saved.get('task_plan')
    assert [entry['tool'] for entry in saved['agent_trace']]==['project_context']
    assert result.get('observation',{}).get('error')


def test_json_source_content_is_not_a_request_to_show_json(turn):
    current=patch('turn',turn['id'],text='探查交易与 JSON 营销内容，生成 Word、Excel 和 PPT 经营分析。')
    assert agent._recover_plan_envelope(current, PLAN_TEXT) is not None
