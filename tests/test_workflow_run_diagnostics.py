"""Failed queued workflows expose repairable evidence without inventing results."""
import copy,hashlib,json

import pytest

from agent_service import agent,data_tools,project_data as data,provider,tasks,workspace
from agent_service.config import DATA
from agent_service.execution import process_once
from agent_service.jobs import get_job,save
from agent_service.sources import revision
from agent_service.store import get,put,patch,all_items,uid,seal,now
from test_mixed_percentage_source import workbook
from test_project_data import project,registered


@pytest.fixture(autouse=True)
def finish_only_this_test_diagnostic_turns():
    # tick() is a real global worker. Preserve receipts/logs, but do not let an
    # unfinished synthetic diagnostic consume a later test's mocked model.
    assert DATA.resolve().name.startswith('atlas-tests-')
    previous={value['id'] for value in all_items('turn')}
    yield
    for value in all_items('turn'):
        if value['id'] not in previous and value.get('state') not in ('done','failed'):
            patch('turn',value['id'],state='done')


def source(pid,path):
    name=uid()+'.xlsx';target=DATA/'uploads'/name;target.parent.mkdir(parents=True,exist_ok=True)
    raw=path.read_bytes();target.write_bytes(raw)
    return revision(put('source',{'project_id':pid,'kind':'file','name':'rates.xlsx','status':'ready',
        'secret_config':seal({'stored_name':name}),'bytes':len(raw),'content_hash':hashlib.sha256(raw).hexdigest()},'s_'+uid()))


def document(sid):
    return {'version':1,'plan':{'name':'Synthetic percentage consistency diagnostic','inputs':{'rates':{
        'meaning':'One observed rate per unique id','grain':['id'],'columns':[
            {'name':'id','data_type':'text','meaning':'observation id'},
            {'name':'rate','data_type':'text','meaning':'source percentage representation'}]}},
        'stages':[{'id':'kept','kind':'select','description':'Preserve each complete validated observation',
                   'input':'rates','columns':['id','rate']}],'output':'kept'},
        'bindings':{'rates':{'source_id':sid,'options':{'cell_range':'A1:B5','header_rows':[1]},
                            'column_map':{'c_1':'id','c_2':'rate'}}}}


def task(pid):
    value=put('turn',{'project_id':pid,'text':'请汇总加工全部输入并运行方案，先生成候选，不要发布。',
        'interaction_mode':'work','state':'agent','created_at':now()},'t_'+uid())
    tasks.ensure(value)
    return get('turn',value['id'])


def run_failed(pid,wid):
    value=task(pid)
    agent._execute_node({'turn_id':value['id'],'step':{'kind':'tool','tool':'run_data_workflow','arguments':{'workflow_id':wid}}})
    waiting=get('turn',value['id']);assert waiting['state']=='waiting_data_workflow'
    assert process_once()
    run=get_job(waiting['data_workflow_job']);assert run['status']=='failed' and not run.get('validated')
    assert data_tools.resume(waiting)
    completed=get('turn',value['id']);assert completed['agent_trace'][-1]['status']=='failed'
    return completed,run,completed['agent_trace'][-1]['observation']


def test_percentage_failure_wait_resume_and_inspection_expose_real_repair_without_result(tmp_path,monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    pid=project();value=source(pid,workbook(tmp_path));doc=document(value['id'])
    wid=registered(pid,doc,path='workflows/rates.json')
    turn,run,observation=run_failed(pid,wid)
    assert observation['result_available'] is False
    assert observation['workflow_failure']['stage_id']=='input:rates'
    assert observation['workflow_failure']['code']=='MIXED_PERCENTAGE_REPRESENTATION'
    details=observation['workflow_failure']['details']
    assert details['excel_column']=='B' and details['source_column']=='c_2' and details['column']=='rate'
    assert 'bindings.rates.options.percentage_columns' in observation['next_step']
    assert 'fraction' in observation['next_step'] and '不能猜测' in observation['next_step']
    assert not {'rows','data_result','download'}&set(observation)
    latest=data.inspect_workflow(pid,wid)['latest_run']
    assert latest['run_id']==run['id'] and latest['matches_current_draft']
    assert latest['workflow_failure']==observation['workflow_failure']
    with pytest.raises(ValueError,match='inspect_data_workflow'):
        data_tools.execute(turn,'read_data_workflow_result',{'run_id':run['id'],'offset':0,'limit':25})
    with pytest.raises(ValueError,match='未验证'):data.result_folder(pid,run['id'])
    assert get('data_workflow',wid)['published_run'] is None and not tasks.verify(get('turn',turn['id']))['verified']
    # A synthetic unit-test repair, not a hand-authored financial benchmark.
    updated=copy.deepcopy(doc);updated['bindings']['rates']['options']['percentage_columns']={'c_2':'fraction'}
    workspace.write_file(pid,'workflows/rates.json',json.dumps(updated));data.register(pid,'workflows/rates.json')
    assert data.inspect_workflow(pid,wid)['latest_run']['matches_current_draft'] is False
    _,second,second_observation=run_failed(pid,wid)
    assert second['error_code']=='INPUT_PERCENTAGE_FORMAT_MISMATCH'
    second_failure=second_observation['workflow_failure']
    assert second_failure['stage_id']=='input:rates' and second_failure['details']['column']=='rate'
    assert second_failure['details']['required_value_format']=='percent_fraction'
    assert 'plan.inputs.rates.columns' in second_observation['next_step'] and '不能保留text/native' in second_observation['next_step']
    assert not second_observation['result_available'] and get('data_workflow',wid)['published_run'] is None


@pytest.fixture
def failed_case(tmp_path,monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'share_data':True})
    pid=project();value=source(pid,workbook(tmp_path));wid=registered(pid,document(value['id']),path='workflows/rates.json')
    turn,run,observation=run_failed(pid,wid)
    return pid,wid,turn,run


@pytest.mark.parametrize('tamper',['project','kind','target','version','frozen_document'])
def test_run_diagnostic_is_scoped_to_owned_flow_kind_and_frozen_version(failed_case,tamper):
    pid,wid,turn,run=failed_case;altered=dict(get_job(run['id']))
    if tamper=='project':altered['project_id']=project()
    elif tamper=='kind':altered['kind']='pipeline'
    elif tamper=='target':altered['target']='wf_wrong_target'
    elif tamper=='version':altered['commit']='f'*64
    else:altered['workflow_document']={'private':'DO_NOT_ECHO_BROKEN_DOCUMENT'}
    save(altered)
    with pytest.raises(ValueError) as error:data.workflow_run_diagnostic(pid,wid,run['id'])
    assert 'DO_NOT_ECHO' not in str(error.value)
    with pytest.raises(ValueError):data.workflow_run_diagnostic(project(),wid,run['id'])


def test_diagnostic_never_echoes_raw_messages_source_values_urls_or_unknown_details(failed_case):
    pid,wid,turn,run=failed_case
    secret='sk-FIXTURE-PRIVATE-0123456789';url='https://private.invalid/?api_key='+secret
    save(dict(get_job(run['id']),error=url,workflow_failure={'stage_id':'input:rates','code':'MIXED_PERCENTAGE_REPRESENTATION',
        'message':'raw cell '+secret+' Traceback '+url,'details':{'column':'rate','source_column':'c_2','excel_column':'B',
            'formats':['General','0.00%','0"%"',url,'"private original value"'],'raw_value':secret,'stack':url,'input_alias':url}}))
    result=data.workflow_run_diagnostic(pid,wid,run['id']);encoded=json.dumps(result)
    assert secret not in encoded and url not in encoded and 'Traceback' not in encoded and 'private original value' not in encoded
    assert result['workflow_failure']['details']['formats']==['General','0.00%','0"%"']
    assert result['workflow_failure']['details']['input_alias']=='rates'
    save(dict(get_job(run['id']),workflow_failure=None,error=url,error_code='TASK_FAILED'))
    fallback=data.workflow_run_diagnostic(pid,wid,run['id'])
    assert fallback['workflow_failure']['code']=='TASK_FAILED' and secret not in json.dumps(fallback)


def test_waiting_tool_cannot_claim_another_workflow_failure(failed_case):
    pid,wid,turn,run=failed_case
    waiting=copy.deepcopy(turn);waiting['state']='waiting_data_workflow';waiting['data_workflow_job']=run['id']
    waiting['agent_trace'][-1]['arguments']['workflow_id']='wf_another_flow'
    with pytest.raises(ValueError,match='当前待处理步骤不一致'):data_tools.resume(waiting)


def test_inexact_sql_failure_preserves_static_safe_decimal_repair_guidance(failed_case):
    pid,wid,turn,run=failed_case
    marker='SYNTHETIC_PRIVATE_SQL_AND_CELL'
    save(dict(get_job(run['id']),error=marker,workflow_failure={
        'stage_id':'kept','code':'SQL_INEXACT_NUMERIC','message':marker,'details':{'sql':marker}}))
    result=data.workflow_run_diagnostic(pid,wid,run['id'])
    assert not result['result_available'] and result['repairable']
    assert result['workflow_failure']['code']=='SQL_INEXACT_NUMERIC'
    assert '二进制浮点' in result['workflow_failure']['message']
    assert all(term in result['next_step'] for term in ('aggregate','compute','rounding_scale','分母为零'))
    assert marker not in json.dumps(result) and get('data_workflow',wid)['published_run'] is None
