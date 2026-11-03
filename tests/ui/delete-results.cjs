'use strict';
const {JSDOM}=require('jsdom');const fs=require('fs'),assert=require('assert/strict');
(async()=>{
 for(const invalid of [false,true,'all']){
  const d=new JSDOM('<section id="data-append-panel" hidden></section>',{runScripts:'outside-only',url:'https://fixture.test'}),w=d.window;
  w.nexusSecureUUID=()=> '11111111-1111-4111-8111-111111111111';w.eval(fs.readFileSync('agent_service/static/append-results.js','utf8'));
  const sha='a'.repeat(64),dsha='b'.repeat(64),calls=[];let fail=true;
  const request=async(path,options)=>{calls.push([path,options]);if(path.endsWith('/data-workflows'))return {items:[{name:'删除清单',published_run:'delta'}]};if(path.includes('/data-results/delta'))return {run_id:'delta',manifest_sha256:dsha};if(fail){fail=false;throw Error('network')}return {workflow_id:'wf_'+'c'.repeat(40),run_id:'run',status:'queued',baseline_run_id:'base',baseline_sha256:sha,delta_run_id:'delta',delta_sha256:dsha,baseline_rows:invalid==='all'?2:3,delta_rows:2,added_rows:0,replaced_rows:0,deleted_rows:invalid===true?1:2,total_rows:invalid==='all'?0:1,mode:'delete'}};
  const panel=w.document.querySelector('#data-append-panel');w.nexusAppendResults.sync({project:'p',workflow:{published_run:'base'},result:{run_id:'base',manifest_sha256:sha},resultId:'base',request,isCurrent:()=>true});await new Promise(r=>setTimeout(r,0));
  const [target,mode]=panel.querySelectorAll('select');target.value='delta';mode.value='delete';mode.dispatchEvent(new w.Event('change'));assert(panel.textContent.includes('逐行核对明确删除清单'));
  panel.querySelector('input').checked=true;target.dispatchEvent(new w.Event('change'));assert.equal(panel.querySelector('input').checked,false);panel.querySelector('input').checked=true;panel.querySelector('textarea').value='已核对删除范围';panel.querySelector('button').click();await new Promise(r=>setTimeout(r,0));assert(panel.textContent.includes('没有确认提交结果'));const first=calls.at(-1)[1].body;
  assert.equal(first.mode,'delete');assert.equal(first.confirm_delete,true);assert.equal(first.confirm_replace,false);assert.equal(first.confirm_append_only,false);assert(mode.disabled);
  panel.querySelector('button').click();await new Promise(r=>setTimeout(r,0));assert.deepEqual(calls.at(-1)[1].body,first);
  if(invalid===true){assert(panel.querySelector('a').hidden);assert(!panel.textContent.includes('预计删除 2 条'))}else{assert(!panel.querySelector('a').hidden);assert(panel.textContent.includes('预计删除 2 条'));assert(panel.textContent.includes('未列出的历史行保留'));if(invalid==='all')assert(panel.textContent.includes('候选结果为空，请特别核对'))}
  d.window.close();
 }
 console.log('explicit delete requires frozen confirmation, retries safely and rejects inconsistent receipts');
})().catch(e=>{console.error(e);process.exit(1)});
