"""Generated files are attached to their actual conversation, before catalog use."""
import copy
import hashlib
import io
import json

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from agent_service import api, conversation, office, workspace, workspace_artifacts as assets, media_tools
from agent_service.config import TOKEN
from agent_service.store import get, patch, put, uid
from test_opencode_agent_bridge import turn


def test_multiple_real_office_files_appear_in_conversation_and_open_without_catalog(turn,monkeypatch):
    pid=turn['project_id'];patch('project',pid,created_at=turn['created_at']);trace=[]
    for i in range(2):
        spec={'kind':'document','title':'报告'+str(i),'sections':[{'body':'精确数值：9007199254740993.000000000001'}]}
        workspace.write_file(pid,'report-'+str(i)+'.json',json.dumps(spec,ensure_ascii=False))
        output=office.render(turn,'report-'+str(i)+'.json','报告'+str(i))
        trace.append({'id':'output-'+str(i),'tool':'render_office_artifact','status':'done','arguments':{},'observation':output})
    saved=patch('turn',turn['id'],state='done',answer='成果已保存',agent_trace=trace)
    monkeypatch.setattr(assets,'catalog',lambda *args,**kwargs:pytest.fail('Conversation must not scan the whole artifact catalog'))
    public=conversation.public_turn(saved)
    assert [item['name'] for item in public['outputs']]==['报告0.docx','报告1.docx']
    assert 'private' not in json.dumps(public['outputs'])
    for item in public['outputs']:
        path,meta=assets.file(pid,item['id'])
        assert hashlib.sha256(path.read_bytes()).hexdigest()==item['sha256']
        assert assets.preview(pid,item['id'])['artifact']['id']==item['id']
    client=TestClient(api.app);response=client.get('/api/projects/'+pid+'/conversation',headers={'Authorization':'Bearer '+TOKEN})
    assert response.status_code==200,response.text
    actual=next(value for value in response.json()['turns'] if value['id']==turn['id'])
    assert len(actual['outputs'])==2


def test_media_receipt_is_inline_and_other_project_or_archived_files_are_not_exposed(turn):
    buffer=io.BytesIO();Image.new('RGB',(4,4),'blue').save(buffer,format='PNG')
    image=media_tools.save(turn['project_id'],buffer.getvalue(),'actual.png',turn_id=turn['id'])
    value=patch('turn',turn['id'],agent_trace=[{'id':'image','tool':'generate_image','status':'done','observation':image}])
    assert conversation.public_turn(value)['outputs'][0]['sha256']==hashlib.sha256(buffer.getvalue()).hexdigest()
    patch('workspace_artifact',image['artifact_id'],archived=True)
    assert not conversation.public_turn(value).get('outputs')
    patch('workspace_artifact',image['artifact_id'],archived=False,project_id='another-project')
    assert not conversation.public_turn(value).get('outputs')


def test_failed_fabricated_or_unfinished_delivery_never_appears_as_an_output(turn):
    value=patch('turn',turn['id'],agent_trace=[{'id':'fake','tool':'write_file','status':'done',
        'observation':{'artifact_id':'wa_'+'a'*44}},{'id':'failed','tool':'render_office_artifact','status':'failed',
        'observation':{'artifact_id':'of_'+'b'*40}}])
    assert not conversation.public_turn(value).get('outputs')
