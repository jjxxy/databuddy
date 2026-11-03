'use strict';
const {JSDOM}=require('jsdom');const fs=require('fs'),assert=require('assert/strict');
(async()=>{
 const d=new JSDOM('<section id="data-append-panel" hidden></section>',{runScripts:'outside-only',url:'https://fixture.test'}),w=d.window;
 w.nexusSecureUUID=()=> '11111111-1111-4111-8111-111111111111';w.eval(fs.readFileSync('agent_service/static/append-results.js','utf8'));
 const sha='a'.repeat(64),dsha='b'.repeat(64),newsha='e'.repeat(64),calls=[];let n=0,oldCurrent=true;
 const request=async(path,options)=>{calls.push([path,options]);if(path.endsWith('/data-append-identity'))return {fingerprint:'d'.repeat(64)};if(path.endsWith('/data-workflows'))return {items:[{name:'删除清单',published_run:'delta'}]};if(path.includes('/data-results/delta'))return {run_id:'delta',manifest_sha256:dsha};if(n++===0)throw Error('network');return {workflow_id:'wf_'+'c'.repeat(40),run_id:'original',status:'queued',baseline_run_id:'base',baseline_sha256:sha,delta_run_id:'delta',delta_sha256:dsha,baseline_rows:3,delta_rows:1,total_rows:2,added_rows:0,replaced_rows:0,deleted_rows:1,mode:'delete'}};
 const panel=w.document.querySelector('#data-append-panel');
 w.nexusAppendResults.sync({project:'p',recoveryScope:'flow',isRecoveryCurrent:()=>true,workflow:{published_run:'base'},result:{run_id:'base',manifest_sha256:sha},resultId:'base',request,isCurrent:()=>oldCurrent});await new Promise(r=>setTimeout(r,0));
 const [target,mode]=panel.querySelectorAll('select');target.value='delta';mode.value='delete';mode.dispatchEvent(new w.Event('change'));panel.querySelector('input').checked=true;panel.querySelector('textarea').value='已核对删除范围';panel.querySelector('button').click();await new Promise(r=>setTimeout(r,0));
 assert(panel.textContent.includes('找回已创建的候选'));const first=calls.find(x=>x[0].endsWith('data-append-candidates'))[1].body;
 oldCurrent=false;
 w.nexusAppendResults.sync({project:'p',recoveryScope:'flow',isRecoveryCurrent:()=>true,workflow:{published_run:'new'},result:{run_id:'new',manifest_sha256:newsha},resultId:'new',request,isCurrent:()=>true});
 assert(panel.textContent.includes('固定版本'));assert(panel.textContent.includes('base')||panel.textContent.includes('原变更结果'));
 panel.querySelector('button').click();await new Promise(r=>setTimeout(r,0));const posts=calls.filter(x=>x[0].endsWith('data-append-candidates'));assert.deepEqual(posts[1][1].body,first);assert.equal(posts.length,2);assert(panel.textContent.includes('候选任务已排队'));assert.equal(panel.querySelector('a').hidden,false);
 d.window.close();console.log('changed official version recovers the original frozen request and candidate');
})().catch(e=>{console.error(e);process.exit(1)});
