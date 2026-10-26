import json
from datetime import datetime,timezone,timedelta
import httpx,pytest
from sqlalchemy import select
from fastapi.testclient import TestClient
from agent_service import company_search as kb,provider,agent
from agent_service.api import app
from agent_service.store import get,put,patch,all_items,uid,now,items,engine
from agent_service.config import TOKEN

client=TestClient(app);H={'Authorization':'Bearer '+TOKEN}

@pytest.fixture
def fixture(monkeypatch):
    pid='p_'+uid();put('project',{'name':'公司知识','created_at':now()},pid)
    monkeypatch.setenv('API_ALLOWED_HOSTS','company.test')
    monkeypatch.setattr('agent_service.connectors.safe_api_url',lambda url:'127.0.0.1')
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://approved-model.test/v1','model':'company-approved','api_key':'model-secret','share_data':False})
    state={'requests':[],'code':200,'rows':[{'id':'policy-1','title':'差旅报销管理办法','url':'https://company.test/doc/1','content':'员工差旅报销必须提交审批单。住宿限额按城市等级执行。','snippet':'内部财务制度摘要','number':'财务〔2026〕1号','status':'有效','published_at':'2026-10-01','version':'v1'}]}
    def respond(request):
        state['requests'].append(request)
        assert request.headers['Authorization']=='Bearer company-secret'
        assert request.headers['Host']=='company.test'
        if state['code']!=200:return httpx.Response(state['code'],json={'secret':'never-expose-upstream-errors'})
        body=json.loads(request.content) if request.method=='POST' else dict(request.url.params)
        if '/document/' in request.url.path:return httpx.Response(200,json=state['rows'][0])
        return httpx.Response(200,json={'data':{'items':state['rows']},'total':len(state['rows'])})
    original=httpx.Client
    monkeypatch.setattr(kb.httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(respond)))
    config=kb.CompanySearchConfig(search_url='https://company.test/search',detail_url='https://company.test/document/{id}',headers={'Authorization':'Bearer company-secret'},results_path='data.items',principal_label='employee-1',user_scoped_ack=True,storage_approved=True,model_approved=True)
    kb.configure(pid,config)
    yield pid,state,config
    for t in all_items('turn'):
        if t['project_id']==pid and t['state'] not in ('done','failed'):patch('turn',t['id'],state='done')

def turn(pid,text='根据公司发文解释差旅报销',mode='work'):
    tid='t_'+uid();return put('turn',{'project_id':pid,'text':text,'interaction_mode':mode,'state':'done','created_at':now()},tid)

def test_real_mapping_and_secret_free_status_encrypted_cache(fixture,monkeypatch):
    pid,state,_=fixture
    s=kb.search(pid,kb.SearchInput(query='差旅'))
    assert s['returned']==1 and s['results'][0]['number']=='财务〔2026〕1号'
    eid=s['results'][0]['evidence_id'];doc=kb.read(pid,eid)
    assert '必须提交审批单' in doc['content'] and doc['scope']=='full_text'
    monkeypatch.setenv('ATLAS_OPERATOR_TOKEN','operator-only-company-config-test')
    response=client.get(f'/api/projects/{pid}/company-search',headers={**H,'X-Atlas-Operator-Token':'operator-only-company-config-test'})
    assert response.status_code==200
    public=response.json()
    assert 'company-secret' not in json.dumps(public) and 'model-secret' not in json.dumps(public)
    record=get('company_evidence',eid)
    assert '必须提交审批单' not in json.dumps(record) and '差旅报销管理办法' not in json.dumps(record)
    assert state['requests'][-1].url.host=='127.0.0.1'

def test_post_body_mapping_is_typed_and_paged(fixture):
    pid,state,c=fixture
    c=c.model_copy(update={'method':'POST','query_field':'search.keyword','limit_field':'page.size','offset_field':'page.offset','parameters':{'department':'finance'}})
    kb.configure(pid,c);kb.search(pid,kb.SearchInput(query='a" & x=1',limit=3,offset=6))
    body=json.loads(state['requests'][-1].content)
    assert body=={'department':'finance','search':{'keyword':'a" & x=1'},'page':{'size':3,'offset':6}}

def test_snippet_upgrades_to_real_detail_without_infinite_stale_loop(fixture):
    pid,state,_=fixture
    state['rows'][0].pop('content')
    s=kb.search(pid,kb.SearchInput(query='差旅'));eid=s['results'][0]['evidence_id']
    state['rows'][0]['content']='员工差旅报销必须提交审批单。住宿限额按城市等级执行。'
    t=turn(pid);result=kb.read_for_model(t,kb.EvidenceInput(evidence_id=eid))
    assert result['evidence_id']!=eid and result['scope']=='full_text'
    t=get('turn',t['id']);docs=kb.for_model(t)
    assert docs[0]['content'].startswith('员工差旅') and docs[0]['evidence_id'] in t['knowledge_read_refs']

@pytest.mark.parametrize('code',[401,403,404,429,500,302])
def test_permission_network_failures_never_reuse_stale_evidence(fixture,code):
    pid,state,_=fixture;eid=kb.search(pid,kb.SearchInput(query='差旅'))['results'][0]['evidence_id']
    state['code']=code
    with pytest.raises(kb.CompanySearchError) as error:kb.read(pid,eid)
    assert 'never-expose' not in str(error.value)
    assert get('company_evidence',eid)['accessible'] is False

def test_changed_document_requires_new_evidence_and_does_not_silently_update(fixture):
    pid,state,_=fixture;eid=kb.search(pid,kb.SearchInput(query='差旅'))['results'][0]['evidence_id']
    state['rows'][0]['version']='v2';state['rows'][0]['content']='制度变更，需重新确认业务口径。'
    with pytest.raises(kb.CompanySearchError,match='已变化'):kb.read(pid,eid)
    new=kb.search(pid,kb.SearchInput(query='差旅'))['results'][0]['evidence_id']
    assert new!=eid and kb.read(pid,new)['version']=='v2'

def test_cross_project_and_reconfigured_principal_cannot_reuse_evidence(fixture):
    pid,state,c=fixture;eid=kb.search(pid,kb.SearchInput(query='差旅'))['results'][0]['evidence_id']
    other='p_'+uid();put('project',{'name':'其他'},other)
    with pytest.raises(ValueError,match='不属于本项目'):kb.read(other,eid)
    kb.configure(pid,c.model_copy(update={'principal_label':'employee-2'}))
    with pytest.raises(ValueError,match='账号已变更'):kb.read(pid,eid)

def test_model_access_is_separate_and_revoked_on_model_change(fixture,monkeypatch):
    pid,state,c=fixture
    kb.configure(pid,c.model_copy(update={'model_approved':False}))
    t=turn(pid)
    with provider.task_scope(t['id']),pytest.raises(ValueError,match='批准当前模型'):provider.complete([{'role':'user','content':'private internal data'}])
    assert kb.search(pid,kb.SearchInput(query='差旅'))['returned']==1
    kb.configure(pid,c)
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://other-model.test','model':'other'})
    with pytest.raises(ValueError):kb.assert_model_allowed(pid)
    assert not kb.status(pid)['model_allowed']

def test_citation_requires_exact_read_quote_and_matching_claim(fixture):
    pid,state,c=fixture;t=turn(pid)
    eid=kb.search(pid,kb.SearchInput(query='差旅'),t)['results'][0]['evidence_id']
    citation={'evidence_id':eid,'claim':'需要审批单','quote':'员工差旅报销必须提交审批单。'}
    with pytest.raises(ValueError,match='实际读取'):kb.validate_citations(get('turn',t['id']),[citation],'需要审批单')
    kb.read(pid,eid,t)
    assert kb.validate_citations(get('turn',t['id']),[citation],'需要审批单')[0]['content_hash']
    with pytest.raises(ValueError,match='不在原文'):kb.validate_citations(get('turn',t['id']),[dict(citation,quote='住宿费用没有任何限额。')],'需要审批单')
    with pytest.raises(ValueError,match='实际出现在回答'):kb.validate_citations(get('turn',t['id']),[citation],'相关制度尚需核对')

def test_company_turns_encrypted_at_rest_and_hide_answer_after_purge(fixture):
    from agent_service.conversation import finish,public_turn
    pid,state,_=fixture;t=turn(pid)
    eid=kb.search(pid,kb.SearchInput(query='差旅'),t)['results'][0]['evidence_id'];kb.read(pid,eid,t)
    patch('turn',t['id'],knowledge_citations=[{'evidence_id':eid}]);finish(get('turn',t['id']),'内部回答：需要审批单',outcome='answered')
    with engine.connect() as db:raw=db.execute(select(items.c.body).where(items.c.id==t['id'])).scalar()
    assert '需要审批单' not in raw and '公司发文' not in raw
    assert '需要审批单' in public_turn(get('turn',t['id']))['answer']
    kb.disconnect(pid)
    assert '已隐藏' in public_turn(get('turn',t['id']))['answer']
    with pytest.raises(ValueError):kb.assert_model_allowed(pid)

def test_expired_and_superseded_documents_cannot_generate_current_rules(fixture):
    pid,state,_=fixture
    state['rows'][0]['status']='废止';eid=kb.search(pid,kb.SearchInput(query='差旅'))['results'][0]['evidence_id']
    with pytest.raises(ValueError,match='当前有效原文'):kb.basis_from_ids(pid,[eid])
    patch('company_evidence',eid,expires_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
    with pytest.raises(ValueError,match='保存期限'):kb.read(pid,eid)
    assert kb.purge(pid,expired_only=True)==1

def test_unsafe_detail_origin_headers_and_links_rejected(fixture):
    pid,state,c=fixture
    with pytest.raises(ValueError,match='同源'):kb.configure(pid,c.model_copy(update={'detail_url':'https://evil.test/doc/{id}'}))
    state['rows'][0]['url']='javascript:alert(1)'
    assert kb.search(pid,kb.SearchInput(query='差旅'))['results'][0]['url'] is None
    state['rows'][0]['url']='https://company.test/doc/1?token=secret'
    assert kb.search(pid,kb.SearchInput(query='差旅'))['results'][0]['url'] is None

def test_search_budget_shared_across_resume_and_plaintext_trace_has_no_document(fixture):
    pid,state,_=fixture;t=turn(pid)
    for _ in range(8):value=kb.search_for_model(get('turn',t['id']),kb.SearchInput(query='差旅'))
    assert '差旅报销管理办法' not in json.dumps(value,ensure_ascii=False)
    resumed=turn(pid);patch('turn',resumed['id'],agent_origin=t['id'])
    with pytest.raises(ValueError,match='预算'):kb.search_for_model(get('turn',resumed['id']),kb.SearchInput(query='差旅'))

def test_detail_snippet_differences_do_not_invalidate_identical_source_text(fixture,monkeypatch):
    pid,state,c=fixture
    eid=kb.search(pid,kb.SearchInput(query='差旅'))['results'][0]['evidence_id']
    state['rows'][0]['snippet']='详情页没有搜索高亮摘要'
    assert kb.read(pid,eid)['content_hash']==get('company_evidence',eid)['content_hash']

def test_real_agent_search_read_build_cites_and_versions_evidence(fixture,monkeypatch):
    from agent_service import workspace
    from agent_service.conversation import tick,public_turn
    from agent_service.execution import process_once
    pid,state,c=fixture
    upload=client.post(f'/api/projects/{pid}/attachments',headers=H,files={'file':('expenses.csv','单号,员工,金额\nQ1,甲,10\nQ2,乙,20\n'.encode(),'text/csv')},data={'request_id':'knowledge-build-upload'})
    assert upload.status_code==200,upload.text
    sid=upload.json()['id'];assert process_once()
    source=get('source',sid);assert source['status']=='ready'
    calls=[]
    def complete(messages,**kwargs):
        info=json.loads(messages[1]['content']);calls.append(info)
        n=len(info['completed_steps'])
        if n==0:step={'kind':'tool','tool':'search_company_documents','arguments':{'query':'差旅报销'}}
        elif n==1:step={'kind':'tool','tool':'read_company_document','arguments':{'evidence_id':info['company_candidates'][0]['evidence_id']}}
        elif n==2:step={'kind':'tool','tool':'create_data_application','arguments':{'source_id':sid,'name':'报销明细','spec':source['proposed_spec']}}
        elif n==3:step={'kind':'tool','tool':'view_result'}
        else:step={'kind':'final','answer':'已生成报销明细候选。公司规定需要审批单，请核对后确认发布。','citations':[{'evidence_id':info['company_evidence'][0]['evidence_id'],'claim':'公司规定需要审批单','quote':'员工差旅报销必须提交审批单。'}]}
        return json.dumps(step,ensure_ascii=False)
    monkeypatch.setattr(provider,'complete',complete)
    response=client.post(f'/api/projects/{pid}/messages',headers=H,json={'text':'根据公司发文创建报销明细应用，先列明细并说明依据','request_id':'knowledge-agent-build','source_id':sid})
    assert response.status_code==200,response.text
    tid=response.json()['id']
    for _ in range(15):
        tick();t=get('turn',tid)
        if t['state']=='waiting_run':assert process_once()
        if t['state'] in ('done','failed'):break
    t=get('turn',tid)
    assert t['outcome']=='verified_complete',json.dumps(public_turn(t),ensure_ascii=False)
    assert public_turn(t)['knowledge_sources'][0]['title']=='差旅报销管理办法'
    pipeline=get('pipeline',get('project',pid)['pipeline_id'])
    assert pipeline['knowledge_basis'] and not pipeline.get('published_run')
    path=workspace.folder(pid)/'pipelines'/pipeline['id']/'knowledge-evidence.json'
    assert path.is_file() and '必须提交审批单' not in path.read_text()
    assert workspace.git(workspace.folder(pid),'show',t['work_version']['commit']+':pipelines/'+pipeline['id']+'/knowledge-evidence.json')
    from agent_service.jobs import get_job
    manifest=json.loads((__import__('agent_service.config',fromlist=['DATA']).DATA/get_job(pipeline['candidate_run'])['artifact_dir']/'output.json').read_text())
    assert manifest['knowledge_basis']==pipeline['knowledge_basis']
    assert calls[-1]['company_evidence'][0]['content'].startswith('员工差旅')
    assert kb.public_sources(t)

def test_generation_api_cannot_bypass_company_evidence_policy(fixture):
    from agent_service.execution import process_once
    pid,state,c=fixture
    upload=client.post(f'/api/projects/{pid}/attachments',headers=H,files={'file':('rows.csv',b'id,value\n1,10\n','text/csv')},data={'request_id':'knowledge-api-upload'})
    sid=upload.json()['id'];assert process_once()
    source=get('source',sid)
    denied=client.post('/api/pipelines',headers=H,json={'source_id':sid,'name':'bypass','spec':source['proposed_spec']})
    assert denied.status_code==400 and '公司原文' in denied.text

def test_truncated_original_cannot_become_a_complete_rule(fixture):
    pid,state,c=fixture;state['rows'][0]['content']='制度正文'*17000
    eid=kb.search(pid,kb.SearchInput(query='差旅'))['results'][0]['evidence_id']
    with pytest.raises(ValueError,match='完整'):kb.basis_from_ids(pid,[eid])

def test_local_work_history_remains_usable_without_model_approval(fixture,monkeypatch):
    from agent_service import workspace
    from agent_service.conversation import handle
    pid,state,c=fixture
    kb.configure(pid,c.model_copy(update={'model_approved':False}))
    workspace.checkpoint(pid)
    monkeypatch.setattr(provider,'complete',lambda *a,**k:pytest.fail('local history must not contact model'))
    t=turn(pid,'查看工作历史');handle(t)
    assert '项目的最近版本' in get('turn',t['id'])['answer']
