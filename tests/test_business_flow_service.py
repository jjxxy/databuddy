"""A real uploaded file drives a frozen flow and a downloadable result."""
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.store import get,uid,unseal
from agent_service.execution import process_once
from agent_service import provider


def test_terminal_inflight_external_action_is_uncertain_and_never_assumed_failed():
    from agent_service.business_flows import _uncertain
    external={'status':'running','steps':[{'status':'running','replay_safe':False}]}
    safe={'status':'running','steps':[{'status':'running','replay_safe':True}]}
    assert _uncertain({'status':'failed'},external)
    assert _uncertain({'status':'cancelled'},external)
    assert not _uncertain({'status':'failed'},safe)
    assert not _uncertain({'status':'running'},external)


def test_confirmed_flow_git_job_and_actual_html(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'','model':'','share_data':True})
    client=TestClient(app);h={'Authorization':'Bearer '+TOKEN}
    def req(method,path,**kw):
        r=client.request(method,path,headers=h,**kw);assert r.status_code==200,r.text;return r.json()
    pid=req('POST','/api/projects',json={'name':'流程纵切','request_id':uid()})['id'];base='/api/projects/'+pid
    uploaded=req('POST',base+'/attachments',files={'file':('income.csv','办事处,收入\n东部,9.00\n东部,9.00\n西部,4.00\n'.encode(),'text/csv')},data={'request_id':uid(),'local_processing':'true'})
    assert process_once();source=get('source',uploaded['id'])
    turn=req('POST',base+'/messages',json={'text':'读取收入明细并生成汇总报告','request_id':uid(),'source_id':source['id']})
    plan={'version':1,'name':'精确汇总','inputs':{'table':{'meaning':'收入明细','grain':['nexus_row'],
        'columns':[{'name':'nexus_row','data_type':'text','meaning':'本次资料行号'},
                   {'name':'c_1','data_type':'text','meaning':'办事处'},
                   {'name':'c_2','data_type':'decimal','meaning':'收入','unit':'CNY','aggregation':'additive'}]}},
        'stages':[{'id':'selected','kind':'filter','description':'选择东部','input':'table',
                   'conditions':[{'column':'c_1','operator':'eq','value':'东部'}]},
                  {'id':'total','kind':'aggregate','description':'按办事处汇总','input':'selected','by':['c_1'],
                   'meaning':'各办事处收入','aggregations':[{'function':'sum','column':'c_2',
                       'output':{'name':'amount','data_type':'decimal','meaning':'收入合计','unit':'CNY'}}]}], 'output':'total'}
    document={'name':'收入汇总','inputs':[],'nodes':[
        {'id':'read','kind':'source_table','title':'读取资料','config':{'source_id':source['id'],'revision':source['revision'],'content_hash':source['content_hash']}},
        {'id':'aggregate','kind':'table_workflow','title':'汇总收入','depends_on':['read'],'inputs':{'table':{'source':'node','name':'read'}},'config':{'plan':plan}},
        {'id':'check','kind':'quality_gate','title':'自动核对金额','depends_on':['aggregate'],'inputs':{'table':{'source':'node','name':'aggregate'}},'config':{'rules':[{'kind':'required','column':'amount'},{'kind':'range','column':'amount','minimum':'0'},{'kind':'unique','columns':['c_1']}]}},
        {'id':'review','kind':'human_review','title':'人工核对','depends_on':['check'],'inputs':{'table':{'source':'node','name':'check'}},'config':{'instructions':'核对收入合计'}},
        {'id':'spec','kind':'table_report_spec','title':'整理报告','depends_on':['review'],'inputs':{'table':{'source':'node','name':'review'}},'config':{'title':'收入汇总','format':'html'}},
        {'id':'report','kind':'html_report','title':'生成报告','depends_on':['spec'],'inputs':{'spec':{'source':'node','name':'spec'}},'config':{'name':'收入汇总','renderer_version':'business-html-v1'}}], 'outputs':['report']}
    # Exercise the same validated Agent tool boundary as the conversation, not
    # only the management endpoint. A replay must not create another draft.
    from agent_service import agent, conversation
    from agent_service.actor_context import actor_scope
    from agent_service.store import patch
    patch('turn',turn['id'],state='agent',interaction_mode='work',discovered_tools=['create_business_flow'])
    with actor_scope('owner'):
        agent.opencode_step(turn['id'],{'kind':'tool','tool':'set_task_plan','arguments':{
            'steps':['读取资料','生成业务流程'], 'current_step':0}},'flow:plan')
        step={'kind':'tool','tool':'create_business_flow','arguments':{'document':document}}
        created=agent.opencode_step(turn['id'],step,'flow:create')
        replay=agent.opencode_step(turn['id'],step,'flow:create')
    marker=get('turn',turn['id']).get('business_flow')
    assert marker,created
    assert replay['replayed'] is True
    assert conversation.public_turn(get('turn',turn['id']))['business_flow']==marker
    # Originating turn may finish before the user approves and runs the draft.
    patch('turn',turn['id'],state='done',outcome='needs_input')
    saved=req('GET',base+'/business-flows/'+marker['id'])
    assert saved['candidate_commit'] and saved['published_run'] is None
    assert len(req('GET',base+'/business-flows')['items'])==1
    flow=base+'/business-flows/'+saved['id'];run_request=uid();body={'request_id':run_request,'expected_digest':saved['candidate_digest'],'inputs':{}}
    debug={'request_id':uid(),'expected_digest':saved['candidate_digest'],'selected_nodes':['read','aggregate'],'inputs':{}}
    missing=client.post(flow+'/debug-runs',headers=h,json={**debug,'request_id':uid(),'selected_nodes':['aggregate']})
    assert missing.status_code==400
    unsafe=client.post(flow+'/debug-runs',headers=h,json={**debug,'request_id':uid(),'selected_nodes':['read','aggregate','spec','report']})
    assert unsafe.status_code==400
    partial=req('POST',flow+'/debug-runs',json=debug)
    assert req('POST',flow+'/debug-runs',json=debug)['run_id']==partial['run_id']
    assert process_once()
    done=req('GET',flow+'/debug-runs/'+partial['run_id'])
    assert done['status']=='succeeded' and done['publishable'] is False,done
    assert [step['node_id'] for step in done['steps']]==['read','aggregate']
    source_page=req('GET',flow+'/debug-runs/'+partial['run_id']+'/tables/read?limit=2')
    assert source_page['total']==3 and len(source_page['rows'])==2 and source_page['publishable'] is False
    result=req('GET',flow+'/debug-runs/'+partial['run_id']+'/tables/aggregate')
    assert result['rows'][0]['amount']=='18.00' and result['publishable'] is False
    from agent_service.business_flow_debug import status as debug_status
    from agent_service.actor_context import actor_scope
    import pytest,copy
    with actor_scope('u_'+'f'*64):
        with pytest.raises(ValueError):debug_status(pid,saved['id'],partial['run_id'],'u_'+'f'*64)
    from agent_service.jobs import get_job,save
    from agent_service.store import seal
    debug_job=get_job(partial['run_id']);tampered=copy.deepcopy(debug_job);envelope=unseal(debug_job['secret_debug_checkpoint'])
    envelope['state']['steps'][0]['input_sha256']='0'*64;tampered['secret_debug_checkpoint']=seal(envelope)
    try:
        save(tampered,tampered['id'])
        assert client.get(flow+'/debug-runs/'+partial['run_id']+'/tables/aggregate',headers=h).status_code==400
    finally:save(debug_job,debug_job['id'])
    clean=req('GET',flow)
    assert clean['latest_run'] is None and clean['candidate_run'] is None and clean['published_run'] is None
    assert client.get(flow+'/runs/'+partial['run_id'],headers=h).status_code==400
    checked=req('POST',flow+'/debug-runs',json={**debug,'request_id':uid(),'selected_nodes':['read','aggregate','check']})
    assert process_once()
    checked_status=req('GET',flow+'/debug-runs/'+checked['run_id'])
    assert checked_status['status']=='succeeded' and checked_status['steps'][-1]['node_id']=='check'
    assert req('GET',flow+'/debug-runs/'+checked['run_id']+'/tables/check')['rows'][0]['amount']=='18.00'
    from agent_service import business_flow_repair
    failed_document=copy.deepcopy(document);failed_document['nodes'][2]['config']['rules'][1]['maximum']='17.99'
    failed_flow=req('POST',base+'/business-flows',json={'request_id':uid(),'turn_id':turn['id'],'document':failed_document})
    failed_url=base+'/business-flows/'+failed_flow['id']
    failed=req('POST',failed_url+'/debug-runs',json={'request_id':uid(),'expected_digest':failed_flow['candidate_digest'],'selected_nodes':['read','aggregate','check'],'inputs':{}})
    with monkeypatch.context() as m:
        m.setattr(business_flow_repair,'enqueue_diagnostic',lambda job:None)
        assert process_once()
    failed_status=req('GET',failed_url+'/debug-runs/'+failed['run_id'])
    assert failed_status['status']=='failed' and failed_status['steps'][-1]['error_code']=='CHECK_RANGE_FAILED'
    assert not any(s['node_id']=='spec' for s in failed_status['steps'])
    stopped=req('POST',flow+'/debug-runs',json={**debug,'request_id':uid()})
    assert req('POST',flow+'/debug-runs/'+stopped['run_id']+'/cancel')['cancel_requested'] is True
    assert req('GET',flow+'/debug-runs/'+stopped['run_id'])['status']=='cancelled'
    usage=req('GET',flow+'/storage');assert usage['project_limit_bytes']>0 and usage['actor_limit_bytes']>0
    run=req('POST',flow+'/runs',json=body)
    assert req('POST',flow+'/runs',json=body)['run_id']==run['run_id']
    conflict=client.post(flow+'/runs',headers=h,json={**body,'request_id':uid()});assert conflict.status_code==400
    assert req('GET',flow+'/requests/'+run_request)['accepted'] is True
    assert req('GET',flow+'/requests/'+uid())['accepted'] is None
    assert process_once();paused=req('GET',flow+'/runs/'+run['run_id']);assert paused['status']=='awaiting_review',paused
    assert paused['human_checkpoint']['instructions']=='核对收入合计'
    assert req('GET',flow+'/runs/'+run['run_id']+'/tables/check')['rows'][0]['amount']=='18.00'
    approval={'expected_checkpoint_sha256':paused['human_checkpoint']['checkpoint_sha256'],'note':'已核对来源和合计'}
    assert client.post(flow+'/runs/'+run['run_id']+'/approve-checkpoint',headers=h,json={**approval,'expected_checkpoint_sha256':'0'*64}).status_code==400
    resumed=req('POST',flow+'/runs/'+run['run_id']+'/approve-checkpoint',json=approval);assert resumed['status']=='retrying'
    assert process_once();status=req('GET',flow+'/runs/'+run['run_id']);assert status['status']=='succeeded',status
    assert status['plan_digest']==saved['candidate_digest'] and status['commit']==saved['candidate_commit']
    assert len(status['steps'])==6 and len(status['artifacts'])==1
    assert status['steps'][1]['table_available']
    page=req('GET',flow+'/runs/'+run['run_id']+'/tables/aggregate?offset=0&limit=1')
    assert page['total']==1 and page['next_offset'] is None and page['rows'][0]['amount']=='18.00'
    assert status['steps'][1]['contribution_available'] is True
    contribution=req('GET',flow+'/runs/'+run['run_id']+'/tables/aggregate/contributions?row_index=0')
    assert contribution['group']=={'c_1':'东部'} and contribution['total']==2
    assert [row['c_2'] for row in contribution['rows']]==['9.00','9.00']
    assert page['financial_verified'] is False and page['run_id']==run['run_id']
    reconciliation=req('GET',flow+'/runs/'+run['run_id']+'/reconciliation')
    assert reconciliation['financial_verified'] is False and reconciliation['sources'][0]['content_hash']==source['content_hash']
    stage=reconciliation['nodes'][0]['stages'][0]
    assert (stage['input_rows'],stage['output_rows'],stage['excluded_rows'])==(3,2,1)
    amount=next(x for x in stage['checks'] if x['column']=='c_2')
    assert amount['input_total']=='22.00' and amount['included_total']=='18.00' and amount['excluded_total']=='4.00'
    assert amount['status']=='balanced' and amount['difference']=='0.00'
    from agent_service.business_flows import get_run_reconciliation
    from agent_service.actor_context import actor_scope
    import pytest
    with actor_scope('u_'+'f'*64):
        with pytest.raises(ValueError):get_run_reconciliation(pid,saved['id'],run['run_id'])
    bad=client.get(flow+'/runs/'+run['run_id']+'/tables/read',headers=h)
    assert bad.status_code==400
    invalid=client.get(flow+'/runs/'+run['run_id']+'/tables/aggregate?limit=101',headers=h)
    assert invalid.status_code==400
    # An interrupted run may expose a completed prefix for inspection only.
    # It must bind every upstream receipt to the frozen inputs and version.
    from agent_service.jobs import get_job,save
    from agent_service.store import seal
    import copy
    original=get_job(run['run_id']);partial=copy.deepcopy(original)
    checkpoint=unseal(original['secret_flow_checkpoint']);checkpoint['steps']=checkpoint['steps'][:2]
    checkpoint['status']='failed';checkpoint.pop('outputs',None)
    partial['status']='failed';partial['secret_flow_checkpoint']=seal(checkpoint)
    try:
        save(partial,partial['id'])
        failed=req('GET',flow+'/runs/'+run['run_id'])
        assert failed['status']=='failed' and failed['steps'][1]['table_available']
        prefix=req('GET',flow+'/runs/'+run['run_id']+'/tables/aggregate?offset=0&limit=1')
        assert prefix['rows'][0]['amount']=='18.00' and '整体流程未完成' in prefix['notice']
        changed=copy.deepcopy(checkpoint);changed['steps'][0]['input_sha256']='0'*64
        partial['secret_flow_checkpoint']=seal(changed);save(partial,partial['id'])
        assert client.get(flow+'/runs/'+run['run_id']+'/tables/aggregate',headers=h).status_code==400
        changed=copy.deepcopy(checkpoint);changed['steps'][1]['output']['rows'][0]['amount']='19.00'
        partial['secret_flow_checkpoint']=seal(changed);save(partial,partial['id'])
        assert client.get(flow+'/runs/'+run['run_id']+'/tables/aggregate',headers=h).status_code==400
        partial['secret_flow_checkpoint']=seal(checkpoint);partial['status']='cancelled';save(partial,partial['id'])
        assert req('GET',flow+'/runs/'+run['run_id']+'/tables/aggregate')['rows'][0]['amount']=='18.00'
    finally:save(original,original['id'])
    result=client.get(status['artifacts'][0]['download_url'],headers=h)
    assert result.status_code==200 and '18.00' in result.text and '未经' not in result.text
    record=unseal(get('run',run['run_id'])['secret_flow_checkpoint'])
    assert record['status']=='succeeded' and record['steps'][1]['output']['rows'][0]['amount']=='18.00'
    assert not status['financial_verified']
    # Inputs and source rows are not committed to the project repository.
    from agent_service.workspace import folder,git_content
    content=git_content(folder(pid),saved['candidate_commit'],'files/business-flows/'+saved['id']+'.json').decode()
    # The declared filter value is part of the versioned plan; raw financial
    # amounts from uploaded source rows remain outside the project repository.
    assert '9.00' not in content and '4.00' not in content
    # A flagged source needs an explicit review tied to this checkpoint.
    from agent_service.jobs import get_job,save
    from agent_service.store import seal
    raw=get_job(run['run_id']);flagged=unseal(raw['secret_flow_checkpoint'])
    flagged['steps'][0]['output']['requires_source_review']=True
    flagged['steps'][0]['output']['evidence']['semantic_review_reasons']=['AUTO_LAYOUT_REVIEW_REQUIRED']
    from agent_service.business_flow_engine import digest
    flagged['steps'][0]['output_sha256']=digest(flagged['steps'][0]['output'])
    raw['secret_flow_checkpoint']=seal(flagged);save(raw,raw['id'])
    review=req('GET',flow+'/runs/'+run['run_id'])['review']
    assert review['required'] and not review['approved'] and len(review['items'])==1
    item=review['items'][0];assert item['source_id']==source['id'] and item['content_hash']==source['content_hash']
    assert item['reasons']==['AUTO_LAYOUT_REVIEW_REQUIRED'] and 'rows' not in item
    cannot=client.post(flow+'/publish',headers=h,json={'run_id':run['run_id'],'expected_digest':saved['candidate_digest']})
    assert cannot.status_code==400 and '核对' in cannot.text
    review_path=flow+'/runs/'+run['run_id']+'/review'
    body_review={'expected_checkpoint_sha256':review['checkpoint_sha256'],'items':[{'node_id':item['node_id'],'kinds':item['kinds'],'note':'已对照原表范围和收入口径'}]}
    stale=client.post(review_path,headers=h,json={**body_review,'expected_checkpoint_sha256':'a'*64});assert stale.status_code==400
    wrong=client.post(review_path,headers=h,json={**body_review,'items':[{'node_id':item['node_id'],'kinds':['semantic'],'note':'已对照原表范围和收入口径'}]});assert wrong.status_code==400
    assert req('POST',review_path,json=body_review)['review']['approved']
    assert req('POST',review_path,json=body_review)['review']['approved']
    original_checkpoint=raw['secret_flow_checkpoint']
    flagged['steps'][0]['output']['revision']=2
    raw['secret_flow_checkpoint']=seal(flagged);save(raw,raw['id'])
    tampered=client.post(flow+'/publish',headers=h,json={'run_id':run['run_id'],'expected_digest':saved['candidate_digest']})
    assert tampered.status_code==400 and '回执' in tampered.text
    raw['secret_flow_checkpoint']=original_checkpoint;save(raw,raw['id'])
    adopted=req('POST',flow+'/publish',json={'run_id':run['run_id'],'expected_digest':saved['candidate_digest']})
    assert adopted['published_run']==run['run_id'] and adopted['published_digest']==saved['candidate_digest']
    # The same published Git version reruns without changing logic or inputs;
    # a successful clean run advances the formal result pointer.
    again=req('POST',flow+'/runs',json={'request_id':uid(),'expected_digest':saved['candidate_digest'],'inputs':{},'use_published':True})
    assert process_once();repeated=req('GET',flow+'/runs/'+again['run_id']);assert repeated['status']=='awaiting_review',repeated
    req('POST',flow+'/runs/'+again['run_id']+'/approve-checkpoint',json={'expected_checkpoint_sha256':repeated['human_checkpoint']['checkpoint_sha256'],'note':'已核对本次收入'})
    assert process_once();repeated=req('GET',flow+'/runs/'+again['run_id']);assert repeated['status']=='succeeded',repeated
    after=req('GET',flow);assert after['published_run']==again['run_id'] and after['published_digest']==saved['candidate_digest']
    req('PUT',flow+'/schedule',json={'cron':'* * * * *','source_bindings':{},'expected_published_digest':saved['candidate_digest']})
    from datetime import datetime,timezone,timedelta
    from agent_service.store import patch
    patch('business_flow',saved['id'],next_due=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
    from agent_service.business_flows import queue_due
    queue_due();assert process_once()
    scheduled_pause=req('GET',flow)['latest_run'];assert scheduled_pause['status']=='awaiting_review'
    req('POST',flow+'/runs/'+scheduled_pause['id']+'/approve-checkpoint',json={'expected_checkpoint_sha256':scheduled_pause['human_checkpoint']['checkpoint_sha256'],'note':'已核对本次收入'})
    assert process_once()
    scheduled=req('GET',flow);assert scheduled['published_run'] not in (run['run_id'],again['run_id'])
    assert scheduled['published_digest']==saved['candidate_digest']
    # A new draft does not replace the adopted version. Project rollback
    # restores both the readable definition and the formal pointer.
    published_history=__import__('agent_service.workspace',fromlist=['history']).history(pid)[0]['commit']
    revised=dict(document,name='新的收入方案')
    patch('turn',turn['id'],state='done',outcome='complete')
    next_turn=req('POST',base+'/messages',json={'text':'修改候选方案名称','request_id':uid(),'source_id':source['id']})
    changed=req('PUT',flow,json={'request_id':uid(),'turn_id':next_turn['id'],'document':revised,'expected_digest':saved['candidate_digest']})
    assert changed['candidate_digest']!=saved['candidate_digest'] and changed['published_digest']==saved['candidate_digest']
    old_run=req('POST',flow+'/runs',json={'request_id':uid(),'expected_digest':saved['candidate_digest'],'inputs':{},'use_published':True})
    assert get('run',old_run['run_id'])['flow_turn_id']==turn['id']
    assert process_once();old_pause=req('GET',flow+'/runs/'+old_run['run_id']);assert old_pause['status']=='awaiting_review'
    req('POST',flow+'/runs/'+old_run['run_id']+'/approve-checkpoint',json={'expected_checkpoint_sha256':old_pause['human_checkpoint']['checkpoint_sha256'],'note':'已核对收入'})
    assert process_once();assert req('GET',flow+'/runs/'+old_run['run_id'])['status']=='succeeded'
    changed_run=req('POST',flow+'/runs',json={'request_id':uid(),'expected_digest':changed['candidate_digest'],'inputs':{}})
    assert process_once();changed_pause=req('GET',flow+'/runs/'+changed_run['run_id']);assert changed_pause['status']=='awaiting_review'
    req('POST',flow+'/runs/'+changed_run['run_id']+'/approve-checkpoint',json={'expected_checkpoint_sha256':changed_pause['human_checkpoint']['checkpoint_sha256'],'note':'已核对收入'})
    assert process_once();assert req('GET',flow+'/runs/'+changed_run['run_id'])['status']=='succeeded'
    new_formal=req('POST',flow+'/publish',json={'run_id':changed_run['run_id'],'expected_digest':changed['candidate_digest']})
    assert new_formal['cron'] is None and '定时已暂停' in new_formal['refresh_error']
    from agent_service import workspace
    workspace.restore(pid,published_history,turn['id'])
    restored=req('GET',flow);assert restored['candidate_digest']==saved['candidate_digest'] and restored['published_digest']==saved['candidate_digest']
    choices=req('GET',flow+'/input-sources');assert any(x['id']==source['id'] for x in choices['items'])
    assert all('secret_config' not in x for x in choices['items'])
    # Runtime changes between queue admission and worker execution must not
    # silently change the behavior of an already committed flow version.
    drift=req('POST',flow+'/runs',json={'request_id':uid(),'expected_digest':saved['candidate_digest'],'inputs':{}})
    from agent_service import business_flows
    real_lock=business_flows._runtime_lock
    monkeypatch.setattr(business_flows,'_runtime_lock',lambda doc:{**real_lock(doc),'python':'changed'})
    assert process_once();drift_status=req('GET',flow+'/runs/'+drift['run_id'])
    assert drift_status['status']=='failed' and '执行器或依赖已变化' in drift_status['message']
    monkeypatch.setattr(business_flows,'_runtime_lock',real_lock)
    # An interrupted external side effect is visible after refresh and cannot
    # be replayed via a fresh request; the original request remains idempotent.
    ambiguous_id=uid();ambiguous_body={'request_id':ambiguous_id,'expected_digest':saved['candidate_digest'],'inputs':{}}
    ambiguous=req('POST',flow+'/runs',json=ambiguous_body)
    from agent_service.jobs import get_job,save
    from agent_service.store import seal
    raw=get_job(ambiguous['run_id']);raw.update(status='failed',secret_flow_checkpoint=seal({'status':'unknown','steps':[]}));save(raw,raw['id'])
    assert req('GET',flow)['latest_run']['status']=='unknown'
    assert req('POST',flow+'/runs',json=ambiguous_body)['run_id']==ambiguous['run_id']
    blocked=client.post(flow+'/runs',headers=h,json={**ambiguous_body,'request_id':uid()});assert blocked.status_code==400 and '未知' in blocked.text
    # A user may confirm that one in-flight external step did not execute.
    # Earlier non-idempotent effects would continue to block a fresh run.
    raw=get_job(ambiguous['run_id']);state=unseal(raw['secret_flow_checkpoint'])
    state['steps']=[{'node_id':'submit','title':'提交','status':'unknown','replay_safe':False}]
    raw['secret_flow_checkpoint']=seal(state);save(raw,raw['id'])
    unknown=req('GET',flow+'/runs/'+ambiguous['run_id'])['unknown_resolution']
    assert unknown['can_confirm_not_executed'] and not unknown['confirmed_not_executed']
    resolution={'expected_checkpoint_sha256':unknown['checkpoint_sha256'],'outcome':'not_executed','note':'已查询系统确认没有提交记录'}
    checked=req('POST',flow+'/runs/'+ambiguous['run_id']+'/resolve-unknown',json=resolution)
    assert checked['status']=='unknown' and checked['unknown_resolution']['confirmed_not_executed']
    assert req('POST',flow+'/runs',json={**ambiguous_body,'request_id':uid()})['run_id']!=ambiguous['run_id']


def test_unknown_resolution_never_replays_prior_external_side_effects():
    from agent_service.business_flows import _resolution_view
    job={'id':'test_unknown_prior','actor_id':'owner','project_id':'p_test','status':'failed'}
    state={'status':'unknown','steps':[{'node_id':'sent','status':'succeeded','replay_safe':False},
                                       {'node_id':'write','status':'unknown','replay_safe':False}]}
    result=_resolution_view(job,state)
    assert not result['can_confirm_not_executed'] and not result['confirmed_not_executed']


def test_fact_alignment_cannot_be_dismissed_as_generic_source_review():
    from agent_service.business_flows import _review_items
    from agent_service.store import put,delete
    pid='p_'+uid();sid='s_'+uid();source={'project_id':pid,'revision':1,'content_hash':'a'*64,
         'fact_review_id':'fact_'+uid()}
    put('source',source,sid)
    try:
        state={'steps':[{'node_id':'source','title':'资料','output':{'source_id':sid,'revision':1,
            'content_hash':'a'*64,'requires_source_review':True,'evidence':{}}}]}
        items,blocked=_review_items(pid,state)
        assert items and '事实语义' in blocked
    finally:delete('source',sid)
def test_office_presentation_implementation_is_pinned(monkeypatch):
    from pathlib import Path
    from agent_service import business_flows
    doc={'nodes':[{'kind':'office_document'}]}
    original=business_flows._runtime_lock(doc)
    assert 'office_presentation.py' in original['modules']
    read_bytes=Path.read_bytes
    def changed(path):
        data=read_bytes(path)
        return data+b'\n# changed' if path.name=='office_presentation.py' else data
    monkeypatch.setattr(Path,'read_bytes',changed)
    assert business_flows._runtime_lock(doc)!=original
