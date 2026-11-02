"""Uploaded group transactions and changing business rules through real flow handlers."""
from fastapi.testclient import TestClient

from agent_service.api import app
from agent_service.business_flow_data import source_table, table_workflow, validate_source, validate_table
from agent_service.business_flow_engine import Handler, run
from agent_service.config import TOKEN
from agent_service.execution import process_once
from agent_service.store import get, uid


def test_uploaded_rules_select_version_at_business_date_and_preserve_evidence():
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    def request(method,path,**kwargs):
        response=client.request(method,path,headers=headers,**kwargs)
        assert response.status_code==200,response.text
        return response.json()
    pid=request('POST','/api/projects',json={'name':'规则生效日期核对','request_id':uid()})['id']
    root='/api/projects/'+pid
    def upload(name,content):
        source=request('POST',root+'/attachments',files={'file':(name,content.encode('utf-8'),'text/csv')},
                       data={'request_id':uid(),'local_processing':'true'})
        for _ in range(100):
            source=get('source',source['id'])
            if source.get('status') in ('ready','failed'):break
            assert process_once()
        assert source['status']=='ready'
        return source
    transaction=upload('transaction.csv','法人,客户,日期,金额\nA,C1,2026-03-31,9.00\nA,C1,2026-04-01,5.00\nB,C1,2026-04-01,2.00\n')
    rules=upload('rules.csv','法人,客户,开始日期,结束日期,归属\nA,C1,2026-01-01,2026-04-01,旧部门\nA,C1,2026-04-01,2027-01-01,新部门\nB,C1,2026-01-01,2027-01-01,其他法人\n')
    turn=request('POST',root+'/messages',json={'text':'按法人和业务日期核对归属','request_id':uid(),
                                               'source_ids':[transaction['id'],rules['id']]})
    context={'project_id':pid,'turn_id':turn['id'],'actor_id':'owner'}
    def col(name,kind='text',**extra):return {'name':name,'data_type':kind,'meaning':name,**extra}
    def table(columns):return {'meaning':'已上传资料','grain':['nexus_row'],'columns':[col('nexus_row')]+columns}
    plan={'name':'按生效日关联','inputs':{
        'transactions':table([col('c_1'),col('c_2'),col('c_3','date'),col('c_4','decimal',unit='CNY',aggregation='additive')]),
        'rules':table([col('c_1'),col('c_2'),col('c_3','date'),col('c_4','date'),col('c_5')])},
        'stages':[{'kind':'lookup_effective','id':'mapped','description':'按法人客户及日期关联规则',
                   'input':'transactions','right':'rules',
                   'keys':[{'left':'c_1','right':'c_1'},{'left':'c_2','right':'c_2'}],
                   'business_date':'c_3','valid_from':'c_3','valid_to':'c_4',
                   'outputs':{'nexus_row':col('rule_row'),'c_3':col('effective_from','date'),
                              'c_4':col('effective_to','date'),'c_5':col('owner')}}], 'output':'mapped'}
    def node(name,source):return {'id':name,'kind':'source_table','title':'读取资料',
        'config':{'source_id':source['id'],'revision':source['revision'],'content_hash':source['content_hash']}}
    nodes=[node('transactions',transaction),node('rules',rules),
           {'id':'mapped','kind':'table_workflow','title':'关联有效规则','depends_on':['transactions','rules'],
            'inputs':{'transactions':{'source':'node','name':'transactions'},'rules':{'source':'node','name':'rules'}},
            'config':{'plan':plan}}]
    handlers={'source_table':Handler(source_table,True,True,validate_source),
              'table_workflow':Handler(table_workflow,True,True,validate_table)}
    result=run({'name':'集团规则核对流程','inputs':[],'nodes':nodes,'outputs':['mapped']},{},handlers,context,persist=lambda value:None)
    assert result['status']=='succeeded',[(step.get('node_id'),step.get('error_code')) for step in result['steps']]
    output=result['outputs']['mapped']
    assert [(r['c_1'],r['c_3'],r['c_4'],r['owner'],r['rule_row'],r['effective_from'],r['effective_to'])
            for r in output['rows']]==[
        ('A','2026-03-31','9.00','旧部门','1','2026-01-01','2026-04-01'),
        ('A','2026-04-01','5.00','新部门','2','2026-04-01','2027-01-01'),
        ('B','2026-04-01','2.00','其他法人','3','2026-01-01','2027-01-01')]
    assert output['requires_semantic_review'] is True and output['selection_lineage_complete'] is True
    assert {ref['source_id'] for ref in output['source_refs']}=={transaction['id'],rules['id']}
