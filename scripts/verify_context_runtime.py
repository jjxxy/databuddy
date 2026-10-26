"""Real OpenCode 2.1.2 compaction protocol check with a simulated local model.

Run after dependency installation with ATLAS_OPENCODE_BINARY pointing to the
pinned deployment binary. No external LLM credentials or business files used.
"""
import os,sys,json,tempfile,subprocess,time,hashlib,shutil
from pathlib import Path
repo=Path(__file__).resolve().parents[1];sys.path.insert(0,str(repo))
root=Path(tempfile.mkdtemp(prefix='atlas-v18-native-context-'))
os.environ.update(DATA_DIR=str(root/'data'),DATABASE_URL='sqlite:///'+str(root/'data/metadata.sqlite'),APP_TOKEN='fixture-context-only-token-123456789',ATLAS_OPENCODE_RUNTIME='1',ATLAS_OPENCODE_BINARY=os.getenv('ATLAS_OPENCODE_BINARY') or shutil.which('opencode') or '')
from scripts.verify_quickstart import local_model,FIXTURE_KEY,FIXTURE_MODEL
from agent_service import opencode_runtime as runtime,provider
from agent_service.store import put,get,patch,now
from agent_service.agent import Step
from agent_service.actor_context import actor_scope
from agent_service import memory_service as memory
with local_model() as (fixture,url):
    selected={'base_url':url,'model':FIXTURE_MODEL,'api_key':FIXTURE_KEY,'share_data':True,'id':'fixture','engine':'atlas'}
    os.environ['API_ALLOWED_HOSTS']='127.0.0.1'
    provider.settings=lambda:selected
    calls=[]
    def message(body):
        names={t.get('function',{}).get('name') for t in body.get('tools',[])}
        is_compaction=any('Summarize ongoing work without inventing facts' in str(m.get('content','')) for m in body.get('messages',[]) if m.get('role')=='system')
        calls.append({'kind':'compaction' if is_compaction else 'primary','model':body['model'],'tools':sorted(names)})
        if is_compaction:return {'role':'assistant','content':'## Objective\nContinue finance work.\n## Constraints\nDo not aggregate balances.\n## Next Move\nRead full source and verify exact results.\nSource/result references remain in Atlas.'},'stop'
        assert names=={'atlas_step'}
        return {'role':'assistant','content':None,'tool_calls':[{'id':'call_ctx_'+str(len(calls)),'type':'function','function':{'name':'atlas_step','arguments':json.dumps({'kind':'tool','tool':'project_context','arguments':{}})}}]},'tool_calls'
    fixture.message=message
    spec=runtime.prepare({'models':[dict(selected,name='Fixture')],'default_model_id':'fixture'},Step.model_json_schema())
    log=(root/'opencode.log').open('w')
    process=subprocess.Popen(spec['command'],env=spec['env'],cwd=spec['cwd'],stdout=log,stderr=log)
    try:
        runtime.wait_ready(30)
        put('project',{'name':'Native compaction fixture'},'p_native_context')
        turn=put('turn',{'project_id':'p_native_context','text':'Continue finance work','state':'agent','created_at':now(),
            'task_contract':{'budgets':{'model_calls':20,'input_characters':1000000,'output_tokens':128000,'duration_ms':300000}}},'t_native_context')
        remembered=memory.save(turn['project_id'],memory.MemoryInput(scope='personal',title='Fixture preference',content='Prefer concise tables.'))
        def first(step,cid):patch('turn',turn['id'],state='waiting_run');return {'source_refs':['source-frozen-v1'],'observed':'first tool complete'}
        first_receipt=runtime.run_session(turn,'Use atlas_step.',{'memory_context':memory.context_for_turn(turn),'request':'Do not aggregate balances. '+('Original source contextual note. '*1100),'context_compaction':{'critical_constraints':[{'quote':'Do not aggregate balances.','verbatim':True}]}},first)
        native=runtime.connection();sid=get('turn',turn['id'])['opencode_session_id']
        before=runtime._request(native,'GET','/api/session/'+sid+'/context')['data']
        (root/'before.json').write_text(json.dumps(before,ensure_ascii=False,indent=2))
        turn=patch('turn',turn['id'],state='agent',opencode_compaction_requested=True)
        def second(step,cid):patch('turn',turn['id'],state='done',answer='Protocol fixture complete',outcome='answered');return {'done':True}
        second_receipt=runtime.run_session(turn,'Use atlas_step and follow preserved constraints.',{'request':'Continue','context_compaction':{'critical_constraints':[{'quote':'Do not aggregate balances.','verbatim':True}]}},second)
        current=get('turn',turn['id']);after=runtime._request(native,'GET','/api/session/'+sid+'/context')['data']
        (root/'after.json').write_text(json.dumps(after,ensure_ascii=False,indent=2))
        originals=runtime._request(native,'GET','/api/session/'+sid+'/message?limit=100&order=asc')
        assert before[0]['id'] in json.dumps(originals)
        assert 'Original source contextual note.' in json.dumps(originals)
        before_forget_session=current['opencode_session_id']
        memory.forget(turn['project_id'],remembered['id'],remembered['version'])
        turn=patch('turn',turn['id'],state='agent')
        third_receipt=runtime.run_session(turn,'Continue using current preferences only.',{'request':'Continue','memory_context':memory.context_for_turn(turn)},second)
        final=get('turn',turn['id']);fresh=runtime._request(native,'GET','/api/session/'+final['opencode_session_id']+'/context')['data']
        assert final['opencode_session_id']!=before_forget_session
        assert 'Prefer concise tables.' not in json.dumps(fresh)
        evidence={'ok':True,'actual_opencode_binary':True,'opencode_version':runtime.VERSION,'simulated_model':True,'real_model_evaluated':False,
            'original_native_messages_verified_after_compaction':True,'memory_forget_rotates_native_session':final['opencode_session_id']!=before_forget_session,'forgotten_content_absent_from_new_context':True,'native_compaction_receipt':current.get('opencode_compaction_receipt'),'budgeted_model_calls':final.get('model_usage',{}).get('calls'),'model_requests':calls,'before_types':[v.get('type') for v in before],
            'after_types':[v.get('type') for v in after],'critical_preserved':any('Do not aggregate balances' in json.dumps(v) for v in after),'evidence_dir':str(root)}
        print(json.dumps(evidence,ensure_ascii=False))
    except Exception:
        print('EVIDENCE_DIR='+str(root));print(json.dumps({'calls':calls},ensure_ascii=False));(root/'failure-context.json').write_text(json.dumps(runtime._request(native,'GET','/api/session/'+sid+'/context'),ensure_ascii=False,indent=2));raise
    finally:
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        log.close()
