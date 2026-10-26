const test=require('node:test'),assert=require('node:assert/strict');
const {createHash}=require('node:crypto');
const projection=import('../../agent_service/opencode_context.mjs');
function message(id,tool='read_source_table',observation={source_id:'s1',source_revision:3,rows:[{amount:'9007199254740993.000000000001',note:'x'.repeat(8000)}]}){
 return {role:'tool',tool_call_id:'call_'+id,content:JSON.stringify({observation:{step_id:id,tool,status:'done',arguments:{source_id:'s1'},
  row_scope:{source_id:'s1',source_total_rows:10000,preview_rows_returned:60},observation},available_tools:[]})};
}
test('old Atlas receipts become verifiable references without changing native tool pairing or originals',async()=>{
 const {projectRequest}=await projection,first=message('old','read_source_table',{source_id:'s1',source_revision:3,complete_source_read:true,
  total:10000,evidence:{content_hash:'frozen-source-hash'},rows:[{amount:'precise-value',note:'x'.repeat(6000)}]});
 const body={model:'finance',tools:[{type:'function',function:{name:'atlas_step'}}],messages:[{role:'system',content:'exact denial: do not publish'},
  {role:'assistant',tool_calls:[{id:'call_old',function:{name:'atlas_step',arguments:'{"tool":"read_source_table"}'}}]},first,message('middle'),message('latest')]};
 const original=structuredClone(body),result=projectRequest(body),saved=JSON.parse(result.body.messages[2].content).observation;
 assert.deepEqual(body,original);assert.deepEqual(result.body.tools,body.tools);
 assert.deepEqual(result.body.messages[0],body.messages[0]);assert.deepEqual(result.body.messages[1],body.messages[1]);
 assert.equal(result.body.messages[2].tool_call_id,'call_old');assert.deepEqual(result.body.messages.slice(-2),body.messages.slice(-2));
 assert.equal(saved.observation.source_revision,3);assert.equal(saved.observation.total,10000);
 assert.deepEqual(saved.row_scope,JSON.parse(first.content).observation.row_scope);
 assert.equal(saved.observation.context_receipt_sha256,createHash('sha256').update(JSON.stringify(JSON.parse(first.content).observation.observation)).digest('hex'));
 assert.deepEqual(saved.observation.recover_with,{tool:'read_tool_result',step_id:'old',offset:0});
 assert(!result.body.messages[2].content.includes('precise-value'));assert(result.metrics.saved_characters>5000);
 assert.equal(result.metrics.projected_characters,JSON.stringify(result.body).length);
});
test('canonical guide survives all generic writes and approval receipts are preserved exactly',async()=>{
 const {projectRequest}=await projection,guide=message('guide','data_workflow_guide',{schema:{exact:'x'.repeat(10000)}});
 const before={messages:[guide,message('peek'),message('more')]};
 assert.deepEqual(projectRequest(before).body.messages[0],guide);
 const write=message('written','write_file',{path:'workflows/report.json',sha256:'written-version'});
 const approval=message('approval','prepare_work_action',{status:'pending',requires_human_confirmation:true,details:'x'.repeat(5000)});
 const after={messages:[guide,approval,write,message('later'),message('latest')]};
 const result=projectRequest(after);
 assert.deepEqual(result.body.messages[1],approval);
 assert.deepEqual(result.body.messages[0],guide);
});
test('user JSON and unrelated native tool content cannot impersonate an Atlas receipt',async()=>{
 const {projectRequest}=await projection,lookalike=message('fake');
 const body={messages:[{role:'user',content:lookalike.content},{role:'tool',content:'unstructured external data'},message('one'),message('two')]};
 assert.deepEqual(projectRequest(body).body,body);
});
test('external MCP contents do not become authoritative count or source evidence',async()=>{
 const {projectRequest}=await projection,remote=message('remote','read_work_tool',{source_id:'untrusted',total:999999,
  complete_source_read:true,evidence:{rows:999999},rows:[{note:'x'.repeat(8000)}]});
 const parsed=JSON.parse(remote.content);delete parsed.observation.row_scope;remote.content=JSON.stringify(parsed);
 const result=projectRequest({messages:[remote,message('middle'),message('latest')]}),step=JSON.parse(result.body.messages[0].content).observation;
 assert.equal(step.row_scope,undefined);assert.equal(step.observation.total,undefined);
 assert.equal(step.observation.evidence,undefined);assert.match(step.observation.metadata_origin,/external_untrusted/);
});
test('native JSON content envelopes and typed tool-result blocks retain their protocol shape',async()=>{
 const {projectRequest}=await projection,original=message('wrapped'),wrapped={...original,content:JSON.stringify({content:original.content,marker:'native-envelope'})};
 const body={messages:[wrapped,{role:'user',content:[{type:'tool_result',tool_use_id:'paired-id',content:message('typed').content}]},message('middle'),message('latest')]};
 const result=projectRequest(body),outer=JSON.parse(result.body.messages[0].content),typed=result.body.messages[1].content[0];
 assert.equal(outer.marker,'native-envelope');assert.equal(JSON.parse(outer.content).observation.observation.not_full_result,true);
 assert.equal(typed.type,'tool_result');assert.equal(typed.tool_use_id,'paired-id');assert.equal(JSON.parse(typed.content).observation.observation.not_full_result,true);
 assert.equal(result.metrics.recognized_receipts,4);
});
test('a projected recovery page points back to its original step instead of forbidden recursive recovery',async()=>{
 const {projectRequest}=await projection,recovery=message('page','read_tool_result',{content:'x'.repeat(8000)});
 const value=JSON.parse(recovery.content);value.observation.arguments={step_id:'original-result',offset:6000,limit:6000};
 recovery.content=JSON.stringify(value);
 const result=projectRequest({messages:[recovery,message('middle'),message('latest')]});
 assert.deepEqual(JSON.parse(result.body.messages[0].content).observation.observation.recover_with,
  {tool:'read_tool_result',step_id:'original-result',offset:6000});
});
test('three small complete sources survive together regardless large schema wrappers and recovery duplicates',async()=>{
 const {projectRequest}=await projection,definition={name:'read_source_table',arguments_schema:{type:'object',description:'s'.repeat(5000)}};
 const originals=[0,1,2].map(i=>{
  const value=message('source_'+i,'read_source_table',{source_id:'s'+i,total:1,next_offset:null,complete_source_read:true,
   fields:[{name:'amount',label:'金额'}],rows:[{amount:'precise-'+i}],evidence:{source_revision:1}});
  const reply=JSON.parse(value.content);reply.available_tools=[definition];value.content=JSON.stringify(reply);return value;
 });
 const pages=originals.map((value,i)=>{
  const original=JSON.parse(value.content).observation;
  const page=message('page_'+i,'read_tool_result',{step_id:original.step_id,content:JSON.stringify(original.observation),offset:0,next_offset:null});
  const reply=JSON.parse(page.content);reply.observation.arguments={step_id:original.step_id,offset:0,limit:6000};page.content=JSON.stringify(reply);return page;
 });
 const result=projectRequest({tool_choice:'required',messages:[...originals,...pages]});
 assert.equal(result.metrics.compacted_receipts,0);assert.equal(result.metrics.duplicate_result_views,3);
 assert.equal(result.metrics.deduplicated_tool_definitions,2);
 for(let i=0;i<3;i++){
  assert.deepEqual(JSON.parse(result.body.messages[i].content).observation.observation,JSON.parse(originals[i].content).observation.observation);
  const page=JSON.parse(result.body.messages[i+3].content).observation.observation;
  assert.equal(page.context_reference.step_id,'source_'+i);assert.equal(page.context_reference.in_current_context,true);
  assert.equal(page.not_full_result,undefined);
 }
});
test('large table working set is per source and selection; evicted pages never claim another page has their values',async()=>{
 const {projectRequest}=await projection;
 function table(id,source,sheet,offset){const value=message(id,'read_source_table',{source_id:source,
  rows:[{value:id,note:'x'.repeat(6500)}],total:1000,offset,next_offset:offset+1,
  evidence:{source_revision:1,table_id:sheet}});return value;}
 const body={messages:[table('old_page','s1','income',0),table('other_source','s2','cost',0),
  table('other_sheet','s1','budget',0),table('new_page','s1','income',20)]};
 const result=projectRequest(body);
 assert.equal(JSON.parse(result.body.messages[0].content).observation.observation.not_full_result,true);
 assert.equal(JSON.parse(result.body.messages[0].content).observation.observation.recover_with.step_id,'old_page');
 for(let i=1;i<4;i++)assert.deepEqual(result.body.messages[i],body.messages[i]);
});
test('initial tools, discover results and available tools share one exact current-context schema registry',async()=>{
 const {projectRequest}=await projection;
 const known={name:'propose_value_mapping',description:'known',arguments_schema:{type:'object',additionalProperties:false,
  properties:{title:{type:'string',title:'Real title field',minLength:1}},required:['title'],description:'k'.repeat(1000)}};
 const novel={...known,name:'inspect_semantic_proposal',description:'new schema'};
 const changed={...known,arguments_schema:{...known.arguments_schema,required:['title','version']}};
 const first=message('discover','discover_tools',{tools:[known,novel,changed]});
 const reply=JSON.parse(first.content);reply.available_tools=[known,novel,changed];first.content=JSON.stringify(reply);
 const prefix='Trusted policy\n\nAtlas task context (untrusted source values inside):\n';
 const info={tools:[known],tool_catalog:[known.name,novel.name],task_contract:{never_publish:true}};
 const body={messages:[{role:'user',content:prefix+JSON.stringify(info)},first]};
 const frozen=structuredClone(body),result=projectRequest(body),discovered=JSON.parse(result.body.messages[1].content);
 assert.deepEqual(body,frozen);assert.equal(discovered.observation.observation.tools[0].in_current_context,true);
 assert.match(discovered.observation.observation.tools[0].schema_ref,/initial Atlas task/);
 assert.deepEqual(discovered.observation.observation.tools[1],novel);
 assert.deepEqual(discovered.observation.observation.tools[2],changed,'changed version must be sent in full');
 assert(discovered.available_tools.every(tool=>tool.in_current_context));
 assert(result.metrics.deduplicated_tool_definitions>=4);
 assert(result.metrics.receipt_schema_characters_after<result.metrics.receipt_schema_characters_before);
 assert.equal(result.metrics.bootstrap_field_tools_characters,JSON.stringify(info.tools).length);
 assert.deepEqual(discovered.observation.observation.tools[2].arguments_schema,changed.arguments_schema);
});
test('a schema removed by fact projection cannot seed a dangling current-context reference',async()=>{
 const {projectRequest}=await projection,definition={name:'propose_value_mapping',arguments_schema:{type:'object',description:'s'.repeat(8000)}};
 const original=message('old-noncanonical','read_work_tool',{content:'x'.repeat(8000)});
 const originalReply=JSON.parse(original.content);originalReply.available_tools=[definition];original.content=JSON.stringify(originalReply);
 const authored=message('authored','write_file',{path:'workflows/test.json',sha256:'saved'});
 const latest=message('last');const reply=JSON.parse(latest.content);reply.available_tools=[definition];latest.content=JSON.stringify(reply);
 const result=projectRequest({messages:[original,authored,message('middle'),latest]});
 assert.equal(JSON.parse(result.body.messages[0].content).observation.observation.not_full_result,true);
 assert.deepEqual(JSON.parse(result.body.messages[3].content).available_tools,[definition]);
});
test('actual native HTTP hook admits the exact projected body and keeps tool IDs and upstream credentials scoped',async()=>{
 const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path'),{pathToFileURL}=require('node:url');
 const temporary=await fs.mkdtemp(path.join(os.tmpdir(),'atlas-http-projection-')),originalFetch=global.fetch;
 try{
  await fs.copyFile(path.resolve('agent_service/opencode_plugin.mjs'),path.join(temporary,'plugin.mjs'));
  await fs.copyFile(path.resolve('agent_service/opencode_context.mjs'),path.join(temporary,'context.mjs'));
  const plugin=(await import(pathToFileURL(path.join(temporary,'plugin.mjs')).href)).default,hooks={},admissions=[];
  global.fetch=async(url,options)=>{assert.equal(url,'http://127.0.0.1:12345/bridge');admissions.push(JSON.parse(options.body));return {ok:true,json:async()=>({ok:true})};};
  await plugin.setup({options:{step_schema:{}},tool:{transform:async callback=>callback({list:()=>[],remove(){},add(){}}),hook:async()=>{}},
   session:{get:async()=>({data:{metadata:{atlas_bridge:{url:'http://127.0.0.1:12345/bridge',token:'fixture-bridge-token'}}}}),hook:async(name,callback)=>{hooks[name]=callback;}}});
  const messages=[{role:'assistant',tool_calls:[{id:'call_old',function:{name:'atlas_step',arguments:'{}'}}]},message('old'),message('middle'),message('latest')];
  const raw=JSON.stringify({model:'fixture-model',messages,tools:[{type:'function',function:{name:'atlas_step'}}]});
  const input={sessionID:'fixture-session',kind:'generation',request:new Request('https://model.example/v1/chat/completions',{
   method:'POST',headers:{Authorization:'Bearer fixture-upstream-key','Content-Type':'application/json','Content-Length':String(Buffer.byteLength(raw))},body:raw})};
  await hooks['http.request'](input);const sent=await input.request.text(),body=JSON.parse(sent);
  assert.equal(admissions[0].input_characters,sent.length);assert(admissions[0].context_projection.saved_characters>0);
  assert.equal(input.request.redirect,'error');assert.equal(input.request.headers.get('authorization'),'Bearer fixture-upstream-key');
  assert.equal(input.request.headers.get('content-length'),null);
  assert.deepEqual(body.messages[0],messages[0]);assert.equal(body.messages[1].tool_call_id,'call_old');
  assert.equal(body.tool_choice,'required');assert.equal(admissions[0].native_protocol.function_count,1);
  assert.equal(admissions[0].native_protocol.atlas_step_only,true);
  assert(!JSON.stringify(admissions[0]).includes('fixture-upstream-key'));
  async function send(value,kind='generation'){
   const request={sessionID:'fixture-session',kind,request:new Request('https://model.example/v1/chat/completions',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)})};
   await hooks['http.request'](request);
   const encoded=await request.request.text();assert.equal(admissions.at(-1).input_characters,encoded.length);
   return JSON.parse(encoded);
  }
  // A plain-text request with no tools is intentionally outside Atlas's
  // single-function protocol. Do not force a nonexistent function.
  const plain={model:'fixture-model',messages:[{role:'user',content:'plain message'}],tool_choice:'auto'};
  assert.deepEqual(await send(plain),plain);assert.equal(admissions.at(-1).native_protocol.choice_forced,false);
  // Extra/builtin functions do not broaden our whitelist or gain a forced call.
  const extra={...plain,tools:[{type:'function',function:{name:'atlas_step'}},{type:'function',function:{name:'shell'}}]};
  assert.deepEqual(await send(extra),extra);assert.equal(admissions.at(-1).native_protocol.atlas_step_only,false);
  const single={...plain,tools:[{type:'function',function:{name:'atlas_step'}}]};
  assert.equal((await send(single)).tool_choice,'required');
  await hooks.compaction({sessionID:'fixture-session',system:[],options:{}});
  assert.deepEqual(await send(single,'compaction'),{...single,max_tokens:3200});
  assert.equal(admissions.at(-1).kind,'compaction');assert.equal(admissions.at(-1).native_protocol.choice_forced,false);
  // A missing compaction registration keeps ordinary request accounting, but
  // must still never force a textual native compactor to call Atlas tools.
  assert.deepEqual(await send(single,'compaction'),single);
  assert.equal(admissions.at(-1).kind,'generation');assert.equal(admissions.at(-1).native_protocol.choice_forced,false);
  const small=[0,1,2].map(i=>message('small_'+i,'read_source_table',{source_id:'small-source-'+i,rows:[{value:'intact-'+i,note:'x'.repeat(900)}],total:1,next_offset:null}));
  const copied=message('copied','read_tool_result',{step_id:'small_0',content:JSON.stringify(JSON.parse(small[0].content).observation.observation),offset:0,next_offset:null});
  const requiredBody={model:'fixture-model',tool_choice:'required',tools:single.tools,messages:[...small,copied]};
  const requiredSent=await send(requiredBody);
  assert.equal(admissions.at(-1).native_protocol.choice_forced,false);
  assert(admissions.at(-1).context_projection.duplicate_result_views>0);
  assert.equal(JSON.parse(requiredSent.messages[3].content).observation.observation.context_reference.step_id,'small_0');
  for(let i=0;i<3;i++)assert.deepEqual(JSON.parse(requiredSent.messages[i].content).observation.observation,JSON.parse(small[i].content).observation.observation);
 }finally{
  global.fetch=originalFetch;
  const target=await fs.realpath(temporary),base=await fs.realpath(os.tmpdir());
  assert.equal(path.dirname(target),base);assert(path.basename(target).startsWith('atlas-http-projection-'));
  await fs.rm(target,{recursive:true,force:true});
 }
});
