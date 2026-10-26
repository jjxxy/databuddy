'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const os=require('node:os');
const crypto=require('node:crypto');
const digest=value=>crypto.createHash('sha256').update(value).digest('hex');

test('real runtime copy retains dbt executable macros under tests and excludes only caches',async()=>{
 const {shouldCopyPayload,verifyRuntimeInventory}=await import('../../packaging/payload-policy.mjs');
 const temporary=await fs.mkdtemp(path.join(os.tmpdir(),'atlas-package-contract-'));
 const source=path.join(temporary,'source'),output=path.join(temporary,'output');
 const macro='python/Lib/site-packages/dbt/include/global_project/macros/materializations/tests/test.sql';
 const fixture='python/Lib/site-packages/runtime_module/tests/required.json';
 const cache='python/Lib/site-packages/dbt/__pycache__/cached.pyc';
 try{
  for(const name of [macro,fixture,cache]){await fs.mkdir(path.dirname(path.join(source,name)),{recursive:true});await fs.writeFile(path.join(source,name),name===macro?'macro test_materialization':name===fixture?'runtime-data':'cache');}
  await fs.cp(source,output,{recursive:true,filter:file=>shouldCopyPayload('runtime',path.relative(source,file))});
  assert.equal(await fs.readFile(path.join(output,macro),'utf8'),'macro test_materialization');
  assert.equal(await fs.readFile(path.join(output,fixture),'utf8'),'runtime-data');
  await assert.rejects(fs.access(path.join(output,cache)),{code:'ENOENT'});
  const files=[],inventory=new Map();
  for(const name of [macro,fixture]){
   const data=await fs.readFile(path.join(output,name)),record={path:'runtime/'+name,bytes:data.length,sha256:digest(data)};
   files.push(record);inventory.set('resources/app/'+record.path,{bytes:record.bytes,sha256:record.sha256});
  }
  assert.deepEqual(verifyRuntimeInventory({files},inventory),{files:2,bytes:38});
  assert.equal(shouldCopyPayload('agent_service','tests/test_only.py'),false);
  assert.equal(shouldCopyPayload('desktop','node_modules/dev/index.js'),false);
 }finally{
  const resolved=path.resolve(temporary);
  if(path.dirname(resolved)!==path.resolve(os.tmpdir())||!path.basename(resolved).startsWith('atlas-package-contract-'))
   throw new Error('Refusing cleanup outside the test temporary directory.');
  await fs.rm(resolved,{recursive:true,force:true});
 }
});

test('runtime verification rejects a pruned macro, changed bytes and an unlisted file',async()=>{
 const {verifyRuntimeInventory}=await import('../../packaging/payload-policy.mjs');
 const name='runtime/python/Lib/site-packages/dbt/include/global_project/macros/materializations/tests/test.sql';
 const data='verified-macro',record={path:name,bytes:data.length,sha256:digest(data)},key='resources/app/'+name;
 assert.throws(()=>verifyRuntimeInventory({files:[record]},new Map()),/Missing bundled runtime payload/);
 assert.throws(()=>verifyRuntimeInventory({files:[record]},new Map([[key,{bytes:record.bytes+1,sha256:record.sha256}]])),/size mismatch/);
 assert.throws(()=>verifyRuntimeInventory({files:[record]},new Map([[key,{bytes:record.bytes,sha256:digest('changed-macro')}]])),/hash mismatch/);
 const valid=new Map([[key,{bytes:record.bytes,sha256:record.sha256}],['resources/app/runtime/python/unlisted.py',{bytes:0,sha256:digest('')}] ]);
 assert.throws(()=>verifyRuntimeInventory({files:[record]},valid),/absent from its manifest/);
});

test('runtime manifest cannot select paths outside runtime or duplicate a Windows file',async()=>{
 const {verifyRuntimeInventory}=await import('../../packaging/payload-policy.mjs');
 const record={path:'runtime/a.py',bytes:1,sha256:digest('a')},map=new Map([['resources/app/runtime/a.py',{bytes:1,sha256:record.sha256}]]);
 for(const name of ['runtime/../../private','runtime\\a.py','/runtime/a.py','C:/runtime/a.py'])
  assert.throws(()=>verifyRuntimeInventory({files:[{...record,path:name}]},map),/invalid file record/);
 assert.throws(()=>verifyRuntimeInventory({files:[]},map),/non-empty/);
 assert.throws(()=>verifyRuntimeInventory({files:[record,{...record,path:'runtime/A.py'}]},map),/duplicate file path/);
});
