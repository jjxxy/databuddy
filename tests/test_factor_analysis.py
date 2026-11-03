"""Two-factor arithmetic uses complete, typed, explicitly dated evidence."""
import pytest
from agent_service.factor_analysis import evaluate


def sample():
    schema={'columns':[{'name':'branch','data_type':'text','meaning':'办事处'},
      {'name':'q0','data_type':'decimal','meaning':'基准销量','unit':'件','period':'2026-08','aggregation':'additive'},
      {'name':'q1','data_type':'decimal','meaning':'本期销量','unit':'件','period':'2026-09','aggregation':'additive'},
      {'name':'p0','data_type':'decimal','meaning':'基准单价','unit':'CNY/件','period':'2026-08','aggregation':'non_additive'},
      {'name':'p1','data_type':'decimal','meaning':'本期单价','unit':'CNY/件','period':'2026-09','aggregation':'non_additive'}],
      'grain':['branch'],'meaning':'每个办事处对应同口径的前后数量和单价'}
    rows=[{'branch':'东区','q0':'10','q1':'12','p0':'3.00','p1':'4.00'},
          {'branch':'西区','q0':'2','q1':'3','p0':'7.00','p1':'6.00'}]
    return {'schema':schema,'rows':rows,'total':2,'complete_result_available':True}


def request():
    return {'expected_handoff_sha256':'a'*64,'quantity_before':'q0','quantity_after':'q1','price_before':'p0','price_after':'p1',
            'baseline_period':'2026-08','current_period':'2026-09','amount_unit':'CNY','group_by':[]}


def test_exact_rowwise_decomposition_and_grouping():
    result=evaluate(sample(),request()); item=result['groups'][0]
    assert (item['before'],item['after'],item['quantity_effect'],item['price_effect'],item['delta'])==('44.00','66.00','13.00','9.00','22.00')
    assert '不证明业务因果' in result['notice']
    r=request();r['group_by']=['branch'];groups=evaluate(sample(),r)['groups']
    assert len(groups)==2 and sum(int(g['count']) for g in groups)==2
    assert groups[0]['values']==['东区'] and groups[0]['delta']=='18.00'


def test_missing_incomplete_or_duplicate_data_fails_closed():
    table=sample();table['rows'][0]['q0']=None
    with pytest.raises(ValueError,match='空值'):evaluate(table,request())
    table=sample();table['rows'].pop()
    with pytest.raises(ValueError,match='完整'):evaluate(table,request())
    table=sample();table['rows'][1]['branch']='东区'
    with pytest.raises(ValueError,match='粒度重复'):evaluate(table,request())


def test_units_periods_and_float_are_never_guessed():
    table=sample();table['schema']['columns'][3]['unit']='USD/件'
    with pytest.raises(ValueError,match='单价单位'):evaluate(table,request())
    table=sample();table['schema']['columns'][3]['period']='2026-09'
    with pytest.raises(ValueError,match='期间'):evaluate(table,request())
    table=sample();table['rows'][0]['p0']=3.5
    with pytest.raises(ValueError):evaluate(table,request())
    table=sample();table['schema']['columns'][3]['aggregation']='unknown'
    with pytest.raises(ValueError,match='不可直接相加'):evaluate(table,request())
    bad=request();bad['baseline_period']='0000-01'
    with pytest.raises(ValueError,match='有效日历'):evaluate(sample(),bad)


def adopted_factor():
    from agent_service import project_data as data
    from test_project_data import project, source, registered, run
    pid=project();s=source(pid,b'branch,q0,q1,p0,p1\nEast,10,12,3,4\nWest,2,3,7,6\n')
    schema=sample()['schema']
    doc={'version':1,'plan':{'name':'factor evidence','inputs':{'detail':schema},'stages':[{'id':'keep','kind':'select','description':'keep rows','input':'detail','columns':['branch','q0','q1','p0','p1']}],'output':'keep'},
         'bindings':{'detail':{'source_id':s['id'],'column_map':{'c_1':'branch','c_2':'q0','c_3':'q1','c_4':'p0','c_5':'p1'}}}}
    wid=registered(pid,doc);job=run(pid,wid);sha=data.result_folder(pid,job['id'])[1]['handoff_sha256']
    return pid,job,sha


def test_http_factor_analysis_requires_adopted_pinned_full_result():
    from fastapi.testclient import TestClient
    from agent_service.api import app
    from agent_service.config import TOKEN
    from agent_service import project_data as data
    pid,job,sha=adopted_factor()
    path=f'/api/projects/{pid}/data-results/{job["id"]}/factor-analysis';client=TestClient(app)
    body=request();body['expected_handoff_sha256']=sha;headers={'Authorization':'Bearer '+TOKEN}
    assert client.post(path,json=body).status_code==401
    assert client.post(path,json=body,headers=headers).status_code==400
    data.publish_result(pid,job['id'],job['commit'])
    response=client.post(path,json=body,headers=headers)
    assert response.status_code==200,response.text
    assert response.json()['groups'][0]['delta']=='22'
    body['expected_handoff_sha256']='0'*64
    assert client.post(path,json=body,headers=headers).status_code==400


def test_project_deletion_waits_for_factor_snapshot(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from agent_service import factor_analysis as module, project_data as data, workspace_lifecycle as lifecycle
    pid,job,sha=adopted_factor();data.publish_result(pid,job['id'],job['commit'])
    body=request();body['expected_handoff_sha256']=sha
    entered=threading.Event();release=threading.Event();deleted=threading.Event()
    original=module.read_all_handoff
    def slow(*args,**kwargs):
        value=original(*args,**kwargs);entered.set();assert release.wait(5);return value
    monkeypatch.setattr(module,'read_all_handoff',slow)
    def remove():
        lifecycle.delete_project(pid);deleted.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        analysis=pool.submit(module.analyze,pid,job['id'],body);assert entered.wait(5)
        removal=pool.submit(remove);assert not deleted.wait(.1);release.set()
        assert analysis.result(timeout=10)['groups'][0]['delta']=='22'
        removal.result(timeout=10)
    with pytest.raises(ValueError,match='回收站'):module.analyze(pid,job['id'],body)


def test_filter_uses_exact_value_and_full_grain_and_discloses_input_range():
    table=sample();body={**request(),'group_by':['branch'],'filters':[{'column':'branch','value':'东区'}]}
    result=evaluate(table,body)
    assert result['input_rows']==2 and result['total_rows']==1
    assert result['filters']==[{'column':'branch','value':'东区'}]
    assert result['groups'][0]['before']=='30.00' and result['groups'][0]['delta']=='18.00'
    table['rows'][1]['branch']='东区'
    with pytest.raises(ValueError,match='粒度重复'):evaluate(table,body)
    table=sample();table['rows'][1]['branch']=None
    with pytest.raises(ValueError,match='粒度存在空值'):evaluate(table,body)
    table=sample();body['filters'][0]['value']='不存在'
    with pytest.raises(ValueError,match='筛选后没有数据'):evaluate(table,body)


def test_boolean_filter_is_strict_and_missing_never_silently_excluded():
    table=sample();table['schema']['columns'].append({'name':'active','data_type':'boolean','meaning':'是否有效'})
    table['rows'][0]['active']=True;table['rows'][1]['active']=False
    body={**request(),'filters':[{'column':'active','value':True}]}
    assert evaluate(table,body)['total_rows']==1
    with pytest.raises(ValueError):evaluate(table,{**body,'filters':[{'column':'active','value':1}]})
    with pytest.raises(ValueError,match='筛选值与字段类型'):evaluate(table,{**body,'filters':[{'column':'active','value':'true'}]})
    with pytest.raises(ValueError,match='同一字段'):evaluate(table,{**body,'filters':body['filters']*2})
    table['rows'][1]['active']=None
    with pytest.raises(ValueError,match='筛选字段存在空值'):evaluate(table,body)


def test_http_factor_filter_keeps_fixed_result_and_range():
    from fastapi.testclient import TestClient
    from agent_service.api import app
    from agent_service.config import TOKEN
    from agent_service import project_data as data
    pid,job,sha=adopted_factor();data.publish_result(pid,job['id'],job['commit'])
    body={**request(),'expected_handoff_sha256':sha,'filters':[{'column':'branch','value':'East'}]}
    path=f'/api/projects/{pid}/data-results/{job["id"]}/factor-analysis'
    response=TestClient(app).post(path,json=body,headers={'Authorization':'Bearer '+TOKEN})
    assert response.status_code==200,response.text
    assert response.json()['total_rows']==1 and response.json()['input_rows']==2 and response.json()['groups'][0]['delta']=='18'
