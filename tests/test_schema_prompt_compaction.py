"""Model context stays smaller without changing allowed tools or schemas."""
import copy
import json

from agent_service import agent, agent_context, provider
from test_bounded_planning_context import turn


def test_schema_compaction_preserves_title_property_and_literal_data():
    schema={
        'title':'outer schema', 'type':'object', 'required':['title','spec'],
        'properties':{
            'title':{'title':'generated field label','type':'string','minLength':1},
            'spec':{'title':'nested label','allOf':[{'$ref':'#/$defs/title'}]},
        },
        '$defs':{'title':{'title':'definition label','type':'object',
                          'properties':{'title':{'title':'inner label','type':'string'}},
                          'required':['title']}},
        'default':{'title':'literal default'},
        'const':{'title':'literal constant'},
        'examples':[{'title':'literal example'}],
        'enum':[{'title':'literal option'}],
        'description':'Required finance and authorization constraint',
    }
    original=copy.deepcopy(schema)
    compact=agent._compact_model_schema(schema)
    assert schema==original
    assert compact['required']==['title','spec']
    assert compact['properties']['title']=={'type':'string','minLength':1}
    assert compact['$defs']['title']['properties']['title']=={'type':'string'}
    assert compact['$defs']['title']['required']==['title']
    for key in ('default','const','examples','enum','description'):assert compact[key]==schema[key]
    assert 'title' not in compact and 'title' not in compact['properties']['spec']


def test_office_template_schemas_load_only_for_template_work():
    regular=turn('读取销售明细并生成 Excel、Word 和 PPT 报告')
    assert not (agent_context.OFFICE_TEMPLATE_TOOLS & set(agent_context.selected(regular)))
    assert agent_context.OFFICE_TEMPLATE_TOOLS <= agent_context.allowed(regular)
    assert agent_context.discover(regular,agent_context.DiscoverTools(query='render_pptx_template',limit=1))['tools']==[agent_context.definition('render_pptx_template')]
    requested=turn('读取销售明细并用公司 PPT 模板生成报告')
    assert agent_context.OFFICE_TEMPLATE_TOOLS <= set(agent_context.selected(requested))
    attached={**turn('生成 PPT 报告'),'attached_sources':[{'id':'s_deck','name':'company.pptx'}]}
    assert agent_context.OFFICE_TEMPLATE_TOOLS <= set(agent_context.selected(attached))


def test_model_schema_compaction_keeps_types_constraints_and_authorization(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True,'engine':'nexus','tool_protocol':'json'})
    current=turn();messages,_=agent.planning_context(current,native=False)
    payload=json.loads(messages[1]['content'])
    for item in payload['tools']:
        assert item['arguments_schema']==agent._compact_model_schema(agent_context.definition(item['name'])['arguments_schema'])
    assert payload['response_schema']==agent._compact_model_schema(agent.response_schema(agent.Step))
    assert set(payload['tool_catalog'])==agent_context.allowed(current)
    assert '前25条' in messages[0]['content'] and '不得' in messages[0]['content']
