"""Independent evidence checker and real loopback Agent collaboration smoke test."""
import base64
import csv
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from scripts import verify_agent_collaboration as verifier


def _sha(raw):return hashlib.sha256(raw).hexdigest()


def _bundle(*,rows=40,last='7.500000000002'):
    records=[]
    for index in range(rows):
        records.append({'values':{'$atlas':'object','items':[
            ['office','Office'+str(index+1).zfill(2)],
            ['actual',{'$atlas':'decimal','value':last if index==rows-1 else '7.500000000000'}],
            ['budget',{'$atlas':'decimal','value':'8.000000000000'}]]}})
    files={'rows.jsonl':('\n'.join(json.dumps(row) for row in records)+'\n').encode(),
           'schema.json':b'{}','context.json':b'{}','provenance.jsonl':b'', 'stages.json':b'[]'}
    manifest=json.dumps({'row_count':rows,'files':{name:{'bytes':len(raw),'sha256':_sha(raw)} for name,raw in files.items()}},sort_keys=True).encode()
    result=io.BytesIO()
    with zipfile.ZipFile(result,'w') as archive:
        archive.writestr('manifest.json',manifest)
        for name,raw in files.items():archive.writestr(name,raw)
    raw=result.getvalue()
    item={'id':'result-real-bytes','kind':'result','name':'complete.zip','media_type':'application/zip',
          'bytes':len(raw),'sha256':_sha(raw),'encoding':'base64','content_base64':base64.b64encode(raw).decode(),
          'result_metadata':{'records':rows,'handoff_manifest_sha256':_sha(manifest)}}
    bundle={'format':'atlas-agent-handoff','version':1,'complete':True,'items':[item]}
    _seal(bundle)
    return bundle


def _seal(bundle):
    clean={key:([{k:v for k,v in item.items() if k!='content_base64'} for item in value] if key=='items' else value)
           for key,value in bundle.items() if key!='manifest_sha256'}
    bundle['manifest_sha256']=_sha(json.dumps(clean,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode())


def test_source_control_uses_full_40_rows_and_exact_decimal_not_binary_float():
    rows=list(csv.DictReader(io.StringIO(verifier.source_bytes().decode())))
    assert len(rows)==40
    assert sum(Decimal(row['actual']) for row in rows)==Decimal('300.000000000002')
    assert sum(Decimal(row['budget']) for row in rows)==Decimal('320')


def test_full_bundle_checker_independently_checks_exact_rows_and_nested_hashes():
    bundle=_bundle()
    checked=verifier.verify_output(bundle)
    assert checked==[{'rows':40,'exact_total':'300.000000000002','sha256':bundle['items'][0]['sha256']}]
    bundle['items'][0]['content_base64']=base64.b64encode(b'changed bytes').decode()
    with pytest.raises(AssertionError):verifier.verify_output(bundle)


@pytest.mark.parametrize('rows,last',[(25,'7.500000000002'),(40,'7.500000000000')])
def test_valid_hashes_do_not_hide_preview_truncation_or_lost_decimal(rows,last):
    with pytest.raises(AssertionError):verifier.verify_output(_bundle(rows=rows,last=last))


def test_manifest_change_and_missing_result_are_rejected():
    bundle=_bundle();bundle['items'][0]['result_metadata']['records']=25
    with pytest.raises(AssertionError,match='manifest'):verifier.verify_output(bundle)
    bundle={'format':'atlas-agent-handoff','version':1,'complete':True,'items':[]};_seal(bundle)
    with pytest.raises(AssertionError,match='No actual complete'):verifier.verify_output(bundle)


def test_native_fixture_resolves_actual_tool_receipt_ids_after_immutable_prompt():
    context={'completed_steps':[]}
    body={'messages':[{'role':'tool','content':json.dumps({'observation':{
        'tool':'register_data_workflow','status':'done','observation':{'workflow_id':'wf_from_actual_receipt'}}})}]}
    assert verifier.CollaborationFixture._observation(context,'register_data_workflow',body)=={
        'workflow_id':'wf_from_actual_receipt'}
    assert verifier.CollaborationFixture._observation({'completed_steps':[{'tool':'run_data_workflow','observation':None}]},
        'run_data_workflow')=={}


def test_native_fixture_step_count_uses_fresh_prompt_after_real_compaction():
    context={'request':'fixture-finance','mode':'work','completed_steps':[{}]*4}
    body={'messages':[{'role':'assistant','tool_calls':[{'function':{'name':'atlas_step'}}]},
        {'role':'user','content':json.dumps(context)},
        {'role':'assistant','tool_calls':[{'function':{'name':'atlas_step'}}]}]}
    assert verifier.CollaborationFixture._step(context,body)==5


def _ledger_fixture(*,parent_limits=None,child_calls=2):
    from agent_service.task_budget import KEYS
    parent={'id':'root','task_contract':{'budget_policy':'unlimited','budgets':dict.fromkeys(KEYS,None)}}
    if parent_limits is not None:
        parent['task_contract']={'budget_policy':'bounded','budgets':parent_limits}
    child={'id':'child','collaboration_parent_turn_id':'root','task_contract':{'budget_policy':'bounded',
        'budgets':{'model_calls':5,'input_characters':120000,'output_tokens':16000,'duration_ms':120000}}}
    baseline={'calls':1,'input_characters':100,'output_tokens_budgeted':100}
    contribution={'calls':child_calls,'input_characters':1000,'output_tokens_budgeted':6400}
    ledger={'limits':dict(parent['task_contract']['budgets']),'limit_overrides':{},
        'baseline_usage':baseline,'per_turn':{'child':contribution},
        'used':{key:value+contribution[key] for key,value in baseline.items()}}
    return ledger,parent,[child]


def test_unlimited_owner_ledger_keeps_exact_reservations_and_bounded_child_grants():
    ledger,parent,children=_ledger_fixture()
    assert verifier.verify_shared_budget(ledger,parent,children)
    ledger['used']['calls']+=1
    with pytest.raises(AssertionError,match='reset or lost'):verifier.verify_shared_budget(ledger,parent,children)


def test_explicit_parent_or_gateway_grant_cannot_be_disguised_as_unlimited():
    from agent_service.task_budget import KEYS
    limits={'model_calls':10,'input_characters':10000,'output_tokens':20000,'duration_ms':120000}
    ledger,parent,children=_ledger_fixture(parent_limits=limits)
    assert verifier.verify_shared_budget(ledger,parent,children)
    ledger['limits']=dict.fromkeys(KEYS,None)
    with pytest.raises(AssertionError,match='parent grant'):verifier.verify_shared_budget(ledger,parent,children)
    parent['gateway_client_id']='explicit-client'
    parent['gateway_limits']=limits
    parent['task_contract']={'budget_policy':'unlimited','budgets':dict.fromkeys(KEYS,None)}
    with pytest.raises(AssertionError,match='parent grant'):verifier.verify_shared_budget(ledger,parent,children)


def test_unlimited_owner_cannot_hide_exhausted_child_budget():
    ledger,parent,children=_ledger_fixture(child_calls=6)
    with pytest.raises(AssertionError,match='Child exceeded'):verifier.verify_shared_budget(ledger,parent,children)


def test_explicit_shared_override_is_verified_and_cannot_enlarge_parent():
    ledger,parent,children=_ledger_fixture()
    ledger['limits']['model_calls']=3;ledger['limit_overrides']={'model_calls':3}
    assert verifier.verify_shared_budget(ledger,parent,children)
    parent['task_contract']={'budget_policy':'bounded','budgets':{'model_calls':2}}
    with pytest.raises(AssertionError,match='enlarged'):verifier.verify_shared_budget(ledger,parent,children)


def test_existing_evidence_is_not_overwritten(tmp_path):
    target=tmp_path/'evidence';target.mkdir();(target/'keep.txt').write_text('keep')
    result=subprocess.run([sys.executable,str(verifier.ROOT/'scripts/verify_agent_collaboration.py'),
        '--evidence-dir',str(target)],cwd=verifier.ROOT,capture_output=True,text=True,timeout=15)
    assert result.returncode==2
    assert (target/'keep.txt').read_text()=='keep'
    assert not (target/'A').exists()


def test_real_two_process_bidirectional_http_collaboration_with_explicit_model_fixture(tmp_path):
    target=tmp_path/'evidence'
    result=subprocess.run([sys.executable,str(verifier.ROOT/'scripts/verify_agent_collaboration.py'),
        '--evidence-dir',str(target)],cwd=verifier.ROOT,capture_output=True,text=True,timeout=300)
    logs='\n'.join((target/name/'service.log').read_text()[-2500:] for name in ('A','B') if (target/name/'service.log').exists())
    assert result.returncode==0,result.stdout+result.stderr+logs
    evidence=json.loads(result.stdout)
    assert evidence==json.loads((target/'verification.json').read_text())
    assert evidence['ok'] and evidence['instances']==2 and evidence['real_api_agent_worker']
    assert all(evidence['checks'].values())
    assert evidence['rows']==40 and evidence['exact_total']=='300.000000000002'
    assert evidence['max_parallel_specialist_requests']>=2
    assert evidence['simulated_model'] is True
    assert evidence['real_model_evaluated'] is False and evidence['docker_executed'] is False
    assert evidence['opencode_runtime_executed'] is False
