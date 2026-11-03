'use strict';
const {JSDOM}=require('jsdom'),fs=require('fs'),assert=require('assert/strict');
const delay=()=>new Promise(r=>setTimeout(r,10));
(async()=>{
 const html=fs.readFileSync('agent_service/static/artifact-shares.html','utf8');const d=new JSDOM(html,{url:'https://fixture.test/artifact-shares.html?project=p_test',runScripts:'outside-only'}),w=d.window;
 const item={id:'wa_'+('a'.repeat(44)),kind:'office',format:'docx',name:'报告.docx',sha256:'b'.repeat(64)};let principal='owner',posts=[],first=true,late;
 const ok=data=>({ok:true,json:async()=>data});w.nexusSecureUUID=()=> 'request_stable_12345678';w.fetch=async(url,opts={})=>{url=String(url);if(url==='/health')return ok({status:'ok'});if(url==='/api/identity')return ok({mode:'oidc',subject:principal});if(url.includes('/artifacts?'))return ok({items:[item],next_offset:null});if(url.endsWith('/artifact-shares')&&opts.method==='POST'){const body=JSON.parse(opts.body);posts.push(body);if(first){first=false;throw Error('disconnect')}return ok({artifact_id:item.id,artifact_sha256:item.sha256,format:'docx'})}if(url.endsWith('/artifact-shares'))return ok({items:[{id:'as_'+('c'.repeat(40)),name:'过期报告',state:'active',expires_at:'2000-01-01T00:00:00Z',content_sha256:'d'.repeat(64)}]});throw Error('unexpected '+url)};
 w.eval(fs.readFileSync('agent_service/static/artifact-shares.js','utf8'));await delay();const $=s=>w.document.querySelector(s);assert($('#shares').textContent.includes('已过期'));assert.equal($('#shares input'),null);
 $('#artifacts button').click();$('#subjects').value='recipient';$('#confirm').checked=true;$('#form').dispatchEvent(new w.Event('submit',{cancelable:true}));await delay();assert.equal(posts.length,1);assert.equal($('#retry').hidden,false);assert.equal(posts[0].expected_owner_subject,'owner');assert(w.sessionStorage.getItem('nexus_artifact_share_pending:p_test'));
 principal='different-owner';$('#retry').click();await delay();assert.equal(posts.length,1);assert(!$('#retry').hidden);principal='owner';$('#retry').click();await delay();assert.equal(posts.length,2);assert.deepEqual(posts[1],posts[0]);assert.equal($('#retry').hidden,true);assert.equal(w.sessionStorage.getItem('nexus_artifact_share_pending:p_test'),null);
 // A page hidden during a late listing response must never render that response.
 w.fetch=async(url)=>String(url).includes('/artifacts?')?await new Promise(resolve=>late=resolve):ok({items:[]});$('#refresh').click();await delay();w.dispatchEvent(new w.Event('blur'));late(ok({items:[item],next_offset:null}));await delay();assert.equal($('#artifacts').children.length,0);assert.equal($('#form').hidden,true);
 console.log('artifact share retries frozen requests, pins owner, marks expiry, hides late responses');
})().catch(error=>{console.error(error);process.exit(1)});
