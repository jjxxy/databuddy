const {JSDOM}=require('jsdom');const fs=require('fs'),assert=require('assert/strict');
(async()=>{
 const d=new JSDOM('<section id="data-append-panel" hidden></section>',{runScripts:'outside-only',url:'https://fixture.test'}),w=d.window;
 w.nexusSecureUUID=()=> '11111111-1111-4111-8111-111111111111';w.eval(fs.readFileSync('agent_service/static/append-results.js','utf8'));
 const sha='a'.repeat(64),dsha='b'.repeat(64),calls=[];let fail=true;
 const request=async(path,options)=>{calls.push([path,options]);if(path.endsWith('/data-workflows'))return {items:[{name:'<svg onload=x>',published_run:'delta'}]};if(path.includes('/data-results/delta'))return {run_id:'delta',manifest_sha256:dsha};if(fail){fail=false;throw Error('network');}return {workflow_id:'wf_'+'c'.repeat(40),run_id:'run',status:'queued',baseline_run_id:'base',baseline_sha256:sha,delta_run_id:'delta',delta_sha256:dsha,baseline_rows:3,delta_rows:2}};
 const panel=d.window.document.querySelector('#data-append-panel');w.nexusAppendResults.sync({project:'p',workflow:{published_run:'base'},result:{run_id:'base',manifest_sha256:sha},resultId:'base',request,isCurrent:()=>true});
 await new Promise(r=>setTimeout(r,0));assert.equal(panel.querySelectorAll('svg').length,0);panel.querySelector('select').value='delta';panel.querySelector('input').checked=true;panel.querySelector('textarea').value='已确认新增明细范围';panel.querySelector('button').click();await new Promise(r=>setTimeout(r,0));assert(panel.textContent.includes('没有确认提交结果'));
 const first=calls.at(-1)[1].body;assert.equal(first.request_id,'11111111-1111-4111-8111-111111111111');panel.querySelector('button').click();await new Promise(r=>setTimeout(r,0));assert.deepEqual(calls.at(-1)[1].body,first);assert(panel.textContent.includes('候选任务已排队'));assert.equal(panel.querySelector('a').hidden,false);
 console.log('append uses adopted pinned results, safe text and same-request retry');
})().catch(e=>{console.error(e);process.exit(1)});
