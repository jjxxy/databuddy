const {JSDOM}=require('jsdom');const fs=require('fs'),assert=require('assert/strict');
(async()=>{
 for(const altered of [{mode:'append'},{added_rows:999},{replaced_rows:-1},{total_rows:'4'},{delta_rows:3}]){
  const d=new JSDOM('<section id="data-append-panel" hidden></section>',{runScripts:'outside-only',url:'https://fixture.test'}),w=d.window;
  w.nexusSecureUUID=()=> '11111111-1111-4111-8111-111111111111';w.eval(fs.readFileSync('agent_service/static/append-results.js','utf8'));
  const sha='a'.repeat(64),dsha='b'.repeat(64);const request=async(path)=>{if(path.endsWith('/data-workflows'))return {items:[{name:'变更',published_run:'delta'}]};if(path.includes('/data-results/delta'))return {run_id:'delta',manifest_sha256:dsha};return {workflow_id:'wf_'+'c'.repeat(40),run_id:'run',status:'queued',baseline_run_id:'base',baseline_sha256:sha,delta_run_id:'delta',delta_sha256:dsha,baseline_rows:3,delta_rows:2,added_rows:1,replaced_rows:1,total_rows:4,mode:'upsert',...altered}};
  const panel=w.document.querySelector('#data-append-panel');w.nexusAppendResults.sync({project:'p',workflow:{published_run:'base'},result:{run_id:'base',manifest_sha256:sha},resultId:'base',request,isCurrent:()=>true});await new Promise(r=>setTimeout(r,0));const [target,mode]=panel.querySelectorAll('select');target.value='delta';mode.value='upsert';mode.dispatchEvent(new w.Event('change'));panel.querySelector('input').checked=true;panel.querySelector('textarea').value='已核对变更范围';panel.querySelector('button').click();await new Promise(r=>setTimeout(r,0));assert(panel.textContent.includes('没有确认提交结果'));assert(panel.querySelector('a').hidden);assert(!panel.textContent.includes('预计新增 1 条'));
 }
 console.log('upsert rejects mismatched mode and inconsistent, negative or non-numeric receipt counts');
})().catch(e=>{console.error(e);process.exit(1)});
