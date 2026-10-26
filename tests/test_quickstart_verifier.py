"""The quickstart verifier uses synthetic responses and real local processes."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('verify_quickstart', ROOT / 'scripts/verify_quickstart.py')
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def test_isolated_cloud_environment_cannot_use_shell_accounts_or_real_model_keys(tmp_path, monkeypatch):
    for key in ('LLM_API_KEY', 'LLM_BASE_URL', 'ATLAS_INITIAL_MODEL', 'ATLAS_BACKUP_KEY',
                'ATLAS_S3_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'RAILWAY_ENVIRONMENT_ID',
                'ATLAS_RUNNER_TOKEN', 'DATABASE_URL', 'API_ALLOWED_HOSTS',
                'OPENCODE_CONFIG_CONTENT','OPENAI_API_KEY','ANTHROPIC_API_KEY',
                'GOOGLE_APPLICATION_CREDENTIALS'):
        monkeypatch.setenv(key, 'must-not-be-used')
    environment = verifier.isolated_environment(tmp_path, 8123, 'disposable-token-123456789')
    assert 'must-not-be-used' not in environment.values()
    assert environment['ATLAS_LLM_CONFIG_FILE'] == str(tmp_path / 'llm.json')
    assert environment['DATA_DIR'] == str(tmp_path / 'data')
    assert environment['ATLAS_MIGRATE_SQLITE'] == '0'
    assert 'ATLAS_INITIAL_MODEL' not in environment and 'LLM_API_KEY' not in environment
    assert 'API_ALLOWED_HOSTS' not in environment  # cloud derives the chosen local model host
    assert environment['ATLAS_OPENCODE_RUNTIME']=='0'


def test_local_model_is_an_explicit_authenticated_protocol_fixture():
    with verifier.local_model() as (fixture, endpoint), httpx.Client(trust_env=False) as client:
        body = {'model': verifier.FIXTURE_MODEL, 'messages': [{'role': 'user', 'content': 'connection check'}]}
        assert client.post(endpoint + '/chat/completions', json=body).status_code == 401
        reply = client.post(endpoint + '/chat/completions', json=body,
            headers={'Authorization': 'Bearer ' + verifier.FIXTURE_KEY})
        assert reply.status_code == 200 and reply.json()['choices'][0]['message']['content'] == 'OK'
        assert fixture.calls == {'connection': 1, 'source_planning': 0, 'agent': 0}
        body['messages'] = [{'role': 'system', 'content': 'synthetic planner'},
            {'role': 'user', 'content': json.dumps({'fields': [], 'baseline': {'object_label': 'fixture'}})}]
        planned = client.post(endpoint + '/chat/completions', json=body,
            headers={'Authorization': 'Bearer ' + verifier.FIXTURE_KEY})
        assert json.loads(planned.json()['choices'][0]['message']['content']) == {
            'proposed_spec': {'object_label': 'fixture'}, 'suggestions': []}
        assert fixture.calls['source_planning'] == 1


def test_nonempty_evidence_folder_is_never_overwritten(tmp_path):
    target = tmp_path / 'existing'; target.mkdir(); (target / 'keep.txt').write_text('keep')
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/verify_quickstart.py'),
        '--evidence-dir', str(target)], cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 2 and (target / 'keep.txt').read_text() == 'keep'
    assert not (target / 'service.log').exists()


def test_setup_real_cloud_agent_exact_deliveries_and_restart_with_simulated_model(tmp_path):
    target = tmp_path / 'evidence'
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/verify_quickstart.py'),
        '--evidence-dir', str(target),'--workspace-features'], cwd=ROOT, capture_output=True, text=True, timeout=300)
    logs = (target / 'service.log').read_text()[-4000:] if (target / 'service.log').exists() else ''
    assert result.returncode == 0, result.stdout + result.stderr + logs
    evidence = json.loads(result.stdout)
    assert evidence == json.loads((target / 'verification.json').read_text())
    for check in ('ok', 'quickstart_cli_executed', 'quickstart_setup_idempotent', 'cloud_supervisor',
                  'api_worker_agent_healthy', 'model_configured_share_data', 'container_health_command_verified',
                  'agent_authored_versioned_files', 'complete_csv_workflow', 'complete_excel',
                  'docx_pptx_exact_assertions', 'restart_persistence', 'model_credentials_survive_restart',
                  'managed_model_selection','business_cannot_configure_models',
                  'company_configuration_operator_only','catalog_owner_restart_update'):
        assert evidence[check] is True
    assert evidence['simulated_model'] is True
    assert evidence['real_model_evaluated'] is False and evidence['docker_executed'] is False
    assert evidence['opencode_runtime_executed'] is False and evidence['opencode_native_tool_loop_calls']==0
    assert evidence['full_rows'] == 2 and evidence['exact_total'] == verifier.EXACT_TOTAL
    assert evidence['model_calls']['connection'] == 4 and evidence['model_calls']['agent'] >= 9
    assert evidence['native_tool_capability_probes']==4
    assert evidence['workspace_features_executed'] is True
    for check in ('local_folder_import','local_import_retry_same_snapshots','local_private_files_excluded',
                  'multifile_input_order_and_versions','agent_authored_code_visible_locally','complete_cross_file_lookup',
                  'installed_skill_loaded_by_agent','actual_stdio_mcp_called_by_agent',
                  'explicit_personal_memory_saved','memory_injected_into_model',
                  'context_checkpoint_and_original_recovered','critical_constraints_retained',
                  'local_export_complete_hashes','local_originals_preserved','memory_survives_restart',
                  'forgotten_memory_not_reinjected_after_restart','installed_skill_survives_restart',
                  'context_checkpoint_persists_restart'):
        assert evidence['workspace_features'][check] is True,check
    assert verifier.FIXTURE_KEY not in result.stdout + result.stderr
    assert verifier.FIXTURE_KEY.encode() not in (target / '业务资料/.atlas/metadata.sqlite').read_bytes()


def test_fixture_advertises_real_native_tool_arguments_instead_of_json_prose():
    fixture=verifier.ModelFixture()
    message,reason=fixture.message({'model':verifier.FIXTURE_MODEL_ALTERNATE,
        'messages':[{'role':'user','content':'native capability check'}],
        'tools':[{'type':'function','function':{'name':'connection_check'}}]})
    assert reason=='tool_calls' and message['content'] is None
    assert json.loads(message['tool_calls'][0]['function']['arguments'])=={'ok':True}
    assert fixture.tool_probes==1 and fixture.models_seen==[verifier.FIXTURE_MODEL_ALTERNATE]


def test_fixture_recovers_atlas_context_and_session_step_from_opencode_messages():
    fixture=verifier.ModelFixture();fixture.workflow={'version':1}
    context={'request':'quickstart-fixture-plan','mode':'work','completed_steps':[]}
    message,reason=fixture.message({'model':verifier.FIXTURE_MODEL,
        'messages':[{'role':'system','content':'synthetic harness'},
            {'role':'user','content':[{'type':'text','text':'Atlas task:\n'+json.dumps(context)}]},
            {'role':'assistant','tool_calls':[{'function':{'name':'atlas_step'}}]},
            {'role':'tool','content':'{"state":"agent"}'}],
        'tools':[{'type':'function','function':{'name':'atlas_step'}}]})
    value=json.loads(message['tool_calls'][0]['function']['arguments'])
    assert reason=='tool_calls' and value['tool']=='write_file'
    assert json.loads(value['arguments']['content'])==fixture.workflow


def test_compaction_fixture_is_counted_separately_from_native_tool_execution():
    fixture=verifier.ModelFixture()
    message,reason=fixture.message({'model':verifier.FIXTURE_MODEL,
        'messages':[{'role':'system','content':'Summarize ongoing work without inventing facts'}],
        'tools':[{'type':'function','function':{'name':'atlas_step'}}]})
    assert reason=='stop' and message['content'] and 'tool_calls' not in message
    assert fixture.native_compaction_calls==1 and fixture.native_loop_calls==0
