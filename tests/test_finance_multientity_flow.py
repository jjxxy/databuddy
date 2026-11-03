"""A frozen two-file business flow: keys, evidence and exact values together."""
from fastapi.testclient import TestClient

from agent_service.api import app
from agent_service.business_flow_data import source_table, table_workflow, validate_source, validate_table
from agent_service.business_flow_engine import Handler, run
from agent_service.config import TOKEN
from agent_service.execution import process_once
from agent_service.store import delete, get, uid


def test_uploaded_multientity_mapping_preserves_keys_values_and_evidence(request):
    client=TestClient(app);headers={'Authorization':'Bearer '+TOKEN}
    def req(method,path,**kwargs):
        response=client.request(method,path,headers=headers,**kwargs)
        assert response.status_code==200,response.text
        return response.json()
    pid=req('POST','/api/projects',json={'name':'跨法人映射测试','request_id':uid()})['id']
    base='/api/projects/'+pid
    def upload(name,content):
        item=req('POST',base+'/attachments',files={'file':(name,content.encode('utf-8'),'text/csv')},
                 data={'request_id':uid(),'local_processing':'true'})
        for _ in range(100):
            current=get('source',item['id'])
            if current.get('status') in ('ready','failed'):break
            assert process_once()
        assert current['status']=='ready'
        return current
    sales=upload('sales.csv','法人,客户,期间,金额\nA,C1,2026-01,9.00\nB,C1,2026-01,5.00\nA,C1,2026-02,2.00\n')
    mapping=upload('mapping.csv','法人,客户,期间,归属\nA,C1,2026-01,东区\nB,C1,2026-01,南区\nA,C1,2026-02,西区\n')
    turn=req('POST',base+'/messages',json={'text':'按法人客户期间核对归属','request_id':uid(),
                                           'source_ids':[sales['id'],mapping['id']]})
    # This test exercises the flow directly rather than executing the turn.
    request.addfinalizer(lambda:delete('turn',turn['id']))
    context={'project_id':pid,'turn_id':turn['id'],'actor_id':'owner'}
    def col(name,kind='text',**extra):return {'name':name,'data_type':kind,'meaning':name,**extra}
    def tab(columns):return {'meaning':'已上传资料','grain':['nexus_row'],
                            'columns':[col('nexus_row')]+columns}
    plan={'name':'按法人客户期间关联','inputs':{
        'sales':tab([col('c_1'),col('c_2'),col('c_3'),col('c_4','decimal',unit='CNY',aggregation='additive')]),
        'mapping':tab([col('c_1'),col('c_2'),col('c_3'),col('c_4')])},
        'stages':[{'kind':'lookup_multi','id':'mapped','description':'按法人客户期间关联',
                   'input':'sales','right':'mapping',
                   'keys':[{'left':key,'right':key} for key in ('c_1','c_2','c_3')],
                   'outputs':{'c_4':col('owner')}}], 'output':'mapped'}
    def node(name,source):return {'id':name,'kind':'source_table','title':'读取资料',
        'config':{key:source[key] for key in ('source_id','revision','content_hash')}}
    # Uploaded source records use id; frozen nodes use source_id.
    nodes=[node('sales',dict(sales,source_id=sales['id'])),node('mapping',dict(mapping,source_id=mapping['id']))]
    nodes.append({'id':'mapped','kind':'table_workflow','title':'关联归属','depends_on':['sales','mapping'],
                  'inputs':{'sales':{'source':'node','name':'sales'},'mapping':{'source':'node','name':'mapping'}},
                  'config':{'plan':plan}})
    handlers={'source_table':Handler(source_table,True,True,validate_source),
              'table_workflow':Handler(table_workflow,True,True,validate_table)}
    result=run({'name':'跨法人业务流程','inputs':[],'nodes':nodes,'outputs':['mapped']},{},handlers,context,persist=lambda value:None)
    assert result['status']=='succeeded',[(step.get('node_id'),step.get('error_code')) for step in result['steps']]
    output=result['outputs']['mapped']
    assert [(r['c_1'],r['c_3'],r['owner'],r['c_4']) for r in output['rows']]==[
        ('A','2026-01','东区','9.00'),('B','2026-01','南区','5.00'),('A','2026-02','西区','2.00')]
    assert output['requires_semantic_review'] is True
    assert output['selection_lineage_complete'] is True
    assert {ref['source_id'] for ref in output['source_refs']}=={sales['id'],mapping['id']}
