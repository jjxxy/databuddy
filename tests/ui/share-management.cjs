'use strict';
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const script=fs.readFileSync(path.join(root,'share-management.js'),'utf8');
assert(fs.readFileSync(path.join(root,'data-workbench.html'),'utf8').includes('/share-management.js?v=2'),'owner form must load the new script');
const tick=()=>new Promise(r=>setTimeout(r,0));
async function boot(mode='direct'){
 const dom=new JSDOM('<section id="data-share-panel" hidden></section>',{url:'https://example.test/data-workbench.html?project=p1',runScripts:'outside-only'}),w=dom.window,requests=[];
 w.fetch=async()=>({ok:true,json:async()=>mode==='gateway'?{scope:'isolated_workspace_gateway'}:{status:'ok'}});
 const s={project:'p1',workflow:{workflow_id:'wf1',published_run:'r1'},resultId:'r1',result:{run_id:'r1',manifest_sha256:'a'.repeat(64)},isCurrent:()=>true,request:async(url,opts={})=>{requests.push({url,opts});if(opts.method==='POST')return {id:'share_'+'a'.repeat(24),result_id:'r1',state:'active'};if(opts.method==='DELETE')return {state:'revoked'};return {items:[{id:'share_'+'a'.repeat(24),result_id:'r1',state:'active',expires_at:'2999-01-01T00:00:00Z',allow_download:false}]};}};
 w.eval(script);w.nexusShareUI.sync(s);await tick();await tick();return {w,s,requests};
}
(async()=>{
 let {w,s,requests}=await boot();let doc=w.document;
 assert(doc.querySelector('#data-share-panel').textContent.includes('企业身份反向代理'));
 assert(doc.querySelector('input[aria-label="分享链接"]').value==='https://example.test/shared-result.html?id=share_'+'a'.repeat(24));
 doc.querySelector('textarea').value='subject-a\nsubject-b';doc.querySelector('form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();await tick();
 const post=requests.find(x=>x.opts.method==='POST');assert(post&&post.url.endsWith('/data-results/r1/shares'));assert.deepEqual(Array.from(post.opts.body.allowed_subjects),['subject-a','subject-b']);assert.equal(post.opts.body.allow_download,false);assert.equal(post.opts.body.allow_comments,false);assert(doc.querySelector('#data-share-panel').textContent.includes('不构成财务审批'));
 assert(!doc.body.innerHTML.includes('<script>bad</script>'));
 ({w,s,requests}=await boot());doc=w.document;doc.querySelector('textarea').value='subject-a';const checks=doc.querySelectorAll('input[type=checkbox]');assert.equal(checks.length,2);checks[1].checked=true;doc.querySelector('form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();await tick();assert.equal(requests.find(x=>x.opts.method==='POST').opts.body.allow_comments,true);
 w.nexusShareUI.sync({...s,resultId:'candidate'});assert(doc.querySelector('#data-share-panel').hidden);
 ({w,requests}=await boot('gateway'));doc=w.document;assert(doc.querySelector('#data-share-panel').textContent.includes('尚不支持跨账号分享'));assert.equal(requests.length,0);assert(doc.querySelector('form').parentElement.hidden);
 // An unconfirmed write must not be blindly repeated; a successful reload is required.
 ({w,s}=await boot());doc=w.document;let postCalls=0;const original=s.request;
 s.request=async(url,opts={})=>{if(opts.method==='POST'){postCalls++;throw Error('connection lost')}return original(url,opts)};
 w.nexusShareUI.clear();w.nexusShareUI.sync(s);await tick();await tick();doc.querySelector('textarea').value='subject-a';
 const form=doc.querySelector('form');form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();await tick();
 assert.equal(postCalls,1);assert(doc.querySelector('[role=status]')||doc.querySelector('p'));assert([...doc.querySelectorAll('p')].some(n=>n.textContent.includes('未确认是否创建成功')));form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();assert.equal(postCalls,1);
 const refresh=[...doc.querySelectorAll('button')].find(b=>b.textContent==='刷新分享记录');refresh.click();await tick();await tick();assert(!form.querySelector('button[type=submit]').disabled);
 // A late result from an older project scope cannot populate the new view.
 let complete;({w,s}=await boot());doc=w.document;w.nexusShareUI.clear();s.request=()=>new Promise(resolve=>{complete=resolve});w.nexusShareUI.sync(s);await tick();assert(complete);w.nexusShareUI.sync({...s,resultId:'candidate'});complete({items:[{id:'share_'+'a'.repeat(24),result_id:'r1',state:'active',expires_at:'2999-01-01T00:00:00Z'}]});await tick();assert(doc.querySelector('#data-share-panel').hidden);assert(!doc.body.textContent.includes('share_'));
 console.log('share management: published version, recipient scope, explicit creation and gateway restriction');
})().catch(error=>{console.error(error);process.exitCode=1});
