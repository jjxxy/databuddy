"""Full CSV values and a user's approval must bind to the reviewed version."""
import csv
from decimal import Decimal
import pytest
from agent_service import agent, data_tools, project_data as data
from agent_service.result_delivery import DeliveryError, export_csv
from test_project_data import project, source, csv_bytes, document, registered, run, COUNT, TOTAL


def test_csv_preserves_exact_values_chinese_headers_and_multiline_text(tmp_path):
    schema=[{'name':'office','label':'办事处'}, {'name':'amount','label':'收入元'}, {'name':'rate','label':'毛利率'}]
    rows=[{'office':'华东,"一部"\n复核','amount':Decimal('123456789012345678.000000000001'), 'rate':Decimal('0.123456789012')},
          {'office':'全部','amount':Decimal('-0.000000000001'), 'rate':None}]
    target=tmp_path/'exact.csv'
    proof=export_csv(iter(rows),schema,target)
    with target.open(encoding='utf-8-sig',newline='') as stream:
        actual=list(csv.DictReader(stream))
    assert proof['complete'] and proof['records']==2
    assert list(actual[0])==['办事处','收入元','毛利率']
    assert actual[0]['办事处']==rows[0]['office']
    assert Decimal(actual[0]['收入元'])==rows[0]['amount']
    assert actual[1]['收入元']=='-0.000000000001' and actual[1]['毛利率']==''
    with pytest.raises(DeliveryError):export_csv(rows,schema,target)
    with pytest.raises(DeliveryError,match='列名重复'):
        export_csv([], [{'name':'taxed','label':'收入'}, {'name':'untaxed','label':'收入'}],tmp_path/'ambiguous.csv')


def test_csv_formula_text_is_literal_but_negative_amounts_remain_numeric(tmp_path):
    path=tmp_path/'literal.csv'
    receipt=export_csv([{'name':' =1+1','amount':Decimal('-13.000000000001')}],
        [{'name':'name','label':'=SUM(1,2)'},{'name':'amount','label':'收入元'}],path)
    with path.open(encoding='utf-8-sig',newline='') as stream:rows=list(csv.reader(stream))
    assert rows==[["'=SUM(1,2)",'收入元'],["' =1+1",'-13.000000000001']]
    assert receipt['formula_like_text_escaped']==2


def test_csv_download_is_full_versioned_repeatable_and_refuses_tampering():
    pid=project();s=source(pid);doc=document(s['id'],exact_total=True)
    doc['plan']['inputs']['orders']['columns'][2]['label']='收入元'
    result=run(pid,registered(pid,doc))
    path=data.result_download(pid,result['id'],'csv')
    with path.open(encoding='utf-8-sig',newline='') as stream:rows=list(csv.DictReader(stream))
    assert len(rows)==COUNT and sum(Decimal(row['收入元']) for row in rows)==Decimal(TOTAL)
    original=path.read_bytes()
    assert data.result_download(pid,result['id'],'csv').read_bytes()==original
    assert data.export_result(pid,result['id'],'reject',format='csv').read_bytes()==original
    assert data.inspect_workflow(pid,result['target'])['published_run'] is None
    with pytest.raises((KeyError,ValueError)):data.result_download(project(),result['id'],'csv')
    path.write_bytes(original+b'forged\n')
    with pytest.raises(DeliveryError,match='完整性'):
        data.result_download(pid,result['id'],'csv')


def test_full_csv_download_keeps_typed_negative_amounts_numeric():
    pid=project();s=source(pid,csv_bytes(amount_offset=-216))
    result=run(pid,registered(pid,document(s['id'])))
    path=data.result_download(pid,result['id'],'csv')
    with path.open(encoding='utf-8-sig',newline='') as stream:rows=list(csv.DictReader(stream))
    assert len(rows)==COUNT
    assert rows[-1]['amount']=='-1.000000000001'
    assert sum(Decimal(row['amount']) for row in rows)==-Decimal(TOTAL)


@pytest.mark.parametrize('text,run_id,plan,allowed',[
    ('确认采用数据结果：运行 reviewed_run，方案 '+'a'*64,'reviewed_run','a'*64,True),
    ('确认采用数据结果：运行 reviewed_run，方案 '+'a'*64,'other_run','a'*64,False),
    ('确认采用数据结果：运行 reviewed_run，方案 '+'a'*64,'reviewed_run','b'*64,False),
    ('不要确认采用数据结果：运行 reviewed_run，方案 '+'a'*64,'reviewed_run','a'*64,False),
    ('确认采用数据结果。','reviewed_run','a'*64,True),
    ('继续','reviewed_run','a'*64,False),
])
def test_current_approval_cannot_be_used_for_another_run_or_plan(monkeypatch,text,run_id,plan,allowed):
    calls=[]
    monkeypatch.setattr(data,'publish_result',lambda *args:calls.append(args) or {'published':True})
    pid=project()
    turn={'project_id':pid,'text':text,'interaction_mode':'work'}
    def invoke():
        args={'run_id':run_id,'expected_plan_sha256':plan}
        agent.authorize(turn,'publish_data_workflow_result',args)
        return data_tools.execute(turn,'publish_data_workflow_result',args)
    if allowed:
        assert invoke()['published'] and calls==[(pid,run_id,plan)]
    else:
        with pytest.raises(ValueError):invoke()
        assert not calls
