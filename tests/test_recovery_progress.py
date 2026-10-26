"""Recovery ends when it stops revealing evidence, never at a task quota."""
import copy
import hashlib
import json

import pytest

from agent_service import agent,agent_progress as progress,opencode_runtime as runtime,workspace
from agent_service.store import get,patch,put,delete,uid
from test_opencode_agent_bridge import turn
from test_opencode_inflight_stop import native_session


def page(offset=0,size=100,total=1000,version='source-v1',resource='turn:history'):
    content='x'*max(0,min(size,total-offset))
    return {'content':content,'recovery':{'kind':'context_original','resource_id':resource,
        'version_sha256':hashlib.sha256(version.encode()).hexdigest(),'offset_unit':'characters','offset':offset,
        'returned_characters':len(content),'total_characters':total,
        'next_offset':offset+len(content) if offset+len(content)<total else None}}


def receipt(value,*,tool='read_context_original',index=0):
    return {'id':'page_'+str(index),'tool':tool,'arguments':{'turn_id':'history','offset':index},
            'status':'done','observation':value}


def request(offset=0,*,tool='read_context_original',identity='history'):
    return {'kind':'tool','tool':tool,'arguments':
            ({'step_id':identity,'offset':offset,'limit':100} if tool=='read_tool_result'
             else {'turn_id':identity,'offset':offset,'limit':100})}


def test_more_than_seventy_new_ranges_and_versions_are_not_quota_limited():
    turn={'agent_trace':[]}
    for index in range(75):
        value=page(index*100,total=7500)
        assert progress.recovery_check(turn,value=value) is None
        turn['agent_trace'].append(receipt(value,index=index))
    assert progress.recovery_check(turn,value=page(0,total=7500))['code']==progress.NO_PROGRESS
    assert progress.recovery_check(turn,value=page(0,total=7500,version='privacy-filtered-v2')) is None
    assert progress.recovery_check(turn,value=page(0,total=7500,resource='turn:new_history')) is None


def test_page_aliases_and_limit_jitter_share_coverage_but_partial_new_range_is_allowed():
    turn={'agent_trace':[receipt(page(0,size=300),index=0)]}
    alias=page(50,size=150)
    blocked=progress.recovery_check(turn,value=alias)
    assert blocked['code']==progress.NO_PROGRESS and not blocked['recovery_guard']['pause']
    turn['agent_trace'].append(receipt(blocked,tool='read_tool_result',index=1))
    assert progress.recovery_check(turn,value=page(0,size=200))['recovery_guard']['pause']
    assert progress.recovery_check(turn,value=page(250,size=200)) is None
    assert progress.recovery_check(turn,value=page(2000,size=100))['code']==progress.NO_PROGRESS


def test_invalid_id_jitter_does_not_credit_progress_and_new_valid_reference_is_always_allowed():
    first=progress.recovery_check({'agent_trace':[]},invalid_reference=True)
    trace=[receipt(first,tool='read_tool_result')]
    assert progress.recovery_check({'agent_trace':trace},invalid_reference=True)['recovery_guard']['pause']
    assert progress.recovery_check({'agent_trace':trace},value=page()) is None
    trace.append(receipt(page()))
    assert not progress.recovery_check({'agent_trace':trace},invalid_reference=True)['recovery_guard']['pause']


def test_user_continue_and_business_progress_start_new_recovery_episode():
    trace=[receipt(page()),receipt(progress.recovery_check({'agent_trace':[receipt(page())]},value=page()))]
    assert progress.recovery_check({'agent_trace':trace},value=page())['recovery_guard']['pause']
    assert progress.recovery_check({'agent_trace':trace,'agent_budget_base':len(trace)},value=page()) is None
    trace.append({'tool':'write_file','status':'done','arguments':{},'observation':{'path':'repaired.json'}})
    assert progress.recovery_check({'agent_trace':trace},value=page()) is None


def test_empty_original_is_delivered_once_and_untrusted_metadata_does_not_invent_identity():
    empty=page(total=0)
    assert progress.recovery_check({'agent_trace':[]},value=empty) is None
    assert progress.recovery_check({'agent_trace':[receipt(empty)]},value=empty)['code']==progress.NO_PROGRESS
    malformed=copy.deepcopy(empty);malformed['recovery']['resource_id']='https://secret.invalid/key'
    assert progress.recovery_check({'agent_trace':[]},value=malformed) is None


class InvalidReference(ValueError):code=progress.REFERENCE_INVALID


def test_native_call_block_receipt_is_truthful_durable_and_replayed_once(turn,monkeypatch):
    calls=[]
    def recover(value,step):
        calls.append(step.arguments)
        if step.tool=='read_tool_result':raise InvalidReference('private rejected target must not be echoed')
        return page()
    monkeypatch.setattr(agent,'execute',recover)
    first=agent.opencode_step(turn['id'],request(tool='read_tool_result',identity='stale_1'),'ses:m1:c1')
    observation=first['observation']['observation']
    assert first['executed'] is False and observation['code']==progress.REFERENCE_INVALID
    assert first['observation']['tool']=='read_tool_result'
    replay=agent.opencode_step(turn['id'],request(tool='read_tool_result',identity='changed'),'ses:m1:c1')
    assert replay['replayed'] and replay['executed'] is False and len(calls)==1
    assert replay['observation']==observation
    # A correct new reference is resolved before the old invalid warning.
    valid=agent.opencode_step(turn['id'],request(),'ses:m2:c2')
    assert valid['observation']['status']=='done' and get('turn',turn['id'])['state']=='agent'
    warned=agent.opencode_step(turn['id'],request(),'ses:m3:c3')
    assert warned['executed'] is False and warned['observation']['observation']['code']==progress.NO_PROGRESS
    stopped=agent.opencode_step(turn['id'],request(tool='read_tool_result',identity='stale_2'),'ses:m4:c4')
    assert stopped['executed'] is False and get('turn',turn['id'])['outcome']=='paused'
    terminal_replay=agent.opencode_step(turn['id'],request(),'ses:m4:c4')
    assert terminal_replay['replayed'] and terminal_replay['terminal'] and terminal_replay['executed'] is False
    assert len(calls)==4 and len(get('turn',turn['id'])['agent_trace'])==4
    assert 'private rejected' not in str(get('turn',turn['id'])['agent_trace'])


def test_real_bridge_stops_canonical_alias_recovery_loop_without_145_calls(turn,monkeypatch):
    workspace.write_file(turn['project_id'],'reports/saved.md','evidence retained')
    reads=[]
    def recover(value,step):
        reads.append(step.arguments)
        return page(resource='turn:original_history')
    monkeypatch.setattr(agent,'execute',recover)
    def during_context(bridge,state):
        for index in range(145):
            admission=bridge({'op':'model_request','kind':'generation','input_characters':100,
                'max_output_tokens':256,'target_url':'https://fixture.test/v1/chat/completions','model_id':'fixture'})
            assert admission['ok']
            reply=bridge({'op':'step','message_id':'msg_'+str(index),'call_id':'call_'+str(index),
                'step':request(tool='read_tool_result',identity='different_wrapper_'+str(index))})
            if reply['terminal']:break
        assert index==2 and reply['executed'] is False
    state=native_session(monkeypatch,during_context)
    result=runtime.run_session(turn,'Synthetic no-progress recovery regression',{},
        lambda step,call:agent.opencode_step(turn['id'],step,call))
    current=get('turn',turn['id'])
    assert result['state']=='done' and result['model_requests']==3 and current['outcome']=='paused'
    assert len(reads)==3 and len(current['agent_trace'])==3
    assert workspace.read_file(turn['project_id'],'reports/saved.md')=='evidence retained'
    assert not state['model_calls'] and state['interrupts']==1


def test_seventy_actual_new_pages_then_write_remain_allowed(turn,monkeypatch):
    original_execute=agent.execute;reads=[]
    def recover(value,step):
        if step.tool!='read_context_original':return original_execute(value,step)
        reads.append(step.arguments['offset'])
        return page(step.arguments['offset'],total=7000)
    monkeypatch.setattr(agent,'execute',recover)
    for index in range(70):
        reply=agent.opencode_step(turn['id'],request(index*100),'ses:page:'+str(index))
        assert reply['observation']['status']=='done' and not reply.get('executed') is False
    agent.opencode_step(turn['id'],{'kind':'tool','tool':'write_file',
        'arguments':{'path':'reports/native.md','content':'real progress after seventy pages'}},'ses:write')
    assert len(reads)==70 and get('turn',turn['id'])['state']=='agent'
    assert workspace.read_file(turn['project_id'],'reports/native.md')=='real progress after seventy pages'


def test_cancel_before_or_during_preflight_never_restores_running_state(turn,monkeypatch):
    def cancel(value,step):
        patch('turn',turn['id'],state='done',outcome='cancelled')
        return page()
    monkeypatch.setattr(agent,'execute',cancel)
    reply=agent.opencode_step(turn['id'],request(),'ses:cancel')
    assert reply['observation'] is None and get('turn',turn['id'])['outcome']=='cancelled'
    assert not get('turn',turn['id']).get('agent_trace')
    monkeypatch.setattr(agent,'execute',lambda *args:pytest.fail('Cancelled tasks must not recover'))
    assert agent.opencode_step(turn['id'],request(),'ses:late')['terminal']


def test_live_new_version_bypasses_legacy_consecutive_equal_arguments(turn,monkeypatch):
    old=page();trace=[receipt(old,index=0),receipt(old,index=1)]
    for item in trace:item['arguments']=request()['arguments']
    patch('turn',turn['id'],agent_trace=trace)
    monkeypatch.setattr(agent,'execute',lambda value,step:page(version='new approved version'))
    reply=agent.opencode_step(turn['id'],request(),'ses:new_version')
    assert reply['observation']['status']=='done' and not reply.get('executed') is False


def test_replay_refreshes_revoked_memory_without_new_trace_or_cached_text(turn):
    from agent_service import memory_service as memory
    historical='t_'+uid();marker='UNIQUE-REVOKED-PREFERENCE-EXAMPLE'
    put('turn',{'project_id':turn['project_id'],'session_id':turn.get('session_id'),
        'actor_id':'owner','created_at':'2000-01-01T00:00:00+00:00','state':'done','text':marker,
        'answer':'saved','outcome':'answered'},historical)
    record=None
    try:
        record=memory.save(turn['project_id'],{'title':'Replay revocation fixture','content':marker},source_turn_id=historical)
        args=request(identity=historical);args['arguments']['limit']=10000
        first=agent.opencode_step(turn['id'],args,'ses:private_page')
        assert marker in str(first['observation'])
        original_trace=copy.deepcopy(get('turn',turn['id'])['agent_trace'])
        memory.forget(turn['project_id'],record['id'],record['version'])
        replay=agent.opencode_step(turn['id'],args,'ses:private_page')
        assert replay['replayed'] and marker not in str(replay)
        assert get('turn',turn['id'])['agent_trace']==original_trace
    finally:
        delete('turn',historical)
        if record:delete('user_memory',record['id'])


def test_replay_checks_current_content_grant_without_leaking_cached_page(turn,monkeypatch):
    from agent_service import provider
    source='original_step';marker='PRIVATE-ORIGINAL-ROW'
    patch('turn',turn['id'],agent_trace=[{'id':source,'tool':'read_file','arguments':{},'status':'done',
        'observation':{'content':marker}}])
    args=request(tool='read_tool_result',identity=source);args['arguments']['limit']=10000
    first=agent.opencode_step(turn['id'],args,'ses:source_page')
    assert marker in str(first['observation'])
    count=len(get('turn',turn['id'])['agent_trace'])
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':False})
    replay=agent.opencode_step(turn['id'],args,'ses:source_page')
    assert replay['replayed'] and marker not in str(replay)
    assert len(get('turn',turn['id'])['agent_trace'])==count


def test_unexecuted_recovery_warning_does_not_mark_completed_business_work_partial(turn,monkeypatch):
    original_execute=agent.execute
    monkeypatch.setattr(agent,'execute',lambda value,step:page() if step.tool=='read_context_original' else original_execute(value,step))
    agent.opencode_step(turn['id'],request(),'ses:initial')
    warned=agent.opencode_step(turn['id'],request(),'ses:redundant')
    assert warned['executed'] is False
    agent.opencode_step(turn['id'],{'kind':'tool','tool':'write_file',
        'arguments':{'path':'reports/native.md','content':'verified actual report'}},'ses:report')
    agent.opencode_step(turn['id'],{'kind':'final','status':'complete','answer':'报告已保存'},'ses:final')
    assert get('turn',turn['id'])['outcome']=='verified_complete'


def test_interrupted_recovery_refreshes_once_and_keeps_original_call_receipt(turn,monkeypatch):
    calls=[]
    monkeypatch.setattr(agent,'execute',lambda value,step:calls.append(True) or page())
    patch('turn',turn['id'],state='agent_tool',agent_trace=[{'id':'interrupted_page',
        'tool':'read_context_original','arguments':request()['arguments'],'status':'started',
        'opencode_call_id':'ses:interrupted'}])
    reply=agent.opencode_step(turn['id'],request(),'ses:interrupted')
    assert reply['replayed'] and reply['tool_status']=='done' and len(calls)==1
    assert len(get('turn',turn['id'])['agent_trace'])==1


def test_native_compaction_epoch_allows_old_range_once_and_ignores_admission_counter():
    value=page();value['recovery_epoch']={'session_id':'ses_original','compaction':0}
    trace=[receipt(value)]
    turn={'opencode_session_id':'ses_original','agent_trace':trace}
    assert progress.recovery_check(turn,value=page())['code']==progress.NO_PROGRESS
    admitted=dict(turn,model_usage={'opencode_compaction_calls':100})
    assert progress.recovery_check(admitted,value=page())['code']==progress.NO_PROGRESS
    compacted=dict(turn,opencode_context_epoch=1)
    assert progress.recovery_check(compacted,value=page()) is None
    restored=page();restored['recovery_epoch']=progress.recovery_epoch(compacted)
    compacted['agent_trace']=trace+[receipt(restored,index=1)]
    assert progress.recovery_check(compacted,value=page())['code']==progress.NO_PROGRESS
    assert progress.recovery_check(dict(compacted,opencode_session_id='ses_rebuilt'),value=page()) is None


def test_actual_source_three_pages_stay_in_projection_then_can_restore_after_compaction(turn):
    source='original_large_result'
    marker='第一页已核对的业务证据；'
    original={'content':marker+'甲'*6990+'第二页证据；'+'乙'*6990+'第三页证据；'+'丙'*8000}
    patch('turn',turn['id'],agent_trace=[{'id':source,'tool':'read_file','arguments':{},
        'status':'done','observation':original}],opencode_session_id='ses_pages')
    pages=[]
    for index,offset in enumerate((0,7000,14000)):
        args=request(offset,tool='read_tool_result',identity=source);args['arguments']['limit']=7000
        reply=agent.opencode_step(turn['id'],args,'ses:distinct:'+str(index))
        assert reply['observation']['status']=='done'
        pages.append(get('turn',turn['id'])['agent_trace'][-1]['observation']['content'])
    current=get('turn',turn['id'])
    messages,_=agent.planning_context(current,native=False)
    projected=json.loads(messages[1]['content'])['completed_steps']
    contents=[item['observation'].get('content') for item in projected if isinstance(item.get('observation'),dict)]
    assert all(value in contents for value in pages),'Distinct live pages must remain visible before compaction'
    back=request(0,tool='read_tool_result',identity=source);back['arguments']['limit']=7000
    blocked=agent.opencode_step(turn['id'],back,'ses:back_before_compaction')
    assert blocked['executed'] is False
    warned=get('turn',turn['id']);guard_id=warned['agent_trace'][-1]['id']
    messages,_=agent.planning_context(warned,native=False)
    after_warning=json.loads(messages[1]['content'])['completed_steps']
    guarded=next(item for item in after_warning if item['step_id']==guard_id)
    assert guarded['observation']['executed'] is False,'Planning must not turn a denied recovery into an executed page'
    assert any(isinstance(item.get('observation'),dict) and item['observation'].get('content')==pages[0]
               for item in after_warning),'A denied duplicate must not replace the original visible page'
    patch('turn',turn['id'],opencode_context_epoch=1)
    restored=agent.opencode_step(turn['id'],back,'ses:back_after_compaction')
    assert restored['observation']['status']=='done'
    assert get('turn',turn['id'])['agent_trace'][-1]['observation']['content']==pages[0]
    again=agent.opencode_step(turn['id'],back,'ses:back_again_same_epoch')
    assert again['executed'] is False
    assert get('turn',turn['id'])['state']=='agent'


@pytest.mark.parametrize('status,observation',[
    ('failed',{'error':'unchanged missing file'}),
    ('started',None),
    ('done',{'executed':False,'code':'TOOL_NO_PROGRESS'}),
])
def test_failed_started_or_unexecuted_business_step_does_not_reset_recovery_coverage(status,observation):
    original=receipt(page())
    warning=progress.recovery_check({'agent_trace':[original]},value=page())
    interrupted={'tool':'read_file','arguments':{'path':'notes/missing.txt'},'status':status,
                 'observation':observation}
    trace=[original,receipt(warning,index=1),interrupted]
    repeated=progress.recovery_check({'agent_trace':trace},value=page())
    assert repeated is not None and repeated['recovery_guard']['pause']
    invalid=progress.recovery_check({'agent_trace':[receipt(warning),interrupted]},invalid_reference=True)
    assert invalid['recovery_guard']['pause']


def test_real_bridge_failed_file_read_cannot_unlock_a_repeated_recovery_page(turn,monkeypatch):
    original_execute=agent.execute
    def recover(value,step):
        return page() if step.tool in progress.RECOVERY_TOOLS else original_execute(value,step)
    monkeypatch.setattr(agent,'execute',recover)
    def during_context(bridge,state):
        for index in range(145):
            action=({'kind':'tool','tool':'read_file','arguments':{'path':'notes/nonexistent.txt'}}
                    if index>=2 and index%2==0 else request(tool='read_tool_result',identity='wrapper_'+str(index)))
            admission=bridge({'op':'model_request','kind':'generation','input_characters':100,
                'max_output_tokens':256,'target_url':'https://fixture.test/v1/chat/completions','model_id':'fixture'})
            assert admission['ok']
            reply=bridge({'op':'step','message_id':'msg_'+str(index),'call_id':'call_'+str(index),'step':action})
            if reply['terminal']:break
        assert index==3 and reply['executed'] is False
    state=native_session(monkeypatch,during_context)
    result=runtime.run_session(turn,'Synthetic failed-operation recovery bypass regression',{},
        lambda step,call:agent.opencode_step(turn['id'],step,call))
    current=get('turn',turn['id'])
    assert result['state']=='done' and result['model_requests']==4 and current['outcome']=='paused'
    assert [item['status'] for item in current['agent_trace']]==['done','failed','failed','failed']
    assert current['agent_trace'][2]['tool']=='read_file'
    assert not state['model_calls'] and state['interrupts']==1
