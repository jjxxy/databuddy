"""A real local pipeline must preserve long IDs at the browser JSON boundary."""
from fastapi.testclient import TestClient

from agent_service.api import app
from agent_service.codegen import generate
from agent_service.config import TOKEN
from agent_service.execution import enqueue, process_once
from agent_service.releases import publish
from agent_service.store import get, patch
from test_agent_handoff import project, source
from test_workspace_artifacts import clean


def test_local_pipeline_preview_and_published_json_keep_large_integer_ids_exact():
    pid = project()
    original = source(pid, b'[{"id":9007199254740993,"amount":1}]', name='large-id.json')
    patch('source', original['id'], skip_model_planning=True)
    enqueue('probe', original['id'])
    assert process_once()
    ready = get('source', original['id'])
    assert ready['status'] == 'ready'
    # The public pipeline API supplies the project-scoped source, rather than
    # repeating project_id. The generated record must retain that ownership.
    task = generate(ready, 'Long ID review', dict(ready['proposed_spec'], primary_key='c_1'),
                    release_mode='manual')
    assert task['project_id'] == pid
    enqueue('pipeline', task['id'], task['commit'])
    assert process_once()
    task = get('pipeline', task['id'])
    assert task['candidate_run'] and not task.get('published_run')
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer ' + TOKEN}
        preview = client.get('/api/pipelines/' + task['id'] + '/preview', headers=headers)
        assert preview.status_code == 200
        assert preview.json()['rows'][0]['c_1'] == '9007199254740993'
        publish(task['id'], task['candidate_run'])
        result = client.get('/api/pipelines/' + task['id'] + '/result', headers=headers)
        assert result.status_code == 200
        assert result.json()['rows'][0]['c_1'] == '9007199254740993'
