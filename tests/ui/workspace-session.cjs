const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const script=fs.readFileSync(path.resolve(__dirname,'../../agent_service/static/workspace-session.js'),'utf8');
const tick=()=>new Promise(r=>setImmediate(r));
async function until(fn){for(let i=0;i<60;i++){await tick();if(fn())return}throw Error('session transition did not settle')}
(async()=>{
 const dom=new JSDOM('<main id="old">上个用户的私密内容</main>',{url:'https://atlas.test/',runScripts:'outside-only'}),w=dom.window;
 w.Headers=globalThis.Headers;w.Request=globalThis.Request;let server='gen-a',accepted=0,apiCalls=0,hold=false,aborted=false;
 w.sessionStorage.setItem('atlas_draft','旧用户草稿');
 w.fetch=async(url,options={})=>{if(url==='/auth/status')return {ok:true,status:200,json:async()=>({enabled:true,authenticated:!!server,generation:server})};
  if(String(url).startsWith('/api/')){apiCalls++;const value=new w.Headers(options.headers).get('X-Atlas-Session');if(value!==server)return {ok:false,status:401};
   if(hold)return await new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{aborted=true;reject(Error('aborted'))},{once:true}));
   accepted++;return {ok:true,status:200};}throw Error('unexpected')};
 w.eval(script);await until(()=>w.sessionStorage.getItem('atlas_gateway_generation')==='gen-a');
 const ok=await w.fetch('/api/projects',{method:'POST'});assert(ok.ok);assert.equal(accepted,1);
 // An already-sent old-session request is aborted at the client on account switch.
 hold=true;const inflight=w.fetch('/api/projects').catch(e=>e);await until(()=>apiCalls===2);
 server='gen-b';w.dispatchEvent(new w.StorageEvent('storage',{key:'atlas_gateway_auth_changed',newValue:'gen-b'}));
 await until(()=>w.document.body.textContent.includes('登录账号已变化'));await inflight;
 assert(aborted);assert.equal(w.sessionStorage.getItem('atlas_draft'),null);assert(!w.document.body.textContent.includes('上个用户的私密内容'));
 await assert.rejects(w.fetch('/api/projects',{method:'POST'}));assert.equal(accepted,1);
 dom.window.close();
 // Before the cross-tab notice arrives, a mismatched generation is rejected by
 // the gateway and the page learns the new identity without issuing a mutation.
 const d2=new JSDOM('<main>旧页面</main>',{url:'https://atlas.test/',runScripts:'outside-only'}),x=d2.window;x.Headers=globalThis.Headers;x.Request=globalThis.Request;let current='one',mutations=0;
 x.fetch=async(url,options={})=>{if(url==='/auth/status')return {ok:true,status:200,json:async()=>({enabled:true,authenticated:true,generation:current})};const value=new x.Headers(options.headers).get('X-Atlas-Session');if(value!==current)return {ok:false,status:401};mutations++;return {ok:true,status:200}};
 x.eval(script);await until(()=>x.sessionStorage.getItem('atlas_gateway_generation')==='one');current='two';await assert.rejects(x.fetch('/api/projects',{method:'POST'}));assert.equal(mutations,0);assert(x.document.body.textContent.includes('登录账号已变化'));d2.window.close();
 // A response accepted before the server changes account is never delivered
 // to an old page, even without a storage event arriving in time.
 const d3=new JSDOM('<main>旧页面</main>',{url:'https://atlas.test/',runScripts:'outside-only'}),y=d3.window;y.Headers=globalThis.Headers;y.Request=globalThis.Request;let active='one',release;
 y.fetch=async(url)=>{if(url==='/auth/status')return {ok:true,status:200,json:async()=>({enabled:true,authenticated:true,generation:active})};return await new Promise(resolve=>{release=()=>resolve({ok:true,status:200})})};
 y.eval(script);await until(()=>y.sessionStorage.getItem('atlas_gateway_generation')==='one');const delayed=y.fetch('/api/projects');await until(()=>!!release);active='two';release();await assert.rejects(delayed);assert(y.document.body.textContent.includes('登录账号已变化'));d3.window.close();
 // A stale tab first announces the new server session to other tabs, then seals.
 const d4=new JSDOM('<main>旧页面</main>',{url:'https://atlas.test/',runScripts:'outside-only'}),z=d4.window;z.Headers=globalThis.Headers;z.Request=globalThis.Request;z.sessionStorage.setItem('atlas_gateway_generation','old');
 z.fetch=async()=>({ok:true,status:200,json:async()=>({enabled:true,authenticated:true,generation:'new'})});z.eval(script);await until(()=>z.document.body.textContent.includes('登录账号已变化'));assert.equal(z.localStorage.getItem('atlas_gateway_auth_changed'),'new');d4.window.close();
 // A hanging startup status cannot swallow a caller's cancellation.
 const d5=new JSDOM('<main>页面</main>',{url:'https://atlas.test/',runScripts:'outside-only'}),a=d5.window;a.Headers=globalThis.Headers;a.Request=globalThis.Request;a.fetch=()=>new Promise(()=>{});a.eval(script);
 const initialController=new a.AbortController(),initial=a.fetch('/api/projects',{signal:initialController.signal});initialController.abort();await assert.rejects(initial,e=>e.name==='AbortError');d5.window.close();
 // A hanging response-time status check is bounded, cancellable and discards
 // the undelivered response body; the original write result remains unknown.
 const d6=new JSDOM('<main>页面</main>',{url:'https://atlas.test/',runScripts:'outside-only'}),b=d6.window;b.Headers=globalThis.Headers;b.Request=globalThis.Request;let checks=0,bodyCancelled=false;
 b.fetch=async(url)=>{if(url==='/auth/status'){if(++checks===1)return {ok:true,status:200,json:async()=>({enabled:true,authenticated:true,generation:'one'})};return await new Promise(()=>{})}return {ok:true,status:200,body:{cancel:()=>{bodyCancelled=true;return Promise.resolve()}}}};
 b.eval(script);await until(()=>b.sessionStorage.getItem('atlas_gateway_generation')==='one');const afterController=new b.AbortController(),after=b.fetch('/api/projects',{method:'POST',signal:afterController.signal});await until(()=>checks===2);afterController.abort();await assert.rejects(after,e=>e.name==='AbortError');assert(bodyCancelled);d6.window.close();
 const d7=new JSDOM('<main>页面</main>',{url:'https://atlas.test/',runScripts:'outside-only'}),c=d7.window;c.Headers=globalThis.Headers;c.Request=globalThis.Request;const originalTimer=c.setTimeout.bind(c);c.setTimeout=(fn,ms)=>originalTimer(fn,Math.min(ms,20));let reads=0,cancelledBody=false;
 c.fetch=async(url)=>{if(url==='/auth/status'){if(++reads===1)return {ok:true,status:200,json:async()=>({enabled:true,authenticated:true,generation:'one'})};return await new Promise(()=>{})}return {ok:true,status:200,body:{cancel:()=>{cancelledBody=true;return Promise.resolve()}}}};
 c.eval(script);await until(()=>c.sessionStorage.getItem('atlas_gateway_generation')==='one');await assert.rejects(c.fetch('/api/projects',{method:'POST'}),/核对操作结果/);assert(cancelledBody);d7.window.close();
 console.log(JSON.stringify({per_tab_generation:true,stale_mutation_rejected:true,inflight_fetch_aborted:true,delayed_response_rejected:true,status_timeout:true,caller_abort:true,response_body_cancelled:true,stale_boot_announces_new_session:true,old_dom_and_draft_cleared:true}));
})().catch(e=>{console.error(e);process.exitCode=1});
