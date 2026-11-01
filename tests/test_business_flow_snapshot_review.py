"""Human review cannot adopt a missing, corrupted, or foreign data snapshot."""
import pytest
from agent_service import business_flow_data, business_flows
from agent_service.business_flow_engine import digest
from agent_service.config import DATA
from agent_service.store import seal,uid


def test_publish_verification_checks_encrypted_table_and_run_scope(monkeypatch):
    monkeypatch.setattr(business_flow_data,'_INLINE_ROWS_LIMIT',0)
    context={'project_id':'p_'+uid(),'actor_id':'owner','turn_id':'t_'+uid(),'run_id':uid()}
    output=business_flow_data._compact_table(context,{'kind':'source_table_v1','complete':True,
                                                     'rows':[{'value':'12.50'}]})
    doc={'name':'核对','nodes':[{'id':'read','kind':'source_table','title':'来源',
         'config':{'source_id':'s_'+uid(),'revision':1,'content_hash':'a'*64}}], 'outputs':['read']}
    monkeypatch.setattr(business_flows,'_read_commit',lambda *args:doc)
    job={'id':context['run_id'],'project_id':context['project_id'],'actor_id':context['actor_id'],
         'flow_turn_id':context['turn_id'],'target':'bf_'+uid(),'commit':'f'*64,
         'project_commit':'b'*40,'secret_flow_payload':seal({'inputs':{}})}
    state={'status':'succeeded','flow_sha256':job['commit'],'inputs_sha256':digest({}),
           'steps':[{'node_id':'read','kind':'source_table','status':'succeeded',
                     'output':output,'output_sha256':digest(output)}], 'outputs':{'read':output}}
    business_flows._verify_completed_state(context['project_id'],job,state)
    with pytest.raises(ValueError,match='不一致'):
        business_flows._verify_completed_state(context['project_id'],dict(job,id=uid()),state)
    ref=output['rows_ref'];path=DATA/'business-flow-tables'/ref['scope']/(ref['sha256']+'.enc')
    original=path.read_bytes();changed=bytearray(original);changed[-1]^=1;path.write_bytes(changed)
    with pytest.raises(ValueError,match='校验失败'):
        business_flows._verify_completed_state(context['project_id'],job,state)
    path.unlink()
    with pytest.raises(ValueError,match='丢失'):
        business_flows._verify_completed_state(context['project_id'],job,state)
