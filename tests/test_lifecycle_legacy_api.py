"""Real legacy API entry points must honor project removal and restoration."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sql_delete

from agent_service import api,jobs
from agent_service.config import TOKEN
from agent_service.store import all_items,delete,get,now,put,uid,engine


H={'Authorization':'Bearer '+TOKEN}


@pytest.fixture
def legacy(monkeypatch):
    kinds=('project','source','pipeline','view','data_workflow','report_connector','work_action')
    previous={kind:{row['id'] for row in all_items(kind)} for kind in kinds}
    old_jobs={row['id'] for row in jobs.list_jobs(limit=100000)}
    live=put('project',{'name':'经营分析','created_at':now()},'p_'+uid())
    removed=put('project',{'name':'回收项目','created_at':now()},'p_'+uid())
    def source(project):
        return put('source',{'project_id':project['id'],'name':'收入.csv','kind':'file','status':'ready'},'s_'+uid())
    a=source(live);b=source(removed)
    def pipeline(source,**extra):
        return put('pipeline',{'name':'旧版汇总','source_id':source['id'],'source_ids':[source['id']],
            'commit':'frozen-test-commit',**extra},'pl_'+uid())
    # A legacy pipeline may predate its own project_id. Its source still defines
    # the lifecycle scope, and a join can introduce a second removed project.
    pa=pipeline(a,project_id=live['id']);pb=pipeline(b)
    joined=pipeline(a,project_id=live['id'],source_ids=[a['id'],b['id']])
    va=put('view',{'pipeline_id':pa['id'],'name':'现有分析','sql':'select 1','chart':'table'},'v_'+uid())
    vb=put('view',{'pipeline_id':pb['id'],'name':'已删项目分析','sql':'select 1','chart':'table'},'v_'+uid())
    client=TestClient(api.app)
    value={'client':client,'live':live,'removed':removed,'a':a,'b':b,'pa':pa,'pb':pb,'joined':joined,'va':va,'vb':vb}
    yield value
    with engine.begin() as connection:
        for row in jobs.list_jobs(limit=100000):
            if row['id'] not in old_jobs:connection.execute(sql_delete(jobs.jobs).where(jobs.jobs.c.id==row['id']))
    for kind in reversed(kinds):
        for row in all_items(kind):
            if row['id'] not in previous[kind]:delete(kind,row['id'])


def remove(value):
    response=value['client'].delete('/api/projects/'+value['removed']['id'],headers=H)
    assert response.status_code==200,response.text


def test_global_lists_hide_removed_projects_and_restore_reveals_existing_records(legacy):
    client=legacy['client'];remove(legacy)
    assert client.get('/api/sources').status_code==401
    sources={row['id'] for row in client.get('/api/sources',headers=H).json()}
    pipelines={row['id'] for row in client.get('/api/pipelines',headers=H).json()}
    views={row['id'] for row in client.get('/api/views',headers=H).json()}
    assert legacy['a']['id'] in sources and legacy['b']['id'] not in sources
    assert legacy['pa']['id'] in pipelines and legacy['pb']['id'] not in pipelines
    assert legacy['joined']['id'] not in pipelines
    assert legacy['va']['id'] in views and legacy['vb']['id'] not in views
    restored=client.post('/api/projects/'+legacy['removed']['id']+'/restore',headers=H)
    assert restored.status_code==200
    assert legacy['b']['id'] in {row['id'] for row in client.get('/api/sources',headers=H).json()}
    assert legacy['pb']['id'] in {row['id'] for row in client.get('/api/pipelines',headers=H).json()}
    assert legacy['vb']['id'] in {row['id'] for row in client.get('/api/views',headers=H).json()}


def test_pipeline_body_primary_and_join_sources_checked_before_codegen_or_enqueue(legacy,monkeypatch):
    client=legacy['client'];remove(legacy);called=[]
    def generate(source,name,spec,**kwargs):
        called.append(source['id'])
        return put('pipeline',{'name':name,'source_id':source['id'],'project_id':source['project_id'],'commit':'checked'},'pl_'+uid())
    monkeypatch.setattr(api,'generate',generate)
    monkeypatch.setattr(api,'enqueue',lambda *args,**kwargs:called.append('enqueue'))
    def body(source_id,join=None):
        spec={'joins':[{'source_id':join,'left_field':'office','right_field':'office'}]} if join else {}
        return {'name':'旧入口加工','source_id':source_id,'spec':spec}
    root='/api/pipelines'
    assert client.post(root,headers=H,json=body(legacy['b']['id'])).status_code==410
    assert client.post(root,headers=H,json=body(legacy['a']['id'],legacy['b']['id'])).status_code==410
    assert client.put(root+'/'+legacy['pa']['id'],headers=H,json=body(legacy['a']['id'],legacy['b']['id'])).status_code==410
    assert client.put(root+'/'+legacy['pb']['id'],headers=H,json=body(legacy['b']['id'])).status_code==410
    assert not called
    accepted=client.post(root,headers=H,json=body(legacy['a']['id']))
    assert accepted.status_code==200,accepted.text
    assert called==[legacy['a']['id'],'enqueue']


def test_view_body_and_legacy_run_urls_check_ancestry_before_work(legacy,monkeypatch,tmp_path):
    client=legacy['client'];remove(legacy);calls=[]
    monkeypatch.setattr(api,'published',lambda pid:(calls.append(pid),tmp_path,{'commit':'frozen'}))
    monkeypatch.setattr(api,'read_query',lambda folder,sql:calls.append(sql))
    denied=client.post('/api/views',headers=H,json={'name':'不应执行','pipeline_id':legacy['pb']['id'],'sql':'select 1'})
    assert denied.status_code==410 and not calls
    assert client.post('/api/pipelines/'+legacy['pb']['id']+'/run',headers=H).status_code==410
    assert client.post('/api/views/'+legacy['vb']['id']+'/run',headers=H).status_code==410
    accepted=client.post('/api/views',headers=H,json={'name':'可用分析','pipeline_id':legacy['pa']['id'],'sql':'select 1'})
    assert accepted.status_code==200,accepted.text
    assert calls==[legacy['pa']['id'],'select 1']


@pytest.mark.parametrize('kind,target_kind',[('probe','source'),('profile','source'),('pipeline','pipeline'),
    ('data_workflow','data_workflow'),('work_action','work_action'),('report_fetch','report_connector')])
def test_run_filters_and_retry_trace_each_job_target_kind(legacy,kind,target_kind):
    client=legacy['client']
    if target_kind=='source':bad_target=legacy['b']
    elif target_kind=='pipeline':bad_target=legacy['pb']
    else:bad_target=put(target_kind,{'project_id':legacy['removed']['id']},'target_'+uid())
    bad=jobs.save({'kind':kind,'target':bad_target['id'],'status':'failed'},'j_'+uid())
    remove(legacy)
    rows=client.get('/api/runs',headers=H,params={'target':bad_target['id']})
    assert rows.status_code==200 and rows.json()==[]
    assert client.get('/api/runs/'+bad['id'],headers=H).status_code==410
    assert client.post('/api/runs/'+bad['id']+'/retry',headers=H).status_code==410
    assert jobs.get_job(bad['id'])['status']=='failed'


def test_run_pagination_applies_after_removed_projects_are_filtered(legacy):
    client=legacy['client'];sequence=[]
    # Newest-first: live2, removed2, live1, removed1. A hidden record must not
    # consume the caller's visible offset or leave an otherwise full page short.
    for index,target in enumerate((legacy['pb']['id'],legacy['pa']['id'],legacy['pb']['id'],legacy['pa']['id'])):
        sequence.append(jobs.save({'kind':'pipeline','target':target,'status':'failed',
            'created_at':f'2099-01-01T00:00:0{index}+00:00'},'j_'+uid()))
    remove(legacy)
    first=client.get('/api/runs',headers=H,params={'limit':1}).json()
    second=client.get('/api/runs',headers=H,params={'limit':1,'offset':1}).json()
    assert first[0]['id']==sequence[3]['id']
    assert second[0]['id']==sequence[1]['id']
