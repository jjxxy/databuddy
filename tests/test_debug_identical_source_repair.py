"""Real-file debug failure and isolated repair; no model or financial data."""
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.store import get,put,all_items,uid,patch
from agent_service.execution import process_once
from agent_service import provider


def test_identical_source_revision_can_be_repaired_in_isolated_copy(monkeypatch):
    from agent_service import business_flow_repair,project_data,sources
    from agent_service.actor_context import actor_scope
    from agent_service.jobs import get_job
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'','model':'','share_data':True})
    monkeypatch.setattr(business_flow_repair,'enqueue_diagnostic',lambda job:None)
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    def execute_until(predicate):
        # Other tests may leave independent queued jobs in the temporary test
        # database. Wait for this test's own job, not whichever job is first.
        for _ in range(100):
            if predicate():return
            assert process_once()
        raise AssertionError('所需测试任务没有完成')
    def req(method,url,**kw):
        response=client.request(method,url,headers=headers,**kw);assert response.status_code==200,response.text;return response.json()
    pid=req('POST','/api/projects',json={'name':'修复验证','request_id':uid()})['id'];base='/api/projects/'+pid
    file=req('POST',base+'/attachments',files={'file':('income.csv',b'name,amount\nA,18.00\n','text/csv')},data={'request_id':uid(),'local_processing':'true'})
    execute_until(lambda:get('source',file['id']).get('status')=='ready');source=get('source',file['id'])
    turn=req('POST',base+'/messages',json={'text':'读取这份资料','request_id':uid(),'source_id':source['id']})
    patch('turn',turn['id'],state='done',outcome='needs_input')
    doc={'name':'读取收入','inputs':[],'nodes':[{'id':'read','kind':'source_table','title':'读取资料',
        'config':{'source_id':source['id'],'revision':source['revision'],'content_hash':source['content_hash']}}],'outputs':['read']}
    flow=req('POST',base+'/business-flows',json={'request_id':uid(),'turn_id':turn['id'],'document':doc})
    url=base+'/business-flows/'+flow['id'];body={'request_id':uid(),'expected_digest':flow['candidate_digest'],'selected_nodes':['read'],'inputs':{}}
    job=req('POST',url+'/debug-runs',json=body)
    # A ready historical snapshot is necessary; an incomplete or absent one
    # must not be inferred from today's metadata.
    old=[v for v in all_items('source_version') if v.get('source_id')==source['id'] and v.get('revision')==source['revision']]
    assert len(old)==1
    put('source_version',{**source,'source_id':source['id'],'revision':source['revision']},old[0]['id'])
    real_read=project_data.read_input;changed=[]
    def update_during_read(*args,**kwargs):
        result=real_read(*args,**kwargs)
        if not changed:changed.append(sources.revision(get('source',source['id'])))
        return result
    monkeypatch.setattr(project_data,'read_input',update_during_read)
    execute_until(lambda:get_job(job['run_id'])['status'] in ('failed','succeeded','cancelled'));failed=req('GET',url+'/debug-runs/'+job['run_id'])
    assert failed['status']=='failed' and failed['steps'][0]['error_code']=='SOURCE_VERSION_CHANGED'
    assert failed['auto_repair']['publishable'] is False
    assert failed['auto_repair']['status'] in ('queued','running','retrying')
    from concurrent.futures import ThreadPoolExecutor
    def attempt():
        with actor_scope('owner'):return business_flow_repair.rebind_identical_source_debug(get_job(job['run_id']))
    with ThreadPoolExecutor(max_workers=2) as pool:
        repair,again=list(pool.map(lambda _:attempt(),range(2)))
    assert repair and repair==again and repair['publishable'] is False
    assert repair['flow_id']!=flow['id']
    assert req('GET',url)['candidate_digest']==flow['candidate_digest']
    execute_until(lambda:get_job(repair['run_id'])['status'] in ('failed','succeeded','cancelled'))
    assert req('GET',url+'/debug-runs/'+job['run_id'])['auto_repair']['status']=='succeeded'
    newbase=base+'/business-flows/'+repair['flow_id'];new=req('GET',newbase+'/debug-runs/'+repair['run_id'])
    assert new['status']=='succeeded' and new['publishable'] is False
    assert req('GET',newbase+'/debug-runs/'+repair['run_id']+'/tables/read')['rows'][0]['c_2']=='18.00'
