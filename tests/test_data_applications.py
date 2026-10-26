"""Actual application lifecycle: UI code, data, rebuilds, schedules and conversations."""
import io,json,zipfile
from decimal import Decimal
from datetime import datetime,timezone,timedelta
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.store import get,patch
from agent_service.conversation import tick
from agent_service.execution import process_once,queue_due
from agent_service import provider,workspace
from agent_service.codegen import git

client=TestClient(app);H={'Authorization':'Bearer '+TOKEN}
def call(method,url,**kwargs):
    r=client.request(method,url,headers=H,**kwargs);assert r.status_code==200,r.text
    return r.json()
def say(pid,text,key,**kwargs):return call('POST',f'/api/projects/{pid}/messages',json={'text':text,'request_id':key,**kwargs})['id']
def done(tid):
    for _ in range(20):
        tick();t=get('turn',tid)
        if t['state'] in ('done','failed'):return t
    raise AssertionError(get('turn',tid))
def tool(tool_name,**args):return {'kind':'tool','tool':tool_name,'arguments':args}
def final(text):return {'kind':'final','answer':text}


def test_data_application_generate_use_rerun_revise_schedule_and_question(monkeypatch):
    pid=call('POST','/api/projects',json={'name':'应用验收','request_id':'data-app-project'})['id']
    root='/api/projects/'+pid
    source=call('POST',root+'/attachments',files={'file':('orders.csv','订单号,客户,金额,退款,状态\nF1,甲,100.25,10.10,已付款\nF2,乙,20,0,待付款\nF3,乙,30,2,已付款\n'.encode(),'text/csv')},data={'request_id':'data-app-source'})
    assert process_once();s=get('source',source['id'])
    spec=dict(s['proposed_spec'],group_by='c_2',amount_field='m_net',amount_label='净收款',computations=[{'name':'m_net','label':'净收款','operation':'subtract','left_field':'c_3','right_field':'c_4'}],filters=[{'field':'c_5','operator':'eq','data_type':'text','value':'已付款'}])
    steps=[tool('source_schema',source_id=s['id']),tool('create_data_application',source_id=s['id'],name='收款追踪',spec=spec,application={'title':'收款追踪','description':'已付款订单金额减退款，按客户汇总','columns':['c_2','m_net'],'chart':'bar'}),tool('view_result'),final('数据和应用已生成，请确认采用。')]
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://model.invalid/v1','model':'simulated','api_key':'disposable','share_data':True})
    def complete(*args,**kwargs):
        assert steps,'unexpected model request'
        return json.dumps(steps.pop(0),ensure_ascii=False)
    monkeypatch.setattr(provider,'complete',complete)
    tid=say(pid,'做一个收款追踪应用，只统计已付款订单，金额减退款，按客户汇总，每天上午9点采集更新','data-app-create',source_id=s['id'])
    tick();tick();assert get('turn',tid)['state']=='waiting_run'
    assert process_once();t=done(tid);assert t['state']=='done',t
    aid=t['result']['application']['id'];url=root+'/applications/'+aid;p=get('pipeline',aid);repo=p['repository']
    assert t['work_version']['changed'] and t['result']['application']['name']=='收款追踪'
    assert (workspace.folder(pid)/'pipelines'/aid/'application/data-app.html').exists()
    preview=call('GET',url);assert preview['preview'] and preview['can_publish']
    assert preview['pending_schedule']=='0 9 * * *' and not preview['schedule']
    initial_run=preview['run_id'];first_commit=preview['result_commit']
    data=call('GET',url+'/data');assert data['total']==2 and Decimal(data['metric']['total'])==Decimal('118.15')
    assert [f['name'] for f in data['fields']]==['c_2','m_net']
    filtered=call('GET',url+'/data',params={'search':'甲'});assert filtered['total']==1 and Decimal(filtered['metric']['total'])==Decimal('90.15')
    assert len(filtered['summary'])==1 and filtered['summary'][0]['category']=='甲'
    assert call('GET',url+'/data',params={'search':"' OR 1=1 --"})['total']==0
    assert client.get(url+'/data',headers=H,params={'sort':'c_2;drop table x'}).status_code==400
    csv=client.get(url+'/export',headers=H,params={'search':'甲','run_id':initial_run}).content.decode('utf-8-sig')
    assert '甲' in csv and '乙' not in csv
    package=client.get(url+'/code.zip',headers=H).content
    with zipfile.ZipFile(io.BytesIO(package)) as z:
        assert 'application/data-app.html' in z.namelist() and 'dbt/models/business_objects.sql' in z.namelist()
        assert json.loads(z.read('application/application.json'))['config']['title']=='收款追踪'
        assert not any('secret_config' in n or n.endswith('.env') for n in z.namelist())
    steps.extend([tool('publish_result'),final('已采用应用。')]);published=done(say(pid,'确认发布','data-app-publish',application_id=aid))
    assert get('pipeline',aid)['cron']=='0 9 * * *' and not get('pipeline',aid).get('pending_schedule')
    assert not published['result']['candidate'] and not call('GET',url)['preview']
    # Historical preview links remain usable after their candidate is published.
    assert call('GET',url,params={'preview':True,'run_id':initial_run})['preview_type']=='published'
    # UI and processing revisions remain candidates over the old working application.
    runtime={'timeout_seconds':180,'max_attempts':1,'freshness_minutes':60};patch('pipeline',aid,runtime=runtime)
    steps.extend([tool('revise_data_application',application_id=aid,name='客户收款明细',application={'title':'客户收款明细','layout':'table','chart':'none','columns':['c_2']}),final('新应用界面已验证，原应用继续可用。')])
    revised=say(pid,'调整这个应用为客户收款明细，只显示客户字段','data-app-revise',application_id=aid)
    tick();assert get('turn',revised)['state']=='waiting_run'
    assert process_once();revision=done(revised);assert revision['state']=='done',revision
    p=get('pipeline',aid);assert p['repository']==repo and p['commit']!=first_commit and p['runtime']==runtime
    assert call('GET',url)['config']['title']=='收款追踪'
    assert call('GET',url,params={'preview':True})['config']['title']=='客户收款明细'
    changes=revision['result']['changes']
    assert changes['kind']=='compared' and changes['added']==changes['removed']==changes['changed']==0
    assert '应用页面' in changes['definition_changes'] and revision['result']['sources'][0]['name']=='orders.csv'
    assert call('GET',url+'/data',params={'preview':True})['fields'][0]['name']=='c_2'
    steps.extend([tool('publish_result'),final('调整后的应用已采用。')]);done(say(pid,'确认发布','data-app-publish-revision',application_id=aid))
    assert call('GET',url)['config']['title']=='客户收款明细'
    historical=call('GET',url,params={'run_id':initial_run});assert historical['preview_type']=='history' and not historical['can_publish']
    assert historical['config']['title']=='收款追踪'
    before=get('pipeline',aid)['published_run'];commit=get('pipeline',aid)['commit'];count=git(repo,'rev-list','--count','HEAD')
    steps.extend([tool('run_data_application',application_id=aid),tool('view_result'),final('应用重跑完成。')])
    rerun=say(pid,'重跑这个应用','data-app-rerun',application_id=aid);tick();assert process_once();result=done(rerun)
    assert result['state']=='done' and get('pipeline',aid)['published_run']!=before
    assert get('pipeline',aid)['commit']==commit and git(repo,'rev-list','--count','HEAD')==count
    assert call('GET',url+'/data')['total']==2
    steps.extend([tool('schedule_daily',pipeline_id=aid,hour=9),final('已安排每天更新。')]);done(say(pid,'每天上午9点更新','data-app-schedule',application_id=aid))
    assert call('GET',url)['schedule']=='0 9 * * *'
    patch('pipeline',aid,next_due=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat());queue_due();assert process_once()
    assert call('GET',url+'/data')['commit']==commit
    steps.extend([tool('pause_updates',pipeline_id=aid),final('已暂停自动更新。')]);done(say(pid,'暂停自动更新','data-app-pause',application_id=aid))
    # Read-only app chat resolves its target without changing project focus.
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'','model':'','api_key':'','share_data':False})
    patch('project',pid,pipeline_id=None,source_id=None)
    answer=done(say(pid,'这个应用有多少条记录','data-app-question',interaction_mode='chat',application_id=aid))
    assert '2 条' in answer['answer'] and not answer.get('work_version')
    assert get('project',pid)['pipeline_id'] is None
    # Failure preserves the working result, and application identity belongs to one project.
    other=call('POST','/api/projects',json={'name':'另一个项目','request_id':'data-app-other-project'})['id']
    assert client.get('/api/projects/'+other+'/applications/'+aid,headers=H).status_code==400
    assert client.post('/api/projects/'+other+'/messages',headers=H,json={'text':'重跑','request_id':'data-app-invalid-bound','application_id':aid}).status_code==400
    assert client.get(url+'/data').status_code==401
    bad=dict(p['spec']);r=client.put('/api/pipelines/'+aid,headers=H,json={'source_id':s['id'],'name':'bad','spec':bad,'application':{'columns':['unknown']}})
    assert r.status_code==400 and git(repo,'diff','--name-only')==''
    assert call('GET',url+'/data')['total']==2

    # Project rollback restores the app interface together with its working data/code.
    old_version=published['work_version']['commit']
    recovered=done(say(pid,'回退到 '+old_version,'data-app-restore'))
    assert recovered['state']=='done'
    restored=call('GET',url)
    assert restored['config']['title']=='收款追踪' and restored['run_id']==initial_run
    assert get('pipeline',aid)['commit']==first_commit

    # Creating another app is distinct from revising the selected app.
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'https://model.invalid/v1','model':'simulated','api_key':'disposable','share_data':True})
    steps.extend([tool('create_data_application',source_id=s['id'],name='第二个收款应用',spec=spec),final('第二个应用已生成，原应用保留。')])
    second=say(pid,'另做一个收款应用','data-app-create-another',source_id=s['id'])
    tick();assert process_once();another=done(second)
    second_id=another['result']['application']['id']
    assert second_id!=aid and get('pipeline',aid)['commit']==first_commit
    assert len(call('GET',root+'/applications'))==2
    # Cross-session focus stays on its app even if another app becomes current.
    original_session=call('POST',root+'/sessions',json={'request_id':'data-app-return-original'})['id']
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'','model':'','api_key':'','share_data':False})
    question=done(say(pid,'这个应用有多少条记录','data-app-session-bound',application_id=aid,session_id=original_session,interaction_mode='chat'))
    assert question['result']['pipeline_id']==aid and '2 条' in question['answer']
    remembered=done(say(pid,'这个应用有多少条记录','data-app-session-remembered',session_id=original_session,interaction_mode='chat'))
    assert remembered['result']['pipeline_id']==aid
    exported=done(say(pid,'导出结果','data-app-pinned-export',session_id=original_session))
    assert exported['download']['url'].endswith('run_id='+initial_run)
