"""Recovery references name authorized original resources, never cached page wrappers."""
import copy,hashlib,json
from datetime import datetime,timedelta,timezone

import pytest

from agent_service import agent_context as ctx,context_compaction as context,memory_service as memory,provider
from agent_service.actor_context import actor_scope
from agent_service.store import put,patch,get,uid,now,all_items,delete

A='u_'+'a'*64
B='u_'+'b'*64


@pytest.fixture
def pair(monkeypatch):
    memory_before={kind:{value['id'] for value in all_items(kind)} for kind in ('user_memory','memory_revision')}
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    pid='p_'+uid();sid='c_'+uid()
    put('project',{'name':'Recovery regression'},pid)
    put('conversation',{'project_id':pid,'name':'Business recovery'},sid)
    base=datetime(2026,10,1,tzinfo=timezone.utc)
    old=put('turn',{'project_id':pid,'session_id':sid,'actor_id':A,'text':'已核对的业务资料'*1500,
        'answer':'原件仍是依据','created_at':base.isoformat(),'state':'done','outcome':'answered',
        'agent_trace':[{'id':'original','tool':'query_sql','arguments':{},'status':'done',
                        'observation':{'rows':[{'amount':'9007199254740993.000000000001','unit':'CNY'}]}}]},'t_'+uid())
    current=put('turn',{'project_id':pid,'session_id':sid,'actor_id':A,'text':'继续处理已有资料',
        'created_at':(base+timedelta(hours=1)).isoformat(),'state':'agent','agent_trace':[]},'t_'+uid())
    yield old,current
    for kind,existing in memory_before.items():
        for value in all_items(kind):
            if value['id'] not in existing:delete(kind,value['id'])


def test_explicit_history_owner_repairs_actual_new_turn_scope_bug_without_global_search(pair):
    old,current=pair
    old=patch('turn',old['id'],text='已核对业务资料')
    with actor_scope(A):
        with pytest.raises(ctx.InvalidRecoveryReference) as error:ctx.read_result(current,ctx.ToolResult(step_id='original'))
        assert error.value.code=='RECOVERY_REFERENCE_INVALID'
        page=context.read_original(current,old['id'],limit=10000)
        # The source ID is supplied by trusted recovery metadata, not guessed.
        original=json.loads(page['content'])['references']['tool_receipts'][0]['recover_with']
        assert original=={'tool':'read_tool_result','source_turn_id':old['id'],'step_id':'original','offset':0}
        result=ctx.read_result(current,ctx.ToolResult(step_id='original',source_turn_id=old['id']))
        assert json.loads(result['content'])==old['agent_trace'][0]['observation']
        r=result['recovery'];assert r['resource_id']=='turn:'+old['id']+':step:original'
        assert r['version_sha256']==hashlib.sha256(result['content'].encode()).hexdigest()
        assert r['returned_characters']==r['total_characters']==len(result['content']) and r['next_offset'] is None


def test_default_resumed_chain_and_alias_absolute_offsets_keep_one_resource(pair):
    old,current=pair
    current=patch('turn',current['id'],resumed_from=old['id'],agent_trace=[
        {'id':'alias','tool':'read_tool_result','arguments':{'source_turn_id':old['id'],'step_id':'original','offset':0,'limit':5},
         'status':'done','observation':{'content':'untrusted cached wrapper'}}])
    with actor_scope(A):
        original=ctx.read_result(current,ctx.ToolResult(step_id='original'))
        alias=ctx.read_result(current,ctx.ToolResult(step_id='alias',offset=5,limit=20))
        assert alias['content']==original['content'][5:25]
        assert alias['recovery']==ctx.recovery_page('tool_result','turn:'+old['id']+':step:original',original['content'],5,20)
        assert alias['resolved_page_steps']==['alias']


def test_historical_recovery_aliases_resolve_but_do_not_expand_the_business_snapshot(pair):
    old,current=pair
    old=patch('turn',old['id'],text='业务目标',agent_trace=old['agent_trace']+[
        {'id':'historic-wrapper','tool':'read_tool_result','arguments':{'step_id':'original','offset':0,'limit':6000},
         'status':'done','observation':{'content':'OLD_WRAPPER_CONTENT'}}])
    with actor_scope(A):
        recovered=json.loads(context.read_original(current,old['id'],limit=10000)['content'])
        assert 'OLD_WRAPPER_CONTENT' not in json.dumps(recovered)
        assert len(recovered['agent_trace'])==1
        assert all(item['tool']!='read_tool_result' for item in recovered['references']['tool_receipts'])
        direct=ctx.read_result(current,ctx.ToolResult(step_id='historic-wrapper',source_turn_id=old['id']))
        assert direct['recovery']['resource_id']=='turn:'+old['id']+':step:original'


def test_self_read_has_stable_finite_version_when_only_recovery_traversal_is_added(pair):
    _,current=pair
    with actor_scope(A):
        first=context.read_original(current,current['id'],limit=10000)
        for index in range(6):
            current=patch('turn',current['id'],agent_trace=current['agent_trace']+[
                {'id':'self_page_'+str(index),'tool':'read_context_original','status':'done',
                 'arguments':{'turn_id':current['id'],'offset':0,'limit':10000},'observation':first}])
            repeated=context.read_original(current,current['id'],limit=10000)
            assert repeated==first
        assert first['total_characters']<2500


def test_all_distinct_current_ranges_remain_exact_while_identical_wrapper_pages_share_one_copy(pair):
    old,current=pair
    with actor_scope(A):
        pages=[context.read_original(current,old['id'],offset=i*5000,limit=5000) for i in range(3)]
        trace=[{'id':'range_'+str(i),'tool':'read_context_original','status':'done',
                'arguments':{'turn_id':old['id'],'offset':i*5000,'limit':5000},'observation':page} for i,page in enumerate(pages)]
        trace.append(dict(trace[0],id='duplicate-wrapper'))
        trace += [{'id':'later'+str(i),'tool':'set_task_plan','status':'done','arguments':{},'observation':{}} for i in range(3)]
        current=patch('turn',current['id'],agent_trace=trace)
        projected=context.trace_for_model(current,ctx.trace_context(current,lambda x:x))
        for i in range(3):assert projected[i]['observation']['content']==pages[i]['content']
        assert projected[3]['observation']['duplicate_result_view']
        assert projected[3]['observation']['context_reference']['step_id']=='range_0'
        assert get('turn',current['id'])['agent_trace']==trace


def test_history_page_alias_replays_original_args_and_preserves_latest_page_after_other_tools(pair):
    old,current=pair
    with actor_scope(A):
        args={'turn_id':old['id'],'offset':3000,'limit':8000};page=context.read_original(current,**args)
        trace=[{'id':'page','tool':'read_context_original','arguments':args,'status':'done','observation':dict(page,content='STALE_CACHE')}]
        trace += [{'id':'later'+str(i),'tool':'set_task_plan','arguments':{},'status':'done','observation':{}} for i in range(5)]
        current=patch('turn',current['id'],agent_trace=trace)
        result=ctx.read_result(current,ctx.ToolResult(step_id='page',offset=0,limit=10000))
        assert result['content']==page['content'] and result['recovery']==page['recovery']
        subpage=ctx.read_result(current,ctx.ToolResult(step_id='page',offset=100,limit=200))
        assert subpage['content']==page['content'][100:300] and subpage['recovery']['offset']==3100
        values=context.trace_for_model(current,ctx.trace_context(current,lambda x:x))
        assert values[0]['observation']['content']==page['content']
        assert values[0]['observation']['next_offset']==11000 and 'context_excerpt' not in values[0]['observation']
        assert get('turn',current['id'])['agent_trace']==trace


def test_history_authorization_share_project_and_internal_session_are_not_relaxed(pair,monkeypatch):
    old,current=pair
    with actor_scope(A):
        other='p_'+uid();put('project',{'name':'Other'},other)
        foreign=put('turn',dict(old,project_id=other),'t_'+uid())
        with pytest.raises(ValueError,match='其他项目'):ctx.read_result(current,ctx.ToolResult(step_id='original',source_turn_id=foreign['id']))
        internal='c_'+uid();put('conversation',{'project_id':old['project_id'],'internal':True},internal)
        hidden=put('turn',dict(old,session_id=internal),'t_'+uid())
        with pytest.raises(ValueError,match='内部协作'):ctx.read_result(current,ctx.ToolResult(step_id='original',source_turn_id=hidden['id']))
        monkeypatch.setattr(provider,'settings',lambda:{'share_data':False})
        with pytest.raises(ValueError,match='读取未获批准'):ctx.read_result(current,ctx.ToolResult(step_id='original',source_turn_id=old['id']))
        with pytest.raises(ctx.InvalidRecoveryReference):ctx.read_result(current,ctx.ToolResult(step_id='original',source_turn_id='missing'))


def test_revocation_changes_live_version_and_never_recovers_cached_preference_or_tool_values(pair):
    old,current=pair
    with actor_scope(A):
        old=patch('turn',old['id'],text='请记住：PRIVATE_WITHDRAWN_PREFERENCE')
        value=memory.tool(old,'remember_memory',{'title':'Recovery preference','quote':'PRIVATE_WITHDRAWN_PREFERENCE','scope':'personal'})
        before=context.read_original(current,old['id'],limit=10000)
        current=patch('turn',current['id'],agent_trace=[{'id':'page','tool':'read_context_original','arguments':{'turn_id':old['id'],'limit':10000},
                                                        'status':'done','observation':before}])
        assert ctx.read_result(current,ctx.ToolResult(step_id='original',source_turn_id=old['id']))['content']
        memory.forget(old['project_id'],value['id'],value['version'])
        with pytest.raises(ValueError,match='撤回'):ctx.read_result(current,ctx.ToolResult(step_id='original',source_turn_id=old['id']))
        after=ctx.read_result(current,ctx.ToolResult(step_id='page',limit=10000))
        assert 'PRIVATE_WITHDRAWN_PREFERENCE' not in after['content'] and '9007199254740993' not in after['content']
        assert after['recovery']['version_sha256']!=before['recovery']['version_sha256']
        visible=context.trace_for_model(current,ctx.trace_context(current,lambda x:x))
        assert 'PRIVATE_WITHDRAWN_PREFERENCE' not in json.dumps(visible)
        # Exercise the actual native HTTP-body projector with an old cached
        # receipt and the newly authorized view, not just a Python DTO check.
        import subprocess,shutil
        node=shutil.which('node');assert node
        def native_message(step):
            return {'role':'tool','tool_call_id':'call_'+step['step_id'],
                    'content':json.dumps({'observation':step,'available_tools':[]},ensure_ascii=False)}
        cached={'step_id':'cached-native','tool':'read_context_original','status':'done',
                'arguments':{'turn_id':old['id'],'limit':10000},'observation':before}
        body={'messages':[native_message(cached),native_message(visible[0])]}
        script="import {projectRequest} from './agent_service/opencode_context.mjs';let s='';for await (const c of process.stdin)s+=c;process.stdout.write(JSON.stringify(projectRequest(JSON.parse(s)).body));"
        result=subprocess.run([node,'--input-type=module','-e',script],input=json.dumps(body,ensure_ascii=False),
                              text=True,encoding='utf-8',capture_output=True,check=True)
        assert 'PRIVATE_WITHDRAWN_PREFERENCE' not in result.stdout and '9007199254740993' not in result.stdout


def test_other_actor_public_work_remains_shared_but_personal_memory_is_not_recovered(pair):
    old,current=pair
    with actor_scope(B):
        assert ctx.read_result(current,ctx.ToolResult(step_id='original',source_turn_id=old['id']))['content']
    with actor_scope(A):
        old=patch('turn',old['id'],text='请记住：PRIVATE_OTHER_ACTOR_STYLE')
        memory.tool(old,'remember_memory',{'title':'Private style','quote':'PRIVATE_OTHER_ACTOR_STYLE','scope':'personal'})
    with actor_scope(B):
        with pytest.raises(ValueError,match='当前用户'):ctx.read_result(current,ctx.ToolResult(step_id='original',source_turn_id=old['id']))
        assert 'PRIVATE_OTHER_ACTOR_STYLE' not in context.read_original(current,old['id'],limit=10000)['content']


def test_checkpoint_recovery_replays_original_arguments_and_is_actor_bound(pair):
    old,current=pair
    with actor_scope(A):
        for i in range(8):put('turn',dict(old,created_at='2026-10-01T00:'+str(i+1).zfill(2)+':00+00:00',agent_trace=[]),'t_'+uid())
        summary=context.context_for_turn(current,force=True);checkpoint=summary['summary_id'];assert checkpoint
        args={'checkpoint_id':checkpoint,'offset':1,'limit':2};page=context.read_checkpoint(current,**args)
        current=patch('turn',current['id'],agent_trace=[{'id':'checkpoint-page','tool':'read_context_checkpoint',
            'arguments':args,'status':'done','observation':{'items':['STALE_CACHE']}}])
        restored=ctx.read_result(current,ctx.ToolResult(step_id='checkpoint-page'))
        assert restored['items']==page['items'] and restored['recovery']==page['recovery']
        assert restored['recovery']['offset']>0 and restored['recovery']['returned_characters']>0
        with pytest.raises(ctx.InvalidRecoveryReference):context.read_checkpoint(current,'missing-checkpoint')
    with actor_scope(B):
        with pytest.raises(ValueError,match='当前项目和用户'):ctx.read_result(current,ctx.ToolResult(step_id='checkpoint-page'))


def test_exact_workflow_duplicates_share_document_while_latest_draft_and_failure_stay_visible(pair):
    _,current=pair
    schema={'columns':[{'name':'amount','data_type':'decimal','unit':'CNY','meaning':'原文'*1800}]}
    document={'plan':{'inputs':{'raw':schema},'stages':[{'id':'keep','kind':'filter'}]},'bindings':{'raw':{'source_id':'source'}}}
    observation={'workflow_id':'workflow','draft_sha256':'d'*64,'document':document,
                 'inputs':{'raw':{'binding':document['bindings']['raw'],'schema':schema}},'steps':document['plan']['stages'],
                 'latest_run':{'workflow_failure':{'code':'TYPE_MISMATCH','stage_id':'input:raw'}}}
    frozen=copy.deepcopy(observation);view=ctx.observation_view(observation,'inspect_data_workflow')
    assert observation==frozen and 'inputs' not in view and 'steps' not in view
    trace=[{'id':'inspect','tool':'inspect_data_workflow','arguments':{},'status':'done','observation':observation}]
    trace += [{'id':'later'+str(i),'tool':'set_task_plan','arguments':{},'status':'done','observation':{}} for i in range(4)]
    values=ctx.trace_context(dict(current,agent_trace=trace),lambda x:x)
    assert values[0]['observation']==view and values[0]['observation']['latest_run']==observation['latest_run']
    changed=copy.deepcopy(observation);changed['inputs']['raw']['schema']=copy.deepcopy(changed['inputs']['raw']['schema'])
    changed['inputs']['raw']['schema']['columns'][0]['unit']='万元'
    assert 'inputs' in ctx.observation_view(changed,'inspect_data_workflow')
