"""Large document previews are bounded; original pages remain exact and scoped."""
import hashlib
import json

import pytest

from agent_service import agent, provider
from agent_service.config import DATA
from agent_service.sources import revision
from agent_service.store import now, patch, put, seal, uid
from test_project_data import project


def json_source(pid, text):
    raw=text.encode('utf-8');stored=uid()+'.json'
    folder=DATA/'uploads';folder.mkdir(parents=True,exist_ok=True)
    (folder/stored).write_bytes(raw)
    source=put('source',{'project_id':pid,'name':'资料.json','kind':'file',
        'secret_config':seal({'stored_name':stored}),'content_hash':hashlib.sha256(raw).hexdigest(),
        'bytes':len(raw),'status':'ready','created_at':now()},'s_'+uid())
    return revision(source),folder/stored


def invoke(turn, name, arguments):
    schema=agent.TOOLS[name][0]
    return agent.execute(turn,agent.Step(kind='tool',tool=name,arguments=schema.model_validate(arguments).model_dump()))


def test_large_json_document_is_bounded_but_pages_reconstruct_verified_original(monkeypatch):
    monkeypatch.setattr(agent,'authorize',lambda *args:None)
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    pid=project();raw=json.dumps({'title':'多表原始文档','contents':'内容😀\\\"'*2500},ensure_ascii=False)
    source,path=json_source(pid,raw);turn={'project_id':pid}
    preview=invoke(turn,'source_preview',{'source_id':source['id']})
    field=preview['fields'][0]['name'];assert preview['rows'][0][field]==raw[:4000]
    assert preview['value_truncated'] is True
    assert preview['truncated'] is True
    assert preview['truncated_field']['total_characters']==len(raw)
    assert '不能将片段当作完整文档' in preview['notice']
    reference=preview['read_original_with'];assert reference['tool']=='read_source_text'
    from agent_service.agent_context import trace_context
    context=trace_context({'id':'t_fixture','agent_trace':[{'id':'st_preview','tool':'source_preview','status':'done','arguments':{},'observation':preview}]},lambda value:value)[0]['observation']
    if context.get('not_full_result'):
        assert 'read_source_text' in context['context_excerpt']
        assert context['recover_with']['step_id']=='st_preview'
    else:assert context['read_original_with']==reference
    assert len(json.dumps(preview,ensure_ascii=False))<12000
    original=[];offset=0
    while True:
        args={key:value for key,value in reference.items() if key!='tool'};args['offset']=offset
        page=invoke(turn,'read_source_text',args)
        original.append(page['content']);assert page['sha256']==source['content_hash']
        assert page['complete_document_in_this_page'] is False
        if page['next_offset'] is None:break
        assert page['next_offset']>offset;offset=page['next_offset']
    assert ''.join(original)==raw
    path.write_bytes(b'{"changed":true}')
    with pytest.raises(ValueError,match='内容与冻结版本不一致'):
        invoke(turn,'read_source_text',{key:value for key,value in reference.items() if key!='tool'})


def test_paged_json_read_is_project_scoped_version_bound_and_share_gated(monkeypatch):
    monkeypatch.setattr(agent,'authorize',lambda *args:None)
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    pid=project();source,_=json_source(pid,'{"value":"hello"}');turn={'project_id':pid}
    args={'source_id':source['id'],'revision':source['revision'],'sha256':source['content_hash']}
    page=invoke(turn,'read_source_text',args)
    assert page['content']=='{"value":"hello"}' and page['next_offset'] is None
    assert page['complete_document_in_this_page'] is True
    with pytest.raises(ValueError,match='不属于当前项目'):
        invoke({'project_id':project()},'read_source_text',args)
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':False})
    with pytest.raises(ValueError,match='尚未启用'):
        invoke(turn,'read_source_text',args)
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    patch('source',source['id'],revision=source['revision']+1)
    with pytest.raises(ValueError,match='版本或内容摘要已变化'):
        invoke(turn,'read_source_text',args)


def test_real_authorization_keeps_frozen_attachment_and_exposes_paging(monkeypatch):
    from agent_service import agent_context, agent_gateway, agent_team, capability_guidance
    from agent_service.conversation import snapshot_attachment

    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    pid=project();original='{"data":"'+('资料'*3000)+'"}'
    source,_=json_source(pid,original)
    for mode in ('chat','work'):
        turn={'project_id':pid,'text':'阅读这份资料','interaction_mode':mode,
              'source_ids':[source['id']],'attached_sources':[snapshot_attachment(source)]}
        preview=invoke(turn,'source_preview',{'source_id':source['id']})
        ref=preview['read_original_with']
        turn['agent_trace']=[{'tool':'source_preview','status':'done','observation':preview}]
        assert 'read_source_text' in agent_context.allowed(turn)
        assert 'read_source_text' in agent_context.selected(turn)
        assert 'data' in capability_guidance.context_for(turn,['read_source_text'])['guidance']
        page=invoke(turn,'read_source_text',{k:v for k,v in ref.items() if k!='tool'})
        assert page['content']==original[:4000] and page['next_offset']==4000
        changed=patch('source',source['id'],revision=source['revision']+1)
        new_args={'source_id':source['id'],'revision':changed['revision'],'sha256':changed['content_hash']}
        with pytest.raises(ValueError,match='任务附件已变更'):
            invoke(turn,'read_source_text',new_args)
        patch('source',source['id'],revision=source['revision'])
    for invalid in (None,'not a mapping',[],17):
        turn['agent_trace']=[{'tool':'source_preview','status':'done','observation':{'read_original_with':invalid}}]
        agent_context.selected(turn)
    assert 'read_source_text' in agent_gateway.READ_TOOLS
    assert 'read_source_text' in agent_team.SOURCE_TOOLS
