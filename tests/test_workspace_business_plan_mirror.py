"""Project versions retain the readable plan without copying credentials."""
import json
from agent_service import workspace


def test_business_plan_is_in_project_version_and_secrets_are_not_copied(tmp_path,monkeypatch):
    source={'name':'finance.csv','kind':'file','revision':1,'status':'ready',
            'secret_config':'PRIVATE-CONNECTION','profile':{'fields':[]}}
    snapshot={'project':{'name':'Finance'},'sources':{'source1':source},
              'pipelines':{'app1':{'commit':'a'*40,'business_plan_confirmation_required':True}}}
    monkeypatch.setattr(workspace,'get',lambda kind,pid:{'repository':'/original'} if (kind,pid)==('pipeline','app1') else None)
    monkeypatch.setattr(workspace,'git',lambda repo,*args:'plan.json\nbusiness-plan.json\n.env\n')
    monkeypatch.setattr(workspace,'git_content',lambda repo,commit,name:json.dumps({'file':name}).encode())
    workspace._mirror(tmp_path,snapshot)
    assert json.loads((tmp_path/'pipelines'/'app1'/'business-plan.json').read_text())=={'file':'business-plan.json'}
    assert not (tmp_path/'pipelines'/'app1'/'.env').exists()
    state=json.loads((tmp_path/'project-state.json').read_text())
    assert state['pipelines']['app1']['business_plan_confirmation_required'] is True
    assert 'PRIVATE-CONNECTION' not in json.dumps(state)
    assert 'business_plan_confirmation_required' in workspace.PIPE_KEYS
