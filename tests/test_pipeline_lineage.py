"""Desktop provenance must describe one real release, never mix draft joins."""
from fastapi.testclient import TestClient

from agent_service.api import app
from agent_service.config import TOKEN
from agent_service.execution import process_once
from agent_service.store import get, uid


def test_formal_lineage_keeps_frozen_join_sources_after_candidate_changes():
    client=TestClient(app)
    headers={'Authorization':'Bearer '+TOKEN}
    project=client.post('/api/projects',headers=headers,json={
        'name':'冻结来源溯源回归','request_id':uid()}).json()

    def upload(name,content):
        response=client.post('/api/projects/'+project['id']+'/attachments',headers=headers,
            files={'file':(name,content,'text/csv')},
            data={'request_id':uid(),'local_processing':'true'})
        assert response.status_code==200,response.text
        assert process_once()
        value=get('source',response.json()['id'])
        assert value['status']=='ready'
        return value

    main=upload('orders.csv',b'id,customer,amount\nA,C1,10.25\nB,C2,20.50\n')
    original=upload('original-customers.csv',b'customer,region\nC1,East\nC2,West\n')
    revised=upload('revised-customers.csv',b'customer,region\nC1,North\nC2,South\n')
    spec=dict(main['proposed_spec'],primary_key='c_1',group_by=None,amount_field=None,
        joins=[{'source_id':original['id'],'left_field':'c_2','right_field':'c_1',
                'label':'客户','required_match':True}])
    response=client.post('/api/pipelines',headers=headers,json={
        'source_id':main['id'],'name':'客户订单来源','spec':spec,'release_mode':'manual'})
    assert response.status_code==200,response.text
    pipeline_id=response.json()['id']
    assert process_once()
    initial=get('pipeline',pipeline_id)
    route='/api/pipelines/'+pipeline_id
    candidate=client.get(route+'/lineage',headers=headers).json()
    assert set(candidate['source_ids'])=={main['id'],original['id']}
    assert 'run_id' not in candidate

    response=client.post(route+'/publish',headers=headers,json={'run_id':initial['candidate_run']})
    assert response.status_code==200,response.text
    spec['joins'][0]['source_id']=revised['id']
    response=client.put(route,headers=headers,json={
        'source_id':main['id'],'name':'客户订单来源','spec':spec,'release_mode':'manual'})
    assert response.status_code==200,response.text
    assert process_once()
    current=get('pipeline',pipeline_id)
    assert set(current['source_ids'])=={main['id'],revised['id']}
    assert current['published_run']==initial['candidate_run']
    assert current['commit']!=initial['commit']
    assert current['candidate_run']!=initial['candidate_run']

    assert client.get(route+'/lineage').status_code==401
    response=client.get(route+'/lineage',headers=headers)
    assert response.status_code==200,response.text
    formal=response.json()
    assert formal['run_id']==initial['candidate_run']
    assert formal['active_commit']==initial['commit']
    assert formal['candidate_commit']==current['commit']
    assert set(formal['source_ids'])=={entry['source_id'] for entry in formal['inputs']}
    assert set(formal['source_ids'])=={main['id'],original['id']}
    assert revised['id'] not in formal['source_ids']
