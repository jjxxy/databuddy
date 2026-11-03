'use strict';
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const html=fs.readFileSync(path.join(root,'shared-result.html'),'utf8'),script=fs.readFileSync(path.join(root,'shared-result.js'),'utf8');
assert(html.includes('/shared-comments.js?v=1')&&html.includes('/shared-result.js?v=2')&&html.includes('/shared-result.css?v=2')&&html.includes('/secure-id.js?v=1'),'comment and result assets must use their new cache keys');
const tick=()=>new Promise(r=>setTimeout(r,0));
async function boot(state='ok'){
 const dom=new JSDOM(html,{url:'https://example.test/shared-result.html?id=share_'+'a'.repeat(24),runScripts:'outside-only'}),w=dom.window,calls=[];
 w.fetch=async url=>{calls.push(url);const n=calls.length; if(state==='denied')return {ok:false,status:403};return {ok:true,json:async()=>({run_id:n>1&&state==='changed'?'r2':'r1',plan_sha256:'a'.repeat(64),manifest_sha256:'b'.repeat(64),total:2,offset:n===1?0:1,next_offset:n===1?1:null,schema:[{name:'value',label:'<img src=x>'}],rows:[{value:'<script>bad</script>'}]})}};
 w.eval(script);await tick();await tick();return {w,calls};
}
(async()=>{
 let {w,calls}=await boot();let doc=w.document;
 assert.equal(doc.querySelector('#share-content').hidden,false);assert.equal(doc.querySelector('#share-head th').textContent,'<img src=x>');assert.equal(doc.querySelector('#share-body td').textContent,'<script>bad</script>');assert.equal(doc.querySelector('#share-content script'),null);assert.equal(calls.length,1);
 ({w}=await boot('denied'));doc=w.document;assert(doc.querySelector('#share-content').hidden);assert(doc.querySelector('#share-status').textContent.includes('无权查看'));
 ({w}=await boot('changed'));doc=w.document;doc.querySelector('#share-next').click();await tick();await tick();assert(doc.querySelector('#share-content').hidden);assert(doc.querySelector('#share-status').textContent.includes('版本已变化'));
 ({w,calls}=await boot());doc=w.document;Object.defineProperty(doc,'hidden',{value:false,configurable:true});let resets=0;w.nexusSharedComments={hasPending:()=>true,reset:()=>{resets++},activate:()=>{}};w.dispatchEvent(new w.Event('blur'));assert(doc.querySelector('#share-content').hidden);assert(doc.querySelector('#share-status').textContent.includes('评论状态未确认'));assert.equal(resets,1);w.dispatchEvent(new w.Event('focus'));await tick();await tick();assert.equal(calls.length,2);
 const insecure=new JSDOM(html,{url:'http://example.test/shared-result.html?id=share_'+'a'.repeat(24),runScripts:'outside-only'});let requested=false;insecure.window.fetch=async()=>{requested=true;return {ok:false}};insecure.window.eval(script);await tick();assert(!requested);assert(insecure.window.document.querySelector('#share-status').textContent.includes('HTTPS'));
 console.log('shared result: identity required, safe rendering and pinned pages');
})().catch(error=>{console.error(error);process.exitCode=1});
