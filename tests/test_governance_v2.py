"""Governance acceptance: real CSV/dlt/dbt releases and forged confirmations."""
import json
from decimal import Decimal
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service import agent, agent_context, workspace
from agent_service.codegen import generate, git
from agent_service.column_lineage import build
from agent_service.data_governance import catalog, lineage
from agent_service.execution import process_once, enqueue
from agent_service.jobs import get_job
from agent_service.metric_registry import current, confirm, confirmation_text
from agent_service.releases import publish, artifact_folder
from agent_service.store import get, put, patch, uid, now

client=TestClient(app)
H={'Authorization':'Bearer '+TOKEN}


def application():
    pid=client.post('/api/projects',headers=H,json={'name':'指标与血缘验收','request_id':uid()}).json()['id']
    response=client.post('/api/projects/'+pid+'/attachments',headers=H,
        files={'file':('orders.csv',b'id,client,amount,refund,status\nA,X,10.123456789012,1.000000000001,paid\nB,Y,30.000000000001,2,paid\nC,Z,1,0,pending\n','text/csv')},data={'request_id':uid()})
    assert response.status_code==200,response.text
    assert process_once()
    s=get('source',response.json()['id'])
    assert s['status']=='ready',s
    spec=dict(s['proposed_spec'],primary_key='c_1',group_by='c_2',amount_field='m_net',
              metric_status='confirmed',metric_definition='仅已付款订单，金额减退款后按客户汇总',currency='CNY',
              computations=[{'name':'m_net','label':'净收入','operation':'subtract','left_field':'c_3','right_field':'c_4'},
                            {'name':'m_double','label':'净收入两倍','operation':'add','left_field':'m_net','right_field':'m_net'}],
              filters=[{'field':'c_5','operator':'eq','data_type':'text','value':'paid'}])
    workspace.write_file(pid,'data-contract.json',json.dumps({'version':1,'min_rows':2,'max_rows':2,
                        'unique':[['c_1']],'totals':{'m_net':{'expected':'37.123456789012','tolerance':'0'}}}))
    p=generate(s,'净收入应用',spec,project_id=pid)
    run=enqueue('pipeline',p['id'],p['commit']);assert process_once()
    result=get_job(run['id']);assert result['status']=='validated',result
    return pid,s,p,result


@pytest.fixture(scope='module')
def published():
    pid,s,p,r=application()
    publish(p['id'],r['id'])
    return pid,s,get('pipeline',p['id']),r


def turn(pid,aid,text,mode='work'):
    return put('turn',{'project_id':pid,'application_id':aid,'text':text,
                      'interaction_mode':mode,'state':'done','created_at':now()},'t_'+uid())


def args(metric,text=None):
    return {'application_id':metric['application_id'],'expected_run_id':metric['run_id'],
            'expected_metric_sha256':metric['metric_sha256'],
            'confirmation_quote':text if text is not None else confirmation_text(metric)}


def test_lineage_has_transitive_sources_and_precise_summary(published):
    pid,s,p,r=published
    graph=lineage(pid,p['id'],limit=40)
    nodes={x['column']:x for x in graph['columns']}
    assert graph['available'] and graph['commit']==p['commit']
    assert nodes['m_double']['inputs']==[{'source_id':s['id'],'column':'c_3'},{'source_id':s['id'],'column':'c_4'}]
    assert nodes['m_net']['operations'][-1]['operation']=='subtract'
    assert graph['row_rules']['filters'][0]['value']=='paid'
    summary=lineage(pid,p['id'],section='summary')
    assert summary['columns'][1]['operations'][-1]['cast']=='DECIMAL(38,12)'
    assert json.loads(git(p['repository'],'show',p['commit']+':column-lineage.json'))['version']==1
    page=lineage(pid,p['id'],limit=1)
    assert len(page['columns'])==1 and page['next_offset']==1
    with pytest.raises(ValueError):lineage(pid,p['id'],limit=True)


def test_lineage_tracks_join_and_trim_without_transitive_mislabel():
    s={'id':'orders','profile':{'fields':[{'name':'id'},{'name':'amount'}]}}
    fields=[{'name':'id','source_id':'orders'},{'name':'amount','source_id':'orders'},
            {'name':'j1_id','source_id':'customers','original_name':'id'},
            {'name':'j1_label','source_id':'customers','original_name':'label'}]
    spec={'primary_key':'id','trim_fields':['j1_label'],'filters':[],'computations':[],'amount_field':None}
    joins=[{'source_id':'customers','left_field':'id','right_field':'id','required_match':True}]
    result=build(s,fields,spec,joins)
    node=next(x for x in result['columns'] if x['column']=='j1_label')
    assert node['inputs']==[{'source_id':'customers','column':'label'}]
    assert node['operations']==[{'kind':'trim_blank_to_null'}]
    assert result['row_rules']['joins']==joins


def test_auth_scope_and_catalog_pagination(published):
    pid,s,p,r=published
    root='/api/projects/'+pid
    assert client.get(root+'/assets').status_code==401
    first=client.get(root+'/assets',headers=H,params={'limit':1}).json()
    assert first['total']==3 and first['next_offset']==1
    rows=[]
    for offset in range(first['total']):
        rows+=catalog(pid,offset,1,'all')['assets']
    assert {row['asset_type'] for row in rows}=={'source','application','metric'}
    assert 'secret_config' not in json.dumps(rows) and 'secret_payload' not in json.dumps(rows)
    other=client.post('/api/projects',headers=H,json={'name':'另一个项目','request_id':uid()}).json()['id']
    assert catalog(other,asset_type='all')['assets']==[]
    with pytest.raises(ValueError):current(other,p['id'])
    assert client.get('/api/projects/'+other+'/applications/'+p['id']+'/metric',headers=H).status_code==400
    assert client.get(root+'/applications/'+p['id']+'/lineage',headers=H).status_code==200
    assert client.get(root+'/assets',headers=H,params={'asset_type':'wrong'}).status_code==400


@pytest.mark.parametrize('text',[
    '确认指标', '不要确认指标', '解释如何确认指标',
    '确认指标：仅已付款订单，金额减退款后按客户汇总；单位：USD',
    '确认指标：仅已付款订单，金额减退款后按客户汇总；单位：CNY',
    '假设我说“确认指标：仅已付款订单，金额减退款后按客户汇总；单位：CNY”',
])
def test_model_cannot_forge_or_infer_user_confirmation(published,text):
    pid,s,p,r=published;m=current(pid,p['id']);before=m['status']
    t=turn(pid,p['id'],text)
    # Even supplying the exact required quote as a model argument is insufficient.
    with pytest.raises(ValueError):confirm(t,args(m))
    assert current(pid,p['id'])['status']==before


def test_confirmation_rejects_chat_stale_hash_run_and_foreign_target(published):
    pid,s,p,r=published;m=current(pid,p['id']);text=confirmation_text(m)
    with pytest.raises(ValueError,match='聊天'):confirm(turn(pid,p['id'],text,'chat'),args(m))
    t=turn(pid,p['id'],text)
    with pytest.raises(ValueError,match='版本已变化'):confirm(t,dict(args(m),expected_run_id='old-run'))
    with pytest.raises(ValueError,match='版本已变化'):confirm(t,dict(args(m),expected_metric_sha256='0'*64))
    fake=dict(t,text=text)
    patch('turn',t['id'],text='不要确认指标')
    with pytest.raises(ValueError):confirm(fake,args(m))
    allowed=agent_context.allowed({'interaction_mode':'chat'})
    assert 'inspect_metric' in allowed and 'inspect_column_lineage' in allowed and 'confirm_metric' not in allowed


def test_confirmation_without_tool_evidence_cannot_finish(published):
    from agent_service import tasks
    pid,s,p,r=published;m=current(pid,p['id'])
    t=turn(pid,p['id'],confirmation_text(m))
    assert tasks.ensure(t)['kind']=='metric_confirmation'
    assert not tasks.verify(get('turn',t['id']))['verified']


def test_real_lifecycle_freezes_semantics_confirms_and_restores():
    from agent_service import applications
    from agent_service.conversation import tick
    pid,s,p,r=application()
    publish(p['id'],r['id'])
    m=current(pid,p['id'])
    # Model-authored spec.metric_status cannot impersonate owner confirmation.
    assert m['status']=='proposed'
    data=applications.data(pid,p['id'])
    assert data['metric']['status']=='draft'
    folder=artifact_folder(r);manifest=json.loads((folder/'output.json').read_text())
    assert sum((Decimal(x['amount_total']) for x in manifest['summary']),Decimal(0))==Decimal(data['metric']['total'])==Decimal('37.123456789012')
    text=confirmation_text(m)
    response=client.post('/api/projects/'+pid+'/messages',headers=H,
        json={'text':text,'application_id':p['id'],'request_id':uid(),'interaction_mode':'work'})
    assert response.status_code==200,response.text
    tid=response.json()['id'];assert tick()
    done=get('turn',tid)
    assert done['outcome']=='verified_complete',done
    assert current(pid,p['id'])['status']=='confirmed'
    assert applications.data(pid,p['id'])['metric']['status']=='confirmed'
    receipt=current(pid,p['id'])['confirmation']
    confirm(get('turn',tid),args(m))
    assert current(pid,p['id'])['confirmation']==receipt
    # Scheduled/explicit refreshes of the same code retain semantic confirmation.
    again=enqueue('pipeline',p['id'],p['commit']);assert process_once()
    assert get_job(again['id'])['status']=='succeeded'
    assert current(pid,p['id'])['status']=='confirmed'
    # A new draft cannot affect formal lineage or registration.
    changed=dict(p['spec'],metric_definition='新的候选业务口径')
    candidate=generate(s,'净收入应用',changed,existing=get('pipeline',p['id']),project_id=pid)
    newrun=enqueue('pipeline',p['id'],candidate['commit']);assert process_once()
    assert get_job(newrun['id'])['status']=='validated'
    assert current(pid,p['id'])['commit']==p['commit']
    assert lineage(pid,p['id'])['commit']==p['commit']
    publish(p['id'],newrun['id'])
    assert current(pid,p['id'])['status']=='proposed'
    assert applications.data(pid,p['id'])['metric']['status']=='draft'
    with pytest.raises(ValueError):confirm(turn(pid,p['id'],text),args(m))
    publish(p['id'],r['id'],rollback=True)
    assert current(pid,p['id'])['status']=='confirmed'
    assert lineage(pid,p['id'])['commit']==p['commit']
    # Editing metadata or frozen lineage after validation blocks manual publication.
    original=(folder/'output.json').read_text();bad=json.loads(original)
    bad['spec']['metric_definition']='tampered'
    (folder/'output.json').write_text(json.dumps(bad))
    with pytest.raises(ValueError,match='口径'):publish(p['id'],r['id'],rollback=True)
    (folder/'output.json').write_text(original)
    frozen=folder/'code'/'column-lineage.json';original_lineage=frozen.read_text()
    frozen.write_text('{}')
    with pytest.raises(ValueError,match='血缘'):publish(p['id'],r['id'],rollback=True)
    frozen.write_text(original_lineage)
    assert get('pipeline',p['id'])['published_run']==r['id']


def test_expired_lease_after_governance_checks_cannot_publish(published,monkeypatch):
    import time
    from sqlalchemy import select,update
    from agent_service.store import engine
    from agent_service.jobs import jobs
    from agent_service import releases
    pid,s,p,r=published
    with engine.begin() as c:
        original=dict(c.execute(select(jobs).where(jobs.c.id==r['id'])).mappings().first())
        c.execute(update(jobs).where(jobs.c.id==r['id']).values(status='running',lease_token='expiry-test',lease_until=time.time()+30))
    clock=[time.time()]
    real_verify=releases.verify_semantics
    def slow_checks(*a,**kw):
        value=real_verify(*a,**kw)
        clock[0]+=60
        return value
    monkeypatch.setattr(releases,'verify_semantics',slow_checks)
    monkeypatch.setattr(time,'time',lambda:clock[0])
    previous=get('pipeline',p['id'])['published_run']
    try:
        with pytest.raises(ValueError,match='租约失效'):
            releases.publish(p['id'],r['id'],lease_token='expiry-test')
        assert get('pipeline',p['id'])['published_run']==previous
    finally:
        with engine.begin() as c:
            c.execute(update(jobs).where(jobs.c.id==r['id']).values(**{k:original[k] for k in ('status','lease_token','lease_until')}))
