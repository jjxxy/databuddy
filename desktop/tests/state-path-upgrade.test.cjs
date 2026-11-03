'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {chooseStateDirectory}=require('../state-path.cjs');

test('old desktop state is selected in place; ambiguous states fail without changes',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'desktop-upgrade-'));
 try{
  const old=path.join(root,'Atlas DataBuddy'),current=path.join(root,'Nexus WorkStudio');
  assert.equal(chooseStateDirectory(root,current),current);
  fs.mkdirSync(old);fs.writeFileSync(path.join(old,'credentials.json'),'old credentials');
  assert.equal(chooseStateDirectory(root,current),old);
  assert.equal(fs.readFileSync(path.join(old,'credentials.json'),'utf8'),'old credentials');
  fs.mkdirSync(current);
  assert.throws(()=>chooseStateDirectory(root,current),/同时存在/);
  assert.equal(fs.readFileSync(path.join(old,'credentials.json'),'utf8'),'old credentials');
  assert.deepEqual(fs.readdirSync(current),[]);
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});

test('symlink state directory is rejected',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'desktop-upgrade-link-'));
 try{
  const target=path.join(root,'outside');fs.mkdirSync(target);
  const old=path.join(root,'Atlas DataBuddy');
  try{fs.symlinkSync(target,old,'dir')}catch(e){if(process.platform==='win32'&&e.code==='EPERM')return;throw e}
  assert.throws(()=>chooseStateDirectory(root,path.join(root,'Nexus WorkStudio')),/安全/);
  assert.deepEqual(fs.readdirSync(target),[]);
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
