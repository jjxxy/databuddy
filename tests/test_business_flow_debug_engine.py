import copy
import pytest

from agent_service.business_flow_engine import Handler
from agent_service.business_flow_debug_engine import run_debug


def test_debug_subset_is_explicit_frozen_and_nonpublishable():
    doc={'name':'测试','inputs':['value'],'nodes':[
        {'id':'read','kind':'read','title':'读取','inputs':{'v':{'source':'input','name':'value'}}},
        {'id':'calculate','kind':'calc','title':'计算','depends_on':['read'],'inputs':{'v':{'source':'node','name':'read'}}},
        {'id':'send','kind':'external','title':'外部发送','depends_on':['calculate'],'inputs':{'v':{'source':'node','name':'calculate'}}}], 'outputs':['send']}
    calls=[];saved=[]
    def operation(name):
        def run(_,config,args):calls.append(name);return {'value':args['v']}
        return run
    handlers={name:Handler(operation(name),True,name!='external') for name in ('read','calc','external')}
    run=run_debug(doc,{'value':'118.15'},handlers,{},['calculate','read'],allowed_kinds={'read','calc'},persist=saved.append)
    assert calls==['read','calc']
    assert run['publishable'] is False and run['kind']=='business_flow_debug'
    assert run['scope']['selected_nodes']==['read','calculate']
    assert run['state']['status']=='succeeded' and run['state']['outputs']['calculate']['value']['value']=='118.15'
    assert saved[-1]==run
    run_debug(doc,{'value':'118.15'},handlers,{},['read','calculate'],allowed_kinds={'read','calc'},checkpoint=run,persist=saved.append)
    assert calls==['read','calc']
    with pytest.raises(ValueError,match='缺少上游'):run_debug(doc,{},handlers,{},['calculate'],allowed_kinds={'read','calc'},persist=saved.append)
    with pytest.raises(ValueError,match='不允许'):run_debug(doc,{'value':'118.15'},handlers,{},['read','calculate','send'],allowed_kinds={'read','calc','external'},persist=saved.append)
    unstable=dict(handlers,read=Handler(operation('unstable'),False,True))
    checked=run_debug(doc,{'value':'118.15'},unstable,{},['read'],allowed_kinds={'read'},persist=saved.append)
    assert checked['state']['steps'][0]['deterministic'] is False
    corrupt=copy.deepcopy(run);corrupt['state']['steps'][0]['output']['value']='999'
    with pytest.raises(ValueError):run_debug(doc,{'value':'118.15'},handlers,{},['read','calculate'],allowed_kinds={'read','calc'},checkpoint=corrupt,persist=saved.append)
    with pytest.raises(ValueError,match='不一致'):run_debug(doc,{'value':'118.16'},handlers,{},['read','calculate'],allowed_kinds={'read','calc'},checkpoint=run,persist=saved.append)
    changed=copy.deepcopy(doc);changed['nodes'][0]['title']='另一个版本'
    with pytest.raises(ValueError,match='不一致'):run_debug(changed,{'value':'118.15'},handlers,{},['read','calculate'],allowed_kinds={'read','calc'},checkpoint=run,persist=saved.append)
    forged=copy.deepcopy(run);forged['publishable']=True
    with pytest.raises(ValueError,match='不一致'):run_debug(doc,{'value':'118.15'},handlers,{},['read','calculate'],allowed_kinds={'read','calc'},checkpoint=forged,persist=saved.append)
