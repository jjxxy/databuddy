'use strict';
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static'),script=fs.readFileSync(path.join(root,'shared-comments.js'),'utf8');
const tick=()=>new Promise(resolve=>setTimeout(resolve,0)),sha='a'.repeat(64),sid='share_'+'a'.repeat(24);
async function boot(mode='ok'){
 const dom=new JSDOM('<section id="share-comments" hidden></section>',{url:'https://example.test/shared-result.html',runScripts:'outside-only'}),w=dom.window,calls=[];let sends=0;
 w.nexusSecureUUID=()=> 'req_12345678901234567890';w.fetch=async(url,options={})=>{calls.push({url,options});if(options.method==='POST'){sends++;if(mode==='uncertain'&&sends===1)throw Error('offline');const body=JSON.parse(options.body);return {ok:true,json:async()=>({id:'comment1',text:body.text,handoff_sha256:sha})}};if(mode==='denied')return {ok:false,status:403};return {ok:true,json:async()=>({items:[{id:'c1',author:'用户 <img>',text:'<script>坏</script>',created_at:'2026-11-03',handoff_sha256:sha}],offset:0,total:1,has_more:false,handoff_sha256:sha})}};
 w.eval(script);w.nexusSharedComments.activate({shareId:sid,manifest:sha});await tick();await tick();return {w,calls};
}
(async()=>{
 let {w,calls}=await boot('uncertain'),doc=w.document,panel=doc.querySelector('#share-comments');
 assert(!panel.hidden);assert(panel.textContent.includes('不构成财务审批'));assert.equal(panel.querySelector('article p:nth-child(2)').textContent,'<script>坏</script>');assert.equal(panel.querySelector('article script'),null);
 const form=panel.querySelector('form'),input=panel.querySelector('textarea');input.value='请核对金额';form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();await tick();
 assert(w.nexusSharedComments.hasPending());assert(input.disabled);assert(panel.textContent.includes('未确认评论是否提交成功'));assert.equal(calls.filter(c=>c.options.method==='POST').length,1);
 form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();assert.equal(calls.filter(c=>c.options.method==='POST').length,1);
 const retry=[...panel.querySelectorAll('button')].find(b=>b.textContent==='再次发送同一条评论');assert(!retry.hidden);retry.click();await tick();await tick();
 const posts=calls.filter(c=>c.options.method==='POST').map(c=>JSON.parse(c.options.body));assert.equal(posts.length,2);assert.deepEqual(posts[0],posts[1]);assert.equal(posts[0].expected_handoff_sha256,sha);assert(!w.nexusSharedComments.hasPending());assert(!input.disabled);
 ({w}=await boot('denied'));panel=w.document.querySelector('#share-comments');assert(panel.querySelector('form').hidden);assert(panel.textContent.includes('评论未启用或当前账号已无权访问'));
 // Pagination uses the actual server cursor; no comment outside the fixed result appears.
 const dom=new JSDOM('<section id="share-comments" hidden></section>',{url:'https://example.test/shared-result.html',runScripts:'outside-only'}),q=dom.window;
 q.fetch=async url=>{const offset=Number(new URL(url,'https://example.test').searchParams.get('offset'));return {ok:true,json:async()=>({items:Array.from({length:offset?1:50},(_,i)=>({id:'c'+(offset+i),author:'用户 123',text:'记录 '+(offset+i),created_at:'2026',handoff_sha256:sha})),offset,total:51,has_more:offset===0,handoff_sha256:sha})}};q.eval(script);q.nexusSharedComments.activate({shareId:sid,manifest:sha});await tick();await tick();let last=[...q.document.querySelectorAll('button')].find(b=>b.textContent==='下一页评论');assert(!last.disabled);last.click();await tick();await tick();assert(q.document.querySelector('#share-comments').textContent.includes('记录 50'));assert(q.document.querySelector('#share-comments').textContent.includes('51–51 条，共 51'));
 console.log('shared comments: explicit OIDC scope, escaped text, pinned result, manual idempotent retry');
})().catch(error=>{console.error(error);process.exitCode=1});
