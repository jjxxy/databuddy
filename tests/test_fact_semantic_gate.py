"""Per-fact text review is not a semantic alignment or aggregation approval."""
import pytest
from agent_service import business_explanation,business_reports,fact_service,project_data as data,workspace_artifacts
from agent_service.jobs import get_job
from test_fact_service import document
from test_project_data import column,registered,run


def fact_source(single=False):
    text='2026-10 合肥办事处净收入123.4500元。\n2026-10 合肥办事处预算收入200元。'
    quotes=text.split('\n');pid,sid,_=document(text)
    facts=[{'value_as_printed':'123.4500','measure':'净收入','unit':'元','period':'2026-10',
            'dimensions':{'office':'合肥办事处'},'start':0,'end':len(quotes[0]),'quote':quotes[0]}]
    if not single:
        facts.append({'value_as_printed':'200','measure':'预算收入','unit':'元','period':'2026-10',
            'dimensions':{'office':'合肥办事处'},'start':len(quotes[0])+1,'end':len(text),'quote':quotes[1]})
    proposal=fact_service.propose_facts(pid,sid,facts)
    reviewed=fact_service.review_facts(pid,proposal['id'],proposal['content_sha256'],'owner')
    assert reviewed['semantic_alignment_required'] is True
    return pid,reviewed['output_source_id']


def workflow(pid,sid,path='workflows/mixed-facts.json'):
    _,fields,_,_=data.read_input(pid,{'source_id':sid})
    names=[f['name'] for f in fields];value=next(f['name'] for f in fields if f['label']=='value')
    ident=next(f['name'] for f in fields if f['label']=='fact_id')
    columns=[column(name) for name in names]
    index=names.index(value);columns[index]=column(value,'decimal','CNY','additive')
    columns[index]['meaning']='净收入';columns[index]['period']='2026-10'
    schema={'columns':columns,'grain':[ident],'meaning':'可能混合语义的来源'}
    doc={'version':1,'plan':{'name':'事实处理','inputs':{'facts':schema},'stages':[
        {'id':'keep','kind':'select','input':'facts','description':'保留金额','columns':[ident,value]}],
        'output':'keep'},'bindings':{'facts':{'source_id':sid}}}
    return registered(pid,doc,path=path),value


def recipe(info,value):
    return {'version':1,'title':'收入','period':'2026-10','expected_schema_sha256':info['schema_sha256'],
            'metrics':[{'id':'total','label':'净收入','kind':'sum','column':value}]}


def test_mixed_fact_source_cannot_be_approved_or_used_for_new_reports():
    pid,sid=fact_source();wid,_=workflow(pid,sid);result=run(pid,wid)
    assert result['status']=='validated' and result['data_result']['requires_source_review']
    assert data.read_result(pid,result['id'])['total']==2
    with pytest.raises(ValueError,match='待核对'):business_reports.inspect_result(pid,result['id'])
    with pytest.raises(ValueError,match='语义对齐'):
        data.publish_result(pid,result['id'],result['commit'],review_actor='owner',
                            review_handoff_sha256=result['data_result']['handoff_sha256'])
    with pytest.raises(ValueError,match='复核|语义对齐'):data.read_input(pid,{'result_id':result['id']})
    assert data.owned(pid,'data_workflow',wid).get('published_run') is None


def test_single_reviewed_fact_is_still_pending_without_real_alignment_receipt():
    pid,sid=fact_source(single=True);wid,_=workflow(pid,sid,path='workflows/one-fact.json');result=run(pid,wid)
    assert data.read_result(pid,result['id'])['total']==1
    with pytest.raises(ValueError,match='语义对齐'):
        data.publish_result(pid,result['id'],result['commit'],review_actor='owner',
                            review_handoff_sha256=result['data_result']['handoff_sha256'])


def test_historical_mixed_report_remains_readable_but_not_reused_or_refreshed(monkeypatch):
    pid,sid=fact_source();wid,value=workflow(pid,sid)
    with monkeypatch.context() as old:
        old.setattr(data,'source_requires_fact_alignment',lambda source,evidence:False)
        old.setattr(data,'source_interpretation_needs_review',lambda source,evidence:False)
        first=run(pid,wid)
        info=business_reports.inspect_result(pid,first['id'])
        report=business_reports.render(pid,first['id'],recipe(info,value),recipe_path='reports/historical-fact-mix.json')
        assert report['assertions'][0]['value']=='323.4500'  # historical wrong total: 123.45 actual + 200 budget
        data.publish_result(pid,first['id'],first['commit'])
    assert business_reports.report_file(pid,report['id']).is_file()
    assert data.read_result(pid,first['id'])['total']==2
    with pytest.raises(ValueError,match='语义对齐'):business_reports.inspect_result(pid,first['id'])
    with pytest.raises(ValueError,match='语义对齐'):data.read_input(pid,{'result_id':first['id']})
    artifact=next(item for item in workspace_artifacts.catalog(pid)['items'] if item['kind']=='report')
    explanation=business_explanation.explain_artifact(pid,artifact['id'])
    assert any(check['label']=='事实业务口径' and check['status']=='needs_review' for check in explanation['checks'])
    newer=run(pid,wid,use_published=True)
    assert newer['status']=='validated' and newer['data_result']['requires_source_review']
    assert data.owned(pid,'data_workflow',wid)['published_run']==first['id']
