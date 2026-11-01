"""Repeated authorized inspections must not trap a task in an endless loop."""
import copy

from agent_service import agent, agent_context, agent_progress
import pytest
from agent_service.store import get, patch, put, delete, uid, now
from test_opencode_agent_bridge import turn


@pytest.fixture(autouse=True)
def sources(turn):
    for sid in ('A','B'):
        put('source',{'project_id':turn['project_id'],'kind':'file','revision':1,
            'revision_id':'v1-'+sid,'content_hash':'a'*64},sid)
    yield
    for sid in ('A','B'):delete('source',sid)


def record(turn, tool, args, value, *, error=False):
    current=get('turn',turn['id'])
    trace=copy.deepcopy(current.get('agent_trace',[]))
    before=agent_context.inspection_source_version(current,tool,args)
    trace.append({'id':uid(),'tool':tool,'arguments':args,'status':'started','started_at':now(),
                  'inspection_source_before':before})
    patch('turn',turn['id'],agent_trace=trace,state='agent_tool')
    agent.observe(turn,value,error=error)
    return get('turn',turn['id'])


def test_interleaved_live_inspections_guide_then_pause_without_replaying_effects(turn):
    project={'sources':['A','B']};a={'source_id':'A'};source={'fields':['amount'],'version':'v1'}
    record(turn,'project_context',{},project)
    record(turn,'source_schema',a,source)
    for i in range(4):
        current=record(turn,'project_context' if i%2==0 else 'source_schema',{} if i%2==0 else a,project if i%2==0 else source)
    assert current['state']=='agent'
    assert current['verification_feedback']['code']=='INSPECTION_NO_NEW_EVIDENCE'
    assert current['agent_trace'][-1]['status']=='done', 'the live read is not falsely labelled as unexecuted'
    for i in range(8):
        current=record(turn,'project_context' if i%2==0 else 'source_schema',{} if i%2==0 else a,project if i%2==0 else source)
    assert current['state']=='done' and current['outcome']=='paused' and current['resume_available']
    assert len(current['agent_trace'])==14


def test_new_versions_pages_new_tools_failures_and_compaction_are_not_repetition(turn):
    record(turn,'source_preview',{'source_id':'A'}, {'version':'v1','rows':[1]})
    record(turn,'project_context',{}, {'sources':['A']})
    record(turn,'source_preview',{'source_id':'A'}, {'version':'v1','rows':[1]})
    patch('source','A',revision=2,revision_id='v2-A',content_hash='b'*64)
    changed=record(turn,'source_preview',{'source_id':'A'}, {'version':'v2','rows':[2]})
    assert not changed.get('verification_feedback')
    other=record(turn,'source_preview',{'source_id':'B'}, {'version':'v2','rows':[2]})
    assert other['agent_trace'][-1]['inspection_signature']!=changed['agent_trace'][-1]['inspection_signature']
    record(turn,'read_source_text',{'source_id':'A','offset':0}, {'text':'first','next_offset':10})
    page=record(turn,'read_source_text',{'source_id':'A','offset':10}, {'text':'next','next_offset':None})
    assert page['agent_trace'][-1]['inspection_signature']!=page['agent_trace'][-2]['inspection_signature']
    record(turn,'source_schema',{'source_id':'A'}, {'error':'temporary'},error=True)
    recovered=record(turn,'source_preview',{'source_id':'A'}, {'version':'v2','rows':[2]})
    assert recovered['state']=='agent'
    patch('turn',turn['id'],context_window_epoch=1)
    after=record(turn,'source_preview',{'source_id':'A'}, {'version':'v2','rows':[2]})
    assert after['state']=='agent' and after['agent_trace'][-1]['inspection_epoch']==agent_progress.recovery_epoch(after)


def test_same_plan_does_not_hide_a_read_loop_but_changed_plan_is_progress(turn):
    plan={'steps':['核对','加工'],'completed_steps':[],'current_step':0}
    record(turn,'set_task_plan',plan,plan)
    record(turn,'project_context',{}, {'project':'x'})
    for _ in range(2):
        record(turn,'set_task_plan',plan,plan)
        current=record(turn,'project_context',{}, {'project':'x'})
    assert current['verification_feedback']['code']=='INSPECTION_NO_NEW_EVIDENCE'
    updated=dict(plan,current_step=1)
    current=record(turn,'set_task_plan',updated,updated)
    assert current.get('verification_feedback') is None


def test_completed_business_work_clears_warning_and_old_loop_counter(turn):
    patch('turn',turn['id'],verification_feedback={'code':'INSPECTION_NO_NEW_EVIDENCE'},loop_attempts=1)
    current=record(turn,'write_file',{'path':'out.md'}, {'path':'out.md','sha256':'new'})
    assert current.get('verification_feedback') is None and current['loop_attempts']==0


def test_comparison_uses_stored_fingerprints_and_bounded_recent_window(turn):
    value={'text':'x'*100000}
    record(turn,'source_preview',{'source_id':'A'},value)
    saved=get('turn',turn['id'])
    assert len(saved['agent_trace'][-1]['inspection_signature'])==64
    # A prior compaction/old receipt without a trusted fingerprint fails open.
    old=copy.deepcopy(saved);old['agent_trace'][-1].pop('inspection_signature')
    result=agent_context.inspection_progress(old,'source_preview',{'source_id':'A'},value,agent_progress.recovery_epoch(old),
        agent_context.inspection_source_version(old,'source_preview',{'source_id':'A'}))
    assert not result['duplicate']
    assert agent_context.inspection_progress(dict(saved,agent_budget_base=len(saved['agent_trace'])),
        'source_preview',{'source_id':'A'},value,agent_progress.recovery_epoch(saved),
        agent_context.inspection_source_version(saved,'source_preview',{'source_id':'A'}))['duplicate'] is False


def test_unchanged_sample_in_new_source_version_is_new_evidence(turn):
    old=record(turn,'source_schema',{'source_id':'A'},{'fields':['amount']})
    before=old['agent_trace'][-1]['inspection_signature']
    patch('source','A',revision=2,revision_id='v2-A',content_hash='b'*64)
    assert agent_context.inspection_progress(old,'source_schema',{'source_id':'A'},
        {'fields':['amount']},agent_progress.recovery_epoch(old),[1,'v1-A','a'*64]) is None
    new=record(turn,'source_schema',{'source_id':'A'},{'fields':['amount']})
    assert new['agent_trace'][-1]['inspection_signature']!=before
    source=get('source','A');source['kind']='api';put('source',source,'A')
    assert agent_context.inspection_progress(new,'source_schema',{'source_id':'A'},
        {'fields':['amount']},agent_progress.recovery_epoch(new)) is None


def test_source_replaced_during_live_read_never_gets_a_duplicate_fingerprint(turn):
    current=get('turn',turn['id']);args={'source_id':'A'}
    before=agent_context.inspection_source_version(current,'source_schema',args)
    patch('turn',turn['id'],state='agent_tool',agent_trace=[{'id':uid(),'tool':'source_schema',
        'arguments':args,'status':'started','started_at':now(),'inspection_source_before':before}])
    patch('source','A',revision=2,revision_id='v2-A',content_hash='b'*64)
    agent.observe(turn,{'fields':['amount']})
    result=get('turn',turn['id'])
    assert result['state']=='agent' and 'inspection_signature' not in result['agent_trace'][-1]
    assert not result.get('verification_feedback')
