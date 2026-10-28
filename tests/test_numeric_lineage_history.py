"""Historical float intermediates cannot be laundered through later runs."""
import hashlib
import json
from pathlib import Path
import subprocess

import duckdb
import pytest

from agent_service import business_reports, data_workflows, project_data
from agent_service.data_contracts import NumericLineageError
from agent_service.data_workflows import digest
from agent_service.config import DATA
from agent_service.jobs import save
from agent_service.store import now,put,uid


def unsafe_sql_history(monkeypatch):
    """Create actual old-style results while temporarily emulating the old SQL gate."""
    from test_project_data import project,source,document,registered,run,row_schema
    pid=project();src=source(pid,b'id,office,amount,note\nA,East,9007199254740993,fixture\n')
    schema=row_schema();next(c for c in schema['columns'] if c['name']=='amount')['period']='2026-09'
    doc=document(src['id']);doc['contract']=None;doc['plan']['inputs']['orders']=schema
    doc['plan']['stages']=[{'id':'unsafe','kind':'sql','description':'历史浮点除法',
        'inputs':['orders'],'sql':'SELECT id, office, CAST(amount / 1 AS DECIMAL(38,12)) AS amount, note FROM orders',
        'output_schema':schema}];doc['plan']['output']='unsafe'
    with monkeypatch.context() as old:
        old.setattr(data_workflows,'validate_sql',lambda sql,allowed_tables,**kwargs:sql)
        workflow=registered(pid,doc);first=run(pid,workflow)
        project_data.publish_result(pid,first['id'],first['commit'])
        info=business_reports.inspect_result(pid,first['id'])
        recipe={'version':1,'title':'历史合成报告','period':'2026-09','expected_schema_sha256':info['schema_sha256'],
                'metrics':[{'id':'total','label':'合计','kind':'sum','column':'amount'}]}
        report=business_reports.render(pid,first['id'],recipe,recipe_path='reports/old-sql.json')
        second=descendant_of(pid,first['id'],schema,'workflows/child.json')
        third=descendant_of(pid,second['id'],schema,'workflows/grandchild.json')
    return pid,first,second,third,report


def descendant_of(pid,rid,schema,path):
    from test_project_data import registered,run
    doc={'version':1,'plan':{'name':'沿用上游','inputs':{'detail':schema},
         'stages':[{'id':'keep','kind':'select','input':'detail','description':'保留记录',
                    'columns':['id','office','amount','note']}],'output':'keep'},
         'bindings':{'detail':{'result_id':rid}}}
    wid=registered(pid,doc,path=path);result=run(pid,wid)
    project_data.publish_result(pid,result['id'],result['commit'])
    return result


def test_actual_old_float_sql_and_two_descendants_cannot_be_reused(monkeypatch):
    pid,first,second,third,report=unsafe_sql_history(monkeypatch)
    assert report['assertions'][0]['value']=='9007199254740992.000000000000'
    assert '9007199254740992' in business_reports.report_file(pid,report['id']).read_text(encoding='utf-8')
    for result in (first,second,third):
        rid=result['id']
        assert project_data.read_result(pid,rid)['rows'][0]['amount']=='9007199254740992.000000000000'
        for action in (lambda:project_data.publish_result(pid,rid,result['commit']),
                       lambda:project_data.read_input(pid,{'result_id':rid}),
                       lambda:business_reports.inspect_result(pid,rid)):
            with pytest.raises(NumericLineageError,match='重新运行') as error:action()
            assert error.value.reason=='unsafe_sql'
    assert business_reports.report_file(pid,report['id']).is_file()


def test_actual_worker_surfaces_safe_error_for_historical_ancestor(monkeypatch):
    from agent_service.execution import process_once
    from agent_service.jobs import get_job
    from test_project_data import registered,row_schema
    pid,_,_,third,_=unsafe_sql_history(monkeypatch)
    schema=row_schema();next(c for c in schema['columns'] if c['name']=='amount')['period']='2026-09'
    doc={'version':1,'plan':{'name':'沿用上游','inputs':{'detail':schema},
         'stages':[{'id':'keep','kind':'select','input':'detail','description':'保留记录',
                    'columns':['id','office','amount','note']}],'output':'keep'},
         'bindings':{'detail':{'result_id':third['id']}}}
    wid=registered(pid,doc,path='workflows/rejected-child.json')
    queued=project_data.enqueue_workflow(pid,wid)
    assert process_once()
    failed=get_job(queued['id'])
    assert failed['status']=='failed' and failed['error_code']=='DATA_CONTRACT_EXACTNESS'
    assert '重新运行' in failed['error'] and 'Traceback' not in failed['error']
    assert 'CAST(' not in failed['error'] and '9007199254740993' not in failed['error']


def test_safe_historical_sql_is_not_blanket_blocked():
    from test_project_data import project,source,document,registered,run,row_schema
    pid=project();src=source(pid);schema=row_schema();doc=document(src['id']);doc['contract']=None
    doc['plan']['stages']=[{'id':'safe','kind':'sql','description':'取所选字段','inputs':['orders'],
         'sql':'SELECT id, office, amount, note FROM orders','output_schema':schema}];doc['plan']['output']='safe'
    result=run(pid,registered(pid,doc))
    project_data.publish_result(pid,result['id'],result['commit'])
    assert len(project_data.read_input(pid,{'result_id':result['id']})[0])==215


def _mock_graph(monkeypatch,edges):
    """Model authenticated handoff responses for cycle and fan-in traversal."""
    import agent_service.jobs as jobs
    import agent_service.result_delivery as delivery
    from test_project_data import row_schema
    pid='fixture-project';schema=row_schema();runs={};metas={};pages={};reads=[]
    sha={name:hashlib.sha256(name.encode()).hexdigest() for name in edges}
    for name,children in edges.items():
        aliases=['input'+str(i) for i in range(len(children))] or ['input0']
        doc={'version':1,'plan':{'name':'图检查','inputs':{alias:schema for alias in aliases},
             'stages':[{'id':'keep','kind':'select','description':'保留','input':aliases[0],
                        'columns':['id','office','amount','note']}],'output':'keep'},
             'bindings':{alias:({'result_id':child} if children else {'source_id':'source_fixture'})
                         for alias,child in zip(aliases,children or [None])}}
        plan=digest(doc)
        meta={'run_id':name,'workflow_id':'workflow_'+name,'plan_sha256':plan,'records':1,
              'contract':None,'handoff_sha256':sha[name]}
        context={'project_id':pid,'plan_sha256':plan,'data_contract':None,
                 'input_evidence':{alias:{'result_id':child,'manifest_sha256':sha.get(child,'f'*64),'complete':True}
                                   for alias,child in zip(aliases,children)}}
        runs[name]={'id':name,'project_id':pid,'kind':'data_workflow','target':'workflow_'+name,
                    'commit':plan,'workflow_document':doc,'data_result':meta}
        metas[name]=meta;pages[name]={'context':context,'total':1}
    def job(name):return runs[name]
    def folder(project_id,name,**kwargs):
        if project_id!=pid or runs[name]['project_id']!=pid:raise ValueError('not this project')
        return Path(name),metas[name]
    def page(path,**kwargs):
        name=Path(path).parent.name;reads.append(name)
        return pages[name]
    monkeypatch.setattr(jobs,'get_job',job)
    monkeypatch.setattr(project_data,'result_folder',folder)
    monkeypatch.setattr(delivery,'read_handoff',page)
    return pid,runs,metas,pages,reads


@pytest.mark.parametrize('fault', ['cycle','missing','wrong_hash','foreign_project'])
def test_ancestor_missing_cycle_cross_project_or_hash_mismatch_is_unknown(monkeypatch,fault):
    graph={'A':['B'],'B':['A'] if fault=='cycle' else []}
    if fault=='missing':graph.pop('B')
    pid,runs,metas,pages,_=_mock_graph(monkeypatch,graph)
    if fault=='wrong_hash':pages['A']['context']['input_evidence']['input0']['manifest_sha256']='f'*64
    if fault=='foreign_project':runs['B']['project_id']='another-project'
    with pytest.raises(NumericLineageError) as error:project_data.require_safe_numeric_lineage(pid,'A')
    assert error.value.reason=='unknown' and '重新运行' in str(error.value)


def test_repeated_ancestor_is_verified_once_and_does_not_look_like_a_cycle(monkeypatch):
    pid,_,_,_,reads=_mock_graph(monkeypatch,{'A':['B','B'],'B':[]})
    project_data.require_safe_numeric_lineage(pid,'A')
    assert reads==['A','B']


def test_lineage_limit_is_an_explicit_unknown_not_a_pass(monkeypatch):
    edges={'N'+str(i):(['N'+str(i+1)] if i<512 else []) for i in range(513)}
    pid,_,_,_,_=_mock_graph(monkeypatch,edges)
    with pytest.raises(NumericLineageError) as error:project_data.require_safe_numeric_lineage(pid,'N0')
    assert error.value.reason=='unknown' and '重新运行' in str(error.value)


def test_existing_legacy_divide_plan_cannot_be_published_or_rolled_back():
    from agent_service.releases import publish,verify_semantics
    pid='pipeline_'+uid();rid=uid();repo=DATA/'repositories'/pid;repo.mkdir(parents=True)
    spec={'computations':[{'name':'m_net','label':'历史除法','operation':'divide',
          'left_field':'amount','right_field':'basis'}],'filters':[]}
    plan={'spec':spec,'metric_aggregation':'sum_decimal_38_12'}
    (repo/'plan.json').write_text(json.dumps(plan),encoding='utf-8')
    for args in [('init','-q'),('config','user.name','Fixture'),('config','user.email','fixture@example.test'),
                 ('add','plan.json'),('commit','-qm','synthetic historical plan')]:
        subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True)
    commit=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
    folder=DATA/'runs'/rid;folder.mkdir(parents=True);path=folder/'business_objects.parquet'
    with duckdb.connect() as con:
        sql="SELECT CAST(CAST('9007199254740993' AS DECIMAL(38,12)) / 1 AS DECIMAL(38,12)) AS m_net"
        assert str(con.execute(sql).fetchone()[0])=='9007199254740992.000000000000'
        con.execute('COPY ('+sql+') TO ? (FORMAT PARQUET)',[str(path)])
    manifest={'run_id':rid,'pipeline_id':pid,'commit':commit,'spec':spec,
              'metric_aggregation':'sum_decimal_38_12','parquet_hash':hashlib.sha256(path.read_bytes()).hexdigest(),
              'published_at':now()}
    (folder/'output.json').write_text(json.dumps(manifest),encoding='utf-8')
    pipeline=put('pipeline',{'id':pid,'project_id':'fixture-project','repository':str(repo),'commit':commit,
                 'spec':spec,'active_commit':None,'published_run':None},pid)
    result=save({'id':rid,'kind':'pipeline','target':pid,'commit':commit,'status':'validated','validated':True,
                 'artifact_dir':folder.relative_to(DATA).as_posix(),'records':1},rid)
    assert verify_semantics(pipeline,result,manifest,folder)['spec']==spec
    for rollback in (False,True):
        with pytest.raises(ValueError,match='重新运行'):publish(pid,rid,rollback=rollback)
    assert path.is_file() and json.loads((folder/'output.json').read_text(encoding='utf-8'))==manifest
