const test=require('node:test'),assert=require('node:assert/strict'),http=require('node:http'),fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),{pathToFileURL}=require('node:url');

async function harness(reply){
 const admissions=[],hooks=new Map();
 const server=http.createServer((req,res)=>{let raw='';req.on('data',part=>raw+=part);req.on('end',()=>{
  assert.equal(req.headers.authorization,'Bearer fixture-bridge');admissions.push(JSON.parse(raw));
  res.setHeader('content-type','application/json');res.end(JSON.stringify(reply));
 })});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const ctx={options:{step_schema:{type:'object'},context_helper_sha256:'f'.repeat(64)},
  tool:{transform:async fn=>fn({list:()=>[],add(){},remove(){}}),hook:async()=>{}},
  session:{get:async()=>({data:{metadata:{atlas_bridge:{url:'http://127.0.0.1:'+server.address().port+'/bridge',token:'fixture-bridge'}}}}),
   hook:async(name,fn)=>hooks.set(name,fn)}};
 const folder=await fs.mkdtemp(path.join(os.tmpdir(),'atlas-plugin-budget-'));
 const cleanup=async()=>{assert.equal(path.dirname(folder),os.tmpdir());assert(path.basename(folder).startsWith('atlas-plugin-budget-'));await fs.rm(folder,{recursive:true,force:true})};
 try{
  await fs.copyFile(path.resolve(__dirname,'../../agent_service/opencode_plugin.mjs'),path.join(folder,'index.mjs'));
  await fs.copyFile(path.resolve(__dirname,'../../agent_service/opencode_context.mjs'),path.join(folder,'context.mjs'));
  await (await import(pathToFileURL(path.join(folder,'index.mjs')).href)).default.setup(ctx);
 }catch(error){server.closeAllConnections();await new Promise(resolve=>server.close(resolve));await cleanup();throw error}
 return {admissions,hooks,async stop(){server.closeAllConnections();await new Promise(resolve=>server.close(resolve));await cleanup()}};
}
function input(max=393216,field='max_tokens'){
 return {sessionID:'synthetic',kind:'generation',request:new Request('https://fixture.test/v1/chat/completions',{
  method:'POST',body:JSON.stringify({model:'fixture',[field]:max,messages:[{role:'user',content:'x'.repeat(90000)}],
    tools:[{type:'function',function:{name:'atlas_step',parameters:{type:'object'}}}],tool_choice:'auto'})})};
}
test('large generation retains upstream output capacity, with exact admission accounting',async()=>{
 const h=await harness({ok:true,max_output_tokens:393216});
 try{
  const value=input();await h.hooks.get('http.request')(value);const raw=await value.request.text(),body=JSON.parse(raw);
  assert.equal(h.admissions.length,1);assert.equal(h.admissions[0].max_output_tokens,393216);
  assert.equal(body.max_tokens,393216);assert.equal(body.messages[0].content.length,90000);
  assert.equal(h.admissions[0].input_characters,raw.length);assert.equal(body.tool_choice,'required');
  assert.equal(value.request.redirect,'error');
 }finally{await h.stop()}
});
test('explicit task allowance narrows actual outbound output without a second admission',async()=>{
 for(const field of ['max_tokens','max_completion_tokens']){
  const h=await harness({ok:true,max_output_tokens:5000});
  try{
   const value=input(393216,field);await h.hooks.get('http.request')(value);const raw=await value.request.text(),body=JSON.parse(raw);
   assert.equal(body[field],5000);assert.equal(h.admissions.length,1);
   assert(h.admissions[0].input_characters>=raw.length,'counter is conservative when an explicit response allowance shrinks');
   assert.equal(body.messages[0].content.length,90000);
  }finally{await h.stop()}
 }
});
test('an actual compaction hook keeps admission and constraints instead of bypassing the bridge',async()=>{
 const h=await harness({ok:true,max_output_tokens:3200});
 try{
  const summary={sessionID:'synthetic',system:[],options:{}};await h.hooks.get('compaction')(summary);
  assert.equal(summary.options.maxTokens,3200);assert(summary.system[0].text.includes('not financial evidence'));
  const value=input(393216);value.kind='compaction';await h.hooks.get('http.request')(value);
  assert.equal(h.admissions[0].kind,'compaction');
  assert.equal(h.admissions[0].max_output_tokens,3200);
  assert.equal(JSON.parse(await value.request.text()).max_tokens,3200,'provider defaults must not override the summary response size');
 }finally{await h.stop()}
});
