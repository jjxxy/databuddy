"""Unreviewed source interpretation must reach workflow approval and descendants."""
import hashlib
import pytest
from agent_service import business_reports, project_data as data
from agent_service.config import DATA
from agent_service.execution import process_once
from agent_service.jobs import get_job
from agent_service.sources import revision
from agent_service.store import seal,put,uid,now
from test_project_data import project,registered,run,column


def indented_source(pid):
    content='华东\n  上海  12  15%\n  安徽  8  10%'.encode();name=uid()+'.txt';folder=DATA/'uploads'
    folder.mkdir(parents=True,exist_ok=True);(folder/name).write_bytes(content)
    source=revision(put('source',{'project_id':pid,'name':'hierarchy.txt','kind':'file',
        'secret_config':seal({'stored_name':name}),'content_hash':hashlib.sha256(content).hexdigest(),
        'bytes':len(content),'status':'ready','created_at':now()},'s_'+uid()))
    result=data.extract_indented_source(pid,source['id'],['办事处','收入','增长率'])
    assert process_once()
    return result['source_id']


def draft(sid):
    names=['c_1','c_2','c_3','c_4']
    schema={'columns':[column(n) for n in names],'grain':['c_1'],'meaning':'缩进来源明细'}
    return {'version':1,'plan':{'name':'读取缩进资料','inputs':{'detail':schema},
        'stages':[{'kind':'select','id':'selected','description':'选择来源记录',
                   'input':'detail','columns':names}],'output':'selected'},
        'bindings':{'detail':{'source_id':sid}}}


def test_actual_indented_source_review_flag_reaches_candidate_and_blocks_report():
    pid=project();sid=indented_source(pid)
    rows,fields,evidence,_=data.read_input(pid,{'source_id':sid})
    assert len(rows)==2 and evidence['requires_semantic_review'] is True
    workflow=registered(pid,draft(sid));result=run(pid,workflow)
    assert result['data_result']['requires_semantic_review'] is True
    with pytest.raises(ValueError,match='待核对'):
        business_reports.inspect_result(pid,result['id'])
    # The existing authenticated manual adoption is the explicit review action.
    data.publish_result(pid,result['id'],result['commit'],review_actor='owner',review_handoff_sha256=result['data_result']['handoff_sha256'])
    assert business_reports.inspect_result(pid,result['id'])['server_adopted_result'] is True


def test_repeated_unreviewed_input_must_not_auto_publish():
    pid=project();sid=indented_source(pid);workflow=registered(pid,draft(sid))
    result=run(pid,workflow);data.publish_result(pid,result['id'],result['commit'],review_actor='owner',review_handoff_sha256=result['data_result']['handoff_sha256'])
    later=run(pid,workflow,use_published=True)
    assert later['status']=='validated' and later['data_result']['requires_semantic_review'] is True
    assert data.owned(pid,'data_workflow',workflow)['published_run']==result['id']
    with pytest.raises(ValueError,match='待核对'):business_reports.inspect_result(pid,later['id'])


def file_source(pid,filename,content):
    name=uid()+__import__('pathlib').Path(filename).suffix
    folder=DATA/'uploads';folder.mkdir(parents=True,exist_ok=True);(folder/name).write_bytes(content)
    return revision(put('source',{'project_id':pid,'name':filename,'kind':'file',
        'secret_config':seal({'stored_name':name}),'content_hash':hashlib.sha256(content).hexdigest(),
        'bytes':len(content),'status':'ready','created_at':now()},'s_'+uid()))


def test_html_tables_are_explicitly_marked_for_review():
    pid=project();source=file_source(pid,'statement.html',b'<table><tr><th>name</th><th>amount</th></tr><tr><td>A</td><td>12</td></tr></table>')
    rows,fields,evidence,_=data.read_input(pid,{'source_id':source['id']})
    assert len(rows)==1 and evidence['requires_semantic_review'] is True
    assert evidence['requires_source_review'] is True


def test_docling_result_never_loses_review_obligation(monkeypatch):
    import sys,types
    from types import SimpleNamespace
    module=types.ModuleType('docling');module.__path__=[]
    converter=types.ModuleType('docling.document_converter')
    class DocumentConverter:
        def convert(self,path):
            segment=SimpleNamespace(text='金额：12元',prov=[])
            return SimpleNamespace(document=SimpleNamespace(iterate_items=lambda:[(segment,0)]))
    converter.DocumentConverter=DocumentConverter
    monkeypatch.setenv('USE_DOCLING','1')
    monkeypatch.setitem(sys.modules,'docling',module);monkeypatch.setitem(sys.modules,'docling.document_converter',converter)
    pid=project();source=file_source(pid,'scan.png',b'isolated-fake-image-for-parser-stub')
    rows,fields,evidence,_=data.read_input(pid,{'source_id':source['id']})
    assert len(rows)==1 and evidence['parser']=='docling' and evidence['requires_source_review'] is True
    assert 'SOURCE_INTERPRETATION_REVIEW_REQUIRED' in evidence['semantic_review_reasons']


def test_review_obligation_propagates_to_downstream_result():
    pid=project();sid=indented_source(pid);first=run(pid,registered(pid,draft(sid)))
    input_doc=draft(sid);input_doc['bindings']['detail']={'result_id':first['id']}
    with pytest.raises(ValueError,match='人工复核'):data.read_input(pid,{'result_id':first['id']})
    data.publish_result(pid,first['id'],first['commit'],review_actor='owner',review_handoff_sha256=first['data_result']['handoff_sha256'])
    second=run(pid,registered(pid,input_doc,path='workflows/second.json'))
    assert second['data_result']['requires_source_review'] is True
    assert second['data_result']['requires_semantic_review'] is True
    with pytest.raises(ValueError,match='待核对'):business_reports.inspect_result(pid,second['id'])


def test_legacy_auto_pipeline_keeps_interpreted_source_as_candidate():
    from agent_service.codegen import generate
    from agent_service.execution import enqueue
    from agent_service.releases import publish,manifest_for
    from agent_service.store import get
    pid=project();sid=indented_source(pid);source=get('source',sid)
    assert source['requires_semantic_review'] is True
    pipeline=generate(source,'历史加工通路',source['proposed_spec'],project_id=pid,release_mode='auto')
    queued=enqueue('pipeline',pipeline['id'],commit=pipeline['commit']);assert process_once()
    first=get_job(queued['id']);_,manifest=manifest_for(first)
    assert first['status']=='validated' and manifest['requires_source_review'] is True
    assert get('pipeline',pipeline['id']).get('published_run') is None
    publish(pipeline['id'],first['id'],confirm_source_review=True,expected_commit=first['commit'],
            expected_parquet_sha256=manifest['parquet_hash'],review_actor='owner')
    assert get('pipeline',pipeline['id'])['published_run']==first['id']
    queued=enqueue('pipeline',pipeline['id'],commit=pipeline['commit']);assert process_once()
    second=get_job(queued['id'])
    assert second['status']=='validated' and get('pipeline',pipeline['id'])['published_run']==first['id']


def test_historical_missing_source_review_marker_blocks_multigeneration_but_keeps_report(monkeypatch):
    from agent_service import business_explanation,workspace_artifacts
    from agent_service.store import patch
    from test_business_reports import schema,recipe
    pid=project();source=file_source(pid,'month.csv',b'office,actual,budget\nEast,100.000001,100\nWest,200.000001,200\n')
    patch('source',source['id'],requires_semantic_review=True)
    columns=['office','actual','budget'];plan={'name':'historical report','inputs':{'detail':schema()},
        'stages':[{'kind':'select','id':'keep','description':'保留全部记录','input':'detail','columns':columns}],'output':'keep'}
    doc={'version':1,'plan':plan,'bindings':{'detail':{'source_id':source['id'],
         'column_map':{'c_1':'office','c_2':'actual','c_3':'budget'}}}}
    with monkeypatch.context() as historical:
        historical.setattr(data,'source_interpretation_needs_review',lambda source,evidence:False)
        first=run(pid,registered(pid,doc,path='workflows/old.json'))
        info=business_reports.inspect_result(pid,first['id']);report_recipe=recipe();report_recipe['expected_schema_sha256']=info['schema_sha256']
        report=business_reports.render(pid,first['id'],report_recipe,recipe_path='reports/old-source.json')
        data.publish_result(pid,first['id'],first['commit'])
        child_doc={'version':1,'plan':plan,'bindings':{'detail':{'result_id':first['id']}}}
        second=run(pid,registered(pid,child_doc,path='workflows/old-child.json'))
        data.publish_result(pid,second['id'],second['commit'])
        child_doc['bindings']['detail']['result_id']=second['id']
        third=run(pid,registered(pid,child_doc,path='workflows/old-grandchild.json'))
    assert business_reports.report_file(pid,report['id']).is_file()
    for item in (first,second,third):
        assert data.read_result(pid,item['id'])['total']==2
        with pytest.raises(ValueError,match='历史来源解释'):
            business_reports.inspect_result(pid,item['id'])
        with pytest.raises(ValueError,match='历史来源解释'):
            data.read_input(pid,{'result_id':item['id']})
    artifact=next(x for x in workspace_artifacts.catalog(pid)['items'] if x['kind']=='report')
    explanation=business_explanation.explain_artifact(pid,artifact['id'])
    assert any(x['label']=='历史来源解释' and x['status']=='needs_review' for x in explanation['checks'])


def test_safe_legacy_csv_result_without_review_marker_is_not_blocked(monkeypatch):
    from test_business_reports import real_result
    with monkeypatch.context() as historical:
        historical.setattr(data,'source_interpretation_needs_review',lambda source,evidence:False)
        pid,rid=real_result()
    assert business_reports.inspect_result(pid,rid)['rows']==2
    assert len(data.read_input(pid,{'result_id':rid})[0])==2


def test_safe_legacy_xlsx_result_without_review_marker_is_not_blocked(monkeypatch):
    import io
    from openpyxl import Workbook
    workbook=Workbook();sheet=workbook.active;sheet.title='明细';sheet.append(['编号','金额']);sheet.append(['A',12]);sheet.append(['B',20])
    buffer=io.BytesIO();workbook.save(buffer);pid=project();source=file_source(pid,'safe.xlsx',buffer.getvalue())
    schema={'columns':[column('c_1'),column('c_2','decimal','CNY','additive')],
            'grain':['c_1'],'meaning':'安全表格明细'}
    doc={'version':1,'plan':{'name':'安全表格','inputs':{'detail':schema},
        'stages':[{'kind':'select','id':'keep','description':'保留明细','input':'detail','columns':['c_1','c_2']}],
        'output':'keep'},'bindings':{'detail':{'source_id':source['id'],'options':{'sheet':'明细','header_rows':[1]}}}}
    with monkeypatch.context() as historical:
        historical.setattr(data,'source_interpretation_needs_review',lambda source,evidence:False)
        result=run(pid,registered(pid,doc,path='workflows/safe-xlsx.json'))
    assert business_reports.inspect_result(pid,result['id'])['rows']==2
    assert len(data.read_input(pid,{'result_id':result['id']})[0])==2


@pytest.mark.parametrize('missing',['snapshot','evidence_hash'])
def test_missing_historical_source_snapshot_or_hash_is_unknown(monkeypatch,missing):
    from test_business_reports import real_result
    import agent_service.jobs as jobs
    pid,rid=real_result();original=jobs.get_job
    def altered(ident):
        value=original(ident)
        if ident==rid:
            if missing=='snapshot':value['source_snapshot']={}
            else:
                snapshot=dict(value['source_snapshot']);source_id=next(iter(snapshot));source=dict(snapshot[source_id])
                source['content_hash']='f'*64;snapshot[source_id]=source;value['source_snapshot']=snapshot
        return value
    monkeypatch.setattr(jobs,'get_job',altered)
    with pytest.raises(ValueError,match='历史来源证据不完整'):
        business_reports.inspect_result(pid,rid)
