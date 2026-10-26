import json
import httpx
from agent_service import planner as planning
from agent_service.store import put,seal,get
from agent_service.config import DATA

def test_model_uses_schema_not_rows_and_rejects_invalid_plan(monkeypatch):
    (DATA/'uploads').mkdir(exist_ok=True)
    (DATA/'uploads'/'model-input.csv').write_text('订单号,客户,金额\nX1,私人客户名字,123.45\n')
    source=put('source',{'name':'input.csv','kind':'file','project_id':'planning-scope','secret_config':seal({'stored_name':'model-input.csv'})})
    put('memory',{'project_id':'planning-scope','note':'业务对象命名为订单','tags':[]})
    put('memory',{'project_id':'other-project','note':'另一个项目的私有约定','tags':[]})
    requests=[];invalid=[False]
    def handler(request):
        body=json.loads(request.content);requests.append(body)
        assert '私人客户名字' not in request.content.decode()
        assert source['secret_config'] not in request.content.decode()
        assert '另一个项目的私有约定' not in request.content.decode()
        response={'proposed_spec':{'object_label':'订单','primary_key':'unknown' if invalid[0] else 'c_1','group_by':'c_2','amount_field':'c_3'},'suggestions':[{'title':'按客户看金额','reason':'先确认业务口径','requires':['客户','金额']}]}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(response,ensure_ascii=False)}}]})
    original=httpx.Client
    monkeypatch.setattr(planning,'LLM_BASE','http://model.test/v1');monkeypatch.setattr(planning,'LLM_MODEL','test-model')
    monkeypatch.setattr(planning.httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(handler)))
    planning.planner.invoke({'source':source})
    assert get('source',source['id'])['planner_mode']=='llm'
    assert '业务对象命名为订单' in json.dumps(requests,ensure_ascii=False)
    invalid[0]=True
    planning.planner.invoke({'source':source})
    result=get('source',source['id'])
    assert result['planner_mode']=='rules' and result['warning']
    assert result['proposed_spec']['primary_key']=='c_1'
