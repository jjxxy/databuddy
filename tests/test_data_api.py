import copy,json
from fastapi.testclient import TestClient
from agent_service.api import app
from agent_service.config import TOKEN
from agent_service import project_data as data
from agent_service.store import get,uid,patch
from test_project_data import project,source,document,registered,run

client=TestClient(app);H={'Authorization':'Bearer '+TOKEN}


def test_data_api_auth_full_values_edit_conflict_and_complete_download():
    pid=project();s=source(pid);wid=registered(pid,document(s['id']));r=run(pid,wid)
    root='/api/projects/'+pid
    assert client.get(root+'/data-workflows').status_code==401
    info=client.get(root+'/data-workflows/'+wid,headers=H).json()
    assert info['workflow_id']==wid and len(info['steps'])==1
    page=client.get(root+'/data-results/'+r['id'],headers=H,params={'offset':214,'limit':1}).json()
    assert page['rows'][0]['amount']=='215.000000000001' and page['total']==215
    wrong=client.put(root+'/data-workflows/'+wid,headers=H,json={'expected_sha256':'0'*64,'document':info['document']})
    assert wrong.status_code==409
    changed=copy.deepcopy(info['document']);changed['plan']['name']='已核对订单'
    saved=client.put(root+'/data-workflows/'+wid,headers=H,json={'expected_sha256':info['draft_sha256'],'document':changed})
    assert saved.status_code==200,saved.text
    assert client.post(root+'/data-results/'+r['id']+'/publish',headers=H,json={'expected_plan_sha256':r['commit']}).status_code==400
    assert client.get(root+'/data-results/'+r['id']+'/download',headers=H,params={'format':'handoff'}).content.startswith(b'PK')
    assert client.get(root+'/data-results/'+r['id']+'/download',headers=H,params={'format':'xlsx'}).content.startswith(b'PK')
    import csv,io
    from decimal import Decimal
    from test_project_data import COUNT,TOTAL
    assert client.get(root+'/data-results/'+r['id']+'/download',params={'format':'csv'}).status_code==401
    csv_response=client.get(root+'/data-results/'+r['id']+'/download',headers=H,params={'format':'csv'})
    assert csv_response.status_code==200
    full=list(csv.DictReader(io.StringIO(csv_response.content.decode('utf-8-sig'))))
    assert len(full)==COUNT and sum(Decimal(row['amount']) for row in full)==Decimal(TOTAL)
    other=project()
    assert client.get('/api/projects/'+other+'/data-results/'+r['id'],headers=H).status_code==400


def test_agent_workflow_waits_for_real_queue_and_has_evidence(monkeypatch):
    from agent_service import agent,tasks
    from agent_service.execution import process_once
    from agent_service.store import put,now
    from agent_service.data_tools import resume
    pid=project();s=source(pid);wid=registered(pid,document(s['id']))
    t=put('turn',{'project_id':pid,'text':'帮我整理并加工订单数据','interaction_mode':'work','state':'agent','created_at':now()},'t_'+uid())
    tasks.ensure(t)
    agent._execute_node({'turn_id':t['id'],'step':{'kind':'tool','tool':'run_data_workflow','arguments':{'workflow_id':wid}}})
    waiting=get('turn',t['id']);assert waiting['state']=='waiting_data_workflow'
    assert not tasks.verify(waiting)['verified']
    assert process_once();resume(get('turn',t['id']))
    completed=get('turn',t['id']);assert completed['agent_trace'][-1]['status']=='done'
    assert tasks.verify(completed)['verified']
    assert completed['agent_download']['url'].endswith('format=handoff')
    # This test stops at the durable tool receipt; do not leak an unfinished
    # model turn into the next test's real conversation worker.
    patch('turn',t['id'],state='done')


def test_chat_and_private_source_reads_are_enforced(monkeypatch):
    from agent_service import agent,provider
    from agent_service.data_tools import execute
    from agent_service.store import put,now
    pid=project();s=source(pid);wid=registered(pid,document(s['id']))
    t=put('turn',{'project_id':pid,'text':'解释这个数据流程','interaction_mode':'chat','state':'agent','created_at':now()},'t_'+uid())
    import pytest
    with pytest.raises(ValueError):agent.authorize(t,'run_data_workflow',{'workflow_id':wid})
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':False})
    with pytest.raises(ValueError):execute(t,'read_source_table',{'source_id':s['id'],'options':{},'offset':0,'limit':10})
    value=agent.public_observation({'rows':[{'a':'SECRET'}],'special_cells':{'a':'SECRET'},'header_cells':{'a':'SECRET'},
                                    'candidate_regions':[{'header_preview':['SECRET']}],'assertions':[{'value':'SECRET'}]})
    assert 'SECRET' not in json.dumps(value)
    patch('turn',t['id'],state='done')


def test_personal_finance_report_api_downloads_and_verified_agent_receipt():
    import hashlib
    from agent_service import business_reports as reports,workspace,tasks
    from agent_service.data_tools import execute
    from agent_service.store import put,now
    from test_business_reports import real_result,recipe
    pid,rid=real_result();info=reports.inspect_result(pid,rid)
    value=recipe();value['expected_schema_sha256']=info['schema_sha256']
    workspace.write_file(pid,'reports/month-end.json',json.dumps(value))
    t=put('turn',{'project_id':pid,'text':'帮我生成经营月报Word汇报材料','interaction_mode':'work',
                  'state':'agent','created_at':now()},'t_'+uid())
    tasks.ensure(t)
    args={'run_id':rid,'recipe_path':'reports/month-end.json'}
    observed=execute(t,'render_business_report',args)
    patch('turn',t['id'],agent_trace=[{'tool':'render_business_report','arguments':args,'status':'done','observation':observed}])
    assert tasks.verify(get('turn',t['id']))['verified']
    root='/api/projects/'+pid+'/business-reports/'+observed['id']+'/download'
    assert client.get(root).status_code==401
    for format in ('docx','pptx'):
        response=client.get(root,headers=H,params={'format':format})
        assert response.status_code==200 and response.content.startswith(b'PK')
    manifest=client.get(root,headers=H,params={'format':'manifest'}).json()
    assert manifest['evaluation']['assertions'][0]['value']=='300.000002'
    other=project()
    assert client.get('/api/projects/'+other+'/business-reports/'+observed['id']+'/download',headers=H).status_code==400
    patch('turn',t['id'],state='done')
