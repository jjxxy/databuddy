"""A review requirement is not evidence that a person reviewed it."""
import copy
import os

import pytest
from fastapi.testclient import TestClient

from agent_service import business_reports, project_data as data
from agent_service.api import app
from agent_service.jobs import get_job, save
from agent_service.store import put, uid
from test_source_semantic_review_guard import draft, indented_source
from test_project_data import project, registered, run


def acknowledge(pid, result, actor='owner'):
    return data.publish_result(pid,result['id'],result['commit'],review_actor=actor,
        review_handoff_sha256=result['data_result']['handoff_sha256'])


def test_pending_upstream_cannot_be_laundered_and_review_binds_full_receipt():
    pid=project();sid=indented_source(pid);first=run(pid,registered(pid,draft(sid)))
    assert first['status']=='validated' and first['data_result']['requires_source_review']
    # A mere marker does not permit interpreting pending upstream as reviewed.
    with pytest.raises(ValueError,match='人工复核'):data.read_input(pid,{'result_id':first['id']})
    with pytest.raises(ValueError):business_reports.inspect_result(pid,first['id'])
    with pytest.raises(ValueError,match='工作台'):data.publish_result(pid,first['id'],first['commit'])
    with pytest.raises(ValueError,match='刷新'):data.publish_result(pid,first['id'],first['commit'],review_actor='owner',review_handoff_sha256='0'*64)
    assert get_job(first['id'])['status']=='validated'
    acknowledge(pid,first)
    reviewed=get_job(first['id']);receipt=reviewed['source_review_receipt']
    assert reviewed['status']=='succeeded' and receipt['reviewer']=='owner'
    assert receipt['handoff_sha256']==first['data_result']['handoff_sha256']
    assert receipt['plan_sha256']==first['commit'] and receipt['run_id']==first['id']
    assert receipt['source_snapshot_sha256']==data.digest(reviewed['source_snapshot'])
    acknowledge(pid,first,actor='u_'+'1'*64)
    assert get_job(first['id'])['source_review_receipt']==receipt
    assert len(data.read_input(pid,{'result_id':first['id']})[0])==2
    doc=draft(sid);doc['bindings']['detail']={'result_id':first['id']}
    second=run(pid,registered(pid,doc,path='workflows/review-child.json'))
    assert second['data_result']['requires_source_review'] and second['status']=='validated'
    with pytest.raises(ValueError,match='人工复核'):data.read_input(pid,{'result_id':second['id']})
    acknowledge(pid,second)
    doc['bindings']['detail']['result_id']=second['id']
    third=run(pid,registered(pid,doc,path='workflows/review-grandchild.json'))
    assert third['data_result']['requires_source_review'] and third['status']=='validated'
    acknowledge(pid,third)
    assert business_reports.inspect_result(pid,third['id'])['server_adopted_result']


def test_historical_succeeded_is_not_human_review_and_keeps_previous_publication():
    pid=project();sid=indented_source(pid);wid=registered(pid,draft(sid));first=run(pid,wid);acknowledge(pid,first)
    later=run(pid,wid,use_published=True);assert later['status']=='validated'
    # An old automatic task could have succeeded without an authenticated review.
    forged=dict(get_job(later['id']),status='succeeded');forged.pop('source_review_receipt',None);save(forged,later['id'])
    with pytest.raises(ValueError,match='人工复核'):data.read_input(pid,{'result_id':later['id']})
    with pytest.raises(ValueError,match='人工复核'):business_reports.inspect_result(pid,later['id'])
    assert data.read_result(pid,later['id'])['total']==2
    assert data.owned(pid,'data_workflow',wid)['published_run']==first['id']
    assert data.read_input(pid,{'result_id':first['id']})[0]


def test_review_receipt_cannot_be_used_for_another_file_or_handoff(monkeypatch):
    pid=project();sid=indented_source(pid);first=run(pid,registered(pid,draft(sid)));acknowledge(pid,first)
    import agent_service.jobs as jobs
    original=jobs.get_job
    for changed in ('handoff_sha256','source_snapshot_sha256','input_evidence_sha256','reviewer','receipt_sha256'):
        def altered(ident,field=changed):
            value=original(ident)
            if ident==first['id']:
                value=copy.deepcopy(value);value['source_review_receipt'][field]='a'*64
            return value
        with monkeypatch.context() as m:
            m.setattr(jobs,'get_job',altered)
            with pytest.raises(ValueError,match='人工复核'):data.read_input(pid,{'result_id':first['id']})


def test_api_requires_explicit_confirm_bound_to_result_and_server_identity():
    pid=project();sid=indented_source(pid);first=run(pid,registered(pid,draft(sid)))
    client=TestClient(app);headers={'Authorization':'Bearer '+os.environ['APP_TOKEN']}
    url=f'/api/projects/{pid}/data-results/{first["id"]}/publish'
    body={'run_id':first['id'],'expected_plan_sha256':first['commit']}
    assert client.post(url,json=body).status_code in (401,403)
    missing=client.post(url,headers=headers,json=body);assert missing.status_code==400 and '工作台' in missing.json()['detail']
    wrong=dict(body,confirm_source_review=True,expected_handoff_sha256='0'*64)
    assert client.post(url,headers=headers,json=wrong).status_code==400
    spoof=dict(body,confirm_source_review=True,expected_handoff_sha256=first['data_result']['handoff_sha256'],review_actor='fake')
    assert client.post(url,headers=headers,json=spoof).status_code==422
    body.update(confirm_source_review=True,expected_handoff_sha256=first['data_result']['handoff_sha256'])
    accepted=client.post(url,headers=headers,json=body);assert accepted.status_code==200,accepted.text
    assert get_job(first['id'])['source_review_receipt']['reviewer']=='owner'


def _original_path(source):
    from agent_service.config import DATA
    from agent_service.store import unseal
    path=(DATA/'uploads'/unseal(source['secret_config'])['stored_name']).resolve()
    assert path.is_relative_to((DATA/'uploads').resolve())
    return path


@pytest.mark.parametrize('mutation',['replace','delete'])
def test_new_review_refuses_changed_or_missing_original_but_prior_review_still_usable(mutation):
    from agent_service.store import get
    pid=project();sid=indented_source(pid);wid=registered(pid,draft(sid))
    original=get('source',get('source',sid)['origin_source_id']);path=_original_path(original)
    first=run(pid,wid);acknowledge(pid,first)
    second=run(pid,wid,use_published=True)
    if mutation=='delete':path.unlink()
    else:path.write_text('tampered',encoding='utf-8')
    with pytest.raises(ValueError):acknowledge(pid,second)
    assert get_job(second['id'])['status']=='validated'
    assert data.owned(pid,'data_workflow',wid)['published_run']==first['id']
    # An immutable old confirmation is already bound to its historical input;
    # retrying that same adoption does not claim the file is still accessible.
    old_receipt=get_job(first['id'])['source_review_receipt']
    acknowledge(pid,first)
    assert get_job(first['id'])['source_review_receipt']==old_receipt
    assert len(data.read_input(pid,{'result_id':first['id']})[0])==2


def test_manual_visual_derived_source_rechecks_original_image(tmp_path):
    from agent_service import visual_fact_service as visual
    from agent_service.store import get
    from test_fact_service import document
    from test_visual_fact_service import image_bytes,input_value,approval
    from test_project_data import column
    pid,sid,_=document(image_bytes(),'.png')
    candidate=visual.propose(pid,input_value(sid))
    approved=visual.review(pid,candidate['id'],approval(candidate),'reviewer')
    derived=approved['output_source_id']
    _,fields,_,_=data.read_input(pid,{'source_id':derived})
    names=[f['name'] for f in fields]
    schema={'columns':[column(name) for name in names],'grain':[names[0]],'meaning':'视觉人工转录'}
    doc={'version':1,'plan':{'name':'视觉人工转录数据','inputs':{'detail':schema},
        'stages':[{'kind':'select','id':'keep','description':'保留转录数据','input':'detail','columns':names}],
        'output':'keep'},'bindings':{'detail':{'source_id':derived}}}
    result=run(pid,registered(pid,doc,path='workflows/visual-review.json'))
    # The derived JSON still exists but is not a substitute for its image.
    # Separately verify the source-chain check even though fact publication is
    # conservatively blocked until a full semantic-alignment receipt exists.
    data._verify_review_originals(pid,get_job(result['id']))
    _original_path(get('source',sid)).unlink()
    with pytest.raises(ValueError,match='原件|来源|文件'):
        data._verify_review_originals(pid,get_job(result['id']))
    with pytest.raises(ValueError,match='语义对齐'):acknowledge(pid,result)
    assert get_job(result['id'])['status']=='validated'


def test_unversioned_external_source_review_fails_without_network(monkeypatch):
    import socket
    pid=project();sid='external_fixture';doc=draft(sid)
    run={'workflow_document':doc,'source_snapshot':{sid:{'id':sid,'project_id':pid,'kind':'api','name':'external'}}}
    def forbidden(*args,**kwargs):raise AssertionError('must not access network')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    with pytest.raises(ValueError,match='外部来源'):
        data._verify_review_originals(pid,run)
