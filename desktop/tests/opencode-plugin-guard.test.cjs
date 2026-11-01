const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const {pathToFileURL}=require('node:url');

test('native Atlas tool errors explain the protocol without opening other tools',async()=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'atlas-tool-guard-'));
 try{
  await fs.copyFile(path.resolve('agent_service/opencode_plugin.mjs'),path.join(dir,'plugin.mjs'));
  await fs.copyFile(path.resolve('agent_service/opencode_context.mjs'),path.join(dir,'context.mjs'));
  const plugin=(await import(pathToFileURL(path.join(dir,'plugin.mjs')).href)).default;
  const toolHooks={},sessionHooks={},added=[],removed=[];
  await plugin.setup({options:{step_schema:{}},tool:{
   transform:async f=>f({list:()=>[{id:'shell'},{id:'read'}],remove:id=>removed.push(id),add:t=>added.push(t)}),
   hook:async(name,cb)=>{toolHooks[name]=cb;}},
   session:{get:async()=>({data:{}}),hook:async(name,cb)=>{sessionHooks[name]=cb;}}});
  assert.deepEqual(removed,['shell','read']);assert.deepEqual(added.map(t=>t.name),['atlas_step']);
  assert.doesNotThrow(()=>toolHooks['execute.before']({tool:'atlas_step'}));
  for(const name of ['source_preview','shell','write_file']){
   assert.throws(()=>toolHooks['execute.before']({tool:name}),e=>/atlas_step/.test(e.message)&&/protocol correction/.test(e.message));
  }
  const context={tools:{atlas_step:{},shell:{}},system:[]};sessionHooks.context(context);
  assert.deepEqual(Object.keys(context.tools),['atlas_step']);
  assert(context.system[0].text.includes('not separately callable native functions'));
  assert(context.system[0].text.includes('a plan is not proof of completion'));
 }finally{await fs.rm(dir,{recursive:true,force:true});}
});
