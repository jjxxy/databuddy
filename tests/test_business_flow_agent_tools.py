"""Conversation tools create drafts; they never publish or impersonate actors."""
import pytest
from agent_service import business_flow_tools as tools
from agent_service.store import put,get,uid
from agent_service.actor_context import actor_scope


def test_agent_create_uses_stable_server_step_and_marks_only_draft(monkeypatch):
    from agent_service import business_flows
    pid='p_'+uid();tid='t_'+uid();step='step_'+uid();fid='bf_'+'a'*40
    put('turn',{'project_id':pid,'actor_id':'owner','state':'agent_tool',
                'agent_trace':[{'id':step,'status':'started'}]},tid)
    calls=[]
    def create(project,actor,turn,document,request_id):
        calls.append((project,actor,turn,document,request_id))
        return {'id':fid,'name':'净收入','candidate_digest':'b'*64,'candidate_commit':'c'*40}
    monkeypatch.setattr(business_flows,'create',create)
    doc={'version':1,'name':'净收入','inputs':[],'nodes':[{'id':'one','kind':'source_table','title':'读取','depends_on':[],'inputs':{},'config':{}}],'outputs':['one']}
    turn=get('turn',tid)
    with actor_scope('owner'):
        first=tools.execute(turn,'create_business_flow',{'document':doc})
        second=tools.execute(turn,'create_business_flow',{'document':doc})
    assert first==second and first['state']=='draft' and len(calls)==2
    assert calls[0][-1]==calls[1][-1] and calls[0][0:3]==(pid,'owner',tid)
    assert get('turn',tid)['business_flow']=={'id':fid,'name':'净收入'}
    with actor_scope('u_'+'f'*64):
        with pytest.raises(ValueError,match='用户'):tools.execute(turn,'create_business_flow',{'document':doc})


def test_guide_uses_actual_schema_and_does_not_publish():
    turn={'project_id':'p_example','actor_id':'owner','id':'t_example'}
    with actor_scope('owner'):result=tools.execute(turn,'business_flow_guide',{})
    assert {'source_table','table_workflow','table_report_spec','html_report','office_document','model','skill'}<=set(result['nodes'])
    assert result['nodes']['model']['nondeterministic'] is True
    assert '尚未执行或发布' in result['notice']
