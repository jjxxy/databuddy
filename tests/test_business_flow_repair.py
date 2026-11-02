from agent_service.business_flow_repair import guidance


def test_business_failure_diagnosis_does_not_invent_corrections():
    result=guidance({'status':'failed','steps':[{'node_id':'n1','status':'failed','error_code':'UNMAPPED_VALUE'}]})
    assert result['node_id']=='n1' and result['action']=='inspect_source_and_plan'
    assert result['retry_allowed'] is False and '不能模糊匹配' in result['next_step']


def test_unknown_external_outcome_blocks_retry_even_if_other_node_failed():
    result=guidance({'status':'failed','steps':[{'status':'failed','error_code':'TYPE_MISMATCH'},{'status':'unknown'}]})
    assert result['status']=='blocked_unknown' and result['retry_allowed'] is False


def test_success_and_unrecognized_error_never_claim_repair():
    assert guidance({'status':'succeeded','steps':[]})['status']=='not_diagnosed'
    result=guidance({'status':'failed','steps':[{'status':'failed','error_code':'OTHER'}]})
    assert result['action']=='inspect_failure' and result['retry_allowed'] is False


def test_quality_gates_do_not_suggest_changing_thresholds_or_deleting_data():
    required=guidance({'status':'failed','steps':[{'status':'failed','error_code':'CHECK_REQUIRED_FAILED'}]})
    unique=guidance({'status':'failed','steps':[{'status':'failed','error_code':'CHECK_UNIQUE_FAILED'}]})
    limit=guidance({'status':'failed','steps':[{'status':'failed','error_code':'CHECK_RANGE_FAILED'}]})
    assert '不能猜测' in required['next_step']
    assert '不得擅自删除' in unique['next_step']
    assert '不能为了通过检查' in limit['next_step']


def test_identical_source_revision_requires_bytes_metadata_and_live_permissions(tmp_path):
    import copy,hashlib
    from agent_service.business_flow_repair import verify_identical_source_revision
    content=b'account,amount\na,12.00\n';sha=hashlib.sha256(content).hexdigest()
    (tmp_path/'old.csv').write_bytes(content);(tmp_path/'new.csv').write_bytes(content)
    current={'id':'s1','project_id':'p1','kind':'file','status':'ready','revision':2,'content_hash':sha,
             'profile':{'fields':[{'name':'amount','unit':'CNY'}]},'secret_config':{'stored_name':'new.csv','sheet':'Sheet1'}}
    old={**current,'id':'v1','source_id':'s1','revision':1,'secret_config':{'stored_name':'old.csv','sheet':'Sheet1'}}
    check=lambda a,b:verify_identical_source_revision(a,b,'p1','s1',sha,tmp_path,lambda v:v)
    assert check(old,current)==2
    for key,value in [('archived',True),('project_id','other'),('status','revoked'),('profile',{'fields':[]}),('secret_config',{'stored_name':'new.csv','sheet':'other'})]:
        changed=copy.deepcopy(current);changed[key]=value;assert check(old,changed) is None
    bad=copy.deepcopy(old);bad['secret_config']['stored_name']='../old.csv';assert check(bad,current) is None
    (tmp_path/'new.csv').write_bytes(b'changed');assert check(old,current) is None
    (tmp_path/'old.csv').unlink();assert check(old,current) is None


def test_restricted_diagnostic_uses_repeatable_id_and_checks_owner(monkeypatch):
    from agent_service import business_flow_repair as repair,business_flows,conversation,store,actor_context,agent_context
    digest='b'*64;fid='bf_'+'a'*40;job={'project_id':'p1','actor_id':'owner','target':fid,'id':'run1','kind':'business_flow','flow_turn_id':'t1'}
    receipt={'status':'failed','plan_digest':digest,'commit':'abc','steps':[{'status':'failed','error_code':'TYPE_MISMATCH'}]}
    monkeypatch.setattr(actor_context,'current_actor',lambda:'owner')
    monkeypatch.setattr(business_flows,'get_run',lambda *a:receipt)
    monkeypatch.setattr(business_flows,'get_view',lambda *a:{'candidate_digest':digest,'candidate_commit':'abc'})
    monkeypatch.setattr(store,'get',lambda *a:{'project_id':'p1','actor_id':'owner','session_id':'c1'})
    calls=[]
    def submit(pid,value,**kw):calls.append((pid,value,kw));return {'id':value.request_id}
    monkeypatch.setattr(conversation,'submit',submit)
    one=repair.enqueue_diagnostic(job);two=repair.enqueue_diagnostic(job)
    assert one==two and calls[0][2]=={'trusted_fields':{'auto_diagnostic':True}}
    assert calls[0][1].workflow_context.digest==digest and calls[0][1].session_id=='c1'
    assert not ({'publish_result','run_business_flow','write_file','prepare_work_action'} & agent_context.allowed({'interaction_mode':'work','auto_diagnostic':True}))
    monkeypatch.setattr(actor_context,'current_actor',lambda:'another')
    import pytest
    with pytest.raises(ValueError):repair.enqueue_diagnostic(job)


def test_diagnostic_does_not_launch_for_unknown_or_changed_version(monkeypatch):
    from agent_service import business_flow_repair as repair,business_flows,actor_context
    job={'project_id':'p1','actor_id':'owner','target':'bf_'+'a'*40,'id':'run1','kind':'business_flow','flow_turn_id':'t1'}
    monkeypatch.setattr(actor_context,'current_actor',lambda:'owner')
    monkeypatch.setattr(business_flows,'get_run',lambda *a:{'status':'unknown','steps':[]})
    assert repair.enqueue_diagnostic(job) is None
    monkeypatch.setattr(business_flows,'get_run',lambda *a:{'status':'failed','plan_digest':'old','steps':[{'status':'failed'}]})
    monkeypatch.setattr(business_flows,'get_view',lambda *a:{'candidate_digest':'new'})
    assert repair.enqueue_diagnostic(job) is None


def test_debug_diagnostic_reads_debug_receipt_and_uses_read_only_tools(monkeypatch):
    from agent_service import business_flow_repair as repair,business_flows,business_flow_debug,conversation,store,actor_context,agent_context
    digest='b'*64;fid='bf_'+'a'*40
    job={'project_id':'p1','actor_id':'owner','target':'isolated_debug','flow_id':fid,'id':'run2','kind':'business_flow_debug','flow_turn_id':'t1'}
    monkeypatch.setattr(actor_context,'current_actor',lambda:'owner')
    monkeypatch.setattr(business_flow_debug,'status',lambda p,f,r,a:{'status':'failed','plan_digest':digest,'commit':'abc','steps':[{'status':'failed','error_code':'TYPE_MISMATCH'}]})
    monkeypatch.setattr(business_flows,'get_view',lambda *a:{'candidate_digest':digest,'candidate_commit':'abc'})
    monkeypatch.setattr(store,'get',lambda *a:{'project_id':'p1','actor_id':'owner','session_id':'c1'})
    calls=[]
    monkeypatch.setattr(conversation,'submit',lambda p,v,**k:calls.append((p,v,k)) or {'id':'turn'})
    assert repair.enqueue_diagnostic(job)=={'id':'turn'}
    assert 'inspect_business_flow_debug_run' in calls[0][1].text
    assert 'inspect_business_flow_debug_run' in agent_context.allowed({'interaction_mode':'work','auto_diagnostic':True})
