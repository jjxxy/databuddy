const test=require('node:test'),assert=require('node:assert/strict');
const projection=import('../../agent_service/opencode_context.mjs');
function message(id,tool,observation,args={}){
 return {role:'tool',tool_call_id:'call_'+id,content:JSON.stringify({observation:{step_id:id,tool,status:'done',arguments:args,observation},available_tools:[]})};
}
function descriptor(resource,offset,version='a'.repeat(64)){
 return {kind:'context_original',resource_id:resource,version_sha256:version,offset_unit:'characters',
  offset,returned_characters:7000,total_characters:24000,next_offset:offset+7000};
}
const later=()=>[message('plan','set_task_plan',{}),message('files','list_files',{}),message('rules','business_rules',{})];

test('all distinct current-version pages keep exact values and server cursor past unrelated tools',async()=>{
 const {projectRequest}=await projection;
 const old=message('old','read_context_original',{content:'old'.repeat(2500),recovery:descriptor('turn:history',0)},
  {turn_id:'history',offset:0,limit:7000});
 const current=message('current','read_tool_result',{content:'精确😀金额'.repeat(1300),recovery:descriptor('turn:history',7000),
  source_turn_id:'current-turn',offset:7000,next_offset:14000},{step_id:'wrapper',offset:0,limit:7000});
 const body={messages:[old,current,...later()]},frozen=structuredClone(body),result=projectRequest(body);
 assert.deepEqual(body,frozen);assert.deepEqual(result.body.messages[0],old);assert.deepEqual(result.body.messages[1],current);
 const recovery=JSON.parse(result.body.messages[1].content).observation.observation.recovery;
 assert.equal(recovery.offset,7000);assert.equal(recovery.returned_characters,7000); // Never recompute Python codepoints as JS UTF16.
});

test('exact same current-version range shares one value copy while newer privacy version removes old text',async()=>{
 const {projectRequest}=await projection;
 const original=message('original','read_context_original',{content:'PRIVATE_OLD_TEXT'+('x'.repeat(7000)),recovery:descriptor('turn:history',0)},
  {turn_id:'history',offset:0,limit:7000});
 const duplicate=message('wrapper','read_tool_result',{content:'PRIVATE_OLD_TEXT'+('x'.repeat(7000)),recovery:descriptor('turn:history',0)},
  {step_id:'original',offset:0,limit:7000});
 const shared=projectRequest({messages:[original,duplicate,...later()]});
 assert.equal(JSON.parse(shared.body.messages[1].content).observation.observation.context_reference.step_id,'original');
 const fresh=message('fresh','read_context_original',{content:'当前内容已撤回',recovery:descriptor('turn:history',0,'b'.repeat(64))},
  {turn_id:'history',offset:0,limit:7000});
 const filtered=projectRequest({messages:[original,duplicate,fresh,...later()]});
 assert(!JSON.stringify(filtered.body).includes('PRIVATE_OLD_TEXT'));
 assert.deepEqual(filtered.body.messages[2],fresh);
});

test('failed unexecuted recovery guard stays truthful and cannot evict a real successful page',async()=>{
 const {projectRequest}=await projection;
 const original=message('original','read_context_original',{content:'precise'.repeat(1300),recovery:descriptor('turn:history',0)},
  {turn_id:'history',offset:0,limit:7000});
 const blocked=message('blocked','read_context_original',{executed:false,code:'RECOVERY_NO_PROGRESS',
  error:'这段原始资料已经恢复',recovery:descriptor('turn:history',0)},{turn_id:'history',offset:0,limit:7000});
 const raw=JSON.parse(blocked.content);raw.observation.status='failed';blocked.content=JSON.stringify(raw);
 const result=projectRequest({messages:[original,blocked,...later()]});
 assert.deepEqual(result.body.messages[0],original);assert.deepEqual(result.body.messages[1],blocked);
});

test('compacted historical result carries explicit owner and original offset rather than wrapper ID',async()=>{
 const {projectRequest}=await projection;
 const page=message('wrapper','read_tool_result',{content:'x'.repeat(9000),source_turn_id:'history'},
  {source_turn_id:'history',step_id:'original',offset:6000,limit:9000});
 const result=projectRequest({messages:[page,...later()]});
 assert.deepEqual(JSON.parse(result.body.messages[0].content).observation.observation.recover_with,
  {tool:'read_tool_result',step_id:'original',source_turn_id:'history',offset:6000});
});

test('workflow view losslessly references duplicate structure and retains latest draft diagnostic',async()=>{
 const {projectRequest}=await projection;
 const schema={columns:[{name:'amount',data_type:'decimal',unit:'CNY',meaning:'源字段'.repeat(3000)}]},
  binding={source_id:'source',options:{percentage_columns:{c_6:'fraction'}}},stages=[{id:'kept',kind:'filter'}];
 const observation={workflow_id:'workflow',draft_sha256:'b'.repeat(64),inputs:{raw:{binding,schema}},steps:stages,
  document:{bindings:{raw:structuredClone(binding)},plan:{inputs:{raw:structuredClone(schema)},stages:structuredClone(stages)}},
  latest_run:{matches_current_draft:true,workflow_failure:{stage_id:'input:raw',code:'TYPE_MISMATCH'}}};
 const first=message('draft','inspect_data_workflow',observation),body={messages:[first,...later()]},frozen=structuredClone(body),
  result=projectRequest(body),kept=JSON.parse(result.body.messages[0].content).observation.observation;
 assert.deepEqual(body,frozen);assert.equal(kept.inputs,undefined);assert.equal(kept.steps,undefined);
 assert.deepEqual(kept.document,observation.document);assert.deepEqual(kept.latest_run,observation.latest_run);
 assert.equal(kept.inputs_reference.exact_values_already_present,true);assert.equal(kept.not_full_result,undefined);
 const altered=structuredClone(observation);altered.inputs.raw.schema.columns[0].unit='万元';
 const mismatch=projectRequest({messages:[message('different','inspect_data_workflow',altered),...later()]});
 assert(mismatch.body.messages[0].content.includes('inputs'));
 assert.deepEqual(JSON.parse(mismatch.body.messages[0].content).observation.observation.inputs,altered.inputs);
});
